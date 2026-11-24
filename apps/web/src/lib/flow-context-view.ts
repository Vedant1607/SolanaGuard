// Presentation of the optional ``flow_context`` object returned by
// GET /api/v1/protocols/{slug}/intelligence?flow_context=true (features-daily-v1).
// It validates and formats what the API returned; it never recomputes flows,
// never fills a missing value and never turns "unavailable" into zero.
// Kept free of runtime imports so `node --test` can load it directly.
//
// Units, as defined by the backend (app/ml/flow_context.py):
//   flow.pct_change, coverage.*_share, lending.utilisation      simple ratios
//   flow.log_change, non_flow_effect.log_change, tvl_log_return,
//   sol_log_return, lending.borrowed_log_change                   natural-log changes
//   contributions.*.share, listed_total, flow_total, other_assets shares of the
//     previous day's matched USD value (simple, they add up to the flow %)
//   lending.utilisation_change                                    change in the ratio

export interface FlowContribution {
  symbol: string;
  share: number;
}

export interface FlowContext {
  feature_version: string;
  measurement_day: string | null;
  previous_day: string | null;
  regime: string | null;
  available: boolean;
  flow: { log_change: number; pct_change: number; robust_z: number | null; valuation: string } | null;
  coverage: {
    matched_share: number;
    removed_share: number | null;
    added_share: number | null;
    unpriced_assets: number | null;
    minimum_matched_share: number;
  } | null;
  non_flow_effect: { log_change: number } | null;
  tvl_log_return: number | null;
  sol_log_return: number | null;
  lending: {
    borrowed_usd: number | null;
    borrowed_log_change: number | null;
    utilisation: number | null;
    utilisation_change: number | null;
    utilisation_change_z: number | null;
    definition: string;
  } | null;
  contributions: {
    unit: string;
    positive: FlowContribution[];
    negative: FlowContribution[];
    unchanged: string[];
    listed_total: number;
    flow_total: number;
    other_assets: number;
    max_listed: number;
  } | null;
  unavailable: { field: string; code: string; detail: string }[];
  source: { source: string; adapters: string[]; metrics: string[] };
  disclaimer: string;
}

/** absent: the API build did not include it; malformed: it did not match the contract. */
export type FlowContextResult =
  | { state: "ok"; data: FlowContext }
  | { state: "absent" }
  | { state: "malformed" };

// --- validation -------------------------------------------------------------

const DAY = /^\d{4}-\d{2}-\d{2}$/;
const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isNumOrNull = (v: unknown) => v === null || isNum(v);
const isStr = (v: unknown): v is string => typeof v === "string";
const isDayOrNull = (v: unknown) => v === null || (isStr(v) && DAY.test(v));
const isStrArray = (v: unknown): v is string[] => Array.isArray(v) && v.every(isStr);

function validContribution(v: unknown): boolean {
  return isObject(v) && isStr(v.symbol) && v.symbol.length > 0 && isNum(v.share);
}

function validFlow(v: unknown): boolean {
  return v === null || (isObject(v) && isNum(v.log_change) && isNum(v.pct_change) && isNumOrNull(v.robust_z) && isStr(v.valuation));
}

function validCoverage(v: unknown): boolean {
  return v === null || (isObject(v) && isNum(v.matched_share) && isNumOrNull(v.removed_share) && isNumOrNull(v.added_share)
    && isNumOrNull(v.unpriced_assets) && isNum(v.minimum_matched_share));
}

function validLending(v: unknown): boolean {
  return v === null || (isObject(v) && isNumOrNull(v.borrowed_usd) && isNumOrNull(v.borrowed_log_change) && isNumOrNull(v.utilisation)
    && isNumOrNull(v.utilisation_change) && isNumOrNull(v.utilisation_change_z) && isStr(v.definition));
}

function validContributions(v: unknown): boolean {
  return v === null || (isObject(v) && isStr(v.unit) && Array.isArray(v.positive) && v.positive.every(validContribution)
    && Array.isArray(v.negative) && v.negative.every(validContribution) && isStrArray(v.unchanged)
    && isNum(v.listed_total) && isNum(v.flow_total) && isNum(v.other_assets) && isNum(v.max_listed));
}

