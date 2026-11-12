import { Suspense } from "react";
import { unstable_rethrow } from "next/navigation";
import { auth } from "@clerk/nextjs/server";
import { fetchProtocols, type Protocol, type ProtocolCategory, type RiskLevel } from "@/lib/api";
import { getWatchedSlugs } from "@/lib/get-watched-slugs";
import { WatchlistButton } from "@/components/WatchlistButton";
import { getAppUser } from "@/lib/get-app-user";
import { getSubscription } from "@/lib/get-subscription";
import { FREE_PROTOCOL_LIMIT } from "@/lib/plan-limits";
import { RecentDetections, RecentDetectionsLoading } from "@/components/RecentDetections";
import { assessFreshness, type DataNotice } from "@/lib/freshness";
import { formatAbsoluteUtc, formatRelative } from "@/lib/time";

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

function formatUsd(value: number | null): string {
  if (value === null) return "—";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 }).format(value);
}

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

function ProtocolCard({ protocol, isAuthenticated, watched, now }: { protocol: Protocol; isAuthenticated: boolean; watched: boolean; now: number }) {
  const freshness = assessFreshness(protocol, now);
  return <article className="rounded-2xl border border-slate-800 bg-slate-900/45 p-5 transition-colors hover:border-slate-700">
    <div className="flex items-start justify-between gap-4"><div className="min-w-0"><h3 className="truncate font-semibold text-slate-100">{protocol.name}</h3><div className={`mt-1 text-xs font-medium ${CATEGORY_STYLES[protocol.category]}`}>{protocol.category.replace("_", " ")}</div></div><div className="flex shrink-0 flex-col items-end gap-2">{protocol.riskLevel && <span className={`rounded border px-2 py-0.5 text-[11px] font-bold tracking-wide ${RISK_STYLES[protocol.riskLevel]}`}>{protocol.riskLevel}</span>}{isAuthenticated && <WatchlistButton slug={protocol.slug} initialWatched={watched} />}</div></div>
    <div className="mt-5 grid grid-cols-3 gap-3 border-y border-slate-800/80 py-4 text-sm">
      <div><div className="text-xs text-slate-500">TVL</div><div className="mt-1 font-semibold text-slate-100">{formatUsd(protocol.tvlUsd)}</div></div>
      <div><div className="text-xs text-slate-500">Risk score</div><div className="mt-1 font-semibold text-slate-100">{protocol.overallScore !== null ? `${protocol.overallScore}/100` : "—"}</div></div>
      <div><div className="text-xs text-slate-500">24h txns</div>{protocol.txCount24h === null ? <div className="mt-1 font-medium text-slate-500" title="The on-chain transaction count could not be retrieved for this snapshot. It is not zero.">Unavailable</div> : <div className="mt-1 font-semibold text-slate-100">{formatCount(protocol.txCount24h)}</div>}</div>
    </div>
    {protocol.explanation && <p className="mt-4 text-sm leading-6 text-slate-400">{protocol.explanation}</p>}
    {freshness.notices.length > 0 && <ul className="mt-4 space-y-2">{freshness.notices.map((notice) => <li key={notice.text} className={`rounded-lg border px-3 py-2 text-xs leading-5 ${NOTICE_STYLES[notice.tone]}`}>{notice.text}</li>)}</ul>}
    <div className="mt-4 space-y-1 text-xs text-slate-600">
      <div title={formatAbsoluteUtc(freshness.snapshotAt)}>Telemetry updated {freshness.snapshotAt ? formatRelative(freshness.snapshotAt, now) : "— never"}</div>
      {freshness.scoredAt && <div title={formatAbsoluteUtc(freshness.scoredAt)}>Risk scored {formatRelative(freshness.scoredAt, now)}</div>}
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
  const protocols = protocolsResult.data;
  // Request time for freshness labels; the page is force-dynamic, so reading
  // the clock once per server render is intended.
  // eslint-disable-next-line react-hooks/purity
  const now = Date.now();
  const visibleCount = plan === "PRO" ? protocols.length : FREE_PROTOCOL_LIMIT;
  const isLimited = visibleCount < protocols.length;
  const elevated = protocols.filter((protocol) => protocol.riskLevel === "HIGH" || protocol.riskLevel === "CRITICAL");
  const elevatedOnStaleBaseline = elevated.filter((protocol) => assessFreshness(protocol, now).staleBaseline).length;
  const latestSnapshot = protocols.map((protocol) => assessFreshness(protocol, now).snapshotAt).filter((date): date is Date => date !== null).sort((a, b) => b.getTime() - a.getTime())[0] ?? null;
  const telegramBot = process.env.NEXT_PUBLIC_TELEGRAM_BOT_USERNAME;
  const telegramLink = telegramBot && appUser?.id ? `https://t.me/${encodeURIComponent(telegramBot)}?start=${encodeURIComponent(appUser.id)}` : null;

  return <main className="min-h-screen bg-slate-950 text-slate-200">
    <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <section className="relative overflow-hidden rounded-2xl border border-slate-800 bg-slate-900/45 px-5 py-7 shadow-2xl shadow-slate-950/30 sm:px-8">
        <div className="absolute -right-24 -top-32 h-72 w-72 rounded-full bg-cyan-400/10 blur-3xl" />
        <div className="relative flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
          <div className="max-w-2xl"><div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-cyan-300"><span className="h-2 w-2 rounded-full bg-cyan-300" /> Security monitoring</div><h1 className="mt-3 text-3xl font-semibold tracking-tight text-white sm:text-4xl">SolanaGuard command center</h1><p className="mt-3 text-sm leading-6 text-slate-400 sm:text-base">Protocol risk telemetry and persisted anomaly detections for the Solana ecosystem.</p></div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">{isAuthenticated && (appUser?.telegramChatId ? <span className="rounded-full border border-emerald-400/25 bg-emerald-400/10 px-3 py-1.5 font-medium text-emerald-300">Telegram connected</span> : telegramLink && <a href={telegramLink} target="_blank" rel="noreferrer" className="font-medium text-cyan-300 hover:text-cyan-200">Connect Telegram →</a>)}<a href="/pricing" className="rounded-full border border-slate-700 px-3 py-1.5 font-medium text-slate-300 hover:border-cyan-400/40 hover:text-cyan-200">View plans</a></div>
        </div>
      </section>

      {!protocolsResult.ok && <div role="alert" className="mt-8 rounded-2xl border border-amber-500/25 bg-amber-500/[0.04] p-5 sm:p-6"><p className="text-xs font-semibold uppercase tracking-[0.16em] text-amber-300">Protocol telemetry unavailable</p><p className="mt-2 max-w-2xl text-sm leading-6 text-slate-400">The SolanaGuard API could not be reached, so protocol data is not shown. No risk levels are being reported until the service responds — reload to try again.</p></div>}

      {protocolsResult.ok && <>
      <section aria-labelledby="overview" className="mt-8"><div className="flex items-end justify-between gap-4"><div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Overview</p><h2 id="overview" className="mt-1 text-xl font-semibold text-white">Current risk coverage</h2></div><span className="text-right text-xs text-slate-500" title={formatAbsoluteUtc(latestSnapshot)}>Latest telemetry {latestSnapshot ? formatRelative(latestSnapshot, now) : "— none recorded"}</span></div><div className="mt-4 grid gap-4 sm:grid-cols-2"><div className="rounded-2xl border border-slate-800 bg-slate-900/40 p-5"><div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">Monitored protocols</div><div className="mt-3 text-3xl font-semibold text-white">{protocols.length.toLocaleString()}</div><p className="mt-2 text-sm text-slate-500">Protocols with current dashboard telemetry.</p></div><div className="rounded-2xl border border-slate-800 bg-slate-900/40 p-5"><div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">Elevated current risk</div><div className="mt-3 text-3xl font-semibold text-white">{elevated.length.toLocaleString()}</div><p className="mt-2 text-sm text-slate-500">Protocols currently classified High or Critical by rule-based risk scoring. Risk classifications are separate from anomaly scores.</p>{elevatedOnStaleBaseline > 0 && <p className="mt-2 text-xs text-amber-300">{elevatedOnStaleBaseline} of these compare against a stale baseline — low confidence.</p>}</div></div></section>

      {isLimited && <div className="mt-6 rounded-xl border border-cyan-400/15 bg-cyan-400/[0.04] px-4 py-3 text-sm text-slate-400">Showing {visibleCount} of {protocols.length} protocols on the Free plan — <a href="/pricing" className="font-medium text-cyan-300 hover:text-cyan-200">upgrade to Pro</a> to access all protocol cards.</div>}

      <section aria-labelledby="protocols" className="mt-8"><div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Protocol telemetry</p><h2 id="protocols" className="mt-1 text-xl font-semibold text-white">Monitored protocols</h2></div>{protocols.length === 0 ? <div className="mt-4 rounded-xl border border-dashed border-slate-700 bg-slate-950/30 px-5 py-8 text-center text-sm text-slate-500">No active protocols are configured.</div> : <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">{protocols.map((protocol, index) => index < visibleCount ? <ProtocolCard key={protocol.slug} protocol={protocol} isAuthenticated={isAuthenticated} watched={watchedSlugs.has(protocol.slug)} now={now} /> : <LockedProtocolCard key={protocol.slug} name={protocol.name} category={protocol.category} />)}</div>}</section>
      </>}
      <div className="mt-8 pb-6"><Suspense fallback={<RecentDetectionsLoading />}><RecentDetections /></Suspense></div>
    </div>
  </main>;
}
