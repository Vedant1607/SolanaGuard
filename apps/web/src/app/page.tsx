import { Suspense } from "react";
import Link from "next/link";
import { unstable_rethrow } from "next/navigation";
import { auth } from "@clerk/nextjs/server";
import { fetchCollectorStatus, fetchHistoryRefreshStatus, fetchProtocolIntelligence, fetchProtocolSecurity, fetchProtocols, type CollectorStatus, type IntelligenceResult, type Protocol, type ProtocolCategory, type RiskLevel, type SecurityLevel, type SecurityResult } from "@/lib/api";
import { getWatchedSlugs } from "@/lib/get-watched-slugs";
import { WatchlistButton } from "@/components/WatchlistButton";
import { TelegramConnect } from "@/components/TelegramConnect";
import { normalizeBotUsername } from "@/lib/telegram-link-core";
import { getAppUser } from "@/lib/get-app-user";
import { getSubscription } from "@/lib/get-subscription";
import { visibleProtocolCount } from "@/lib/plan-limits";
import { DATA_UNAVAILABLE_LABEL, UNAVAILABLE_LABEL, openProtocolSlugs, protocolMetricLabels, sortByDataAvailability } from "@/lib/protocol-availability";
import { summarizeSecurity } from "@/lib/security-view";
import { LevelBadge, SupportBadge } from "@/components/SecurityBadges";
import { IntelligenceCardSection } from "@/components/Intelligence";
import { RecentDetections, RecentDetectionsLoading } from "@/components/RecentDetections";
import { assessFreshness, type DataNotice } from "@/lib/freshness";
import { formatAbsoluteUtc, formatRelative, parseApiTimestamp } from "@/lib/time";
import { describeHistory, type HistoryNotice } from "@/lib/history-view";

export const dynamic = "force-dynamic";

const RISK_STYLES: Record<RiskLevel, string> = {
  LOW: "border-emerald-400/30 bg-emerald-400/10 text-emerald-300",
  MEDIUM: "border-amber-400/30 bg-amber-400/10 text-amber-300",
  HIGH: "border-orange-400/30 bg-orange-400/10 text-orange-300",
  CRITICAL: "border-red-400/30 bg-red-400/10 text-red-300",
};

const CATEGORY_STYLES: Record<ProtocolCategory, string> = {
  DEX: "text-cyan-300",
  LENDING: "text-amber-300",
  LIQUID_STAKING: "text-violet-300",
  PERPS: "text-rose-300",
};

const NOTICE_STYLES: Record<DataNotice["tone"], string> = {
  warning: "border-amber-400/25 bg-amber-400/[0.06] text-amber-200",
  info: "border-slate-700 bg-slate-800/40 text-slate-400",
};

function formatCount(value: number): string {
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

/** Resolve a best-effort loader to a fallback so one failing dependency cannot crash the page. */
async function orFallback<T>(label: string, load: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await load();
  } catch (error) {
    unstable_rethrow(error); // let Next.js dynamic-rendering/redirect signals through
    console.error(`[dashboard] ${label} failed`, error instanceof Error ? error.message : error);
    return fallback;
  }
}

function LockedProtocolCard({ name, category }: { name: string; category: ProtocolCategory }) {
  return <div className="relative overflow-hidden rounded-2xl border border-slate-800 bg-slate-900/30 p-5">
    <div className="pointer-events-none select-none opacity-45 blur-[2px]"><div className="font-semibold text-slate-100">{name}</div><div className={`mt-1 text-xs ${CATEGORY_STYLES[category]}`}>{category.replace("_", " ")}</div></div>
    <div className="absolute inset-0 flex flex-col items-center justify-center bg-slate-950/75 p-5 text-center"><span className="text-xs font-medium text-slate-300">Protocol details are available on Pro</span><a href="/pricing" className="mt-2 text-xs font-semibold text-cyan-300 hover:text-cyan-200">View plans →</a></div>
  </div>;
}

const SECURITY_UNAVAILABLE_TEXT: Record<Exclude<SecurityResult, { ok: true }>["reason"], string> = {
  not_found: "No security record exists for this protocol.",
  not_ready: "Security data is not available yet: the security registry has not been set up on this server.",
  unavailable: "The security assessment could not be loaded. No security level is shown until it can be.",
};

