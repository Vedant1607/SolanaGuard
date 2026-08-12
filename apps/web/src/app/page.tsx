import { fetchProtocols, type RiskLevel, type ProtocolCategory } from "@/lib/api";

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

export default async function DashboardPage() {
  const protocols = await fetchProtocols();

  return (
    <div className="min-h-screen bg-slate-950 text-slate-200 font-mono">
      <header className="border-b border-slate-800 px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-2 font-bold text-lg">
          <span className="text-cyan-400">Solana</span>Guard
        </div>
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
          {protocols.length} protocols monitored
        </div>
      </header>

      <main className="p-6 max-w-6xl mx-auto">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {protocols.map((p) => (
            <div
              key={p.slug}
              className="rounded-lg border border-slate-800 bg-slate-900/50 p-5"
            >
              <div className="flex items-start justify-between mb-3">
                <div>
                  <div className="font-bold text-slate-100">{p.name}</div>
                  <div className={`text-xs mt-0.5 ${CATEGORY_STYLES[p.category]}`}>
                    {p.category.replace("_", " ")}
                  </div>
                </div>
                {p.riskLevel && (
                  <span
                    className={`text-xs font-bold px-2 py-0.5 rounded border ${RISK_STYLES[p.riskLevel]}`}
                  >
                    {p.riskLevel}
                  </span>
                )}
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
                <p className="text-xs text-slate-500 mt-3 leading-relaxed">
                  {p.explanation}
                </p>
              )}

              <div className="text-xs text-slate-600 mt-3">
                Updated {timeAgo(p.snapshotAt)}
              </div>
            </div>
          ))}
        </div>
      </main>
    </div>
  );
}