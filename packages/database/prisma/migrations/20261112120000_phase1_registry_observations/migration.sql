-- Phase 1 (schema only): protocol address registry and metric observations.
--
-- Strictly additive: new enums, one new enum value, one new defaulted column,
-- two new tables, their indexes, foreign keys and CHECK constraints. No
-- existing row, column, type or constraint is rewritten or removed.
--
-- The new PERPS value is not used here: PostgreSQL forbids using an enum
-- value in the transaction that added it, so catalogue data lives in the
-- following migration.
--
-- Foreign keys are ON DELETE/UPDATE RESTRICT: protocols and addresses that
-- have registry rows or observations can be deactivated, never deleted or
-- re-keyed, so history is never silently cascaded away.

-- CreateEnum
CREATE TYPE "SupportLevel" AS ENUM ('FULL', 'PARTIAL', 'UNSUPPORTED');

-- CreateEnum
CREATE TYPE "AddressKind" AS ENUM ('PROGRAM', 'STAKE_POOL', 'MARKET', 'MINT');

-- CreateEnum
CREATE TYPE "AddressVerification" AS ENUM ('UNVERIFIED', 'VERIFIED', 'MISMATCH');

-- CreateEnum
CREATE TYPE "ObservationStatus" AS ENUM ('OK', 'MISSING', 'STALE', 'ERROR');

-- AlterEnum
ALTER TYPE "ProtocolCategory" ADD VALUE IF NOT EXISTS 'PERPS';

-- AlterTable
ALTER TABLE "protocols" ADD COLUMN     "supportLevel" "SupportLevel" NOT NULL DEFAULT 'PARTIAL';

-- CreateTable
CREATE TABLE "protocol_addresses" (
    "id" TEXT NOT NULL,
    "protocolId" TEXT NOT NULL,
    "address" TEXT NOT NULL,
    "kind" "AddressKind" NOT NULL,
    "label" TEXT NOT NULL,
    "isSharedDependency" BOOLEAN NOT NULL DEFAULT false,
    "expectedOwner" TEXT,
    "verification" "AddressVerification" NOT NULL DEFAULT 'UNVERIFIED',
    "verifiedAt" TIMESTAMP(3),
    "verificationNote" TEXT,
    "isActive" BOOLEAN NOT NULL DEFAULT true,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "protocol_addresses_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "metric_observations" (
    "id" TEXT NOT NULL,
    "protocolId" TEXT NOT NULL,
    "addressId" TEXT,
    "metricKey" TEXT NOT NULL,
    "status" "ObservationStatus" NOT NULL,
    "valueNumeric" DECIMAL(40,12),
    "valueText" TEXT,
    "unit" TEXT NOT NULL,
    "source" TEXT NOT NULL,
    "sourceObservedAt" TIMESTAMP(3),
    "sourceSlot" BIGINT,
    "collectedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "adapterKey" TEXT NOT NULL,
    "adapterVersion" TEXT NOT NULL,
    "errorCode" TEXT,

    CONSTRAINT "metric_observations_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "protocol_addresses_address_idx" ON "protocol_addresses"("address");

-- CreateIndex
CREATE UNIQUE INDEX "protocol_addresses_protocolId_address_key" ON "protocol_addresses"("protocolId", "address");

-- CreateIndex
CREATE UNIQUE INDEX "protocol_addresses_id_protocolId_key" ON "protocol_addresses"("id", "protocolId");

-- CreateIndex
CREATE INDEX "metric_observations_protocolId_metricKey_collectedAt_idx" ON "metric_observations"("protocolId", "metricKey", "collectedAt" DESC);

-- CreateIndex
CREATE INDEX "metric_observations_addressId_metricKey_collectedAt_idx" ON "metric_observations"("addressId", "metricKey", "collectedAt" DESC);

-- AddForeignKey
ALTER TABLE "protocol_addresses" ADD CONSTRAINT "protocol_addresses_protocolId_fkey" FOREIGN KEY ("protocolId") REFERENCES "protocols"("id") ON DELETE RESTRICT ON UPDATE RESTRICT;

-- AddForeignKey
ALTER TABLE "metric_observations" ADD CONSTRAINT "metric_observations_protocolId_fkey" FOREIGN KEY ("protocolId") REFERENCES "protocols"("id") ON DELETE RESTRICT ON UPDATE RESTRICT;

-- AddForeignKey
ALTER TABLE "metric_observations" ADD CONSTRAINT "metric_observations_addressId_protocolId_fkey" FOREIGN KEY ("addressId", "protocolId") REFERENCES "protocol_addresses"("id", "protocolId") ON DELETE RESTRICT ON UPDATE RESTRICT;
-- Integrity rules Prisma cannot express (documented in schema.prisma).

-- AddCheckConstraint: a registry address must look like a base58 public key.
ALTER TABLE "protocol_addresses" ADD CONSTRAINT "protocol_addresses_address_base58_chk"
    CHECK ("address" ~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$');

-- AddCheckConstraint: OK/STALE carry a value; MISSING/ERROR never do, so an
-- absent measurement can never be read back as zero.
ALTER TABLE "metric_observations" ADD CONSTRAINT "metric_observations_value_matches_status_chk"
    CHECK (
        ("status" IN ('OK', 'STALE') AND ("valueNumeric" IS NOT NULL OR "valueText" IS NOT NULL))
        OR ("status" IN ('MISSING', 'ERROR') AND "valueNumeric" IS NULL AND "valueText" IS NULL)
    );

-- AddCheckConstraint: an ERROR must say why.
ALTER TABLE "metric_observations" ADD CONSTRAINT "metric_observations_error_has_code_chk"
    CHECK ("status" <> 'ERROR' OR "errorCode" IS NOT NULL);
