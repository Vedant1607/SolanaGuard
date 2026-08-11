-- CreateEnum
CREATE TYPE "ProtocolCategory" AS ENUM ('DEX', 'LENDING', 'LIQUID_STAKING');

-- CreateEnum
CREATE TYPE "RiskLevel" AS ENUM ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL');

-- CreateEnum
CREATE TYPE "AnomalyType" AS ENUM ('LIQUIDITY_WITHDRAWAL', 'TX_SPIKE', 'WHALE_MOVEMENT', 'PRICE_DEVIATION', 'RUG_PULL_SIGNAL');

-- CreateTable
CREATE TABLE "protocols" (
    "id" TEXT NOT NULL,
    "slug" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "category" "ProtocolCategory" NOT NULL,
    "programId" TEXT NOT NULL,
    "isActive" BOOLEAN NOT NULL DEFAULT true,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "protocols_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "protocol_snapshots" (
    "id" TEXT NOT NULL,
    "protocolId" TEXT NOT NULL,
    "tvlUsd" DOUBLE PRECISION NOT NULL,
    "volume24hUsd" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "txCount24h" INTEGER NOT NULL DEFAULT 0,
    "uniqueWallets24h" INTEGER NOT NULL DEFAULT 0,
    "liquidityDepth" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "utilizationRate" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "snapshotAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "protocol_snapshots_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "risk_scores" (
    "id" TEXT NOT NULL,
    "protocolId" TEXT NOT NULL,
    "overallScore" DOUBLE PRECISION NOT NULL,
    "riskLevel" "RiskLevel" NOT NULL,
    "method" TEXT NOT NULL,
    "explanation" TEXT,
    "scoredAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "risk_scores_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "anomalies" (
    "id" TEXT NOT NULL,
    "protocolId" TEXT NOT NULL,
    "type" "AnomalyType" NOT NULL,
    "severity" "RiskLevel" NOT NULL,
    "score" DOUBLE PRECISION NOT NULL,
    "description" TEXT NOT NULL,
    "acknowledged" BOOLEAN NOT NULL DEFAULT false,
    "detectedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "anomalies_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "protocols_slug_key" ON "protocols"("slug");

-- CreateIndex
CREATE UNIQUE INDEX "protocols_programId_key" ON "protocols"("programId");

-- CreateIndex
CREATE INDEX "protocol_snapshots_protocolId_snapshotAt_idx" ON "protocol_snapshots"("protocolId", "snapshotAt");

-- CreateIndex
CREATE INDEX "risk_scores_protocolId_scoredAt_idx" ON "risk_scores"("protocolId", "scoredAt");

-- CreateIndex
CREATE INDEX "anomalies_protocolId_detectedAt_idx" ON "anomalies"("protocolId", "detectedAt");

-- AddForeignKey
ALTER TABLE "protocol_snapshots" ADD CONSTRAINT "protocol_snapshots_protocolId_fkey" FOREIGN KEY ("protocolId") REFERENCES "protocols"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "risk_scores" ADD CONSTRAINT "risk_scores_protocolId_fkey" FOREIGN KEY ("protocolId") REFERENCES "protocols"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "anomalies" ADD CONSTRAINT "anomalies_protocolId_fkey" FOREIGN KEY ("protocolId") REFERENCES "protocols"("id") ON DELETE CASCADE ON UPDATE CASCADE;
