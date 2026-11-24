import type { EvidenceLevel, SecurityAssessment } from "@/lib/api";

export const SECURITY_LEVEL_STYLES: Record<EvidenceLevel, string> = {
  UNKNOWN: "border-slate-600 bg-slate-800/60 text-slate-300",
  INFO: "border-sky-400/30 bg-sky-400/10 text-sky-300",
  LOW: "border-emerald-400/30 bg-emerald-400/10 text-emerald-300",
  MEDIUM: "border-amber-400/30 bg-amber-400/10 text-amber-300",
  HIGH: "border-orange-400/30 bg-orange-400/10 text-orange-300",
  CRITICAL: "border-red-400/30 bg-red-400/10 text-red-300",
};

export function LevelBadge({ level, prefix }: { level: EvidenceLevel; prefix?: string }) {
  return <span className={`inline-flex items-center rounded border px-2 py-0.5 text-[11px] font-bold tracking-wide ${SECURITY_LEVEL_STYLES[level]}`}>{prefix ? <span className="mr-1 font-medium opacity-75">{prefix}</span> : null}{level}</span>;
}

const SUPPORT_TEXT: Record<SecurityAssessment["protocol"]["support_level"], string> = {
  FULL: "Full coverage",
  PARTIAL: "Partial coverage",
  UNSUPPORTED: "Not supported",
};

export function SupportBadge({ supportLevel }: { supportLevel: SecurityAssessment["protocol"]["support_level"] }) {
  return <span className="inline-flex items-center rounded-full border border-slate-700 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-400" title="How much of this protocol SolanaGuard can currently measure. PARTIAL: on-chain security checks only; protocol-specific metrics are not collected yet.">{SUPPORT_TEXT[supportLevel]}</span>;
}
