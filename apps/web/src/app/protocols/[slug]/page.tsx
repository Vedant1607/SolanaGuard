import Link from "next/link";
import { notFound, unstable_rethrow } from "next/navigation";
import { fetchProtocolSecurity, fetchProtocols, type Protocol, type RegistryAddress, type SecurityEvidence, type SecurityObservation } from "@/lib/api";
import { getSubscription } from "@/lib/get-subscription";
import { visibleProtocolCount } from "@/lib/plan-limits";
import { activitySourceText, openProtocolSlugs, protocolMetricLabels } from "@/lib/protocol-availability";
import { RULE_CONTEXT, explorerUrl, ruleTitle, shortKey, sortEvidence, summarizeSecurity } from "@/lib/security-view";
import { formatAbsoluteUtc, formatRelative, parseApiTimestamp } from "@/lib/time";
import { LevelBadge, SupportBadge } from "@/components/SecurityBadges";

export const dynamic = "force-dynamic";

const KIND_TEXT: Record<RegistryAddress["kind"], string> = {
  PROGRAM: "Program",
  STAKE_POOL: "Stake pool account",
  MARKET: "State / market account",
  MINT: "Token mint",
};

const STATUS_STYLES: Record<SecurityObservation["status"], string> = {
  OK: "text-emerald-300",
  STALE: "text-amber-300",
  MISSING: "text-slate-400",
  ERROR: "text-orange-300",
};

function Shell({ children }: { children: React.ReactNode }) {
  return <main className="min-h-screen bg-slate-950 text-slate-200"><div className="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8"><Link href="/" className="text-xs font-medium text-cyan-300 hover:text-cyan-200">← Dashboard</Link>{children}</div></main>;
}

function Notice({ title, children }: { title: string; children: React.ReactNode }) {
  return <div role="alert" className="mt-6 rounded-2xl border border-amber-500/25 bg-amber-500/[0.04] p-5"><p className="text-xs font-semibold uppercase tracking-[0.16em] text-amber-300">{title}</p><p className="mt-2 text-sm leading-6 text-slate-400">{children}</p></div>;
}

function ObservationTable({ observations, now }: { observations: SecurityObservation[]; now: number }) {
  if (observations.length === 0) return <p className="mt-3 text-xs text-slate-500">No observation has been collected for this indicator yet.</p>;
  return <div className="mt-3 overflow-x-auto"><table className="w-full min-w-[560px] text-left text-xs">
    <thead className="text-slate-500"><tr><th className="py-1 pr-3 font-medium">Measured</th><th className="py-1 pr-3 font-medium">Value</th><th className="py-1 pr-3 font-medium">Status</th><th className="py-1 pr-3 font-medium">Collected</th><th className="py-1 pr-3 font-medium">Slot</th><th className="py-1 font-medium">Source · adapter · id</th></tr></thead>
    <tbody className="text-slate-300">{observations.map((o) => {
      const collected = parseApiTimestamp(o.collected_at);
      return <tr key={o.id} className="border-t border-slate-800/80 align-top">
        <td className="py-1.5 pr-3 font-mono text-[11px]">{o.metric_key}</td>
        <td className="py-1.5 pr-3 font-mono text-[11px] break-all">{o.value === null ? <span className="text-slate-500">no value</span> : o.unit === "pubkey" ? <a href={explorerUrl(o.value)} target="_blank" rel="noreferrer" className="text-cyan-300 hover:text-cyan-200" title={o.value}>{shortKey(o.value)}</a> : `${o.value}${o.unit === "slot" ? "" : o.unit === "bool" ? "" : ` ${o.unit}`}`}</td>
        <td className={`py-1.5 pr-3 font-semibold ${STATUS_STYLES[o.status]}`}>{o.status}{o.error_code && <span className="ml-1 font-normal text-slate-400">({o.error_code})</span>}</td>
        <td className="py-1.5 pr-3 whitespace-nowrap" title={formatAbsoluteUtc(collected)}>{collected ? formatRelative(collected, now) : "unknown"}</td>
        <td className="py-1.5 pr-3 font-mono text-[11px]">{o.source_slot ?? "—"}</td>
        <td className="py-1.5 text-slate-500">{o.source} · {o.adapter} · <span className="font-mono text-[10px]" title={o.id}>{o.id.slice(0, 8)}</span></td>
      </tr>;
    })}</tbody>
  </table></div>;
}

