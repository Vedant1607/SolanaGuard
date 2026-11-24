-- Secure Telegram account linking.
--
-- Additive only: one new table, its indexes, CHECK constraints and foreign key,
-- plus a UNIQUE index on users."telegramChatId". No existing row is updated or
-- deleted. The table and index statements below were generated offline with
-- `prisma migrate diff` from schema.prisma; the guard and CHECKs are hand-written.
--
-- users."telegramChatId" becomes UNIQUE so one Telegram chat can belong to at
-- most one account (NULLs remain unlimited). The guard aborts the migration,
-- before any change, if existing data already has a chat linked to more than
-- one user; resolve those rows manually and re-run. It never modifies them.
--
-- Rollback (manual; Prisma has no down migrations). Dropping the table discards
-- outstanding link tokens only, which are short-lived and re-issuable:
--   DROP INDEX IF EXISTS "users_telegramChatId_key";
--   DROP TABLE IF EXISTS "telegram_link_tokens";
--   DELETE FROM "_prisma_migrations" WHERE migration_name = '20261120120000_telegram_link_tokens';
-- Rolling back while the new application code is deployed disables Telegram
-- linking (the webhook cannot find the table); deploy the previous code first.

-- Guard: refuse to proceed if a chat ID is shared by several users.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM "users"
        WHERE "telegramChatId" IS NOT NULL
        GROUP BY "telegramChatId"
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'users."telegramChatId" has duplicate values; resolve them before applying 20261120120000_telegram_link_tokens';
    END IF;
END
$$;

-- CreateTable
CREATE TABLE "telegram_link_tokens" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "tokenHash" TEXT NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "expiresAt" TIMESTAMP(3) NOT NULL,
    "consumedAt" TIMESTAMP(3),
    "revokedAt" TIMESTAMP(3),

    CONSTRAINT "telegram_link_tokens_pkey" PRIMARY KEY ("id")
);

-- Only a lowercase hex SHA-256 digest may be stored (a plaintext token, which is
-- 43 base64url characters, can never satisfy this).
ALTER TABLE "telegram_link_tokens" ADD CONSTRAINT "telegram_link_tokens_tokenHash_sha256_check"
    CHECK ("tokenHash" ~ '^[0-9a-f]{64}$');

ALTER TABLE "telegram_link_tokens" ADD CONSTRAINT "telegram_link_tokens_expiry_check"
    CHECK ("expiresAt" > "createdAt");

-- CreateIndex
CREATE UNIQUE INDEX "telegram_link_tokens_tokenHash_key" ON "telegram_link_tokens"("tokenHash");

-- CreateIndex
CREATE INDEX "telegram_link_tokens_userId_idx" ON "telegram_link_tokens"("userId");

-- CreateIndex
CREATE INDEX "telegram_link_tokens_expiresAt_idx" ON "telegram_link_tokens"("expiresAt");

-- CreateIndex
CREATE UNIQUE INDEX "users_telegramChatId_key" ON "users"("telegramChatId");

-- AddForeignKey
ALTER TABLE "telegram_link_tokens" ADD CONSTRAINT "telegram_link_tokens_userId_fkey" FOREIGN KEY ("userId") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;