function SecuritySection({ slug, security, now }: { slug: string; security: SecurityResult; now: number }) {
  if (!security.ok) {
    return <div className="mt-4 rounded-lg border border-slate-700 bg-slate-800/40 px-3 py-2 text-xs leading-5 text-slate-400">{SECURITY_UNAVAILABLE_TEXT[security.reason]}</div>;
  }
  const summary = summarizeSecurity(security.data);
  const latest = summary.latestObservationAt === null ? null : new Date(summary.latestObservationAt);
  return <div className="mt-4 rounded-xl border border-slate-800 bg-slate-950/40 p-3 text-xs">
    <div className="flex flex-wrap items-center justify-between gap-2"><span className="font-semibold uppercase tracking-[0.14em] text-slate-500">On-chain security</span><SupportBadge supportLevel={security.data.protocol.support_level} /></div>
    <p className="mt-2 leading-5 text-slate-400">{security.data.dimension.explanation}</p>
    <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-slate-500"><span>{summary.coverageText}</span>{summary.unknown.length > 0 && <span className="text-slate-400">{summary.unknown.length} unknown</span>}{summary.failedObservations.length > 0 && <span className="text-amber-300">{summary.failedObservations.length} failed reading(s)</span>}</div>
    <div className="mt-1 text-slate-600" title={formatAbsoluteUtc(latest)}>{latest ? `Observed ${formatRelative(latest, now)} via ${summary.sources.join(", ")} (${summary.adapters.join(", ")})` : "No on-chain observations collected yet"}</div>
    <Link href={`/protocols/${encodeURIComponent(slug)}`} className="mt-2 inline-block font-semibold text-cyan-300 hover:text-cyan-200">View security evidence →</Link>
  </div>;
}

/** Plain-language warnings for the collector card; an explicitly disabled collector is never reported as a missing RPC endpoint. */
function collectorNotice(status: CollectorStatus, notFresh: number, failing: number): string[] {
  const notices: string[] = [];
  const { state, last_outcome: outcome, last_report: report, consecutive_failures: failures } = status.instance;
  if (state === "disabled") notices.push("Security collection is disabled on this server (SECURITY_COLLECTOR_ENABLED=false). Assessments use the last stored readings and turn UNKNOWN as they go stale.");
  else if (state === "no_rpc_endpoint") notices.push("No RPC endpoint is configured; security checks are idle.");
  else if (outcome === "failed") notices.push(`The last collection run failed for every protocol${failures > 1 ? ` (${failures} runs in a row)` : ""}: ${(report?.failed_protocols ?? []).join(", ") || "unknown"}.`);
  else if (outcome === "error") notices.push(`The last collection run stopped with an error${failures > 1 ? ` (${failures} runs in a row)` : ""}.`);
  else if (outcome === "partial") notices.push(`The last collection run failed for: ${(report?.failed_protocols ?? []).join(", ")}.`);
  if (notFresh > 0) notices.push(`${notFresh} protocol(s) have no fresh reading.`);
  if (failing > 0) notices.push(`${failing} protocol(s) had failed readings in their latest batch.`);
  return notices;
}

function SecurityOverview({ status, securityBySlug, now }: { status: CollectorStatus | null; securityBySlug: Map<string, SecurityResult>; now: number }) {
  const levels = new Map<string, number>();
  for (const result of securityBySlug.values()) {
    const level = result.ok ? result.data.dimension.level : "Unavailable";
    levels.set(level, (levels.get(level) ?? 0) + 1);
  }
  const active = status?.protocols?.filter((p) => p.is_active) ?? [];
  const lastCollected = active.map((p) => parseApiTimestamp(p.last_collected_at)).filter((d): d is Date => d !== null).sort((a, b) => b.getTime() - a.getTime())[0] ?? null;
  const notFresh = active.filter((p) => !p.fresh).length;
  const failing = active.filter((p) => p.latest_batch.error + p.latest_batch.missing > 0).length;
  const next = parseApiTimestamp(status?.instance.next_run_at);
  return <div className="rounded-2xl border border-slate-800 bg-slate-900/40 p-5">
    <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">On-chain security checks</div>
    <div className="mt-3 flex flex-wrap gap-2">{levels.size === 0 ? <span className="text-sm text-slate-500">No open protocols</span> : [...levels.entries()].map(([level, count]) => <span key={level} className="text-sm text-slate-300">{level === "Unavailable" ? <span className="text-slate-400">Unavailable</span> : <LevelBadge level={level as SecurityLevel} />} <span className="ml-0.5 font-semibold text-white">×{count}</span></span>)}</div>
    {status === null ? <p className="mt-2 text-sm text-amber-300">Collector status could not be loaded.</p> : <>
      <p className="mt-2 text-sm text-slate-500" title={formatAbsoluteUtc(lastCollected)}>{lastCollected ? `Last mainnet collection ${formatRelative(lastCollected, now)}` : "No collection recorded yet"}{status.instance.state !== "disabled" && status.instance.rpc_host ? ` via ${status.instance.rpc_host}` : ""}{status.instance.state !== "disabled" && status.instance.public_fallback ? " (public endpoint — development only)" : ""}{status.instance.state === "active" && next && next.getTime() > now ? `; next in ~${Math.max(1, Math.round((next.getTime() - now) / 60_000))} min` : ""}.</p>
      {collectorNotice(status, notFresh, failing).map((text) => <p key={text} className="mt-2 text-xs text-amber-300">{text}</p>)}
    </>}
  </div>;
}

