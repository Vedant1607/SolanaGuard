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

// --- Security assessment (GET /api/v1/protocols/{slug}/security) -----------
// Evidence-based, rule-versioned and separate from the TVL/activity score
// above. Levels are never defaulted: missing or stale data is UNKNOWN.

export type SecurityLevel = "UNKNOWN" | "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type EvidenceLevel = SecurityLevel | "INFO";
export type ObservationStatus = "OK" | "MISSING" | "STALE" | "ERROR";

export interface SecurityObservation {
  id: string;
  metric_key: string;
  status: ObservationStatus;
  value: string | null;
  unit: string;
  source: string;
  source_slot: number | null;
  source_observed_at: string | null;
  collected_at: string;
  adapter: string;
  error_code: string | null;
}

export interface SecurityEvidence {
  rule_id: string;
  indicator: string;
  level: EvidenceLevel;
  explanation: string;
  address_id: string | null;
  address_label: string | null;
  observations: SecurityObservation[];
  rule_version: string;
}

export interface RegistryAddress {
  id: string;
  address: string;
  kind: "PROGRAM" | "STAKE_POOL" | "MARKET" | "MINT";
  label: string;
  is_shared_dependency: boolean;
  expected_owner: string | null;
  verification: "UNVERIFIED" | "VERIFIED" | "MISMATCH";
  verified_at: string | null;
  verification_note: string | null;
}

export interface SecurityAssessment {
  protocol: { slug: string; name: string; category: ProtocolCategory; is_active: boolean; support_level: "FULL" | "PARTIAL" | "UNSUPPORTED" };
  evaluated_at: string;
  dimension: { dimension: string; level: SecurityLevel; explanation: string; assessed_indicators: number; total_indicators: number; rule_version: string };
  evidence: SecurityEvidence[];
  addresses: RegistryAddress[];
}

export type SecurityResult =
  | { ok: true; data: SecurityAssessment }
  | { ok: false; reason: "not_found" | "not_ready" | "unavailable" };

/** Never throws: a failed lookup is reported as unavailable, never as a level. */
export async function fetchProtocolSecurity(slug: string): Promise<SecurityResult> {
  try {
    const res = await fetch(`${process.env.API_URL}/api/v1/protocols/${encodeURIComponent(slug)}/security`, { cache: "no-store" });
    if (res.status === 404) return { ok: false, reason: "not_found" };
    if (res.status === 503) return { ok: false, reason: "not_ready" };
    if (!res.ok) return { ok: false, reason: "unavailable" };
    return { ok: true, data: (await res.json()) as SecurityAssessment };
  } catch {
    return { ok: false, reason: "unavailable" };
  }
}

export interface CollectorStatus {
  evaluated_at: string;
  adapter: string;
  stale_after_seconds: number;
  instance: {
    /** "disabled" (SECURITY_COLLECTOR_ENABLED=false) is distinct from "no_rpc_endpoint". */
    state: "disabled" | "no_rpc_endpoint" | "starting" | "active" | "failing";
    enabled: boolean;
    interval_seconds: number;
    rpc_configured: boolean;
    rpc_host: string | null;
    public_fallback: boolean;
    last_started_at: string | null;
    last_finished_at: string | null;
    /** succeeded | partial | failed | recently_collected | locked | schema_missing | no_rpc_endpoint | error */
    last_outcome: string | null;
    last_report: { failed_protocols?: string[]; error?: string } | null;
    next_run_at: string | null;
    consecutive_failures: number;
  };
  protocols: { slug: string; is_active: boolean; last_collected_at: string | null; fresh: boolean; latest_batch: { ok: number; missing: number; stale: number; error: number }; error_codes: string[] }[] | null;
}

/** Never throws; null when the status cannot be loaded. */
export async function fetchCollectorStatus(): Promise<CollectorStatus | null> {
  try {
    const res = await fetch(`${process.env.API_URL}/api/v1/security/collector-status`, { cache: "no-store" });
    return res.ok ? ((await res.json()) as CollectorStatus) : null;
  } catch {
    return null;
  }
}
