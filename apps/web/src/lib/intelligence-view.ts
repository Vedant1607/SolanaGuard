// Presentation helpers for GET /api/v1/protocols/{slug}/intelligence
// (tvl-robust-v1). They validate and summarise what the API returned; they
// never recompute the model, change its threshold or fill missing days.
// Kept free of runtime imports so `node --test` can load it directly.

export type IntelligenceClassification = "NORMAL" | "UNUSUAL" | "INSUFFICIENT_HISTORY";

export interface IntelligenceSupportingObservation {
  id: string;
  role: "latest_day" | "previous_day" | string;
  day: string;
  value_usd: number;
  source_observed_at: string;
  collected_at: string;
}

export interface IntelligenceHistoryDay {
  day: string;
  observation_id: string;
  excess_log_return: number | null;
  daily_log_return: number | null;
  peer_median_return: number | null;
  robust_z: number | null;
  unusual: boolean;
  market_wide: boolean | null;
  reasons: string[];
}

export interface IntelligenceReport {
  protocol: { slug: string; name: string; is_active: boolean };
  model: { name: string; version: string; method: string; min_history_days: number; target_flag_rate: number };
  evaluated_at: string;
  classification: IntelligenceClassification;
  robust_z: number | null;
  threshold: number;
  direction: "decline" | "increase" | null;
  drawdown_30d: number | null;
  excess_log_return: number | null;
  daily_log_return: number | null;
  market: { peer_median_return: number | null; market_z: number | null; market_wide: boolean | null };
  latest_closed_day: string | null;
  latest_age_days: number | null;
  observation_window: {
    baseline_start: string | null;
    baseline_end: string | null;
    baseline_days: number;
    baseline_points: number;
    history_days_before_latest: number;
    peers: number;
  };
  source: { source: string; adapter: string; metric_key: string; resolution: string };
  supporting_observations: IntelligenceSupportingObservation[];
  explanation: string[];
  insufficient_reasons: { code: string; detail: string }[];
  disclaimer: string;
  history_window: { start: string; end: string; days: number } | null;
  history: IntelligenceHistoryDay[];
  /**
   * Raw ``flow_context`` object, present only when requested with
   * ``flowContext: true`` and served by the API build. Validated separately by
   * ``parseFlowContext`` so a malformed explanation never hides the classification.
   */
  flow_context?: unknown;
}

export type IntelligenceResult =
  | { ok: true; data: IntelligenceReport }
  | { ok: false; reason: "not_found" | "not_ready" | "unavailable" | "malformed" };

// --- validation -------------------------------------------------------------

const CLASSIFICATIONS = new Set(["NORMAL", "UNUSUAL", "INSUFFICIENT_HISTORY"]);
const DAY = /^\d{4}-\d{2}-\d{2}$/;

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isNumOrNull = (v: unknown) => v === null || isNum(v);
const isStr = (v: unknown): v is string => typeof v === "string";
const isDayOrNull = (v: unknown) => v === null || (isStr(v) && DAY.test(v));

function validHistoryDay(v: unknown): boolean {
  return isObject(v) && isStr(v.day) && DAY.test(v.day) && isStr(v.observation_id)
    && isNumOrNull(v.excess_log_return) && isNumOrNull(v.daily_log_return) && isNumOrNull(v.peer_median_return)
    && isNumOrNull(v.robust_z) && typeof v.unusual === "boolean" && Array.isArray(v.reasons);
}

/** Fields of observation_window that the UI reads and formats. */
function validObservationWindow(v: unknown): boolean {
  return isObject(v) && isDayOrNull(v.baseline_start) && isDayOrNull(v.baseline_end)
    && isNum(v.baseline_days) && isNum(v.baseline_points) && isNum(v.history_days_before_latest) && isNum(v.peers);
}

/** Fields of a supporting observation that the UI reads and formats. */
function validSupportingObservation(v: unknown): boolean {
  return isObject(v) && isStr(v.id) && v.id.length > 0 && isStr(v.role) && isStr(v.day) && DAY.test(v.day)
    && isNum(v.value_usd) && isStr(v.collected_at) && DAY.test(v.collected_at.slice(0, 10));
}

