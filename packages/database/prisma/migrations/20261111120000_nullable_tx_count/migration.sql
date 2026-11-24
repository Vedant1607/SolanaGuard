-- NULL now means "transaction count unavailable" (e.g. the RPC provider failed or
-- the window could not be counted exactly). Dropping the default ensures an
-- omitted value can no longer silently become a fabricated zero.
-- Schema-only change: existing rows are neither rewritten nor deleted.

-- AlterTable
ALTER TABLE "protocol_snapshots" ALTER COLUMN "txCount24h" DROP NOT NULL,
ALTER COLUMN "txCount24h" DROP DEFAULT;
