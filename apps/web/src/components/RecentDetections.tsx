import { fetchAnomalies, type Anomaly, type RiskLevel } from "@/lib/api";
import { formatAbsoluteUtc, formatRelative, parseApiTimestamp } from "@/lib/time";
import { unstable_rethrow } from "next/navigation";

const SEVERITY_STYLES: Record<RiskLevel, string> = {
  LOW: "border-emerald-400/30 bg-emerald-400/10 text-emerald-300",
  MEDIUM: "border-amber-400/30 bg-amber-400/10 text-amber-300",
  HIGH: "border-orange-400/30 bg-orange-400/10 text-orange-300",
  CRITICAL: "border-red-400/30 bg-red-400/10 text-red-300",
};

function label(value: string) { return value.toLowerCase().replaceAll("_", " "); }
function score(value: number) { return Number.isFinite(value) ? `${value.toFixed(1)}th percentile` : "Unavailable"; }
function detectedAt(value: string | null | undefined) {
  const date = parseApiTimestamp(value);
  if (!date) return { relative: "Timestamp unavailable", absolute: "Timestamp unavailable" };
  return { relative: formatRelative(date), absolute: formatAbsoluteUtc(date) };
}

function DetectionRow({ anomaly }: { anomaly: Anomaly }) {
  const time = detectedAt(anomaly.detected_at);
  return <article className="grid gap-4 border-b border-slate-800/80 py-5 last:border-0 md:grid-cols-[minmax(11rem,0.8fr)_minmax(0,2fr)_auto] md:items-start">
    <div><div className="font-semibold text-slate-100">{anomaly.protocol.name}</div><div className="mt-1 text-xs text-slate-500">@{anomaly.protocol.slug}</div><div className="mt-3 text-xs text-slate-500" title={time.absolute}>Detected <span className="text-slate-300">{time.relative}</span></div><div className="mt-1 text-[11px] text-slate-600">{time.absolute}</div></div>
    <div><div className="flex flex-wrap items-center gap-2"><span className="text-sm font-semibold capitalize text-slate-100">{label(anomaly.type)}</span><span className={`rounded border px-2 py-0.5 text-[11px] font-bold tracking-wide ${SEVERITY_STYLES[anomaly.severity]}`}>{anomaly.severity}</span></div><p className="mt-2 max-w-2xl text-sm leading-6 text-slate-400">{anomaly.description || "No detector explanation was provided."}</p></div>
    <div className="rounded-lg border border-cyan-400/15 bg-cyan-400/[0.03] px-3 py-2 text-left md:min-w-32 md:text-right"><div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">Anomaly score</div><div className="mt-1 text-sm font-semibold text-cyan-300">{score(anomaly.calibrated_score)}</div></div>
  </article>;
}

export function RecentDetectionsLoading() { return <section aria-busy="true" className="rounded-2xl border border-slate-800 bg-slate-900/40 p-5 sm:p-6"><div className="h-4 w-40 animate-pulse rounded bg-slate-800" /><div className="mt-5 h-24 animate-pulse rounded-xl bg-slate-800/60" /></section>; }

export async function RecentDetections() {
  // Only the request is guarded; rendering happens outside try/catch so
  // render errors still reach the nearest error boundary.
  let feed: Awaited<ReturnType<typeof fetchAnomalies>>;
  try {
    feed = await fetchAnomalies({ limit: 6 });
  } catch (error) {
    unstable_rethrow(error);
    return <section aria-labelledby="recent-detections" className="rounded-2xl border border-amber-500/25 bg-amber-500/[0.04] p-5 sm:p-6"><p className="text-xs font-semibold uppercase tracking-[0.16em] text-amber-300">Detector feed unavailable</p><h2 id="recent-detections" className="mt-2 text-xl font-semibold text-white">Recent detections</h2><p className="mt-2 max-w-2xl text-sm leading-6 text-slate-400">The anomaly service could not be reached. Protocol risk data remains available independently; this panel will resume when the feed is reachable.</p></section>;
  }
  const { data, pagination } = feed;
  return <section aria-labelledby="recent-detections" className="rounded-2xl border border-slate-800 bg-slate-900/40 p-5 shadow-2xl shadow-slate-950/20 sm:p-6">
    <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end"><div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-cyan-300">Detector feed</p><h2 id="recent-detections" className="mt-2 text-xl font-semibold text-white">Recent detections</h2><p className="mt-1 text-sm text-slate-400">Persisted detector results, ordered from newest to oldest.</p></div><div className="rounded-lg border border-slate-700 bg-slate-950/60 px-3 py-2 text-left sm:text-right"><div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">Recorded anomalies</div><div className="mt-1 text-lg font-semibold text-white">{pagination.total.toLocaleString()}</div></div></div>
    {data.length === 0 ? <div className="mt-6 rounded-xl border border-dashed border-slate-700 bg-slate-950/30 px-5 py-8 text-center"><p className="font-medium text-slate-200">No detections recorded yet</p><p className="mx-auto mt-2 max-w-2xl text-sm leading-6 text-slate-500">This is not an all-clear. The detector only scores a protocol once it has roughly 18 days of continuous 5-minute snapshots and a usable transaction-count history; until then it abstains rather than guessing, so an empty feed means no protocol is being assessed yet.</p></div> : <div className="mt-2">{data.map((anomaly) => <DetectionRow key={anomaly.id} anomaly={anomaly} />)}</div>}
  </section>;
}