const HISTORY_TONE: Record<HistoryNotice["tone"], string> = {
  ok: "text-slate-400",
  warning: "text-amber-300",
  info: "text-slate-500",
};

/** Daily-history freshness: the stored closed day and the last refresh run, kept separate. */
function HistoryFreshness({ view }: { view: ReturnType<typeof describeHistory> }) {
  return <div className="mt-4 rounded-xl border border-slate-800 bg-slate-900/30 px-4 py-3 text-xs leading-5" aria-label="Daily history freshness">
    <p className={HISTORY_TONE[view.availability.tone]}><span className="font-semibold uppercase tracking-[0.14em] text-slate-500">Daily history</span> <span className="ml-1">{view.availability.text}</span></p>
    <p className={`mt-0.5 ${HISTORY_TONE[view.refresh.tone]}`}>{view.refresh.text}{view.lagging.length > 0 && <span className="text-amber-300"> Behind: {view.lagging.join(", ")}.</span>}</p>
  </div>;
}

function ProtocolCard({ protocol, security, intelligence, isAuthenticated, watched, now }: { protocol: Protocol; security: SecurityResult; intelligence: IntelligenceResult; isAuthenticated: boolean; watched: boolean; now: number }) {
  const freshness = assessFreshness(protocol, now);
  const labels = protocolMetricLabels(protocol);
  return <article className="rounded-2xl border border-slate-800 bg-slate-900/45 p-5 transition-colors hover:border-slate-700">
    <div className="flex items-start justify-between gap-4"><div className="min-w-0"><h3 className="truncate font-semibold text-slate-100">{protocol.name}</h3><div className={`mt-1 text-xs font-medium ${CATEGORY_STYLES[protocol.category]}`}>{protocol.category.replace("_", " ")}</div></div><div className="flex shrink-0 flex-col items-end gap-2">{security.ok ? <span title="Evidence-based on-chain security assessment. UNKNOWN means some indicators could not be verified; it never defaults to LOW."><LevelBadge level={security.data.dimension.level} prefix="Security" /></span> : <span className="rounded border border-slate-700 bg-slate-800/40 px-2 py-0.5 text-[11px] font-medium text-slate-400">Security unavailable</span>}{labels.dataUnavailable && <span className="rounded border border-slate-700 bg-slate-800/40 px-2 py-0.5 text-[11px] font-medium text-slate-400" title="SolanaGuard has no TVL or activity score for this protocol yet. Missing data is not treated as low risk.">{DATA_UNAVAILABLE_LABEL}</span>}{isAuthenticated && <WatchlistButton slug={protocol.slug} initialWatched={watched} />}</div></div>
    <div className="mt-5 grid grid-cols-3 gap-3 border-y border-slate-800/80 py-4 text-sm">
      <div><div className="text-xs text-slate-500">TVL</div>{labels.tvl === UNAVAILABLE_LABEL ? <div className="mt-1 font-medium text-slate-500" title="No TVL has been recorded for this protocol. It is not zero.">{UNAVAILABLE_LABEL}</div> : <div className="mt-1 font-semibold text-slate-100">{labels.tvl}</div>}</div>
      <div><div className="text-xs text-slate-500" title="Rule-based score from the change in TVL and transaction count between the two latest snapshots. It is not a security assessment.">Activity signal</div>{labels.riskScore === UNAVAILABLE_LABEL ? <div className="mt-1 font-medium text-slate-500" title="No activity score has been computed for this protocol. Missing data is not treated as low risk.">{UNAVAILABLE_LABEL}</div> : <div className="mt-1 flex flex-wrap items-center gap-1.5 font-semibold text-slate-100">{labels.riskScore}{labels.riskLevel && <span className={`rounded border px-1.5 py-px text-[10px] font-bold ${RISK_STYLES[labels.riskLevel]}`}>{labels.riskLevel}</span>}</div>}</div>
      <div><div className="text-xs text-slate-500">24h txns</div>{protocol.txCount24h === null ? <div className="mt-1 font-medium text-slate-500" title="The on-chain transaction count could not be retrieved for this snapshot. It is not zero.">Unavailable</div> : <div className="mt-1 font-semibold text-slate-100">{formatCount(protocol.txCount24h)}</div>}</div>
    </div>
    {protocol.explanation && <p className="mt-4 text-sm leading-6 text-slate-400"><span className="text-slate-500">Activity: </span>{protocol.explanation}</p>}
    <SecuritySection slug={protocol.slug} security={security} now={now} />
    <IntelligenceCardSection result={intelligence} now={now} />
    {freshness.notices.length > 0 && <ul className="mt-4 space-y-2">{freshness.notices.map((notice) => <li key={notice.text} className={`rounded-lg border px-3 py-2 text-xs leading-5 ${NOTICE_STYLES[notice.tone]}`}>{notice.text}</li>)}</ul>}
    <div className="mt-4 space-y-1 text-xs text-slate-600">
      <div title={formatAbsoluteUtc(freshness.snapshotAt)}>Telemetry updated {freshness.snapshotAt ? formatRelative(freshness.snapshotAt, now) : "— never"}</div>
      {freshness.scoredAt && <div title={formatAbsoluteUtc(freshness.scoredAt)}>Activity scored {formatRelative(freshness.scoredAt, now)}</div>}
    </div>
  </article>;
}

