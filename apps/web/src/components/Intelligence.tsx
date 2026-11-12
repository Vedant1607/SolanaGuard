import type { IntelligenceReport, IntelligenceResult } from "@/lib/api";
import {
  buildExcessChart,
  chartDayTitle,
  dayRanges,
  formatDay,
  formatLogReturn,
  formatRatio,
  formatZ,
  summarizeIntelligence,
  type ChartDay,
  type IntelligenceClassification,
  type IntelligenceSummary,
} from "@/lib/intelligence-view";
import { formatAbsoluteUtc, parseApiTimestamp } from "@/lib/time";

// TVL-behaviour intelligence (tvl-robust-v1). Deliberately styled apart from
// the security LevelBadge: this is statistical unusualness of daily TVL, not a
// security level, and never a probability of an exploit.

const CLASS_STYLES: Record<IntelligenceClassification, string> = {
  NORMAL: "border-sky-400/25 bg-sky-400/[0.07] text-sky-200",
  UNUSUAL: "border-fuchsia-400/35 bg-fuchsia-400/10 text-fuchsia-200",
  INSUFFICIENT_HISTORY: "border-slate-600 bg-slate-800/60 text-slate-300",
};

const CLASS_GLYPH: Record<IntelligenceClassification, string> = { NORMAL: "●", UNUSUAL: "▲", INSUFFICIENT_HISTORY: "○" };

const UNAVAILABLE_TEXT: Record<Exclude<IntelligenceResult, { ok: true }>["reason"], string> = {
  not_found: "TVL behaviour is not served by this API build yet.",
  not_ready: "TVL behaviour data is not available on this server yet.",
  unavailable: "TVL behaviour could not be loaded. No classification is shown until it can be.",
  malformed: "The TVL behaviour response was not understood, so no classification is shown.",
};

