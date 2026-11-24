import type { IntelligenceResult } from "@/lib/api";
import { describeFlowContext, parseFlowContext, type ContributionRow, type FlowContextView, type ReasonNote } from "@/lib/flow-context-view";
import { formatDay } from "@/lib/intelligence-view";

// "What moved TVL": the features-daily-v1 explanation of the latest closed
// day's TVL change. Styled apart from both the security LevelBadge and the
// TVL-behaviour classification: it describes composition, it is not a score.

const HEADING_ID = "flow-context-heading";

function Shell({ children, subtitle }: { children: React.ReactNode; subtitle?: React.ReactNode }) {
  return <section className="mt-6 rounded-2xl border border-slate-800 bg-slate-900/40 p-5" aria-labelledby={HEADING_ID}>
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 id={HEADING_ID} className="font-semibold text-white">What moved TVL</h2>
        <p className="mt-1 text-xs text-slate-500">{subtitle ?? "Token quantity flow versus other effects, from DefiLlama daily token data."}</p>
      </div>
      <span className="rounded-full border border-slate-700 px-2 py-0.5 text-[11px] text-slate-400" title="An explanation of the TVL change. It is neither a security level nor the TVL-behaviour classification.">Explanation · not a risk score</span>
    </div>
    {children}
  </section>;
}

function Unavailable({ children }: { children: React.ReactNode }) {
  return <Shell><p className="mt-3 rounded-lg border border-dashed border-slate-700 px-3 py-4 text-sm text-slate-400">{children}</p></Shell>;
}

const NA = <span className="font-normal text-slate-500">Not available</span>;

function Stat({ label, hint, value, sub, tone = "text-slate-100" }: { label: string; hint: string; value: React.ReactNode; sub?: React.ReactNode; tone?: string }) {
  return <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-3" title={hint}>
    <dt className="text-[11px] text-slate-500">{label} <span aria-hidden="true" className="cursor-help text-slate-600">ⓘ</span><span className="sr-only">: {hint}</span></dt>
    <dd className={`mt-1 text-lg font-semibold tabular-nums ${tone}`}>{value}</dd>
    {sub && <dd className="mt-0.5 text-xs leading-5 text-slate-500">{sub}</dd>}
  </div>;
}

function Notes({ notes }: { notes: ReasonNote[] }) {
  if (notes.length === 0) return null;
  return <ul className="mt-4 space-y-2 text-sm">{notes.map((n) => <li key={n.code} className="rounded-lg border border-slate-700 bg-slate-800/40 px-3 py-2">
    <div className="text-xs font-medium text-slate-300">{n.fields.join(" · ")} <span className="font-mono text-[10px] font-normal text-slate-500">{n.code}</span></div>
    <p className="mt-0.5 leading-6 text-slate-400">{n.text}</p>
  </li>)}</ul>;
}

function Bars({ rows, tone, empty }: { rows: ContributionRow[]; tone: "in" | "out"; empty: string }) {
  if (rows.length === 0) return <p className="mt-2 text-xs text-slate-500">{empty}</p>;
  const bar = tone === "in" ? "bg-emerald-400/60" : "bg-rose-400/60";
  const text = tone === "in" ? "text-emerald-200" : "text-rose-200";
  return <ul className="mt-2 space-y-1.5">{rows.map((r) => <li key={r.symbol} className="grid grid-cols-[5.5rem_1fr_5.5rem] items-center gap-2 text-xs">
    <span className="truncate font-mono text-slate-300" title={r.symbol}>{r.symbol}</span>
    <span className="h-1.5 rounded-full bg-slate-800" aria-hidden="true"><span className={`block h-1.5 rounded-full ${bar}`} style={{ width: `${Math.max(2, r.scale * 100)}%` }} /></span>
    <span className={`text-right tabular-nums ${text}`}>{r.text}</span>
  </li>)}</ul>;
}

