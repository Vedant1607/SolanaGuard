// Prisma-backed TelegramLinkStore. Takes the client as a parameter so the same
// code runs in the server action and in the throwaway-PostgreSQL test.
import type { TelegramLinkStore } from "./telegram-link-core";

// The subset of PrismaClient used here (structural, so tests can pass any client).
type TokenDelegate = {
  updateMany(args: { where: { userId: string; consumedAt: null; revokedAt: null }; data: { revokedAt: Date } }): Promise<unknown>;
  create(args: { data: { userId: string; tokenHash: string; createdAt: Date; expiresAt: Date } }): Promise<unknown>;
};
type LinkTx = {
  $queryRaw(query: TemplateStringsArray, ...values: unknown[]): Promise<unknown>;
  telegramLinkToken: TokenDelegate;
};
export type LinkDb = {
  user: { findUnique(args: { where: { clerkId: string }; select: { id: true } }): Promise<{ id: string } | null> };
  $transaction<T>(fn: (tx: LinkTx) => Promise<T>, options?: { maxWait?: number; timeout?: number }): Promise<T>;
};

export function prismaTelegramLinkStore(db: LinkDb): TelegramLinkStore {
  return {
    async findUserIdByClerkId(clerkId) {
      const user = await db.user.findUnique({ where: { clerkId }, select: { id: true } });
      return user?.id ?? null;
    },
    async replaceTokens(userId, tokenHash, createdAt, expiresAt) {
      // Interactive transaction: the users-row lock serialises concurrent issuance
      // for the SAME user only (other users lock other rows), so revoke + insert
      // run as one unit and exactly one unused token survives. Three short
      // statements; nothing else happens while the lock is held.
      // Lock order shared with consumption (apps/api/app/telegram_linking.py):
      // users row FIRST, then that user's token rows. Changing it reintroduces
      // an issuance/consumption deadlock.
      await db.$transaction(async (tx) => {
        await tx.$queryRaw`SELECT id FROM users WHERE id = ${userId} FOR UPDATE`;
        await tx.telegramLinkToken.updateMany({
          where: { userId, consumedAt: null, revokedAt: null },
          data: { revokedAt: createdAt },
        });
        await tx.telegramLinkToken.create({ data: { userId, tokenHash, createdAt, expiresAt } });
      }, { maxWait: 5_000, timeout: 10_000 });
    },
  };
}
