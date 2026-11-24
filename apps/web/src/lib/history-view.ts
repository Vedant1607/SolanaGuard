// Presentation of GET /api/v1/history/refresh-status. Two separate facts are
// shown and never merged: the latest closed day actually stored (what the
// analysis uses) and what the refresh scheduler last did (in-memory on the
// API; unknown after a restart). Kept free of runtime imports for node --test.

export interface HistoryRefreshStatus {
  evaluated_at: string;
  refresh: {
    enabled: boolean;
    state: string;
    running: boolean;
    interval_seconds: number;
    last_attempt_started_at: string | null;
    last_attempt_finished_at: string | null;
    last_outcome: string | null;
    last_success_at: string | null;
    last_failure: string | null;
    next_run_at: string | null;
  };
  availability:
    | { ok: true; available_through: string | null; age_days: number | null; metrics: Record<string, { latest_day: string; lagging_protocols: string[]; protocols: Record<string, string> }> }
    | { ok: false; reason: string };
}

/** Same limit as the model's MAX_LATEST_AGE_DAYS. */
export const HISTORY_STALE_AFTER_DAYS = 3;
const DAY_MS = 86_400_000;

export interface HistoryNotice {
  tone: "ok" | "warning" | "info";
  text: string;
}

export interface HistoryView {
  availableThrough: string | null;
  availability: HistoryNotice;
  refresh: HistoryNotice;
  lagging: string[];
}

function ms(value: string | null | undefined): number | null {
  if (!value) return null;
  const t = Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`);
  return Number.isNaN(t) ? null : t;
}

function ago(t: number, now: number): string {
  const minutes = Math.max(0, Math.round((now - t) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 1_440) return `${Math.round(minutes / 60)} h ago`;
  return `${Math.round(minutes / 1_440)} d ago`;
}

function until(t: number, now: number): string {
  const minutes = Math.max(0, Math.round((t - now) / 60_000));
  return minutes < 60 ? `${minutes} min` : `${Math.round(minutes / 60)} h`;
}

function dayAge(day: string, now: number): number {
  return Math.round((Math.floor(now / DAY_MS) * DAY_MS - Date.parse(`${day}T00:00:00Z`)) / DAY_MS);
}

export function describeHistory(status: HistoryRefreshStatus | null, now: number): HistoryView {
  if (!status) {
    return {
      availableThrough: null, lagging: [],
      availability: { tone: "info", text: "Daily history status could not be loaded." },
      refresh: { tone: "info", text: "Refresh status unknown." },
    };
  }
  let availableThrough: string | null = null;
  let lagging: string[] = [];
  let availability: HistoryNotice;
  if (!status.availability.ok) {
    availability = { tone: "info", text: "Daily history availability could not be read." };
  } else if (!status.availability.available_through) {
    availability = { tone: "warning", text: "No daily history is stored yet." };
  } else {
    availableThrough = status.availability.available_through;
    const age = dayAge(availableThrough, now);
    const tvl = status.availability.metrics["tvl.usd.daily"];
    lagging = tvl ? tvl.lagging_protocols.map((s) => `${s} (${tvl.protocols[s]})`) : [];
    availability = age > HISTORY_STALE_AFTER_DAYS
      ? { tone: "warning", text: `Daily historical data available through ${availableThrough} (UTC), ${age} days old: TVL behaviour is shown as insufficient until it is refreshed.` }
      : { tone: "ok", text: `Daily historical data available through ${availableThrough} (UTC closed day).` };
  }

  const r = status.refresh;
  const started = ms(r.last_attempt_started_at);
  const finished = ms(r.last_attempt_finished_at);
  const next = ms(r.next_run_at);
  const nextText = next && next > now ? ` Next run in ~${until(next, now)}.` : "";
  let refresh: HistoryNotice;
  if (!r.enabled) {
    refresh = { tone: "info", text: "Automatic history refresh is off on this server; history is updated by running the backfill scripts." };
  } else if (r.running) {
    refresh = { tone: "info", text: `History refresh in progress${started ? ` (started ${ago(started, now)})` : ""}.` };
  } else if (!r.last_outcome || finished === null) {
    refresh = { tone: "info", text: `No refresh has completed since the API started.${nextText}` };
  } else if (r.last_outcome === "succeeded") {
    refresh = { tone: "ok", text: `Last history refresh succeeded ${ago(finished, now)}.${nextText}` };
  } else if (r.last_outcome === "partial") {
    refresh = { tone: "warning", text: `Last history refresh partly failed ${ago(finished, now)}${r.last_failure ? `: ${r.last_failure}` : ""}.${nextText}` };
  } else if (r.last_outcome === "skipped_locked" || r.last_outcome === "skipped_running") {
    refresh = { tone: "info", text: `Last history refresh was skipped ${ago(finished, now)}: another refresh was running.${nextText}` };
  } else {
    refresh = { tone: "warning", text: `Last history refresh failed ${ago(finished, now)}${r.last_failure ? `: ${r.last_failure}` : ""}.${nextText}` };
  }
  return { availableThrough, availability, refresh, lagging };
}