/**
 * Structural check of an intelligence response. Anything that does not match
 * returns null and is shown as unavailable, never as a classification.
 */
export function parseIntelligence(value: unknown): IntelligenceReport | null {
  if (!isObject(value)) return null;
  const v = value;
  if (!isStr(v.classification) || !CLASSIFICATIONS.has(v.classification)) return null;
  if (!isNum(v.threshold) || v.threshold <= 0) return null;
  if (!isNumOrNull(v.robust_z) || !isNumOrNull(v.drawdown_30d) || !isNumOrNull(v.excess_log_return) || !isNumOrNull(v.daily_log_return)) return null;
  if (!isDayOrNull(v.latest_closed_day) || !isNumOrNull(v.latest_age_days)) return null;
  if (!isObject(v.protocol) || !isStr(v.protocol.slug)) return null;
  if (!isObject(v.model) || !isStr(v.model.version) || !isStr(v.model.name) || !isStr(v.model.method)) return null;
  if (!isObject(v.market) || !isObject(v.observation_window) || !isObject(v.source)) return null;
  if (!Array.isArray(v.supporting_observations) || !Array.isArray(v.explanation) || !Array.isArray(v.insufficient_reasons)) return null;
  if (!validObservationWindow(v.observation_window) || !v.supporting_observations.every(validSupportingObservation)) return null;
  if (!isStr(v.disclaimer) || !v.disclaimer) return null;
  // A scored classification without a score is inconsistent: reject it.
  if (v.classification !== "INSUFFICIENT_HISTORY" && (v.robust_z === null || v.latest_closed_day === null)) return null;
  const history = v.history === undefined ? [] : v.history;
  if (!Array.isArray(history) || !history.every(validHistoryDay)) return null;
  const window = v.history_window ?? null;
  if (window !== null && !(isObject(window) && isStr(window.start) && DAY.test(window.start) && isStr(window.end) && DAY.test(window.end))) return null;
  return { ...(v as unknown as IntelligenceReport), history: history as IntelligenceHistoryDay[], history_window: window as IntelligenceReport["history_window"] };
}

// --- loading ----------------------------------------------------------------

export const INTELLIGENCE_TIMEOUT_MS = 8_000;
export const MAX_HISTORY_DAYS = 180;

type FetchLike = (url: string, init: { cache: "no-store"; signal: AbortSignal }) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

/** Fetch and validate one intelligence response. Never throws. */
export async function loadIntelligence(fetchImpl: FetchLike, url: string, historyDays = 0, timeoutMs = INTELLIGENCE_TIMEOUT_MS, { flowContext = false }: { flowContext?: boolean } = {}): Promise<IntelligenceResult> {
  const days = Math.min(MAX_HISTORY_DAYS, Math.max(0, Math.floor(historyDays)));
  const params = new URLSearchParams();
  if (days > 0) params.set("history_days", String(days));
  if (flowContext) params.set("flow_context", "true");
  const query = params.toString();
  try {
    const res = await fetchImpl(query ? `${url}?${query}` : url, { cache: "no-store", signal: AbortSignal.timeout(timeoutMs) });
    if (res.status === 404) return { ok: false, reason: "not_found" };
    if (res.status === 503) return { ok: false, reason: "not_ready" };
    if (!res.ok) return { ok: false, reason: "unavailable" };
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      return { ok: false, reason: "malformed" };
    }
    const data = parseIntelligence(body);
    return data ? { ok: true, data } : { ok: false, reason: "malformed" };
  } catch {
    return { ok: false, reason: "unavailable" }; // network error or timeout
  }
}

// --- dates ------------------------------------------------------------------

/** The model treats a latest closed day older than this as stale (MAX_LATEST_AGE_DAYS). */
export const MAX_FRESH_AGE_DAYS = 3;
const DAY_MS = 86_400_000;

function dayStart(day: string): number {
  return Date.parse(`${day}T00:00:00Z`);
}

export function addDays(day: string, n: number): string {
  return new Date(dayStart(day) + n * DAY_MS).toISOString().slice(0, 10);
}

