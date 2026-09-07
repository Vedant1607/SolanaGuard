-- Add a truthful TVL signal type; TVL alone does not establish a liquidity withdrawal.
ALTER TYPE "AnomalyType" ADD VALUE IF NOT EXISTS 'TVL_DECLINE';

-- Attribute every anomaly to a source snapshot and detector configuration.
ALTER TABLE "anomalies"
    ADD COLUMN "sourceSnapshotId" TEXT,
    ADD COLUMN "detectorVersion" TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN "detectionMethod" TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN "rawScore" DOUBLE PRECISION,
    ADD COLUMN "scoreThreshold" DOUBLE PRECISION;

-- Existing rows predate source-snapshot attribution. Their own IDs provide a
-- stable legacy identity so the new constraint can be applied safely.
UPDATE "anomalies"
SET "sourceSnapshotId" = id
WHERE "sourceSnapshotId" IS NULL;

ALTER TABLE "anomalies"
    ALTER COLUMN "sourceSnapshotId" SET NOT NULL;

CREATE UNIQUE INDEX "anomalies_protocolId_sourceSnapshotId_detectorVersion_type_key"
ON "anomalies"("protocolId", "sourceSnapshotId", "detectorVersion", "type");