function Contributions({ c }: { c: NonNullable<FlowContextView["contributions"]> }) {
  return <div className="mt-6 border-t border-slate-800 pt-5">
    <h3 className="text-sm font-semibold text-slate-200">Which assets moved</h3>
    <p className="mt-1 text-xs leading-5 text-slate-500">Each asset&apos;s quantity change valued at the previous day&apos;s price, in percentage points of the previous day&apos;s matched value; together they add up to the estimated flow. {c.truncated ? `Only the ${c.maxListed} largest contributions are listed; the rest are summed below.` : "All assets with a non-zero contribution are listed."}</p>
    <div className="mt-3 grid gap-5 sm:grid-cols-2">
      <div><div className="text-xs font-semibold text-slate-300">Added quantity ({c.positive.length})</div><Bars rows={c.positive} tone="in" empty="No listed asset gained quantity." /></div>
      <div><div className="text-xs font-semibold text-slate-300">Reduced quantity ({c.negative.length})</div><Bars rows={c.negative} tone="out" empty="No listed asset lost quantity." /></div>
    </div>
    {c.unchanged.length > 0 && <p className="mt-3 text-xs text-slate-500">Unchanged quantity: <span className="font-mono text-slate-400">{c.unchanged.join(", ")}</span></p>}
    <dl className="mt-4 grid grid-cols-3 gap-2 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-xs tabular-nums">
      <div><dt className="text-slate-500">Listed assets</dt><dd className="mt-0.5 font-semibold text-slate-200">{c.listed}</dd></div>
      <div title="Matched assets not listed above (flow total minus listed total)"><dt className="text-slate-500">Other matched assets</dt><dd className="mt-0.5 font-semibold text-slate-200">{c.other}</dd></div>
      <div><dt className="text-slate-500">Estimated flow</dt><dd className="mt-0.5 font-semibold text-white">{c.total}</dd></div>
    </dl>
  </div>;
}

function Lending({ view }: { view: FlowContextView }) {
  const l = view.lending;
  if (!l) {
    // Not a lending protocol (or no borrowed history): one muted line, never a zero.
    const note = view.lendingNotes.find((n) => n.code === "not_applicable");
    return note ? <p className="mt-4 text-xs text-slate-500"><span className="text-slate-400">Utilisation:</span> {note.text}</p> : null;
  }
  return <div className="mt-6 border-t border-slate-800 pt-5">
    <h3 className="text-sm font-semibold text-slate-200">Lending</h3>
    <dl className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
      <Stat label="Borrowed value" hint="DefiLlama borrowed USD value on the measurement day" value={l.borrowed ?? NA} sub={l.borrowedChange ? `${l.borrowedChange} on the day` : undefined} />
      <Stat label="Utilisation" hint={l.definition} value={l.utilisation ?? NA} sub={l.utilisationChange ? `${l.utilisationChange} on the day` : undefined} />
      <Stat label="Utilisation change z" hint="Robust z-score of today's utilisation change against its trailing 90-day history. A historical statistic, not a risk score." value={l.utilisationZ ?? NA} sub="vs trailing 90-day changes" />
    </dl>
    <p className="mt-2 text-xs leading-5 text-slate-500">Utilisation = borrowed ÷ (borrowed + available liquidity). For this protocol DefiLlama&apos;s TVL is the available (unborrowed) liquidity, not gross deposits.</p>
    <Notes notes={view.lendingNotes} />
  </div>;
}