function validUnavailable(v: unknown): boolean {
  return Array.isArray(v) && v.every((u) => isObject(u) && isStr(u.field) && isStr(u.code) && isStr(u.detail));
}

/** Structural check. Anything that does not match is "malformed" and nothing from it is shown. */
export function parseFlowContext(value: unknown): FlowContextResult {
  if (value === undefined || value === null) return { state: "absent" };
  if (!isObject(value)) return { state: "malformed" };
  const v = value;
  const ok = isStr(v.feature_version) && isDayOrNull(v.measurement_day) && isDayOrNull(v.previous_day)
    && (v.regime === null || isStr(v.regime)) && typeof v.available === "boolean"
    && validFlow(v.flow) && validCoverage(v.coverage)
    && (v.non_flow_effect === null || (isObject(v.non_flow_effect) && isNum(v.non_flow_effect.log_change)))
    && isNumOrNull(v.tvl_log_return) && isNumOrNull(v.sol_log_return)
    && validLending(v.lending) && validContributions(v.contributions) && validUnavailable(v.unavailable)
    && isObject(v.source) && isStr(v.source.source) && isStrArray(v.source.adapters) && isStrArray(v.source.metrics)
    && isStr(v.disclaimer) && v.disclaimer.length > 0;
  // "available" must agree with the flow it describes.
  if (!ok || (v.available === true) !== (v.flow !== null)) return { state: "malformed" };
  return { state: "ok", data: v as unknown as FlowContext };
}

// --- formatting -------------------------------------------------------------

const MINUS = "−";

function signOf(v: number): string {
  return v > 0 ? "+" : v < 0 ? MINUS : "";
}

/**
 * A simple ratio as a percentage, e.g. 0.03015 -> "+3.02%". Non-zero values too
 * small for the requested precision are shown as "<0.01%", never as "0.00%".
 */
export function formatPct(ratio: number | null, { digits = 2, signed = true }: { digits?: number; signed?: boolean } = {}): string | null {
  if (ratio === null || !Number.isFinite(ratio)) return null;
  const pct = ratio * 100;
  const sign = signed ? signOf(pct) : pct < 0 ? MINUS : "";
  const floor = 10 ** -digits;
  if (pct !== 0 && Math.abs(pct) < floor) return `${sign}<${floor.toFixed(digits)}%`;
  return `${sign}${Math.abs(pct).toFixed(digits)}%`;
}

/** A natural-log change as the equivalent simple percentage change: expm1(log). */
export function formatLogAsPct(logChange: number | null, digits = 2): string | null {
  return logChange === null || !Number.isFinite(logChange) ? null : formatPct(Math.expm1(logChange), { digits });
}

/** The raw log value, e.g. -0.0131 -> "−0.0131". */
export function formatLog(logChange: number | null, digits = 4): string | null {
  if (logChange === null || !Number.isFinite(logChange)) return null;
  const rounded = Number(logChange.toFixed(digits));
  return `${signOf(rounded)}${Math.abs(rounded).toFixed(digits)}`;
}

/** A share of the previous day's matched value, in percentage points of the flow. */
export function formatPoints(share: number | null, digits = 2): string | null {
  const pct = formatPct(share, { digits });
  return pct === null ? null : `${pct.slice(0, -1)} pts`;
}

export function formatFlowZ(z: number | null): string | null {
  return z === null || !Number.isFinite(z) ? null : `${signOf(z)}${Math.abs(z).toFixed(2)}`;
}

export function formatUsd(value: number | null): string | null {
  if (value === null || !Number.isFinite(value)) return null;
  return `$${new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value)}`;
}

// --- unavailable reasons -----------------------------------------------------

export const FIELD_LABEL: Record<string, string> = {
  tvl_log_return: "Daily TVL change",
  flow_log: "Estimated token flow",
  flow_z: "Flow z-score",
  borrowed_log_change: "Borrowed value change",
  utilisation: "Utilisation",
  utilisation_change: "Utilisation change",
  utilisation_change_z: "Utilisation change z-score",
};

const LENDING_FIELDS = new Set(["borrowed_log_change", "utilisation", "utilisation_change", "utilisation_change_z"]);

