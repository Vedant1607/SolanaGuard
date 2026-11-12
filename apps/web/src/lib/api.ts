export type RiskLevel = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type ProtocolCategory = "DEX" | "LENDING" | "LIQUID_STAKING" | "PERPS";
export type AnomalyType = "LIQUIDITY_WITHDRAWAL" | "TX_SPIKE" | "TVL_DECLINE" | "WHALE_MOVEMENT" | "PRICE_DEVIATION" | "RUG_PULL_SIGNAL";

export interface Protocol {
  slug: string;
  name: string;
  category: ProtocolCategory;
  tvlUsd: number | null;
  txCount24h: number | null;
  snapshotAt: string | null;
  /** Snapshot the latest score was compared against; absent on older API builds. */
  previousSnapshotAt?: string | null;
  overallScore: number | null;
  riskLevel: RiskLevel | null;
  explanation: string | null;
  scoredAt: string | null;
}

export interface Anomaly {
  id: string;
  type: AnomalyType;
  severity: RiskLevel;
  description: string;
  detected_at: string;
  protocol: { id: string; name: string; slug: string };
  calibrated_score: number;
  raw_score: number | null;
  threshold: number | null;
  detector: { version: string; method: string };
}

export interface AnomalyListResponse {
  data: Anomaly[];
  pagination: { limit: number; offset: number; total: number };
}

export async function fetchProtocols(): Promise<Protocol[]> {
  const res = await fetch(`${process.env.API_URL}/api/v1/protocols`, {
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`Failed to fetch protocols: ${res.status}`);
  }
  return res.json();
}

export async function fetchAnomalies({ limit = 6, offset = 0 }: { limit?: number; offset?: number } = {}): Promise<AnomalyListResponse> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  const res = await fetch(`${process.env.API_URL}/api/v1/anomalies?${query.toString()}`, {
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`Failed to fetch anomalies: ${res.status}`);
  return res.json();
}
