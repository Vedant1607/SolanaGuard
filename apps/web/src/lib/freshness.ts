import type { Protocol } from "@/lib/api";
import { formatDuration, formatRelative, parseApiTimestamp } from "@/lib/time";

// Ingestion runs every 5 minutes (INGEST_INTERVAL_SECONDS); three missed cycles
// is treated as stale. These rules only interpret timestamps the API returns —
// they never adjust or recompute a risk score.
export const STALE_AFTER_MS = 15 * 60_000;
// A score written in the same cycle as its snapshot lands within milliseconds.
const SAME_CYCLE_MS = 60_000;

export interface DataNotice {
  tone: "warning" | "info";
  text: string;
}

export interface ProtocolFreshness {
  snapshotAt: Date | null;
  scoredAt: Date | null;
  telemetryStale: boolean;
  staleBaseline: boolean;
  notices: DataNotice[];
}

export function assessFreshness(protocol: Protocol, now: number = Date.now()): ProtocolFreshness {
  const snapshotAt = parseApiTimestamp(protocol.snapshotAt);
  const scoredAt = parseApiTimestamp(protocol.scoredAt);
  const previousSnapshotAt = parseApiTimestamp(protocol.previousSnapshotAt);
  const notices: DataNotice[] = [];

  const telemetryStale = snapshotAt !== null && now - snapshotAt.getTime() > STALE_AFTER_MS;
  if (!snapshotAt) {
    notices.push({ tone: "info", text: "No telemetry has been recorded for this protocol yet." });
  } else if (telemetryStale) {
    notices.push({ tone: "warning", text: `Telemetry is stale — last snapshot ${formatRelative(snapshotAt, now)}.` });
  }

  let staleBaseline = false;
  if (protocol.overallScore === null || !scoredAt) {
    if (snapshotAt) notices.push({ tone: "info", text: "Not scored yet — scoring needs two snapshots." });
  } else if (snapshotAt && scoredAt.getTime() < snapshotAt.getTime() - SAME_CYCLE_MS) {
    notices.push({ tone: "info", text: `Risk score predates the latest snapshot (scored ${formatRelative(scoredAt, now)}).` });
  } else if (snapshotAt && previousSnapshotAt) {
    const baselineGap = snapshotAt.getTime() - previousSnapshotAt.getTime();
    if (baselineGap > STALE_AFTER_MS) {
      staleBaseline = true;
      notices.push({
        tone: "warning",
        text: `Score compares against a snapshot ${formatDuration(baselineGap)} older, not the previous 5-minute cycle — low confidence.`,
      });
    }
  }

  return { snapshotAt, scoredAt, telemetryStale, staleBaseline, notices };
}