/** User-facing explanation of an unavailable code; unknown codes fall back to the API's own detail. */
export function reasonText(code: string, apiDetail: string, protocolName: string): string {
  switch (code) {
    case "no_token_data":
      return `DefiLlama does not publish per-asset token history for ${protocolName}, so token quantity flows cannot be estimated. Only the total TVL change is known.`;
    case "regime_break":
      return `${protocolName}'s stored TVL series changed meaning after a regime break and now tracks a residual balance, so day-to-day comparisons with its earlier history are not meaningful.`;
    case "not_applicable":
      return `Not applicable: DefiLlama reports no borrowed value for ${protocolName}, so there is no utilisation to show.`;
    case "no_observation":
      return "No token observation is stored for this day yet (DefiLlama may publish it later).";
    case "previous_day_missing":
      return "The previous day has no stored token observation, so a one-day change cannot be measured.";
    case "zero_dispersion":
      return "The trailing baseline has no variation, so a historical z-score is undefined.";
    case "insufficient_baseline":
      return "There are not yet enough earlier days to compare this change with.";
    default:
      return apiDetail || code; // below_min_base and asset_set_changed carry their thresholds in the API detail
  }
}

export interface ReasonNote {
  code: string;
  fields: string[];
  text: string;
}

/** Unavailable entries grouped by code (one note per reason), in API order. Lending entries are kept apart. */
export function groupReasons(unavailable: FlowContext["unavailable"], protocolName: string, { lending }: { lending: boolean }): ReasonNote[] {
  const notes = new Map<string, ReasonNote>();
  for (const u of unavailable) {
    if (LENDING_FIELDS.has(u.field) !== lending) continue;
    const note = notes.get(u.code) ?? { code: u.code, fields: [], text: reasonText(u.code, u.detail, protocolName) };
    note.fields.push(FIELD_LABEL[u.field] ?? u.field);
    notes.set(u.code, note);
  }
  return [...notes.values()];
}

// --- view model -------------------------------------------------------------

export type FlowStatus = "available" | "residual" | "no_token_data" | "unavailable";

export interface ContributionRow {
  symbol: string;
  text: string;
  /** Bar length relative to the largest listed contribution, 0..1. */
  scale: number;
}

export interface FlowContextView {
  status: FlowStatus;
  headline: string;
  measurementDay: string | null;
  previousDay: string | null;
  flow: { pct: string; log: string; z: string | null; valuation: string } | null;
  otherEffect: { pct: string; log: string } | null;
  tvlChange: { pct: string; log: string } | null;
  /** "+0.0166 = +0.0297 flow −0.0131 other", only when all three parts are present. */
  identity: string | null;
  solChange: string | null;
  coverage: {
    matched: string;
    minimum: string;
    belowMinimum: boolean;
    added: string | null;
    removed: string | null;
    unpriced: string | null;
  } | null;
  contributions: {
    positive: ContributionRow[];
    negative: ContributionRow[];
    unchanged: string[];
    listedCount: number;
    maxListed: number;
    /** True when the list is (probably) cut off at max_listed. */
    truncated: boolean;
    listed: string;
    other: string;
    total: string;
    unit: string;
  } | null;
  lending: {
    borrowed: string | null;
    borrowedChange: string | null;
    utilisation: string | null;
    utilisationChange: string | null;
    utilisationZ: string | null;
    definition: string;
  } | null;
  lendingNotes: ReasonNote[];
  notes: ReasonNote[];
  source: string;
  disclaimer: string;
  featureVersion: string;
}

function status(ctx: FlowContext): FlowStatus {
  if (ctx.regime === "residual") return "residual";
  if (ctx.unavailable.some((u) => u.field === "flow_log" && u.code === "no_token_data")) return "no_token_data";
  return ctx.available && ctx.flow ? "available" : "unavailable";
}

function headline(s: FlowStatus, ctx: FlowContext, protocolName: string): string {
  if (s === "residual") return "Not comparable: the TVL series is in a residual regime";
  if (s === "no_token_data") return "No per-asset token history at the source";
  if (s === "unavailable") return "Token flow could not be estimated for this day";
  const pct = ctx.flow!.pct_change;
  if (pct === 0) return `No estimated change in ${protocolName}'s token quantities`;
  return `Estimated token ${pct > 0 ? "inflow" : "outflow"} of ${formatPct(Math.abs(pct), { signed: false })} by quantity`;
}

