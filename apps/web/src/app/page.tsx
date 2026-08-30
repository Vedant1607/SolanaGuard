import { auth } from "@clerk/nextjs/server";
import { fetchProtocols, type RiskLevel, type ProtocolCategory } from "@/lib/api";
import { getWatchedSlugs } from "@/lib/get-watched-slugs";
import { WatchlistButton } from "@/components/WatchlistButton";
import { getAppUser } from "@/lib/get-app-user";
import { getSubscription } from "@/lib/get-subscription";
import { FREE_PROTOCOL_LIMIT } from "@/lib/plan-limits";

export const dynamic = "force-dynamic";

const RISK_STYLES: Record<RiskLevel, string> = {
  LOW: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
  MEDIUM: "bg-amber-500/10 text-amber-400 border-amber-500/30",
  HIGH: "bg-orange-500/10 text-orange-400 border-orange-500/30",
  CRITICAL: "bg-red-500/10 text-red-400 border-red-500/30",
};

const CATEGORY_STYLES: Record<ProtocolCategory, string> = {
  DEX: "text-cyan-400",
  LENDING: "text-amber-400",
  LIQUID_STAKING: "text-purple-400",
};

function formatUsd(value: number | null): string {
  if (value === null) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
}

function LockedProtocolCard({ name, category }: { name: string; category: ProtocolCategory }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/30 p-5 relative overflow-hidden">
      <div className="blur-sm select-none pointer-events-none">
        <div className="font-bold text-slate-100">{name}</div>
        <div className={`text-xs mt-0.5 ${CATEGORY_STYLES[category]}`}>{category.replace("_", " ")}</div>
        <div className="grid grid-cols-2 gap-3 text-sm mt-3">
          <div>
            <div className="text-slate-500 text-xs">TVL</div>
            <div className="font-semibold">$XXX</div>
          </div>
          <div>
            <div className="text-slate-500 text-xs">Risk Score</div>
            <div className="font-semibold">XX/100</div>
          </div>
        </div>
      </div>
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-slate-950/70">
        <span className="text-xs text-slate-400">🔒 Pro only</span>
        <a href="/pricing" className="text-xs text-cyan-400 underline">Upgrade to unlock</a>
      </div>
    </div>
  );
}

export default async function DashboardPage() {
  const [protocols, watchedSlugs, { isAuthenticated }, appUser, { plan }] = await Promise.all([
    fetchProtocols(),
    getWatchedSlugs(),
    auth(),
    getAppUser(),
    getSubscription(),
  ]);

  const visibleCount = plan === "PRO" ? protocols.length : FREE_PROTOCOL_LIMIT;
  const isLimited = visibleCount < protocols.length;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-200 font-mono">
      <header className="border-b border-slate-800 px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-2 font-bold text-lg">
          <span className="text-cyan-400">Solana</span>Guard
        </div>
        <div className="flex items-center gap-3">
          <a href="/pricing" className="text-xs text-slate-400 hover:text-slate-200">Pricing</a>
          {isAuthenticated && (
            appUser?.telegramChatId ? (
              <span className="text-xs text-emerald-400">✓ Telegram connected</span>
            ) : (
              <a
                href={`https://t.me/${process.env.NEXT_PUBLIC_TELEGRAM_BOT_USERNAME}?start=${appUser?.id}`}
                target="_blank"
                className="text-xs text-cyan-400 underline"
              >
                Connect Telegram
              </a>
            )
          )}
          <div className="flex items-center gap-2 text-xs text-slate-500">
            <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
            {protocols.length} protocols monitored
          </div>
        </div>
      </header>

      <main className="p-6 max-w-6xl mx-auto">
        {isLimited && (
          <div className="mb-4 text-xs text-slate-400 bg-slate-900/50 border border-slate-800 rounded px-3 py-2">
            Showing {visibleCount} of {protocols.length} protocols on the Free plan —{" "}
            <a href="/pricing" className="text-cyan-400 underline">upgrade to Pro</a> to see everything.
          </div>
        )}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {protocols.map((p, i) =>
            i < visibleCount ? (
              <div key={p.slug} className="rounded-lg border border-slate-800 bg-slate-900/50 p-5">
                <div className="flex items-start justify-between mb-3">
                  <div>
                    <div className="font-bold text-slate-100">{p.name}</div>
                    <div className={`text-xs mt-0.5 ${CATEGORY_STYLES[p.category]}`}>
                      {p.category.replace("_", " ")}
                    </div>
                  </div>
                  <div className="flex flex-col items-end gap-2">
                    {p.riskLevel && (
                      <span className={`text-xs font-bold px-2 py-0.5 rounded border ${RISK_STYLES[p.riskLevel]}`}>
                        {p.riskLevel}
                      </span>
                    )}
                    {isAuthenticated && (
                      <WatchlistButton slug={p.slug} initialWatched={watchedSlugs.has(p.slug)} />
                    )}
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-3 text-sm">
                  <div>
                    <div className="text-slate-500 text-xs">TVL</div>
                    <div className="font-semibold">{formatUsd(p.tvlUsd)}</div>
                  </div>
                  <div>
                    <div className="text-slate-500 text-xs">Risk Score</div>
                    <div className="font-semibold">
                      {p.overallScore !== null ? `${p.overallScore}/100` : "—"}
                    </div>
                  </div>
                </div>

                {p.explanation && (
                  <p className="text-xs text-slate-500 mt-3 leading-relaxed">{p.explanation}</p>
                )}

                <div className="text-xs text-slate-600 mt-3">Updated {timeAgo(p.snapshotAt)}</div>
              </div>
            ) : (
              <LockedProtocolCard key={p.slug} name={p.name} category={p.category} />
            )
          )}
        </div>
      </main>
    </div>
  );
}