/** Whole UTC days between a closed day and the current UTC date. */
export function dayAge(day: string, now: number): number {
  const today = Math.floor(now / DAY_MS) * DAY_MS;
  return Math.round((today - dayStart(day)) / DAY_MS);
}

export function formatDay(day: string | null): string {
  if (!day) return "—";
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(dayStart(day)));
}

// --- formatting -------------------------------------------------------------

/** A log return as a simple percentage change, e.g. -0.105 -> "-10.0%". */
export function formatLogReturn(logReturn: number | null, digits = 1): string {
  if (logReturn === null) return "—";
  const pct = Math.expm1(logReturn) * 100;
  const sign = pct > 0 ? "+" : pct < 0 ? "−" : "";
  return `${sign}${Math.abs(pct).toFixed(digits)}%`;
}

export function formatRatio(value: number | null, digits = 1): string {
  if (value === null) return "—";
  const pct = value * 100;
  const sign = pct > 0 ? "+" : pct < 0 ? "−" : "";
  return `${sign}${Math.abs(pct).toFixed(digits)}%`;
}

export function formatZ(z: number | null): string {
  if (z === null) return "—";
  return `${z > 0 ? "+" : z < 0 ? "−" : ""}${Math.abs(z).toFixed(2)}`;
}

// --- summary ----------------------------------------------------------------

export const CLASSIFICATION_LABEL: Record<IntelligenceClassification, string> = {
  NORMAL: "Normal",
  UNUSUAL: "Unusual",
  INSUFFICIENT_HISTORY: "Insufficient history",
};

export interface IntelligenceSummary {
  /** Never NORMAL or UNUSUAL when the data is missing, unscored or stale. */
  classification: IntelligenceClassification;
  label: string;
  stale: boolean;
  ageDays: number | null;
  freshnessText: string;
  zText: string;
  drawdownText: string;
  reasons: { code: string; detail: string }[];
}

const STALE_REASON = {
  code: "stale_history",
  detail: `The latest closed observation is more than ${MAX_FRESH_AGE_DAYS} days old; re-run the DefiLlama backfill.`,
};

export function freshnessText(day: string | null, now: number): string {
  if (!day) return "No closed daily observation stored";
  const age = dayAge(day, now);
  if (age <= 0) return "Latest closed day is today (UTC)";
  if (age === 1) return "Latest closed UTC day (1 day old)";
  return `${age} days old${age > MAX_FRESH_AGE_DAYS ? " — stale" : ""}`;
}

/**
 * Card/header summary. Staleness is re-checked against the render time, so a
 * response that aged in a cache can never keep showing NORMAL or UNUSUAL.
 */
export function summarizeIntelligence(report: IntelligenceReport, now: number): IntelligenceSummary {
  const ageDays = report.latest_closed_day ? dayAge(report.latest_closed_day, now) : null;
  const stale = ageDays !== null && ageDays > MAX_FRESH_AGE_DAYS;
  const reasons = [...report.insufficient_reasons];
  let classification = report.classification;
  if (stale && !reasons.some((r) => r.code === STALE_REASON.code)) reasons.push(STALE_REASON);
  if (stale || report.robust_z === null || report.latest_closed_day === null) classification = "INSUFFICIENT_HISTORY";
  if (classification === "INSUFFICIENT_HISTORY" && reasons.length === 0) {
    reasons.push({ code: "unscored", detail: "The latest closed day could not be scored." });
  }
  const scored = classification !== "INSUFFICIENT_HISTORY";
  return {
    classification,
    label: CLASSIFICATION_LABEL[classification],
    stale,
    ageDays,
    freshnessText: freshnessText(report.latest_closed_day, now),
    zText: scored ? formatZ(report.robust_z) : "—",
    drawdownText: formatRatio(report.drawdown_30d),
    reasons,
  };
}

// --- chart ------------------------------------------------------------------

export type ChartDayKind = "scored" | "unscored" | "missing";

export interface ChartDay {
  index: number;
  day: string;
  kind: ChartDayKind;
  /** Excess log return; null for missing days and days without one. Never 0 as a stand-in. */
  value: number | null;
  z: number | null;
  unusual: boolean;
  marketWide: boolean | null;
  clipped: boolean;
  observationId: string | null;
  reasons: string[];
}