/** Flow explanation on /protocols/[slug]; reads the flow_context of the intelligence response. */
export function FlowContextPanel({ result, protocolName }: { result: IntelligenceResult; protocolName: string }) {
  if (!result.ok) return <Unavailable>The TVL explanation could not be loaded with the TVL behaviour data, so no flow figures are shown.</Unavailable>;
  const parsed = parseFlowContext(result.data.flow_context);
  if (parsed.state === "absent") return <Unavailable>This API build does not provide the token-flow explanation yet.</Unavailable>;
  if (parsed.state === "malformed") return <Unavailable>The token-flow explanation was not understood, so no flow figures are shown.</Unavailable>;

  const v = describeFlowContext(parsed.data, protocolName);
  const day = v.measurementDay ? `${formatDay(v.previousDay)} → ${formatDay(v.measurementDay)} (UTC closed days)` : "No closed day";
  const available = v.status === "available";
  const flowTone = !v.flow ? undefined : v.flow.pct.startsWith("+") ? "text-emerald-200" : v.flow.pct.startsWith("−") ? "text-rose-200" : undefined;

  return <Shell subtitle={<>Measured {day} · <span className="font-mono">{v.featureVersion}</span></>}>
    <p className={`mt-4 text-sm font-medium ${available ? "text-slate-100" : "text-slate-300"}`}>{v.headline}</p>

    {available || v.tvlChange ? <dl className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
      <Stat label="Estimated token flow" hint="Change in token quantities, each valued at the previous day's DefiLlama USD price. An estimate, not proof of deposits or withdrawals."
        value={v.flow ? v.flow.pct : NA} tone={flowTone}
        sub={v.flow ? <>Historical flow z {v.flow.z ?? "not defined"} · log {v.flow.log}</> : "Needs per-asset token history"} />
      <Stat label="Other effects" hint="TVL change not explained by quantity changes: price moves plus assets that were added, removed or unpriced. Not a pure price effect."
        value={v.otherEffect ? v.otherEffect.pct : NA} sub={v.otherEffect ? <>log {v.otherEffect.log}{v.solChange ? ` · SOL price ${v.solChange}` : ""}</> : "Cannot be separated without a flow"} />
      <Stat label="Daily TVL change" hint="Change in DefiLlama's stored daily TVL between the two closed days."
        value={v.tvlChange ? v.tvlChange.pct : NA} sub={v.tvlChange ? `log ${v.tvlChange.log}` : undefined} />
    </dl> : null}

    {v.identity && <p className="mt-2 text-xs text-slate-500">In log terms the TVL change splits exactly: <span className="font-mono text-slate-400">{v.identity}</span>. The historical flow z compares today&apos;s flow with the trailing 90 days; it is a statistic about flows, not a security or risk score.</p>}

    <Notes notes={v.notes} />

    {v.coverage && <div className="mt-6 border-t border-slate-800 pt-5">
      <h3 className="text-sm font-semibold text-slate-200">Data coverage</h3>
      <dl className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <div title="Share of the previous day's value held in assets present on both days"><dt className="text-xs text-slate-500">Matched value</dt><dd className={`mt-1 font-semibold tabular-nums ${v.coverage.belowMinimum ? "text-amber-300" : "text-slate-100"}`}>{v.coverage.matched}</dd><dd className="text-xs text-slate-500">minimum {v.coverage.minimum}</dd></div>
        <div title="Value of assets that appeared on the measurement day, as a share of the previous day's value"><dt className="text-xs text-slate-500">Newly added assets</dt><dd className="mt-1 font-semibold tabular-nums text-slate-100">{v.coverage.added ?? NA}</dd><dd className="text-xs text-slate-500">of previous-day value</dd></div>
        <div title="Previous-day value of assets that are no longer reported"><dt className="text-xs text-slate-500">Removed assets</dt><dd className="mt-1 font-semibold tabular-nums text-slate-100">{v.coverage.removed ?? NA}</dd><dd className="text-xs text-slate-500">of previous-day value</dd></div>
        <div title="Assets reported without a USD price"><dt className="text-xs text-slate-500">Unpriced assets</dt><dd className="mt-1 font-semibold tabular-nums text-slate-100">{v.coverage.unpriced ?? NA}</dd></div>
      </dl>
      <p className="mt-2 text-xs leading-5 text-slate-500">Only assets present on both days are used for the flow. Newly added, removed and unpriced assets are excluded from it and end up in other effects.</p>
    </div>}

    {v.contributions && <Contributions c={v.contributions} />}

    <Lending view={v} />

    <details className="mt-6 border-t border-slate-800 pt-4 text-xs text-slate-500">
      <summary className="cursor-pointer text-slate-500 hover:text-slate-300">Method and source</summary>
      <ul className="mt-2 space-y-1 leading-5">
        <li>Flow = change in each asset&apos;s quantity × its previous-day DefiLlama USD price, summed over assets present on both days, as a share of their previous-day value.</li>
        <li>Other effects = TVL log change − flow log change: price moves, plus added, removed and unpriced assets.</li>
        <li>Flow z = (today&apos;s flow − median) ÷ scaled MAD of the trailing 90 days. Shown for context only; it does not change the TVL behaviour classification.</li>
        <li>Source: <span className="font-mono">{v.source}</span></li>
      </ul>
    </details>

    <p className="mt-4 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-xs leading-5 text-slate-400">{v.disclaimer}</p>
  </Shell>;
}