function EvidenceItem({ evidence, now }: { evidence: SecurityEvidence; now: number }) {
  return <li className="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><div className="text-sm font-semibold text-slate-100">{ruleTitle(evidence.rule_id)} <span className="ml-1 font-mono text-[11px] font-normal text-slate-500">{evidence.rule_id}</span></div><LevelBadge level={evidence.level} /></div>
    <p className="mt-2 text-sm leading-6 text-slate-300"><span className="text-slate-500">Observed: </span>{evidence.explanation}</p>
    {RULE_CONTEXT[evidence.rule_id] && <p className="mt-1 text-xs leading-5 text-slate-500"><span className="text-slate-400">Why it matters: </span>{RULE_CONTEXT[evidence.rule_id].why}</p>}
    <ObservationTable observations={evidence.observations} now={now} />
  </li>;
}

export default async function ProtocolSecurityPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;

  // Access follows the dashboard: the plan's open cards only. If the protocol
  // list or plan cannot be checked, no protocol details are shown.
  let protocols: Protocol[];
  let plan: string;
  try {
    [protocols, { plan }] = await Promise.all([fetchProtocols(), getSubscription()]);
  } catch (error) {
    unstable_rethrow(error);
    console.error("[protocol page] access check failed", error instanceof Error ? error.message : error);
    return <Shell><Notice title="Protocol data unavailable">The SolanaGuard API could not be reached, so access to this protocol cannot be checked and no details are shown. Reload to try again.</Notice></Shell>;
  }
  const protocol = protocols.find((p) => p.slug === slug);
  if (!protocol) notFound();
  if (!openProtocolSlugs(protocols, visibleProtocolCount(plan, protocols.length)).has(slug)) {
    return <Shell><h1 className="mt-4 text-2xl font-semibold text-white">{protocol.name}</h1><Notice title="Available on Pro">Security evidence for this protocol is available on the Pro plan. <Link href="/pricing" className="font-medium text-cyan-300 hover:text-cyan-200">View plans →</Link></Notice></Shell>;
  }

  const security = await fetchProtocolSecurity(slug);
  // eslint-disable-next-line react-hooks/purity -- force-dynamic page: one clock read per request
  const now = Date.now();
  const labels = protocolMetricLabels(protocol);
  const activity = <section className="mt-6 rounded-2xl border border-slate-800 bg-slate-900/40 p-5 text-sm">
    <h2 className="font-semibold text-white">Activity signal (separate from security)</h2>
    <p className="mt-2 leading-6 text-slate-400">A rule-based score from the change in TVL and transaction count between the two latest snapshots. It reacts to sudden outflows or activity spikes; it says nothing about program upgrade keys, ownership or code. LOW here means &quot;no significant change&quot;, not &quot;secure&quot;.</p>
    <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3"><div><dt className="text-xs text-slate-500">TVL</dt><dd className="mt-1 font-semibold text-slate-100">{labels.tvl}</dd></div><div><dt className="text-xs text-slate-500">Activity score</dt><dd className="mt-1 font-semibold text-slate-100">{labels.riskScore}{labels.riskLevel ? ` · ${labels.riskLevel}` : ""}</dd></div><div><dt className="text-xs text-slate-500">Sources</dt><dd className="mt-1 text-slate-300">{activitySourceText(protocol)}</dd></div></dl>
    {protocol.explanation && <p className="mt-3 text-xs text-slate-500">Latest: {protocol.explanation}</p>}
  </section>;

  if (!security.ok) {
    return <Shell><h1 className="mt-4 text-2xl font-semibold text-white">{protocol.name}</h1><Notice title="Security assessment unavailable">{security.reason === "not_ready" ? "The security registry has not been set up on this server yet." : "The security assessment could not be loaded. No security level is shown until it can be."}</Notice>{activity}</Shell>;
  }

  const data = security.data;
  const summary = summarizeSecurity(data);
  const latest = summary.latestObservationAt === null ? null : new Date(summary.latestObservationAt);
  const evaluated = parseApiTimestamp(data.evaluated_at);
  const byAddress = new Map<string, SecurityEvidence[]>();
  for (const e of sortEvidence(data.evidence)) {
    const key = e.address_id ?? "protocol";
    byAddress.set(key, [...(byAddress.get(key) ?? []), e]);
  }

  return <Shell>
    <header className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div><h1 className="text-2xl font-semibold text-white sm:text-3xl">{data.protocol.name}</h1><div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-400"><span>{data.protocol.category.replace("_", " ")}</span><SupportBadge supportLevel={data.protocol.support_level} /></div></div>
      <div className="flex flex-col items-start gap-1 sm:items-end"><LevelBadge level={data.dimension.level} prefix="Security" /><span className="text-xs text-slate-500" title={formatAbsoluteUtc(evaluated)}>Evaluated {evaluated ? formatRelative(evaluated, now) : "—"} · {data.dimension.rule_version}</span></div>
    </header>

    <section className="mt-6 rounded-2xl border border-slate-800 bg-slate-900/40 p-5">
      <h2 className="font-semibold text-white">Assessment</h2>
      <p className="mt-2 text-sm leading-6 text-slate-300">{data.dimension.explanation}</p>
      <dl className="mt-4 grid grid-cols-1 gap-3 text-sm sm:grid-cols-3">
        <div><dt className="text-xs text-slate-500">Coverage</dt><dd className="mt-1 text-slate-200">{summary.coverageText}</dd></div>
        <div><dt className="text-xs text-slate-500">Latest observation</dt><dd className="mt-1 text-slate-200" title={formatAbsoluteUtc(latest)}>{latest ? formatRelative(latest, now) : "None collected yet"}</dd></div>
        <div><dt className="text-xs text-slate-500">Source</dt><dd className="mt-1 text-slate-200">{summary.sources.length ? `${summary.sources.join(", ")} (${summary.adapters.join(", ")})` : "—"}</dd></div>
      </dl>
      <p className="mt-4 text-xs leading-5 text-slate-500">Levels are not weighted scores. Any HIGH or CRITICAL finding sets the level; otherwise any indicator that cannot be verified makes the level UNKNOWN, so missing, stale or failed readings never produce LOW. Upgradeable programs are MEDIUM until the authority&apos;s controls (multisig, timelock, governance) can be verified.</p>
      {summary.unknown.length > 0 && <div className="mt-4"><h3 className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">What remains unknown</h3><ul className="mt-2 space-y-1 text-sm text-slate-400">{summary.unknown.map((e, i) => <li key={`${e.rule_id}-${e.address_id}-${i}`}>• <span className="text-slate-300">{ruleTitle(e.rule_id)}</span>{e.address_label ? ` on ${e.address_label}` : ""}: {e.explanation.replace(/^Could not verify: /, "")}</li>)}</ul></div>}
      {data.protocol.support_level !== "FULL" && <p className="mt-4 text-xs leading-5 text-slate-500">Partial coverage: SolanaGuard currently checks on-chain program and account security only. Protocol-specific metrics (market reserves, pool liquidity, collateral health) are not collected for this protocol.</p>}
    </section>

    <section className="mt-6"><h2 className="font-semibold text-white">Evidence by monitored address</h2>
      {data.addresses.length === 0 ? <p className="mt-3 text-sm text-slate-500">No active addresses are registered for this protocol.</p> :
      <div className="mt-3 space-y-5">{data.addresses.map((a) => <article key={a.id} className="rounded-2xl border border-slate-800 bg-slate-900/40 p-5">
        <div className="flex flex-wrap items-start justify-between gap-2"><div><h3 className="font-semibold text-slate-100">{a.label}</h3><div className="mt-1 text-xs text-slate-500">{KIND_TEXT[a.kind]}{a.is_shared_dependency && <span className="ml-2 rounded-full border border-sky-400/30 px-2 py-px text-sky-300" title="A program shared by many protocols; it is not operated by this protocol. Its upgrade authority can change code this protocol depends on.">Shared dependency — not operated by {data.protocol.name}</span>}</div></div>
          <a href={explorerUrl(a.address)} target="_blank" rel="noreferrer" className="font-mono text-xs text-cyan-300 hover:text-cyan-200" title={a.address}>{shortKey(a.address)} ↗</a></div>
        <p className="mt-2 text-xs text-slate-500">Registry: {a.verification}{a.verified_at ? ` ${formatAbsoluteUtc(parseApiTimestamp(a.verified_at))}` : ""}{a.expected_owner ? ` · expected owner ${shortKey(a.expected_owner)}` : ""}</p>
        <ul className="mt-4 space-y-3">{(byAddress.get(a.id) ?? []).map((e, i) => <EvidenceItem key={`${e.rule_id}-${i}`} evidence={e} now={now} />)}</ul>
      </article>)}</div>}
    </section>

    {activity}
  </Shell>;
}