export interface ExcessChart {
  start: string;
  end: string;
  days: ChartDay[];
  missingDays: string[];
  unusualDays: ChartDay[];
  /** Symmetric y-limit (log-return units). Bars beyond it are clipped and flagged. */
  limit: number;
  clippedCount: number;
  scoredCount: number;
}

const MIN_LIMIT = 0.02;

function quantile(sorted: number[], q: number): number {
  if (sorted.length === 0) return 0;
  const pos = (sorted.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

/**
 * Dense calendar for the excess-return chart. Every calendar day in the window
 * appears once; days with no stored observation are kind "missing" with a null
 * value, so a gap can never be drawn as a zero return. Extreme days are clipped
 * to a robust y-limit (and flagged) so one crash does not flatten the rest.
 */
export function buildExcessChart(history: IntelligenceHistoryDay[], window: { start: string; end: string }): ExcessChart {
  const byDay = new Map(history.map((h) => [h.day, h]));
  const length = Math.max(0, Math.round((dayStart(window.end) - dayStart(window.start)) / DAY_MS) + 1);
  const values = history.map((h) => h.excess_log_return).filter((v): v is number => v !== null && Number.isFinite(v));
  const abs = values.map(Math.abs).sort((a, b) => a - b);
  const maxAbs = abs.length ? abs[abs.length - 1] : 0;
  const robust = Math.max(quantile(abs, 0.95) * 2.5, MIN_LIMIT);
  const limit = Math.max(Math.min(maxAbs, robust), MIN_LIMIT);

  const days: ChartDay[] = [];
  for (let i = 0; i < length; i++) {
    const day = addDays(window.start, i);
    const h = byDay.get(day);
    if (!h) {
      days.push({ index: i, day, kind: "missing", value: null, z: null, unusual: false, marketWide: null, clipped: false, observationId: null, reasons: [] });
      continue;
    }
    const value = h.excess_log_return !== null && Number.isFinite(h.excess_log_return) ? h.excess_log_return : null;
    days.push({
      index: i, day, kind: h.robust_z === null ? "unscored" : "scored", value, z: h.robust_z,
      unusual: h.unusual && h.robust_z !== null, marketWide: h.market_wide, clipped: value !== null && Math.abs(value) > limit,
      observationId: h.observation_id, reasons: h.reasons,
    });
  }
  return {
    start: window.start,
    end: window.end,
    days,
    missingDays: days.filter((d) => d.kind === "missing").map((d) => d.day),
    unusualDays: days.filter((d) => d.unusual),
    limit,
    clippedCount: days.filter((d) => d.clipped).length,
    scoredCount: days.filter((d) => d.kind === "scored").length,
  };
}

/** Collapse sorted ISO days into ranges for compact listing ("Jun 3 – Jun 5, 2026"). */
export function dayRanges(days: string[]): { start: string; end: string; count: number }[] {
  const ranges: { start: string; end: string; count: number }[] = [];
  for (const day of days) {
    const last = ranges[ranges.length - 1];
    if (last && addDays(last.end, 1) === day) {
      last.end = day;
      last.count += 1;
    } else {
      ranges.push({ start: day, end: day, count: 1 });
    }
  }
  return ranges;
}

export function chartDayTitle(d: ChartDay, threshold: number): string {
  const date = formatDay(d.day);
  if (d.kind === "missing") return `${date}: no observation stored (not a zero return)`;
  if (d.value === null) return `${date}: not scored — ${d.reasons.join(", ") || "no excess return"}`;
  const parts = [`${date}: ${formatLogReturn(d.value)} vs peer median`];
  if (d.z !== null) parts.push(`robust z ${formatZ(d.z)}`);
  if (d.unusual) parts.push(`UNUSUAL (|z| ≥ ${threshold})`);
  else if (d.kind === "unscored") parts.push(`not scored: ${d.reasons.join(", ")}`);
  if (d.marketWide) parts.push("market-wide move");
  if (d.clipped) parts.push("bar clipped to chart range");
  return parts.join(" · ");
}