function rows(items: FlowContribution[], max: number, order: 1 | -1): ContributionRow[] {
  return [...items]
    .sort((a, b) => order * (b.share - a.share))
    .map((c) => ({ symbol: c.symbol, text: formatPoints(c.share)!, scale: max > 0 ? Math.abs(c.share) / max : 0 }));
}

export function describeFlowContext(ctx: FlowContext, protocolName: string): FlowContextView {
  const s = status(ctx);
  const flowLog = ctx.flow?.log_change ?? null;
  const otherLog = ctx.non_flow_effect?.log_change ?? null;
  const tvlLog = ctx.tvl_log_return;

  let contributions: FlowContextView["contributions"] = null;
  const c = ctx.contributions;
  if (c && ctx.flow) {
    const listed = [...c.positive, ...c.negative];
    const max = Math.max(0, ...listed.map((x) => Math.abs(x.share)));
    const listedCount = listed.length + c.unchanged.length;
    contributions = {
      positive: rows(c.positive, max, 1),
      negative: rows(c.negative, max, -1),
      unchanged: c.unchanged,
      listedCount,
      maxListed: c.max_listed,
      truncated: listedCount >= c.max_listed,
      listed: formatPoints(c.listed_total)!,
      other: Math.abs(c.other_assets) < 5e-7 ? "0.00 pts" : formatPoints(c.other_assets)!,
      total: formatPoints(c.flow_total)!,
      unit: c.unit,
    };
  }

  const cov = ctx.coverage;
  const lend = ctx.lending;
  return {
    status: s,
    headline: headline(s, ctx, protocolName),
    measurementDay: ctx.measurement_day,
    previousDay: ctx.previous_day,
    flow: ctx.flow ? { pct: formatPct(ctx.flow.pct_change)!, log: formatLog(flowLog)!, z: formatFlowZ(ctx.flow.robust_z), valuation: ctx.flow.valuation } : null,
    otherEffect: otherLog === null ? null : { pct: formatLogAsPct(otherLog)!, log: formatLog(otherLog)! },
    tvlChange: tvlLog === null ? null : { pct: formatLogAsPct(tvlLog)!, log: formatLog(tvlLog)! },
    identity: tvlLog !== null && flowLog !== null && otherLog !== null
      ? `${formatLog(tvlLog)} = ${formatLog(flowLog)} flow ${formatLog(otherLog)!.replace(/^([+−])/, "$1 ")} other`
      : null,
    solChange: formatLogAsPct(ctx.sol_log_return),
    coverage: cov ? {
      matched: formatPct(cov.matched_share, { digits: 1, signed: false })!,
      minimum: formatPct(cov.minimum_matched_share, { digits: 0, signed: false })!,
      belowMinimum: cov.matched_share < cov.minimum_matched_share,
      added: formatPct(cov.added_share, { signed: false }),
      removed: formatPct(cov.removed_share, { signed: false }),
      unpriced: cov.unpriced_assets === null ? null : `${cov.unpriced_assets} ${cov.unpriced_assets === 1 ? "asset" : "assets"}`,
    } : null,
    contributions,
    lending: lend ? {
      borrowed: formatUsd(lend.borrowed_usd),
      borrowedChange: formatLogAsPct(lend.borrowed_log_change),
      utilisation: formatPct(lend.utilisation, { signed: false }),
      utilisationChange: formatPoints(lend.utilisation_change),
      utilisationZ: formatFlowZ(lend.utilisation_change_z),
      definition: lend.definition,
    } : null,
    lendingNotes: groupReasons(ctx.unavailable, protocolName, { lending: true }),
    notes: groupReasons(ctx.unavailable, protocolName, { lending: false }),
    source: `${ctx.source.source} · ${ctx.source.adapters.join(", ")}`,
    disclaimer: ctx.disclaimer,
    featureVersion: ctx.feature_version,
  };
}
