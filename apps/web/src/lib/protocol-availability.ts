import type { Protocol } from "./api";

// Dashboard ordering by data availability. These helpers only read what the
// API returned: they never fill in, default or recompute a metric, and a null
// is always "unavailable", never zero or LOW. (A reported TVL of exactly 0 is
// a real measurement and counts as available.)
//
// Kept free of runtime imports so `node --test` can load it directly.

/** Rank 0 = TVL and risk score, 1 = only one of them, 2 = neither. */
export type AvailabilityRank = 0 | 1 | 2;

export function hasTvl(protocol: Pick<Protocol, "tvlUsd">): boolean {
  return typeof protocol.tvlUsd === "number" && Number.isFinite(protocol.tvlUsd);
}

/** A usable risk score needs both the numeric score and its level. */
export function hasRiskScore(protocol: Pick<Protocol, "overallScore" | "riskLevel">): boolean {
  return typeof protocol.overallScore === "number" && Number.isFinite(protocol.overallScore) && protocol.riskLevel !== null;
}

export function availabilityRank(protocol: Protocol): AvailabilityRank {
  const available = Number(hasTvl(protocol)) + Number(hasRiskScore(protocol));
  return available === 2 ? 0 : available === 1 ? 1 : 2;
}

export const UNAVAILABLE_LABEL = "Unavailable";
export const DATA_UNAVAILABLE_LABEL = "Data unavailable";

export interface ProtocolMetricLabels {
  tvl: string;
  riskScore: string;
  /** null when no risk level was reported: never shown as LOW. */
  riskLevel: Protocol["riskLevel"];
  /** True when neither TVL nor a risk score is available. */
  dataUnavailable: boolean;
}

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 });

/** Display strings for a card's headline metrics; a missing value reads "Unavailable", never 0. */
export function protocolMetricLabels(protocol: Protocol): ProtocolMetricLabels {
  return {
    tvl: hasTvl(protocol) ? usd.format(protocol.tvlUsd as number) : UNAVAILABLE_LABEL,
    riskScore: typeof protocol.overallScore === "number" && Number.isFinite(protocol.overallScore) ? `${protocol.overallScore}/100` : UNAVAILABLE_LABEL,
    riskLevel: protocol.riskLevel ?? null,
    dataUnavailable: availabilityRank(protocol) === 2,
  };
}

/**
 * Protocols with more available data first. Within a rank the incoming order
 * (the API's existing alphabetical order) is kept, so the sort is stable and
 * deterministic. Returns a new array; the input is not modified. No protocol
 * is dropped: those without data are listed last, not hidden.
 */
export function sortByDataAvailability<T extends Protocol>(protocols: readonly T[]): T[] {
  return protocols
    .map((protocol, index) => ({ protocol, index, rank: availabilityRank(protocol) }))
    .sort((a, b) => a.rank - b.rank || a.index - b.index)
    .map(({ protocol }) => protocol);
}

/**
 * Which ingestion sources actually back a protocol's activity data, judged
 * from what the API returned (TVL ingestion uses DefiLlama; 24-hour
 * transaction counts use Helius). A source is named only when its value is
 * present; a protocol with no snapshot at all claims no source.
 */
export function activitySourceText(protocol: Pick<Protocol, "tvlUsd" | "txCount24h" | "snapshotAt">): string {
  if (protocol.snapshotAt === null && !hasTvl(protocol) && protocol.txCount24h === null) {
    return "No TVL or transaction data has been ingested for this protocol.";
  }
  const tvl = hasTvl(protocol) ? "TVL from DefiLlama" : "TVL unavailable";
  const tx = protocol.txCount24h !== null ? "24h transactions from Helius" : "24h transactions unavailable in the latest snapshot";
  return `${tvl} · ${tx}`;
}

/**
 * Slugs a plan may open, given its card allowance (``visibleProtocolCount``).
 * The dashboard grid and the protocol detail page both use this, so a locked
 * protocol cannot be opened by URL.
 */
export function openProtocolSlugs(protocols: readonly Protocol[], visibleCount: number): Set<string> {
  return new Set(sortByDataAvailability(protocols).slice(0, Math.max(0, visibleCount)).map((p) => p.slug));
}
