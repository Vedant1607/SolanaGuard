// Prisma implementations of the billing stores (logic lives in billing-core.ts).
// Takes the client as a parameter so the same code runs in the app and in the
// disposable-PostgreSQL tests.
//
// Lock order (shared by every writer): the users row FIRST (checkout only),
// then the subscriptions row. Webhook transactions lock only an event row
// (via the unique primary key) and one subscriptions row, never a users row.
// Retired mappings are insert-only. Checkout inserts them under its users-row
// lock; a webhook inserts one (late charge on a terminal row) under the
// subscription-row lock, and the foreign key then takes KEY SHARE on the users
// row. Checkout therefore locks users FOR NO KEY UPDATE: it still serialises
// checkouts per user but never conflicts with that KEY SHARE, so no cycle forms.
import type { db } from "@solanaguard/database";
import type {
  CheckoutStore, EventClaim, StoredSubscription, SubscriptionPatch, WebhookStore, WebhookTx,
} from "./billing-core";

type Client = typeof db;
type Tx = Omit<Client, "$transaction" | "$connect" | "$disconnect" | "$on" | "$extends">;

const TX_OPTIONS = { maxWait: 5_000, timeout: 10_000 } as const;

const STORED = {
  id: true, userId: true, status: true, razorpaySubscriptionId: true, currentPeriodEnd: true,
  paymentConfirmedAt: true, cancelMode: true, cancelSubscriptionId: true, lastEventAt: true, lastEventType: true,
  checkoutAttemptId: true, checkoutStatus: true, checkoutStartedAt: true,
} as const;

const RETIRED = {
  razorpaySubscriptionId: true, userId: true, checkoutAttemptId: true, providerStatus: true, reason: true, retiredAt: true,
} as const;

async function lockedRow(tx: Tx, ids: { id: string }[]): Promise<StoredSubscription | null> {
  if (ids.length === 0) return null;
  return tx.subscription.findUnique({ where: { id: ids[0].id }, select: STORED });
}

function webhookTx(tx: Tx): WebhookTx {
  return {
    async claimEvent(claim: EventClaim) {
      // INSERT ... ON CONFLICT DO NOTHING: a concurrent claim of the same
      // identity waits for the first transaction, then inserts nothing.
      const { count } = await tx.billingWebhookEvent.createMany({ data: [claim], skipDuplicates: true });
      return count === 1;
    },
    async lockByProviderId(razorpaySubscriptionId) {
      return lockedRow(tx, await tx.$queryRaw<{ id: string }[]>`SELECT id FROM subscriptions WHERE "razorpaySubscriptionId" = ${razorpaySubscriptionId} FOR UPDATE`);
    },
    async lockByCheckoutAttempt(checkoutAttemptId) {
      return lockedRow(tx, await tx.$queryRaw<{ id: string }[]>`SELECT id FROM subscriptions WHERE "checkoutAttemptId" = ${checkoutAttemptId} FOR UPDATE`);
    },
    async updateSubscription(rowId: string, patch: SubscriptionPatch) {
      await tx.subscription.update({ where: { id: rowId }, data: patch });
    },
    async findRetired(razorpaySubscriptionId) {
      return tx.retiredBillingSubscription.findUnique({ where: { razorpaySubscriptionId }, select: RETIRED });
    },
    async insertRetired(mapping) {
      await tx.retiredBillingSubscription.create({ data: mapping });
    },
    async finishEvent(id, outcome, reason, processedAt, userId = null) {
      await tx.billingWebhookEvent.update({ where: { id }, data: { outcome, reason, processedAt, userId } });
    },
  };
}

export function prismaBillingWebhookStore(client: Client, wrap: (tx: WebhookTx) => WebhookTx = (t) => t): WebhookStore {
  return {
    transaction: (fn) => client.$transaction((tx) => fn(wrap(webhookTx(tx))), TX_OPTIONS),
  };
}

const OPEN_CHECKOUT = { in: ["RESERVED", "UNRESOLVED"] as ("RESERVED" | "UNRESOLVED")[] };

export function prismaCheckoutStore(client: Client): CheckoutStore {
  return {
    reserve(clerkUserId, attemptId, now, decide) {
      return client.$transaction(async (tx) => {
        const users = await tx.$queryRaw<{ id: string }[]>`SELECT id FROM users WHERE "clerkId" = ${clerkUserId} FOR NO KEY UPDATE`;
        if (users.length === 0) return { kind: "no_account" as const };
        const userId = users[0].id;
        // Lock the subscription row too (users -> subscriptions, as everywhere), so
        // the decision cannot rest on a snapshot that a webhook holding the row
        // lock is about to change (e.g. a late payment on a retiring checkout).
        await tx.$queryRaw`SELECT id FROM subscriptions WHERE "userId" = ${userId} FOR UPDATE`;
        const row = await tx.subscription.findUnique({ where: { userId }, select: STORED });
        // Mappings are insert-only and this owner's reservations are serialised by
        // the users-row lock, so this read cannot go stale before the insert below.
        const archived = row?.razorpaySubscriptionId
          ? await tx.retiredBillingSubscription.findUnique({
            where: { razorpaySubscriptionId: row.razorpaySubscriptionId }, select: { userId: true, checkoutAttemptId: true },
          })
          : null;
        const decision = decide(row, archived);
        if (decision.kind === "reserve") {
          // Insert-only: never overwritten. A concurrent writer of the same key
          // (none exists in the app) would fail the primary key and roll back everything.
          if (decision.retire && !decision.retireRecorded) await tx.retiredBillingSubscription.create({ data: decision.retire });
          // Start a fresh attempt on the user's single row. The previous Razorpay
          // ID is cleared, so late events for an old subscription find no row.
          const fresh = {
            razorpaySubscriptionId: null, plan: "FREE" as const, status: "created", currentPeriodEnd: null,
            checkoutAttemptId: attemptId, checkoutStatus: "RESERVED" as const, checkoutStartedAt: now,
            paymentConfirmedAt: null, lastEventAt: null, lastEventType: null, endedAt: null,
            cancelMode: null, cancelRequestedAt: null, cancelSubscriptionId: null,
          };
          await tx.subscription.upsert({ where: { userId }, create: { userId, ...fresh }, update: fresh });
        }
        return { ...decision, userId };
      }, TX_OPTIONS);
    },
    async bind(attemptId, razorpaySubscriptionId) {
      try {
        const { count } = await client.subscription.updateMany({
          where: {
            checkoutAttemptId: attemptId,
            checkoutStatus: OPEN_CHECKOUT,
            OR: [{ razorpaySubscriptionId: null }, { razorpaySubscriptionId }],
          },
          data: { razorpaySubscriptionId, checkoutStatus: "CREATED" },
        });
        if (count === 1) return true;
      } catch {
        return false; // e.g. the ID is already mapped to a different row
      }
      // An early webhook may already have bound this exact ID.
      const row = await client.subscription.findUnique({ where: { checkoutAttemptId: attemptId }, select: { razorpaySubscriptionId: true } });
      return row?.razorpaySubscriptionId === razorpaySubscriptionId;
    },
    async markFailed(attemptId) {
      await client.subscription.updateMany({
        where: { checkoutAttemptId: attemptId, checkoutStatus: OPEN_CHECKOUT, razorpaySubscriptionId: null },
        data: { checkoutStatus: "FAILED" },
      });
    },
    async markUnresolved(attemptId) {
      await client.subscription.updateMany({
        where: { checkoutAttemptId: attemptId, checkoutStatus: "RESERVED" },
        data: { checkoutStatus: "UNRESOLVED" },
      });
    },
  };
}
