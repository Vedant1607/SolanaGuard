import type { EvidenceLevel, SecurityAssessment, SecurityEvidence, SecurityObservation } from "./api";

// Presentation helpers for the evidence-based security assessment. They only
// summarise what the API returned; they never assign or soften a level.
// Kept free of runtime imports so `node --test` can load it directly.

/** Plain-language context per rule: what it checks and why it matters. */
export const RULE_CONTEXT: Record<string, { title: string; why: string }> = {
  "REG-01": {
    title: "Registered account ownership",
    why: "Confirms the address SolanaGuard monitors is still the account it was verified as. A different owner means the registry entry is wrong or the account was reassigned.",
  },
  "SEC-01": {
    title: "Program upgradeability",
    why: "Whoever holds the upgrade authority can replace the program's code. An upgradeable program is only as safe as that key and the process around it.",
  },
  "SEC-02": {
    title: "Upgrade-authority change",
    why: "A change of upgrade authority can precede a malicious or unreviewed upgrade. Detecting it needs at least two successful readings.",
  },
  "SEC-03": {
    title: "Recent redeployment",
    why: "A recent redeploy is a signal to check release notes, audits and governance approval. It is not evidence of compromise.",
  },
};

export function ruleTitle(ruleId: string): string {
  return RULE_CONTEXT[ruleId]?.title ?? ruleId;
}

const SEVERITY: Record<EvidenceLevel, number> = { CRITICAL: 5, HIGH: 4, MEDIUM: 3, UNKNOWN: 2, LOW: 1, INFO: 0 };

/** Most severe first; UNKNOWN sorts above LOW so open questions stay visible. */
export function sortEvidence(evidence: readonly SecurityEvidence[]): SecurityEvidence[] {
  return evidence
    .map((item, index) => ({ item, index }))
    .sort((a, b) => SEVERITY[b.item.level] - SEVERITY[a.item.level] || a.index - b.index)
    .map(({ item }) => item);
}

function parseUtc(value: string | null | undefined): number | null {
  if (!value) return null;
  const time = Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`);
  return Number.isNaN(time) ? null : time;
}

function allObservations(assessment: SecurityAssessment): SecurityObservation[] {
  const byId = new Map<string, SecurityObservation>();
  for (const evidence of assessment.evidence) for (const o of evidence.observations) byId.set(o.id, o);
  return [...byId.values()];
}

export interface SecuritySummary {
  /** Newest collection time among the observations the evidence cites (ms), or null. */
  latestObservationAt: number | null;
  /** Distinct sources and adapter versions behind the evidence. */
  sources: string[];
  adapters: string[];
  /** Evidence that could not be evaluated, with the reason. */
  unknown: SecurityEvidence[];
  /** Observations whose last measurement failed or was missing. */
  failedObservations: SecurityObservation[];
  coverageText: string;
  hasEvidence: boolean;
}

export function summarizeSecurity(assessment: SecurityAssessment): SecuritySummary {
  const observations = allObservations(assessment);
  const times = observations.map((o) => parseUtc(o.collected_at)).filter((t): t is number => t !== null);
  const { assessed_indicators: assessed, total_indicators: total } = assessment.dimension;
  return {
    latestObservationAt: times.length ? Math.max(...times) : null,
    sources: [...new Set(observations.map((o) => o.source))].sort(),
    adapters: [...new Set(observations.map((o) => o.adapter))].sort(),
    unknown: assessment.evidence.filter((e) => e.level === "UNKNOWN"),
    failedObservations: observations.filter((o) => o.status === "ERROR" || o.status === "MISSING"),
    coverageText: total === 0 ? "No indicators evaluated" : `${assessed} of ${total} indicators assessed`,
    hasEvidence: total > 0,
  };
}

/** Explorer link for an on-chain address (read-only, public). */
export function explorerUrl(address: string): string {
  return `https://explorer.solana.com/address/${encodeURIComponent(address)}`;
}

export function shortKey(key: string | null): string {
  if (!key) return "—";
  return key.length > 12 ? `${key.slice(0, 4)}…${key.slice(-4)}` : key;
}