export default async function DashboardPage() {
  const [protocolsResult, watchedSlugs, { isAuthenticated }, appUser, { plan }] = await Promise.all([
    fetchProtocols().then(
      (data) => ({ ok: true as const, data }),
      (error: unknown) => {
        unstable_rethrow(error);
        console.error("[dashboard] Protocol API unavailable", error instanceof Error ? error.message : error);
        return { ok: false as const, data: [] as Protocol[] };
      },
    ),
    orFallback("watchlist lookup", getWatchedSlugs, new Set<string>()),
    auth(),
    orFallback("user lookup", getAppUser, null),
    orFallback("subscription lookup", getSubscription, { plan: "FREE" as const }),
  ]);
  // Data-backed protocols first so a limited plan's open slots go to protocols
  // with something to show; the API's alphabetical order breaks ties.
  const protocols = sortByDataAvailability(protocolsResult.data);
  // Request time for freshness labels; the page is force-dynamic, so reading
  // the clock once per server render is intended.
  // eslint-disable-next-line react-hooks/purity
  const now = Date.now();
  const visibleCount = visibleProtocolCount(plan, protocols.length);
  const isLimited = visibleCount < protocols.length;
  const openSlugs = openProtocolSlugs(protocols, visibleCount);
  const collectorStatusPromise = fetchCollectorStatus();
  const historyStatusPromise = fetchHistoryRefreshStatus();
  // One request per open card for each assessment, all in parallel; locked
  // (Pro-only) cards fetch nothing. Intelligence omits history on the cards.
  const openProtocols = protocols.filter((p) => openSlugs.has(p.slug));
  const [securityEntries, intelligenceEntries] = await Promise.all([
    Promise.all(openProtocols.map(async (p) => [p.slug, await fetchProtocolSecurity(p.slug)] as const)),
    Promise.all(openProtocols.map(async (p) => [p.slug, await fetchProtocolIntelligence(p.slug)] as const)),
  ]);
  const securityBySlug = new Map(securityEntries);
  const intelligenceBySlug = new Map(intelligenceEntries);
  const elevated = protocols.filter((protocol) => protocol.riskLevel === "HIGH" || protocol.riskLevel === "CRITICAL");
  const elevatedOnStaleBaseline = elevated.filter((protocol) => assessFreshness(protocol, now).staleBaseline).length;
  const latestSnapshot = protocols.map((protocol) => assessFreshness(protocol, now).snapshotAt).filter((date): date is Date => date !== null).sort((a, b) => b.getTime() - a.getTime())[0] ?? null;
  const collectorStatus = await collectorStatusPromise;
  const historyView = describeHistory(await historyStatusPromise, now);
  // Links are single-use tokens issued on click (TelegramConnect); no identifier is put in the page.
  const telegramConfigured = normalizeBotUsername(process.env.NEXT_PUBLIC_TELEGRAM_BOT_USERNAME) !== null;

  return <main className="min-h-screen bg-slate-950 text-slate-200">
    <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <section className="relative overflow-hidden rounded-2xl border border-slate-800 bg-slate-900/45 px-5 py-7 shadow-2xl shadow-slate-950/30 sm:px-8">
        <div className="absolute -right-24 -top-32 h-72 w-72 rounded-full bg-cyan-400/10 blur-3xl" />
        <div className="relative flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
          <div className="max-w-2xl"><div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-cyan-300"><span className="h-2 w-2 rounded-full bg-cyan-300" /> Security monitoring</div><h1 className="mt-3 text-3xl font-semibold tracking-tight text-white sm:text-4xl">SolanaGuard command center</h1><p className="mt-3 text-sm leading-6 text-slate-400 sm:text-base">Protocol risk telemetry and persisted anomaly detections for the Solana ecosystem.</p></div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">{isAuthenticated && (appUser?.telegramChatId ? <span className="rounded-full border border-emerald-400/25 bg-emerald-400/10 px-3 py-1.5 font-medium text-emerald-300">Telegram connected</span> : telegramConfigured && appUser && <TelegramConnect />)}<a href="/pricing" className="rounded-full border border-slate-700 px-3 py-1.5 font-medium text-slate-300 hover:border-cyan-400/40 hover:text-cyan-200">View plans</a></div>
        </div>
      </section>

      {!protocolsResult.ok && <div role="alert" className="mt-8 rounded-2xl border border-amber-500/25 bg-amber-500/[0.04] p-5 sm:p-6"><p className="text-xs font-semibold uppercase tracking-[0.16em] text-amber-300">Protocol telemetry unavailable</p><p className="mt-2 max-w-2xl text-sm leading-6 text-slate-400">The SolanaGuard API could not be reached, so protocol data is not shown. No risk levels are being reported until the service responds — reload to try again.</p></div>}

      {protocolsResult.ok && <>
      <section aria-labelledby="overview" className="mt-8"><div className="flex items-end justify-between gap-4"><div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Overview</p><h2 id="overview" className="mt-1 text-xl font-semibold text-white">Current risk coverage</h2></div><span className="text-right text-xs text-slate-500" title={formatAbsoluteUtc(latestSnapshot)}>Latest telemetry {latestSnapshot ? formatRelative(latestSnapshot, now) : "— none recorded"}</span></div><div className="mt-4 grid gap-4 md:grid-cols-3"><div className="rounded-2xl border border-slate-800 bg-slate-900/40 p-5"><div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">Monitored protocols</div><div className="mt-3 text-3xl font-semibold text-white">{protocols.length.toLocaleString()}</div><p className="mt-2 text-sm text-slate-500">Protocols with current dashboard telemetry.</p></div><div className="rounded-2xl border border-slate-800 bg-slate-900/40 p-5"><div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">Elevated activity signal</div><div className="mt-3 text-3xl font-semibold text-white">{elevated.length.toLocaleString()}</div><p className="mt-2 text-sm text-slate-500">Protocols whose latest TVL or transaction change is classified High or Critical by the rule-based activity score. This is separate from the on-chain security assessment and from anomaly scores.</p>{elevatedOnStaleBaseline > 0 && <p className="mt-2 text-xs text-amber-300">{elevatedOnStaleBaseline} of these compare against a stale baseline — low confidence.</p>}</div><SecurityOverview status={collectorStatus} securityBySlug={securityBySlug} now={now} /></div><HistoryFreshness view={historyView} /></section>

      {isLimited && <div className="mt-6 rounded-xl border border-cyan-400/15 bg-cyan-400/[0.04] px-4 py-3 text-sm text-slate-400">Showing {visibleCount} of {protocols.length} protocols on the Free plan — <a href="/pricing" className="font-medium text-cyan-300 hover:text-cyan-200">upgrade to Pro</a> to access all protocol cards.</div>}

      <section aria-labelledby="protocols" className="mt-8"><div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Protocol telemetry</p><h2 id="protocols" className="mt-1 text-xl font-semibold text-white">Monitored protocols</h2></div>{protocols.length === 0 ? <div className="mt-4 rounded-xl border border-dashed border-slate-700 bg-slate-950/30 px-5 py-8 text-center text-sm text-slate-500">No active protocols are configured.</div> : <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">{protocols.map((protocol) => openSlugs.has(protocol.slug) ? <ProtocolCard key={protocol.slug} protocol={protocol} security={securityBySlug.get(protocol.slug) ?? { ok: false, reason: "unavailable" }} intelligence={intelligenceBySlug.get(protocol.slug) ?? { ok: false, reason: "unavailable" }} isAuthenticated={isAuthenticated} watched={watchedSlugs.has(protocol.slug)} now={now} /> : <LockedProtocolCard key={protocol.slug} name={protocol.name} category={protocol.category} />)}</div>}</section>
      </>}
      <div className="mt-8 pb-6"><Suspense fallback={<RecentDetectionsLoading />}><RecentDetections /></Suspense></div>
    </div>
  </main>;
}