export function IntelligenceBadge({ summary, prefix = true }: { summary: IntelligenceSummary; prefix?: boolean }) {
  return <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold ${CLASS_STYLES[summary.classification]}`}>
    <span aria-hidden="true" className="text-[9px]">{CLASS_GLYPH[summary.classification]}</span>
    {prefix && <span className="font-medium opacity-75">TVL behaviour</span>} {summary.label}{summary.stale ? " · stale" : ""}
  </span>;
}

function Metric({ label, value, hint, muted }: { label: string; value: string; hint?: string; muted?: boolean }) {
  return <div title={hint}><div className="text-[11px] text-slate-500">{label}</div><div className={`mt-0.5 font-semibold tabular-nums ${muted ? "text-slate-500" : "text-slate-100"}`}>{value}</div></div>;
}

/** Compact block for a dashboard card. */
export function IntelligenceCardSection({ result, now }: { result: IntelligenceResult; now: number }) {
  const heading = <span className="font-semibold uppercase tracking-[0.14em] text-slate-500" title="tvl-robust-v1: how unusual the latest daily TVL change is versus this protocol's history and its peers. Not a security level.">TVL behaviour</span>;
  if (!result.ok) {
    return <div className="mt-4 rounded-xl border border-slate-800 bg-slate-950/40 p-3 text-xs">{heading}<p className="mt-2 leading-5 text-slate-400">{UNAVAILABLE_TEXT[result.reason]}</p></div>;
  }
  const report = result.data;
  const s = summarizeIntelligence(report, now);
  const scored = s.classification !== "INSUFFICIENT_HISTORY";
  return <div className="mt-4 rounded-xl border border-slate-800 bg-slate-950/40 p-3 text-xs">
    <div className="flex flex-wrap items-center justify-between gap-2">{heading}<IntelligenceBadge summary={s} prefix={false} /></div>
    <div className="mt-3 grid grid-cols-3 gap-2 text-sm">
      <Metric label="Robust z" value={s.zText} muted={!scored} hint={`Unusual at |z| ≥ ${report.threshold}`} />
      <Metric label="30d drawdown" value={s.drawdownText} muted={report.drawdown_30d === null} hint="Latest closed day versus the highest of the preceding 30 days" />
      <Metric label="Closed day" value={report.latest_closed_day ? formatDay(report.latest_closed_day).replace(/, \d{4}$/, "") : "—"} muted={!report.latest_closed_day} hint="Latest closed UTC day with a stored DefiLlama observation" />
    </div>
    {scored
      ? <p className="mt-2 leading-5 text-slate-500">{report.market.market_wide ? "Market-wide move; " : ""}{formatLogReturn(report.daily_log_return)} day change, {formatLogReturn(report.excess_log_return)} vs peers.</p>
      : <p className="mt-2 leading-5 text-slate-400">{s.reasons[0]?.detail}</p>}
    <div className={`mt-1 ${s.stale ? "text-amber-300" : "text-slate-600"}`}>{s.freshnessText} · DefiLlama daily TVL</div>
  </div>;
}

// --- detail page ------------------------------------------------------------

const W = 720;
const H = 220;
const PAD = { top: 18, right: 12, bottom: 34, left: 52 };
const PLOT_H = H - PAD.top - PAD.bottom;
const PLOT_W = W - PAD.left - PAD.right;

/** Real daily excess returns over the history window. Missing days are drawn as gaps, never as zero. */
export function ExcessReturnChart({ report }: { report: IntelligenceReport }) {
  if (!report.history_window) return null;
  const chart = buildExcessChart(report.history, report.history_window);
  if (chart.days.length === 0) return null;
  if (chart.scoredCount === 0) {
    return <p className="mt-3 rounded-lg border border-dashed border-slate-700 px-3 py-6 text-center text-xs text-slate-500">No scored days in the last {chart.days.length} days{chart.missingDays.length ? ` (${chart.missingDays.length} days have no observation)` : ""}.</p>;
  }
  const step = PLOT_W / chart.days.length;
  const barW = Math.max(1, step * 0.72);
  const y = (v: number) => PAD.top + PLOT_H / 2 - (Math.max(-chart.limit, Math.min(chart.limit, v)) / chart.limit) * (PLOT_H / 2);
  const zeroY = y(0);
  const x = (d: ChartDay) => PAD.left + d.index * step + (step - barW) / 2;
  const ticks = [chart.limit, chart.limit / 2, 0, -chart.limit / 2, -chart.limit];
  const labelIdx = [0, Math.floor(chart.days.length / 2), chart.days.length - 1];
  const summaryText = `Daily excess return versus the peer median, ${formatDay(chart.start)} to ${formatDay(chart.end)}: ${chart.scoredCount} scored days, ${chart.unusualDays.length} unusual, ${chart.missingDays.length} without an observation.`;

  return <figure className="mt-4">
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full min-w-[560px]" role="img" aria-label={summaryText}>
        <defs>
          <pattern id="missing-hatch" width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="4" stroke="rgb(100 116 139 / 0.45)" strokeWidth="1.2" /></pattern>
        </defs>
        {ticks.map((t) => <g key={t}>
          <line x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} stroke={t === 0 ? "rgb(148 163 184 / 0.7)" : "rgb(51 65 85 / 0.5)"} strokeDasharray={t === 0 ? undefined : "2 4"} strokeWidth={t === 0 ? 1 : 0.75} />
          <text x={PAD.left - 6} y={y(t) + 3} textAnchor="end" className="fill-slate-500 text-[10px] tabular-nums">{t === 0 ? "0%" : formatLogReturn(t, Math.abs(Math.expm1(t)) < 0.1 ? 1 : 0)}</text>
        </g>)}
        {chart.days.map((d) => {
          const title = <title>{chartDayTitle(d, report.threshold)}</title>;
          if (d.kind === "missing") {
            return <g key={d.day}><rect x={PAD.left + d.index * step} y={PAD.top} width={step} height={PLOT_H} fill="url(#missing-hatch)" />{title}</g>;
          }
          if (d.value === null) {
            return <g key={d.day}><circle cx={x(d) + barW / 2} cy={zeroY} r={Math.max(1.5, barW / 2)} fill="none" stroke="rgb(148 163 184 / 0.8)" strokeWidth="1" />{title}</g>;
          }
          const top = Math.min(y(d.value), zeroY);
          const height = Math.max(1, Math.abs(y(d.value) - zeroY));
          const fill = d.unusual ? "rgb(232 121 249)" : d.kind === "unscored" ? "rgb(100 116 139 / 0.7)" : d.value < 0 ? "rgb(125 211 252 / 0.55)" : "rgb(125 211 252 / 0.85)";
          return <g key={d.day}>
            <rect x={PAD.left + d.index * step} y={PAD.top} width={step} height={PLOT_H} fill="transparent" />
            <rect x={x(d)} y={top} width={barW} height={height} fill={fill} rx={barW > 3 ? 1 : 0} />
            {d.clipped && <path d={d.value < 0 ? `M${x(d)} ${PAD.top + PLOT_H + 2} l${barW / 2} 4 l${barW / 2} -4z` : `M${x(d)} ${PAD.top - 2} l${barW / 2} -4 l${barW / 2} 4z`} fill={fill} />}
            {d.unusual && <path d={`M${x(d) + barW / 2 - 4} ${PAD.top - 9} l4 6 l4 -6z`} fill="rgb(232 121 249)" stroke="rgb(15 23 42)" strokeWidth="0.75" />}
            {title}
          </g>;
        })}
        {labelIdx.map((i, n) => <text key={i} x={PAD.left + chart.days[i].index * step + step / 2} y={H - 12} textAnchor={n === 0 ? "start" : n === 2 ? "end" : "middle"} className="fill-slate-500 text-[10px]">{formatDay(chart.days[i].day)}</text>)}
      </svg>
    </div>
    <figcaption className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
      <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-sky-300/80 align-middle" />Excess return vs peer median</span>
      <span><span className="mr-1 text-fuchsia-300">▼</span>Unusual (|z| ≥ {report.threshold})</span>
      <span><span className="mr-1 inline-block h-2 w-3 align-middle" style={{ backgroundImage: "repeating-linear-gradient(45deg, rgb(100 116 139 / 0.6) 0 1px, transparent 1px 3px)" }} />No observation (not zero)</span>
      <span><span className="mr-1 inline-block h-2 w-2 rounded-full border border-slate-400 align-middle" />Stored but not scored</span>
      {chart.clippedCount > 0 && <span className="text-slate-400">{chart.clippedCount} bar(s) extend beyond the axis range (marked with a triangle); hover for the actual value</span>}
    </figcaption>
    <details className="mt-3 text-xs text-slate-400">
      <summary className="cursor-pointer text-slate-500 hover:text-slate-300">Unusual and missing days</summary>
      <div className="mt-2 grid gap-3 sm:grid-cols-2">
        <div><div className="font-semibold text-slate-300">Unusual days ({chart.unusualDays.length})</div>{chart.unusualDays.length === 0 ? <p className="mt-1 text-slate-500">None in this window.</p> : <ul className="mt-1 space-y-0.5">{chart.unusualDays.map((d) => <li key={d.day} className="tabular-nums">{formatDay(d.day)} · {formatLogReturn(d.value)} vs peers · z {formatZ(d.z)}{d.marketWide ? " · market-wide" : ""}</li>)}</ul>}</div>
        <div><div className="font-semibold text-slate-300">Days without an observation ({chart.missingDays.length})</div>{chart.missingDays.length === 0 ? <p className="mt-1 text-slate-500">None — every calendar day in the window has a stored observation.</p> : <ul className="mt-1 space-y-0.5">{dayRanges(chart.missingDays).map((r) => <li key={r.start}>{r.count === 1 ? formatDay(r.start) : `${formatDay(r.start)} – ${formatDay(r.end)} (${r.count} days)`}</li>)}</ul>}</div>
      </div>
    </details>
  </figure>;
}

function Row({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return <div title={hint}><dt className="text-xs text-slate-500">{label}</dt><dd className="mt-1 font-semibold tabular-nums text-slate-100">{children}</dd></div>;
}

/** Intelligence section on /protocols/[slug]. */
export function IntelligencePanel({ result, now }: { result: IntelligenceResult; now: number }) {
  const title = <div className="flex flex-wrap items-baseline justify-between gap-2"><h2 className="font-semibold text-white">TVL behaviour intelligence</h2><span className="text-xs text-slate-500">Separate from the security assessment</span></div>;
  if (!result.ok) {
    return <section className="mt-6 rounded-2xl border border-slate-800 bg-slate-900/40 p-5">{title}<p className="mt-3 text-sm text-slate-400">{UNAVAILABLE_TEXT[result.reason]}</p></section>;
  }
  const r = result.data;
  const s = summarizeIntelligence(r, now);
  const scored = s.classification !== "INSUFFICIENT_HISTORY";
  const w = r.observation_window;
  const evaluated = parseApiTimestamp(r.evaluated_at);
  return <section className="mt-6 rounded-2xl border border-slate-800 bg-slate-900/40 p-5" aria-labelledby="intelligence-heading">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 id="intelligence-heading" className="font-semibold text-white">TVL behaviour intelligence</h2><p className="mt-1 text-xs text-slate-500">Separate from the security assessment and the activity signal.</p></div>
      <div className="flex flex-col items-start gap-1 sm:items-end"><IntelligenceBadge summary={s} /><span className={`text-xs ${s.stale ? "text-amber-300" : "text-slate-500"}`}>{s.freshnessText}</span></div>
    </div>

    {scored
      ? <ul className="mt-4 space-y-1.5 text-sm leading-6 text-slate-300">{r.explanation.map((line) => <li key={line} className="flex gap-2"><span aria-hidden="true" className="text-slate-600">—</span><span>{line}</span></li>)}</ul>
      : <div className="mt-4 rounded-lg border border-slate-700 bg-slate-800/40 px-3 py-2 text-sm text-slate-300"><p className="font-medium">Not classified: no NORMAL or UNUSUAL result is shown.</p><ul className="mt-1 space-y-0.5 text-xs text-slate-400">{s.reasons.map((reason) => <li key={reason.code}>{reason.detail}</li>)}</ul></div>}

    <dl className="mt-5 grid grid-cols-2 gap-x-4 gap-y-4 text-sm sm:grid-cols-4">
      <Row label="Robust z / threshold" hint="Robust z-score of the excess return against the trailing 90-day median and MAD">{scored ? <>{formatZ(r.robust_z)} <span className="font-normal text-slate-500">/ ±{r.threshold}</span></> : <span className="text-slate-500">— / ±{r.threshold}</span>}</Row>
      <Row label="Daily TVL change">{formatLogReturn(r.daily_log_return, 2)}</Row>
      <Row label="Peer median change" hint={`Median daily change of the other monitored protocols (${w.peers} peers)`}>{formatLogReturn(r.market.peer_median_return, 2)}{r.market.market_wide ? <span className="ml-1 text-xs font-normal text-amber-300">market-wide</span> : null}</Row>
      <Row label="Excess log return" hint="Protocol log return minus the peer median log return">{r.excess_log_return === null ? "—" : r.excess_log_return.toFixed(4)}</Row>
      <Row label="30-day drawdown">{formatRatio(r.drawdown_30d)}</Row>
      <Row label="Latest closed day">{formatDay(r.latest_closed_day)}</Row>
      <Row label="Baseline window" hint="Strictly trailing: the scored day is never in its own baseline">{w.baseline_start ? <>{formatDay(w.baseline_start).replace(/, \d{4}$/, "")} – {formatDay(w.baseline_end)}</> : "—"}<div className="text-xs font-normal text-slate-500">{w.baseline_points} of {w.baseline_days} days · {w.peers} peers</div></Row>
      <Row label="History used" hint="Closed daily observations before the latest day that were loaded for scoring. The API reads a bounded recent window, so this is not the protocol's full stored history.">{w.history_days_before_latest >= r.model.min_history_days ? "Sufficient" : "Insufficient"}<div className="text-xs font-normal text-slate-500">{w.history_days_before_latest.toLocaleString()} prior days loaded · minimum {r.model.min_history_days}</div></Row>
    </dl>

    <div className="mt-6 border-t border-slate-800 pt-5">
      <h3 className="text-sm font-semibold text-slate-200">Daily excess return, last {r.history_window?.days ?? 0} days</h3>
      <p className="mt-1 text-xs text-slate-500">Each bar is the protocol&apos;s daily TVL change minus the median change of its peers, from stored closed-day observations. Hover a day for its value and score.</p>
      {r.history_window ? <ExcessReturnChart report={r} /> : <p className="mt-3 text-xs text-slate-500">History was not included in this response.</p>}
    </div>

    <div className="mt-6 grid gap-4 border-t border-slate-800 pt-5 text-xs text-slate-500 sm:grid-cols-2">
      <div>
        <div className="font-semibold uppercase tracking-[0.14em] text-slate-500">Model</div>
        <p className="mt-1 text-slate-300">{r.model.name} · <span className="font-mono">{r.model.version}</span></p>
        <p className="mt-1 leading-5">{r.model.method}</p>
        <p className="mt-1" title={formatAbsoluteUtc(evaluated)}>Computed on request; threshold frozen at |z| ≥ {r.threshold} (tuned 2023–2024 for a ~{(r.model.target_flag_rate * 100).toFixed(0)}% flag rate).</p>
      </div>
      <div>
        <div className="font-semibold uppercase tracking-[0.14em] text-slate-500">Provenance</div>
        <p className="mt-1 text-slate-300">{r.source.source} · <span className="font-mono">{r.source.adapter}</span> · <span className="font-mono">{r.source.metric_key}</span> ({r.source.resolution})</p>
        {r.supporting_observations.length === 0 ? <p className="mt-1">No supporting observations.</p> : <ul className="mt-1 space-y-0.5">{r.supporting_observations.map((o) => <li key={o.id} className="tabular-nums">{o.role === "latest_day" ? "Latest day" : o.role === "previous_day" ? "Previous day" : o.role}: {formatDay(o.day)} · ${new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(o.value_usd)} · <span className="font-mono text-[10px]" title={o.id}>{o.id.slice(0, 8)}</span> <span title={formatAbsoluteUtc(parseApiTimestamp(o.collected_at))}>(collected {formatDay(o.collected_at.slice(0, 10))})</span></li>)}</ul>}
      </div>
    </div>

    <p className="mt-5 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-xs leading-5 text-slate-400">{r.disclaimer}</p>
  </section>;
}
