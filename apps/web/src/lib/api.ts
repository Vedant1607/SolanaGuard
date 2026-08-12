export type RiskLevel = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type ProtocolCategory = "DEX" | "LENDING" | "LIQUID_STAKING";

export interface Protocol {
  slug: string;
  name: string;
  category: ProtocolCategory;
  tvlUsd: number | null;
  txCount24h: number | null;
  snapshotAt: string | null;
  overallScore: number | null;
  riskLevel: RiskLevel | null;
  explanation: string | null;
  scoredAt: string | null;
}

export async function fetchProtocols(): Promise<Protocol[]> {
  const res = await fetch(`${process.env.API_URL}/api/v1/protocols`, {
    next: { revalidate: 60 },
  });
  if (!res.ok) {
    throw new Error(`Failed to fetch protocols: ${res.status}`);
  }
  return res.json();
}