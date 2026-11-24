// Billing stores and migration against REAL PostgreSQL (opt-in).
//
// Runs only when SOLANAGUARD_TEST_DATABASE_URL points at a DISPOSABLE database;
// skipped otherwise. Never touches existing tables: it creates its own schema
// with a minimal users table, applies the real subscription and billing
// migrations there, and drops the schema afterwards. Fake Razorpay client
// only; identifiers are fake.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { createHmac, randomUUID } from "node:crypto";

import { computeEntitlement, handleRazorpayWebhook, startCheckout } from "./billing-core.ts";
import { prismaBillingWebhookStore, prismaCheckoutStore } from "./billing-store.ts";

const URL_ = process.env.SOLANAGUARD_TEST_DATABASE_URL;
const skip = URL_ ? false : "set SOLANAGUARD_TEST_DATABASE_URL to a DISPOSABLE database to run";
const DB_PKG = new URL("../../../../packages/database/", import.meta.url);
const migration = (name) => readFileSync(new URL(`prisma/migrations/${name}/migration.sql`, DB_PKG), "utf8");

const SECRET = "whsec_test_only_value_0123456789";
const T0 = 1_790_000_000;
const DAY = 86_400;
const schema = `billing_test_${randomUUID().replaceAll("-", "").slice(0, 12)}`;
let admin, prisma, pg;

// [key, plan, status, currentPeriodEnd offset | null] -> expected: backfilled?, PRO after migration?
const LEGACY_ROWS = [
  ["ProActive", "PRO", "active", "20 days"],      // the only shape the old webhook wrote on payment
  ["ProActiveLapsed", "PRO", "active", "-3 days"], // backfilled, but the period already ended
  ["ProActiveNoEnd", "PRO", "active", null],
  ["FreeActive", "FREE", "active", "20 days"],
  ["ProHalted", "PRO", "halted", "20 days"],
  ["ProCancelled", "PRO", "cancelled", "20 days"],
  ["ProExpired", "PRO", "expired", "20 days"],
  ["ProAuthenticated", "PRO", "authenticated", "20 days"],
  ["ProPending", "PRO", "pending", "20 days"],
  ["FreeCancelled", "FREE", "cancelled", null],
];
const BACKFILL_EXPECTED = { ProActive: [true, "PRO"], ProActiveLapsed: [true, "FREE"] }; // all others: [false, "FREE"]

before(async () => {
  if (skip) return;
  const require = createRequire(new URL("package.json", DB_PKG));
  pg = require("pg");
  const { PrismaPg } = require("@prisma/adapter-pg");
  const { PrismaClient } = await import(new URL("generated/client/client.ts", DB_PKG).href);
  admin = new pg.Client({ connectionString: URL_ });
  await admin.connect();
  await admin.query(`CREATE SCHEMA "${schema}"`);
  await admin.query(`SET search_path TO "${schema}"`);
  await admin.query(`CREATE TABLE users (id TEXT PRIMARY KEY, "clerkId" TEXT NOT NULL UNIQUE, email TEXT NOT NULL UNIQUE,
                     "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP, "telegramChatId" TEXT)`);
  await admin.query(migration("20260829095825_add_subscription"));
  // Legacy rows in every shape the previous webhook (or manual edits) could leave, to exercise the backfill.
  for (const [key, plan, status, periodEnd] of LEGACY_ROWS) {
    await admin.query(`INSERT INTO users (id, "clerkId", email) VALUES ($1, $2, $3)`, [`legacy_${key}`, `clerk_legacy_${key}`, `legacy_${key}@example.test`]);
    await admin.query(`INSERT INTO subscriptions (id, "userId", "razorpaySubscriptionId", plan, status, "currentPeriodEnd", "updatedAt")
                       VALUES ($1, $2, $3, $4::"PlanTier", $5, ${periodEnd === null ? "NULL" : `now() + interval '${periodEnd}'`}, now())`,
      [`legacy_s_${key}`, `legacy_${key}`, `sub_Legacy${key}`, plan, status]);
  }
  await admin.query(migration("20261123120000_billing_webhook_reliability"));
  await admin.query(`SET search_path TO public`);
  prisma = new PrismaClient({
    adapter: new PrismaPg({ connectionString: URL_, options: `-c search_path=${schema}`, max: 20 }, { schema }),
  });
});

after(async () => {
  if (skip) return;
  await prisma?.$disconnect();
  await admin.query(`DROP SCHEMA IF EXISTS "${schema}" CASCADE`);
  await admin.end();
});

const q = (sql, params) => admin.query(sql.replaceAll("$S", `"${schema}"`), params);

async function addUser({ sub } = {}) {
  const id = `u_${randomUUID().slice(0, 8)}`;
  await q(`INSERT INTO $S.users (id, "clerkId", email) VALUES ($1, $2, $3)`, [id, `clerk_${id}`, `${id}@example.test`]);
  if (sub) {
    await q(`INSERT INTO $S.subscriptions (id, "userId", "updatedAt", "razorpaySubscriptionId", status, "checkoutAttemptId", "checkoutStatus",
               "paymentConfirmedAt", "currentPeriodEnd", "lastEventAt")
             VALUES ($1, $2, now(), $3, $4, $5, $6::$S."CheckoutStatus",
                     to_timestamp($7) AT TIME ZONE 'UTC', to_timestamp($8) AT TIME ZONE 'UTC', to_timestamp($9) AT TIME ZONE 'UTC')`, // UTC, as Prisma stores it
      [`s_${id}`, id, sub.rid ?? null, sub.status ?? "created", sub.attempt ?? null, sub.attempt ? (sub.checkoutStatus ?? "CREATED") : null,
       sub.paid ? T0 : null, sub.paid ? T0 + 30 * DAY : null, sub.lastEventAt ?? null]);
  }
  return { id, clerkId: `clerk_${id}` };
}

const subRow = async (userId) => (await prisma.subscription.findUnique({ where: { userId } }));
const event = async (id) => (await q(`SELECT outcome, reason, "userId" FROM $S.billing_webhook_events WHERE id = $1`, [id])).rows[0];
const retiredRows = async (userId) => (await q(`SELECT "razorpaySubscriptionId", "userId", "checkoutAttemptId", "providerStatus", reason
  FROM $S.retired_billing_subscriptions WHERE "userId" = $1 ORDER BY "retiredAt", "razorpaySubscriptionId"`, [userId])).rows;
const nowSec = () => Math.floor(Date.now() / 1000);
/** Waits until `n` backends in this database are blocked on a lock (proves the interleaving). */
async function lockWaiters(n, timeoutMs = 5000) {
  const until = Date.now() + timeoutMs;
  for (;;) {
    const { rows } = await admin.query(`SELECT count(*)::int AS c FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'`);
    if (rows[0].c >= n) return;
    if (Date.now() > until) throw new Error(`expected ${n} lock waiter(s), saw ${rows[0].c}`);
    await new Promise((r) => setTimeout(r, 10));
  }
}
const enc = (s) => new TextEncoder().encode(s);

function body({ type, status, rid, createdAt = T0, paid = 1, notes = {} }) {
  return JSON.stringify({ event: type, created_at: createdAt, contains: ["subscription"],
    payload: { subscription: { entity: { id: rid, status, paid_count: paid, current_end: createdAt + 30 * DAY, notes } } } });
}

function deliver(text, eventId, store = prismaBillingWebhookStore(prisma), logs = []) {
  const raw = enc(text);
  const h = new Map([["x-razorpay-signature", createHmac("sha256", SECRET).update(raw).digest("hex")]]);
  if (eventId) h.set("x-razorpay-event-id", eventId);
  return handleRazorpayWebhook({ rawBody: raw, headers: { get: (n) => h.get(n) ?? null }, secret: SECRET, store,
    now: () => new Date((T0 + 60) * 1000), log: (l) => logs.push(l) });
}

const attempt = () => `ca_${randomUUID().replaceAll("-", "")}`; // 32 hex chars

test("migration backfill: only PRO + active + period-end rows are payment-confirmed; no broader access", { skip }, async () => {
  for (const [key] of LEGACY_ROWS) {
    const legacy = await subRow(`legacy_${key}`);
    const [backfilled, plan] = BACKFILL_EXPECTED[key] ?? [false, "FREE"];
    assert.equal(legacy.paymentConfirmedAt !== null, backfilled, key);
    assert.equal(computeEntitlement(legacy, new Date()).plan, plan, key);
    assert.deepEqual([legacy.checkoutAttemptId, legacy.lastEventAt, legacy.lastEventType], [null, null, null], key);
  }
});

test("migration: constraints hold", { skip }, async () => {
  // CHECKs: no free-text reasons, fallback IDs must be the body digest, attempt format.
  const bad = [
    `INSERT INTO $S.billing_webhook_events (id, "idSource", "eventType", "providerCreatedAt", "receivedAt", "bodySha256", reason)
     VALUES ('evt_1', 'PROVIDER_HEADER', 'x', now(), now(), '${"a".repeat(64)}', 'Free text with spaces')`,
    `INSERT INTO $S.billing_webhook_events (id, "idSource", "eventType", "providerCreatedAt", "receivedAt", "bodySha256")
     VALUES ('evt_2', 'BODY_SHA256', 'x', now(), now(), '${"a".repeat(64)}')`,
    `INSERT INTO $S.billing_webhook_events (id, "idSource", "eventType", "providerCreatedAt", "receivedAt", "bodySha256")
     VALUES ('evt_3', 'PROVIDER_HEADER', 'x', now(), now(), '{"raw":"body"}')`,
    `UPDATE $S.subscriptions SET "checkoutAttemptId" = 'not-an-attempt', "checkoutStatus" = 'RESERVED' WHERE id = 'legacy_s_ProActive'`,
    `INSERT INTO $S.billing_webhook_events (id, "idSource", "eventType", "receivedAt", "bodySha256")
     VALUES ('evt_4', 'PROVIDER_HEADER', 'subscription.charged', now(), '${"a".repeat(64)}')`, // only "unparsed" may lack a timestamp
    // Reconciliation entries always name an owner and a subscription.
    `INSERT INTO $S.billing_webhook_events (id, "idSource", "eventType", "providerCreatedAt", "receivedAt", "bodySha256", "razorpaySubscriptionId", outcome, "processedAt")
     VALUES ('evt_5', 'PROVIDER_HEADER', 'subscription.charged', now(), now(), '${"a".repeat(64)}', 'sub_X1', 'RECONCILIATION_REQUIRED', now())`,
    // Retired mappings: provider ID / attempt formats, final status, APP_CANCELLED => cancelled.
    `INSERT INTO $S.retired_billing_subscriptions VALUES ('not_a_sub', 'legacy_ProActive', NULL, 'cancelled', 'APP_CANCELLED', now())`,
    `INSERT INTO $S.retired_billing_subscriptions VALUES ('sub_Chk1', 'legacy_ProActive', 'bad', 'cancelled', 'APP_CANCELLED', now())`,
    `INSERT INTO $S.retired_billing_subscriptions VALUES ('sub_Chk2', 'legacy_ProActive', NULL, 'created', 'ALREADY_FINAL', now())`,
    `INSERT INTO $S.retired_billing_subscriptions VALUES ('sub_Chk3', 'legacy_ProActive', NULL, 'expired', 'APP_CANCELLED', now())`,
  ];
  for (const sql of bad) await assert.rejects(q(sql), /check constraint/, sql);
  // Foreign keys: a mapping's owner and an event's attributed user must exist.
  await assert.rejects(q(`INSERT INTO $S.retired_billing_subscriptions VALUES ('sub_Fk1', 'no_such_user', NULL, 'cancelled', 'APP_CANCELLED', now())`), /foreign key/);
  await q(`INSERT INTO $S.retired_billing_subscriptions VALUES ('sub_Fk2', 'legacy_FreeActive', NULL, 'expired', 'ALREADY_FINAL', now())`);
  await assert.rejects(q(`INSERT INTO $S.billing_webhook_events (id, "idSource", "eventType", "providerCreatedAt", "receivedAt", "bodySha256", "razorpaySubscriptionId", "userId")
     VALUES ('evt_6', 'PROVIDER_HEADER', 'subscription.charged', now(), now(), '${"a".repeat(64)}', 'sub_Fk2', 'no_such_user')`), /foreign key/);
  // A user is attributed only together with a subscription ID.
  await assert.rejects(q(`INSERT INTO $S.billing_webhook_events (id, "idSource", "eventType", "providerCreatedAt", "receivedAt", "bodySha256", "userId")
     VALUES ('evt_7', 'PROVIDER_HEADER', 'subscription.charged', now(), now(), '${"a".repeat(64)}', 'legacy_ProActive')`), /check constraint/);
  // A user with attributed billing events cannot be deleted either.
  await q(`INSERT INTO $S.billing_webhook_events (id, "idSource", "eventType", "providerCreatedAt", "receivedAt", "bodySha256", "razorpaySubscriptionId", "userId")
     VALUES ('evt_8', 'PROVIDER_HEADER', 'subscription.charged', now(), now(), '${"a".repeat(64)}', 'sub_Fk8', 'legacy_ProHalted')`);
  await assert.rejects(q(`DELETE FROM $S.users WHERE id = 'legacy_ProHalted'`), /foreign key/);
  await q(`DELETE FROM $S.billing_webhook_events WHERE id = 'evt_8'`);
  // Insert-only / unique: a second mapping for the same provider ID is rejected, and the owner cannot be deleted.
  await assert.rejects(q(`INSERT INTO $S.retired_billing_subscriptions VALUES ('sub_Fk2', 'legacy_ProActive', NULL, 'expired', 'ALREADY_FINAL', now())`), /duplicate key/);
  await assert.rejects(q(`DELETE FROM $S.users WHERE id = 'legacy_FreeActive'`), /foreign key/);
  await q(`DELETE FROM $S.retired_billing_subscriptions WHERE "razorpaySubscriptionId" = 'sub_Fk2'`);
});

test("concurrent duplicate deliveries are applied exactly once", { skip }, async () => {
  const u = await addUser({ sub: { rid: "sub_Dup1", status: "authenticated", attempt: attempt() } });
  const text = body({ type: "subscription.charged", status: "active", rid: "sub_Dup1" });
  const results = await Promise.all(Array.from({ length: 12 }, () => deliver(text, "evt_ConcurrentDup1")));
  const outcomes = results.map((r) => r.body.outcome).sort();
  assert.deepEqual(outcomes, ["duplicate", ...Array(10).fill("duplicate"), "processed"].sort());
  const { rows } = await q(`SELECT outcome, reason FROM $S.billing_webhook_events WHERE id = 'evt_ConcurrentDup1'`);
  assert.deepEqual(rows, [{ outcome: "PROCESSED", reason: "applied" }]);
  assert.equal(computeEntitlement(await subRow(u.id), new Date((T0 + 60) * 1000)).plan, "PRO");
});

test("a failure after the event insert rolls back the event and the subscription; the retry succeeds", { skip }, async () => {
  const u = await addUser({ sub: { rid: "sub_Fail1", status: "authenticated", attempt: attempt() } });
  const before = await subRow(u.id);
  const failing = prismaBillingWebhookStore(prisma, (tx) => ({ ...tx, async finishEvent() { throw new Error("injected"); } }));
  const text = body({ type: "subscription.charged", status: "active", rid: "sub_Fail1" });
  const r = await deliver(text, "evt_Fail1", failing);
  assert.equal(r.status, 503);
  assert.equal((await q(`SELECT count(*)::int n FROM $S.billing_webhook_events WHERE id = 'evt_Fail1'`)).rows[0].n, 0);
  assert.deepEqual(await subRow(u.id), before);
  const retry = await deliver(text, "evt_Fail1");
  assert.equal(retry.body.outcome, "processed");
});

test("racing distinct events: a terminal event is never undone by an older charge", { skip }, async () => {
  for (let round = 0; round < 5; round++) {
    const rid = `sub_Race${round}`;
    const u = await addUser({ sub: { rid, status: "active", attempt: attempt(), paid: true, lastEventAt: T0 } });
    await Promise.all([
      deliver(body({ type: "subscription.cancelled", status: "cancelled", rid, createdAt: T0 + 100 }), `evt_C${round}`),
      deliver(body({ type: "subscription.charged", status: "active", rid, createdAt: T0 + 50 }), `evt_A${round}`),
      deliver(body({ type: "subscription.halted", status: "halted", rid, createdAt: T0 + 70 }), `evt_H${round}`),
    ]);
    const s = await subRow(u.id);
    assert.equal(s.status, "cancelled", `round ${round}`);
    assert.equal(computeEntitlement(s, new Date((T0 + 200) * 1000)).plan, "FREE");
  }
});

test("early webhook binds by attempt ID; the checkout's own bind then agrees", { skip }, async () => {
  const a = attempt();
  const u = await addUser({ sub: { attempt: a, checkoutStatus: "RESERVED" } });
  const r = await deliver(body({ type: "subscription.authenticated", status: "authenticated", rid: "sub_Early1", paid: 0, notes: { userId: u.id, checkoutAttemptId: a } }), "evt_Early1");
  assert.equal(r.body.outcome, "processed");
  const store = prismaCheckoutStore(prisma);
  assert.equal(await store.bind(a, "sub_Early1"), true);
  assert.equal(await store.bind(a, "sub_SomethingElse"), false);
  const s = await subRow(u.id);
  assert.deepEqual([s.razorpaySubscriptionId, s.checkoutStatus, s.status], ["sub_Early1", "CREATED", "authenticated"]);
});

test("old (ended) subscription events after a new reservation never apply; wrong notes user never remaps", { skip }, async () => {
  const oldAttempt = attempt();
  const u = await addUser({ sub: { rid: "sub_OldGen", status: "cancelled", attempt: oldAttempt } });
  const other = await addUser();
  const provider = { calls: 0, async create() { this.calls++; return { id: "sub_NewGen" }; } };
  const r = await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider });
  assert.deepEqual(r, { ok: true, subscriptionId: "sub_NewGen" });
  const before = await subRow(u.id);
  const late = await deliver(body({ type: "subscription.charged", status: "active", rid: "sub_OldGen", notes: { userId: u.id, checkoutAttemptId: oldAttempt } }), "evt_OldGen");
  assert.equal(late.body.outcome, "reconciliation_required"); // archived on replacement: flagged, never applied
  const hijack = await deliver(body({ type: "subscription.charged", status: "active", rid: "sub_NewGen", notes: { userId: other.id } }), "evt_Hijack");
  assert.equal(hijack.body.outcome, "reconciliation_required"); // flagged for the LOCAL owner, never the notes user
  assert.deepEqual(await event("evt_Hijack"), { outcome: "RECONCILIATION_REQUIRED", reason: "notes_user_mismatch", userId: u.id });
  assert.deepEqual(await subRow(u.id), before);
  assert.equal(await subRow(other.id), null);
});

test("simultaneous checkouts for one user: one reservation, one remote create", { skip }, async () => {
  const u = await addUser();
  let release;
  const gate = new Promise((r) => { release = r; });
  const calls = [];
  const provider = { async create(notes) { calls.push(notes); await gate; return { id: "sub_Once1" }; } };
  const store = prismaCheckoutStore(prisma);
  const all = Promise.all(Array.from({ length: 8 }, () => startCheckout({ clerkUserId: u.clerkId, store, provider })));
  await new Promise((r) => setTimeout(r, 200));
  release();
  const results = await all;
  assert.equal(calls.length, 1);
  assert.equal(results.filter((r) => r.ok).length, 1);
  assert.ok(results.filter((r) => !r.ok).every((r) => r.reason === "in_progress"));
  const s = await subRow(u.id);
  assert.deepEqual([s.razorpaySubscriptionId, s.checkoutStatus, s.checkoutAttemptId], ["sub_Once1", "CREATED", calls[0].checkoutAttemptId]);
});

test("timeout: unresolved reservation persists and blocks any further remote creation", { skip }, async () => {
  const u = await addUser();
  const store = prismaCheckoutStore(prisma);
  let calls = 0;
  const provider = { create: () => { calls++; return new Promise(() => {}); } }; // never settles
  assert.deepEqual(await startCheckout({ clerkUserId: u.clerkId, store, provider, timeoutMs: 20 }), { ok: false, reason: "provider_unconfirmed" });
  assert.equal((await subRow(u.id)).checkoutStatus, "UNRESOLVED");
  assert.deepEqual(await startCheckout({ clerkUserId: u.clerkId, store, provider }), { ok: false, reason: "unresolved" });
  assert.equal(calls, 1);
});

test("provider rejection marks FAILED without a remote ID", { skip }, async () => {
  const u = await addUser();
  const store = prismaCheckoutStore(prisma);
  const r = await startCheckout({ clerkUserId: u.clerkId, store, provider: { create: async () => { throw { statusCode: 400, error: {} }; } } });
  assert.deepEqual(r, { ok: false, reason: "provider_rejected" });
  const s = await subRow(u.id);
  assert.deepEqual([s.checkoutStatus, s.razorpaySubscriptionId], ["FAILED", null]);
});

test("stored event rows hold no payload, signature, payment or contact data", { skip }, async () => {
  await addUser({ sub: { rid: "sub_Priv1", status: "authenticated", attempt: attempt() } });
  const text = JSON.stringify({ event: "subscription.charged", created_at: T0, payload: {
    subscription: { entity: { id: "sub_Priv1", status: "active", paid_count: 1, current_end: T0 + 30 * DAY, notes: [] } },
    payment: { entity: { id: "pay_PRIVATE1", email: "p@example.test", contact: "+910000000001", amount: 49900 } } } });
  const logs = [];
  await deliver(text, "evt_Priv1", undefined, logs);
  const { rows } = await q(`SELECT * FROM $S.billing_webhook_events WHERE id = 'evt_Priv1'`);
  const dump = JSON.stringify(rows) + logs.join("\n");
  for (const s of ["pay_PRIVATE1", "p@example.test", "+910000000001", "49900", SECRET, '"payload"']) assert.ok(!dump.includes(s), s);
});

test("signed malformed bodies are recorded (no payload) and acknowledged; redelivery is a duplicate", { skip }, async () => {
  const logs = [];
  const r1 = await deliver('{"email":"m@example.test", broken', "evt_Malformed1", undefined, logs);
  const r2 = await deliver(JSON.stringify({ event: "subscription.charged", created_at: T0, payload: {} }), null, undefined, logs);
  assert.deepEqual([r1.status, r1.body.outcome, r2.status, r2.body.outcome], [200, "ignored", 200, "ignored"]);
  assert.equal((await deliver('{"email":"m@example.test", broken', "evt_Malformed1")).body.outcome, "duplicate");
  const { rows } = await q(`SELECT * FROM $S.billing_webhook_events WHERE "eventType" = 'unparsed' ORDER BY reason`);
  assert.deepEqual(rows.map((e) => [e.reason, e.outcome, e.providerCreatedAt, e.idSource]),
    [["invalid_event", "IGNORED", null, "BODY_SHA256"], ["malformed_json", "IGNORED", null, "PROVIDER_HEADER"]]);
  assert.ok(!(JSON.stringify(rows) + logs.join("\n")).includes("m@example.test"));
});

test("same-second paused -> resumed restores access in the database", { skip }, async () => {
  const u = await addUser({ sub: { rid: "sub_PauseRes1", status: "active", attempt: attempt(), paid: true, lastEventAt: T0 } });
  const S = T0 + 500;
  assert.equal((await deliver(body({ type: "subscription.paused", status: "paused", rid: "sub_PauseRes1", createdAt: S }), "evt_PR_p")).body.outcome, "processed");
  assert.equal((await deliver(body({ type: "subscription.resumed", status: "active", rid: "sub_PauseRes1", createdAt: S }), "evt_PR_r")).body.outcome, "processed");
  assert.equal((await deliver(body({ type: "subscription.paused", status: "paused", rid: "sub_PauseRes1", createdAt: S }), "evt_PR_p2")).body.outcome, "ignored");
  const s = await subRow(u.id);
  assert.deepEqual([s.status, s.lastEventType], ["active", "subscription.resumed"]);
  assert.equal(computeEntitlement(s, new Date((S + 60) * 1000)).plan, "PRO");
});

async function addStaleCheckout(rid) {
  const a = attempt();
  const u = await addUser({ sub: { rid, status: "created", attempt: a } });
  await q(`UPDATE $S.subscriptions SET "checkoutStartedAt" = (now() AT TIME ZONE 'UTC') - interval '3 days' WHERE "userId" = $1`, [u.id]);
  return { ...u, attempt: a };
}

test("stale unpaid checkout: late payment during retirement keeps the old mapping and grants access", { skip }, async () => {
  const u = await addStaleCheckout("sub_StaleDb1");
  let creates = 0;
  const provider = {
    create: async () => { creates++; return { id: "sub_NeverDb" }; },
    fetch: async () => ({ status: "created", paid_count: 0 }),
    cancel: async () => {
      // The customer pays on the OLD link; its webhook commits before our cancel returns.
      await deliver(body({ type: "subscription.charged", status: "active", rid: "sub_StaleDb1", createdAt: Math.floor(Date.now() / 1000) - 5,
        notes: { userId: u.id, checkoutAttemptId: u.attempt } }), "evt_StaleLatePay");
      return { status: "cancelled", paid_count: 0 };
    },
  };
  const r = await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider });
  assert.deepEqual(r, { ok: false, reason: "already_pro" });
  assert.equal(creates, 0);
  const s = await subRow(u.id);
  assert.deepEqual([s.razorpaySubscriptionId, s.status], ["sub_StaleDb1", "active"]);
});

test("stale unpaid checkout confirmed cancelled is replaced; its late non-payment events are ignored", { skip }, async () => {
  const u = await addStaleCheckout("sub_StaleDb2");
  const provider = { create: async () => ({ id: "sub_FreshDb2" }), fetch: async () => ({ status: "created", paid_count: 0 }), cancel: async () => ({ status: "cancelled", paid_count: 0 }) };
  assert.deepEqual(await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider }), { ok: true, subscriptionId: "sub_FreshDb2" });
  const before = await subRow(u.id);
  const late = await deliver(body({ type: "subscription.cancelled", status: "cancelled", rid: "sub_StaleDb2", paid: 0, notes: { userId: u.id, checkoutAttemptId: u.attempt } }), "evt_StaleDb2Cancel");
  assert.equal(late.body.outcome, "ignored");
  assert.deepEqual(await event("evt_StaleDb2Cancel"), { outcome: "IGNORED", reason: "retired_subscription", userId: u.id });
  assert.deepEqual(await subRow(u.id), before);
});

test("stale unpaid checkout: provider failure keeps the old mapping", { skip }, async () => {
  const u = await addStaleCheckout("sub_StaleDb3");
  const before = await subRow(u.id);
  const provider = { create: async () => ({ id: "sub_NeverDb3" }), fetch: async () => { throw new TypeError("network"); }, cancel: async () => ({ status: "cancelled", paid_count: 0 }) };
  assert.deepEqual(await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider }), { ok: false, reason: "provider_unconfirmed" });
  assert.deepEqual(await subRow(u.id), before);
});

// ---------------------------------------------------------------------------
// Retired subscription reconciliation
// ---------------------------------------------------------------------------

const unpaidProvider = (freshId) => ({
  creates: 0,
  async create() { this.creates++; return { id: freshId }; },
  fetch: async () => ({ status: "created", paid_count: 0 }),
  cancel: async () => ({ status: "cancelled", paid_count: 0 }),
});

test("retirement persists original owner and attempt; later checkouts never overwrite it", { skip }, async () => {
  const u = await addStaleCheckout("sub_RetA1");
  const store = prismaCheckoutStore(prisma);
  assert.deepEqual(await startCheckout({ clerkUserId: u.clerkId, store, provider: unpaidProvider("sub_RetA2") }), { ok: true, subscriptionId: "sub_RetA2" });
  const first = await retiredRows(u.id);
  assert.deepEqual(first, [{ razorpaySubscriptionId: "sub_RetA1", userId: u.id, checkoutAttemptId: u.attempt, providerStatus: "cancelled", reason: "APP_CANCELLED" }]);
  const second = await subRow(u.id);
  assert.notEqual(second.checkoutAttemptId, u.attempt);
  // The second checkout goes stale and is retired too: a second mapping, the first unchanged.
  await q(`UPDATE $S.subscriptions SET "checkoutStartedAt" = (now() AT TIME ZONE 'UTC') - interval '3 days' WHERE "userId" = $1`, [u.id]);
  assert.deepEqual(await startCheckout({ clerkUserId: u.clerkId, store, provider: unpaidProvider("sub_RetA3") }), { ok: true, subscriptionId: "sub_RetA3" });
  const both = await retiredRows(u.id);
  assert.deepEqual(both[0], first[0]);
  assert.deepEqual(both.map((r) => [r.razorpaySubscriptionId, r.checkoutAttemptId]).sort(), [["sub_RetA1", u.attempt], ["sub_RetA2", second.checkoutAttemptId]].sort());
  // Re-pointing the current row (a different attempt) at an already-retired ID: conflicting ownership,
  // the reservation is abandoned with nothing written.
  await q(`UPDATE $S.subscriptions SET "razorpaySubscriptionId" = 'sub_RetA1', status = 'created', "checkoutStatus" = 'CREATED',
           "checkoutStartedAt" = (now() AT TIME ZONE 'UTC') - interval '3 days' WHERE "userId" = $1`, [u.id]);
  const beforeDupe = await subRow(u.id);
  const p4 = unpaidProvider("sub_RetNever");
  assert.deepEqual(await startCheckout({ clerkUserId: u.clerkId, store, provider: p4 }), { ok: false, reason: "needs_attention" });
  assert.equal(p4.creates, 0);
  assert.deepEqual(await subRow(u.id), beforeDupe);
  assert.deepEqual((await retiredRows(u.id))[0], first[0]);
});

test("late events for a retired subscription: payment flagged for the original owner; nothing applied; idempotent", { skip }, async () => {
  const u = await addStaleCheckout("sub_RetB1");
  const provider = unpaidProvider("sub_RetB2");
  assert.equal((await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider })).ok, true);
  // The new subscription gets paid normally.
  const cur = await subRow(u.id);
  assert.equal((await deliver(body({ type: "subscription.charged", status: "active", rid: "sub_RetB2", createdAt: nowSec() - 10,
    notes: { userId: u.id, checkoutAttemptId: cur.checkoutAttemptId } }), "evt_RetB_new")).body.outcome, "processed");
  const before = await subRow(u.id);
  const notes = { userId: u.id, checkoutAttemptId: u.attempt };
  // Concurrent duplicate deliveries of the late charge.
  const charge = body({ type: "subscription.charged", status: "active", rid: "sub_RetB1", createdAt: nowSec(), notes });
  const results = await Promise.all(Array.from({ length: 8 }, () => deliver(charge, "evt_RetB_pay")));
  assert.deepEqual(results.map((r) => r.body.outcome).sort(), [...Array(7).fill("duplicate"), "reconciliation_required"].sort());
  assert.deepEqual(await event("evt_RetB_pay"), { outcome: "RECONCILIATION_REQUIRED", reason: "retired_payment", userId: u.id });
  // Non-payment events: recorded with the owner, ignored.
  for (const [type, status, id] of [["subscription.activated", "active", "evt_RetB_act"], ["subscription.cancelled", "cancelled", "evt_RetB_can"],
    ["subscription.authenticated", "authenticated", "evt_RetB_auth"]]) {
    assert.equal((await deliver(body({ type, status, rid: "sub_RetB1", createdAt: nowSec() + 5, paid: 0, notes }), id)).body.outcome, "ignored", type);
    assert.equal((await event(id)).userId, u.id);
  }
  assert.deepEqual(await subRow(u.id), before); // current subscription untouched
  assert.equal(computeEntitlement(before, new Date()).plan, "PRO"); // access is the NEW subscription's, not revived/removed
  assert.equal(provider.creates, 1);
  // Unknown provider IDs stay ignored, with no user association.
  assert.equal((await deliver(body({ type: "subscription.charged", status: "active", rid: "sub_RetBNobody", notes }), "evt_RetB_unknown")).body.outcome, "ignored");
  assert.deepEqual(await event("evt_RetB_unknown"), { outcome: "IGNORED", reason: "unknown_subscription", userId: null });
  // The operator query from the migration header finds exactly the flagged payment.
  const { rows } = await q(`SELECT e.id, r."userId", r."checkoutAttemptId" FROM $S.billing_webhook_events e
    JOIN $S.retired_billing_subscriptions r USING ("razorpaySubscriptionId", "userId") WHERE e.outcome = 'RECONCILIATION_REQUIRED' AND r."userId" = $1`, [u.id]);
  assert.deepEqual(rows, [{ id: "evt_RetB_pay", userId: u.id, checkoutAttemptId: u.attempt }]);
});

test("a late retired payment for a user without a new checkout never grants Pro", { skip }, async () => {
  const u = await addStaleCheckout("sub_RetC1");
  // Retire, but the new create is rejected: the row stays FAILED and FREE.
  const provider = { ...unpaidProvider(), create: async () => { throw { statusCode: 400, error: {} }; } };
  assert.deepEqual(await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider }), { ok: false, reason: "provider_rejected" });
  const before = await subRow(u.id);
  assert.equal((await deliver(body({ type: "subscription.charged", status: "active", rid: "sub_RetC1", createdAt: nowSec(), notes: { userId: u.id, checkoutAttemptId: u.attempt } }), "evt_RetC")).body.outcome, "reconciliation_required");
  assert.deepEqual(await subRow(u.id), before);
  assert.equal(computeEntitlement(await subRow(u.id), new Date()).plan, "FREE");
});

for (let round = 0; round < 5; round++) {
  test(`race, webhook holds the row lock first: retirement waits, re-decides and does not detach (round ${round})`, { skip }, async () => {
    const rid = `sub_RaceW${round}`;
    const u = await addStaleCheckout(rid);
    let locked, release;
    const lockedP = new Promise((r) => { locked = r; });
    const gate = new Promise((r) => { release = r; });
    // Pause the webhook transaction right after it has locked the subscription row.
    const paused = prismaBillingWebhookStore(prisma, (tx) => ({ ...tx,
      async lockByProviderId(id) { const row = await tx.lockByProviderId(id); locked(); await gate; return row; } }));
    let webhook;
    const provider = {
      creates: 0, async create() { this.creates++; return { id: `sub_RaceWNew${round}` }; },
      fetch: async () => ({ status: "created", paid_count: 0 }),
      cancel: async () => {
        webhook = deliver(body({ type: "subscription.charged", status: "active", rid, createdAt: nowSec(), notes: { userId: u.id, checkoutAttemptId: u.attempt } }), `evt_RaceW${round}`, paused);
        await lockedP;
        return { status: "cancelled", paid_count: 0 }; // provider lag: the cancel response does not show the payment yet
      },
    };
    const checkout = startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider });
    await lockWaiters(1); // the retirement transaction is blocked on the subscription row
    release();
    assert.deepEqual(await checkout, { ok: false, reason: "already_pro" });
    assert.equal((await webhook).body.outcome, "processed");
    const s = await subRow(u.id);
    assert.deepEqual([s.razorpaySubscriptionId, s.checkoutAttemptId, s.status], [rid, u.attempt, "active"]);
    assert.deepEqual(await retiredRows(u.id), []);
    assert.equal(provider.creates, 0);
  });

  test(`race, retirement holds the row lock first: the waiting webhook is flagged against the retired mapping (round ${round})`, { skip }, async () => {
    const rid = `sub_RaceR${round}`;
    const u = await addStaleCheckout(rid);
    // A separate connection holds an uncommitted insert of the same mapping key, so the
    // retirement transaction stops INSIDE itself, after locking the users and subscription rows.
    const blocker = new pg.Client({ connectionString: URL_ });
    await blocker.connect();
    await blocker.query(`SET search_path TO "${schema}"`);
    await blocker.query("BEGIN");
    // Owned by an unrelated user: its FK key-share lock must not touch this user's row.
    await blocker.query(`INSERT INTO retired_billing_subscriptions VALUES ($1, 'legacy_FreeCancelled', NULL, 'expired', 'ALREADY_FINAL', now())`, [rid]);
    try {
      const provider = unpaidProvider(`sub_RaceRNew${round}`);
      const checkout = startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider });
      await lockWaiters(1); // retirement waits on the mapping key while holding the row lock
      const webhook = deliver(body({ type: "subscription.charged", status: "active", rid, createdAt: nowSec(), notes: { userId: u.id, checkoutAttemptId: u.attempt } }), `evt_RaceR${round}`);
      await lockWaiters(2); // the webhook waits on the subscription row
      await blocker.query("ROLLBACK");
      assert.deepEqual(await checkout, { ok: true, subscriptionId: `sub_RaceRNew${round}` });
      assert.equal((await webhook).body.outcome, "reconciliation_required");
      assert.deepEqual(await event(`evt_RaceR${round}`), { outcome: "RECONCILIATION_REQUIRED", reason: "retired_payment", userId: u.id });
      const s = await subRow(u.id);
      assert.deepEqual([s.razorpaySubscriptionId, s.status, s.paymentConfirmedAt], [`sub_RaceRNew${round}`, "created", null]);
      assert.equal((await retiredRows(u.id))[0].checkoutAttemptId, u.attempt);
    } finally {
      await blocker.end();
    }
  });
}

test("payment already recorded before retirement (paid, then cancelled) prevents silent detachment", { skip }, async () => {
  const u = await addStaleCheckout("sub_RetD1");
  const notes = { userId: u.id, checkoutAttemptId: u.attempt };
  const provider = {
    creates: 0, async create() { this.creates++; return { id: "sub_RetDNever" }; },
    fetch: async () => ({ status: "created", paid_count: 0 }),
    cancel: async () => {
      // Charge and then our own cancellation both land before the retirement transaction.
      await deliver(body({ type: "subscription.charged", status: "active", rid: "sub_RetD1", createdAt: nowSec() - 2, notes }), "evt_RetD_pay");
      await deliver(body({ type: "subscription.cancelled", status: "cancelled", rid: "sub_RetD1", createdAt: nowSec() - 1, notes }), "evt_RetD_can");
      return { status: "cancelled", paid_count: 0 }; // stale count: must not be trusted over the recorded payment
    },
  };
  assert.deepEqual(await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider }), { ok: false, reason: "needs_attention" });
  const s = await subRow(u.id);
  assert.deepEqual([s.razorpaySubscriptionId, s.checkoutAttemptId, s.status, s.paymentConfirmedAt !== null], ["sub_RetD1", u.attempt, "cancelled", true]);
  assert.deepEqual(await retiredRows(u.id), []);
  assert.equal(provider.creates, 0);
});

test("provider response with a paid cycle is never an ordinary retirement", { skip }, async () => {
  for (const [i, fetchR, cancelR] of [[1, { status: "created", paid_count: 0 }, { status: "cancelled", paid_count: 1 }], [2, { status: "cancelled", paid_count: 1 }, null]]) {
    const u = await addStaleCheckout(`sub_RetE${i}`);
    const before = await subRow(u.id);
    const provider = { create: async () => ({ id: "sub_RetENever" }), fetch: async () => fetchR, cancel: async () => cancelR };
    assert.deepEqual(await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider }), { ok: false, reason: "needs_attention" });
    assert.deepEqual(await subRow(u.id), before);
    assert.deepEqual(await retiredRows(u.id), []);
  }
});

// ---------------------------------------------------------------------------
// Archiving subscriptions that ended via webhooks
// ---------------------------------------------------------------------------

async function addEnded(rid, status = "cancelled", { withAttempt = true } = {}) {
  const a = withAttempt ? attempt() : null;
  const u = await addUser({ sub: { rid, status, attempt: a ?? undefined, lastEventAt: T0 } });
  return { ...u, attempt: a };
}
const createOnly = (freshId) => ({
  creates: 0, ops: [],
  async create() { this.creates++; return { id: freshId }; },
  async fetch() { this.ops.push("fetch"); throw new Error("not expected"); },
  async cancel() { this.ops.push("cancel"); throw new Error("not expected"); },
});

test("ended subscriptions are archived (ALREADY_FINAL, original owner + attempt) when a replacement is reserved", { skip }, async () => {
  for (const status of ["cancelled", "completed", "expired"]) {
    const rid = `sub_Arch${status}`;
    const u = await addEnded(rid, status);
    const provider = createOnly(`sub_ArchNew${status}`);
    assert.deepEqual(await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider }), { ok: true, subscriptionId: `sub_ArchNew${status}` });
    assert.deepEqual(await retiredRows(u.id), [{ razorpaySubscriptionId: rid, userId: u.id, checkoutAttemptId: u.attempt, providerStatus: status, reason: "ALREADY_FINAL" }]);
    assert.deepEqual(provider.ops, []);
    const s = await subRow(u.id);
    assert.equal(s.razorpaySubscriptionId, `sub_ArchNew${status}`);
    assert.notEqual(s.checkoutAttemptId, u.attempt);
  }
  // Legacy row (no attempt ID, written by the previous webhook) is archived with a null attempt.
  const legacy = await addEnded("sub_ArchLegacy", "cancelled", { withAttempt: false });
  assert.equal((await startCheckout({ clerkUserId: legacy.clerkId, store: prismaCheckoutStore(prisma), provider: createOnly("sub_ArchLegacyNew") })).ok, true);
  assert.equal((await retiredRows(legacy.id))[0].checkoutAttemptId, null);
});

test("late charged for an archived ended subscription is flagged for the old account; the new one is untouched", { skip }, async () => {
  const u = await addEnded("sub_ArchPay1");
  assert.equal((await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider: createOnly("sub_ArchPay2") })).ok, true);
  const before = await subRow(u.id);
  const charge = body({ type: "subscription.charged", status: "active", rid: "sub_ArchPay1", createdAt: nowSec(), notes: { userId: u.id, checkoutAttemptId: u.attempt } });
  const results = await Promise.all(Array.from({ length: 6 }, () => deliver(charge, "evt_ArchPay")));
  assert.deepEqual(results.map((r) => r.body.outcome).sort(), [...Array(5).fill("duplicate"), "reconciliation_required"].sort());
  assert.deepEqual(await event("evt_ArchPay"), { outcome: "RECONCILIATION_REQUIRED", reason: "retired_payment", userId: u.id });
  assert.deepEqual(await subRow(u.id), before);
  assert.equal(computeEntitlement(await subRow(u.id), new Date()).plan, "FREE");
});

test("existing mapping with matching ownership is kept unchanged; conflicting ownership fails atomically", { skip }, async () => {
  // Matching: same owner and attempt, recorded earlier with different details -> kept exactly, reservation proceeds.
  const ok = await addEnded("sub_ArchKeep1", "expired");
  await q(`INSERT INTO $S.retired_billing_subscriptions VALUES ('sub_ArchKeep1', $1, $2, 'cancelled', 'APP_CANCELLED', now() - interval '30 days')`, [ok.id, ok.attempt]);
  const keepSql = `SELECT * FROM $S.retired_billing_subscriptions WHERE "razorpaySubscriptionId" = 'sub_ArchKeep1'`;
  const keptBefore = (await q(keepSql)).rows;
  assert.equal((await startCheckout({ clerkUserId: ok.clerkId, store: prismaCheckoutStore(prisma), provider: createOnly("sub_ArchKeep2") })).ok, true);
  assert.deepEqual((await q(keepSql)).rows, keptBefore); // still APP_CANCELLED / cancelled / original timestamp
  assert.equal((await subRow(ok.id)).razorpaySubscriptionId, "sub_ArchKeep2");
  // Conflicting: another user, another attempt, or a null attempt -> needs_attention; row and mapping unchanged; no create.
  const other = await addUser();
  for (const [i, owner, att] of [[1, "other", "same"], [2, "same", "new"], [3, "same", null]]) {
    const rid = `sub_ArchClash${i}`;
    const u = await addEnded(rid);
    await q(`INSERT INTO $S.retired_billing_subscriptions VALUES ($1, $2, $3, 'cancelled', 'ALREADY_FINAL', now())`,
      [rid, owner === "other" ? other.id : u.id, att === "same" ? u.attempt : att === "new" ? attempt() : null]);
    const mapBefore = (await q(`SELECT * FROM $S.retired_billing_subscriptions WHERE "razorpaySubscriptionId" = $1`, [rid])).rows;
    const rowBefore = await subRow(u.id);
    const provider = createOnly("sub_ArchClashNever");
    assert.deepEqual(await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider }), { ok: false, reason: "needs_attention" }, `case ${i}`);
    assert.equal(provider.creates, 0);
    assert.deepEqual(await subRow(u.id), rowBefore);
    assert.deepEqual((await q(`SELECT * FROM $S.retired_billing_subscriptions WHERE "razorpaySubscriptionId" = $1`, [rid])).rows, mapBefore);
  }
});

test("non-terminal subscriptions are not archived by a new checkout", { skip }, async () => {
  for (const status of ["authenticated", "active", "pending", "halted", "paused"]) {
    const rid = `sub_ArchLive${status}`;
    const u = await addEnded(rid, status);
    const before = await subRow(u.id);
    const provider = createOnly("sub_ArchLiveNever");
    assert.deepEqual(await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider }), { ok: false, reason: "needs_attention" }, status);
    assert.deepEqual(await subRow(u.id), before);
    assert.deepEqual(await retiredRows(u.id), []);
    assert.equal(provider.creates, 0);
  }
});

for (let round = 0; round < 5; round++) {
  test(`race, late charge holds the ended row first: it archives + flags, the waiting reservation keeps that mapping (round ${round})`, { skip }, async () => {
    const rid = `sub_ArchRaceW${round}`;
    const u = await addEnded(rid);
    let locked, release;
    const lockedP = new Promise((r) => { locked = r; });
    const gate = new Promise((r) => { release = r; });
    const paused = prismaBillingWebhookStore(prisma, (tx) => ({ ...tx,
      async lockByProviderId(id) { const row = await tx.lockByProviderId(id); locked(); await gate; return row; } }));
    const notes = { userId: u.id, checkoutAttemptId: u.attempt };
    const first = deliver(body({ type: "subscription.charged", status: "active", rid, createdAt: T0 + 5, notes }), `evt_ArchRaceW1_${round}`, paused);
    await lockedP;
    const checkout = startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider: createOnly(`sub_ArchRaceWNew${round}`) });
    await lockWaiters(1); // the reservation holds the users row and is blocked on the subscription row
    release(); // the webhook now inserts the mapping: its FK takes KEY SHARE on that same users row
    assert.equal((await first).body.outcome, "reconciliation_required");
    assert.deepEqual(await event(`evt_ArchRaceW1_${round}`), { outcome: "RECONCILIATION_REQUIRED", reason: "terminal_payment", userId: u.id });
    const byWebhook = (await q(`SELECT * FROM $S.retired_billing_subscriptions WHERE "razorpaySubscriptionId" = $1`, [rid])).rows;
    assert.deepEqual(await checkout, { ok: true, subscriptionId: `sub_ArchRaceWNew${round}` });
    assert.deepEqual((await q(`SELECT * FROM $S.retired_billing_subscriptions WHERE "razorpaySubscriptionId" = $1`, [rid])).rows, byWebhook); // kept, not re-inserted
    assert.equal(byWebhook[0].checkoutAttemptId, u.attempt);
    // The next late payment resolves through the archive.
    assert.equal((await deliver(body({ type: "subscription.charged", status: "active", rid, createdAt: T0 + 6, notes }), `evt_ArchRaceW2_${round}`)).body.outcome, "reconciliation_required");
    assert.equal((await subRow(u.id)).razorpaySubscriptionId, `sub_ArchRaceWNew${round}`);
  });

  test(`race, reservation holds the ended row first: the waiting webhook resolves through the archive (round ${round})`, { skip }, async () => {
    const rid = `sub_ArchRaceR${round}`;
    const u = await addEnded(rid);
    const blocker = new pg.Client({ connectionString: URL_ });
    await blocker.connect();
    await blocker.query(`SET search_path TO "${schema}"`);
    await blocker.query("BEGIN");
    await blocker.query(`INSERT INTO retired_billing_subscriptions VALUES ($1, 'legacy_FreeCancelled', NULL, 'expired', 'ALREADY_FINAL', now())`, [rid]);
    try {
      const checkout = startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider: createOnly(`sub_ArchRaceRNew${round}`) });
      await lockWaiters(1); // reservation holds users + subscription rows, waits on the mapping key
      const webhook = deliver(body({ type: "subscription.charged", status: "active", rid, createdAt: nowSec(), notes: { userId: u.id, checkoutAttemptId: u.attempt } }), `evt_ArchRaceR${round}`);
      await lockWaiters(2); // webhook waits on the subscription row
      await blocker.query("ROLLBACK");
      assert.deepEqual(await checkout, { ok: true, subscriptionId: `sub_ArchRaceRNew${round}` });
      assert.equal((await webhook).body.outcome, "reconciliation_required");
      assert.deepEqual(await event(`evt_ArchRaceR${round}`), { outcome: "RECONCILIATION_REQUIRED", reason: "retired_payment", userId: u.id });
      assert.equal((await retiredRows(u.id))[0].userId, u.id);
    } finally {
      await blocker.end();
    }
  });
}

test("uncoordinated storm: concurrent reservations and late webhooks for ended subscriptions (no deadlock, no lost mapping)", { skip }, async () => {
  const users = await Promise.all(Array.from({ length: 6 }, (_, i) => addEnded(`sub_ArchStorm${i}`)));
  const work = [];
  users.forEach((u, i) => {
    const notes = { userId: u.id, checkoutAttemptId: u.attempt };
    for (let k = 0; k < 3; k++) work.push(startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider: createOnly(`sub_ArchStormNew${i}`) }));
    for (let k = 0; k < 4; k++) {
      work.push(deliver(body({ type: "subscription.charged", status: "active", rid: `sub_ArchStorm${i}`, createdAt: T0 + 10 + k, notes }), `evt_ArchStorm${i}_${k}`));
    }
  });
  const results = await Promise.all(work);
  assert.ok(results.every((r) => r.status === undefined || r.status === 200), "no webhook needed a retry");
  for (const [i, u] of users.entries()) {
    const s = await subRow(u.id);
    assert.equal(s.razorpaySubscriptionId, `sub_ArchStormNew${i}`);
    assert.deepEqual((await retiredRows(u.id)).map((r) => [r.razorpaySubscriptionId, r.checkoutAttemptId]), [[`sub_ArchStorm${i}`, u.attempt]]);
    const { rows } = await q(`SELECT outcome, reason, "userId" FROM $S.billing_webhook_events WHERE "razorpaySubscriptionId" = $1`, [`sub_ArchStorm${i}`]);
    assert.equal(rows.length, 4);
    for (const e of rows) {
      // Before replacement: terminal_payment on the still-mapped ended row. After: retired_payment. Always the original owner.
      assert.ok(e.outcome === "RECONCILIATION_REQUIRED" && ["terminal_payment", "retired_payment"].includes(e.reason) && e.userId === u.id, JSON.stringify(e));
    }
    assert.equal(computeEntitlement(s, new Date()).plan, "FREE");
  }
});

// ---------------------------------------------------------------------------
// Late charge on a terminal subscription that is still the current mapping
// ---------------------------------------------------------------------------

async function addTerminalPaid(rid, status = "cancelled") {
  const a = attempt();
  const u = await addUser({ sub: { rid, status, attempt: a, paid: true, lastEventAt: T0 + 100 } });
  return { ...u, attempt: a, notes: { userId: u.id, checkoutAttemptId: a } };
}
const mappingRow = async (rid) => (await q(`SELECT * FROM $S.retired_billing_subscriptions WHERE "razorpaySubscriptionId" = $1`, [rid])).rows;

test("terminal, then late charges before any replacement: archived + flagged once per event; row unchanged; no Pro", { skip }, async () => {
  const u = await addTerminalPaid("sub_Term1");
  const before = await subRow(u.id);
  const charge = body({ type: "subscription.charged", status: "active", rid: "sub_Term1", createdAt: T0 + 50, notes: u.notes });
  const rs = await Promise.all(Array.from({ length: 8 }, () => deliver(charge, "evt_Term1_a")));
  assert.deepEqual(rs.map((r) => r.body.outcome).sort(), [...Array(7).fill("duplicate"), "reconciliation_required"].sort());
  assert.deepEqual(await event("evt_Term1_a"), { outcome: "RECONCILIATION_REQUIRED", reason: "terminal_payment", userId: u.id });
  assert.deepEqual(await retiredRows(u.id), [{ razorpaySubscriptionId: "sub_Term1", userId: u.id, checkoutAttemptId: u.attempt, providerStatus: "cancelled", reason: "ALREADY_FINAL" }]);
  // Concurrent DISTINCT late charges serialise on the row lock: one mapping, every event flagged.
  const distinct = await Promise.all(Array.from({ length: 6 }, (_, i) =>
    deliver(body({ type: "subscription.charged", status: "active", rid: "sub_Term1", createdAt: T0 + 60 + i, notes: u.notes }), `evt_Term1_d${i}`)));
  assert.ok(distinct.every((r) => r.body.outcome === "reconciliation_required"));
  assert.equal((await retiredRows(u.id)).length, 1);
  assert.deepEqual(await subRow(u.id), before);
  assert.equal(computeEntitlement(await subRow(u.id), new Date((T0 + 200) * 1000)).plan, "FREE");
});

test("historical paid_count on terminal / status events and authentication stay terminal_state; nothing archived", { skip }, async () => {
  const u = await addTerminalPaid("sub_Term2", "completed");
  for (const [type, status, paid, id] of [["subscription.cancelled", "cancelled", 7, "evt_T2_c"], ["subscription.completed", "completed", 7, "evt_T2_done"],
    ["subscription.activated", "active", 1, "evt_T2_act"], ["subscription.authenticated", "authenticated", 0, "evt_T2_auth"]]) {
    assert.equal((await deliver(body({ type, status, rid: "sub_Term2", createdAt: T0 + 150, paid, notes: u.notes }), id)).body.outcome, "ignored", type);
    assert.deepEqual(await event(id), { outcome: "IGNORED", reason: "terminal_state", userId: null }, type);
  }
  assert.deepEqual(await retiredRows(u.id), []);
});

test("a replacement checkout after a flagged late charge keeps that mapping; the newer subscription stays untouched", { skip }, async () => {
  const u = await addTerminalPaid("sub_Term3");
  await deliver(body({ type: "subscription.charged", status: "active", rid: "sub_Term3", createdAt: T0 + 50, notes: u.notes }), "evt_T3_pre");
  const mapped = await mappingRow("sub_Term3");
  assert.equal((await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider: createOnly("sub_Term3New") })).ok, true);
  assert.deepEqual(await mappingRow("sub_Term3"), mapped);
  const cur = await subRow(u.id);
  await deliver(body({ type: "subscription.charged", status: "active", rid: "sub_Term3New", createdAt: nowSec() - 5, notes: { userId: u.id, checkoutAttemptId: cur.checkoutAttemptId } }), "evt_T3_new");
  const newer = await subRow(u.id);
  assert.equal(computeEntitlement(newer, new Date()).plan, "PRO"); // paid on its own new subscription
  assert.equal((await deliver(body({ type: "subscription.charged", status: "active", rid: "sub_Term3", createdAt: T0 + 70, notes: u.notes }), "evt_T3_post")).body.outcome, "reconciliation_required");
  assert.deepEqual(await event("evt_T3_post"), { outcome: "RECONCILIATION_REQUIRED", reason: "retired_payment", userId: u.id });
  assert.deepEqual(await subRow(u.id), newer);
});

test("conflicting mapping: reconciliation_conflict, nothing overwritten; a failure after the insert rolls the mapping back", { skip }, async () => {
  const other = await addUser();
  const c = await addTerminalPaid("sub_Term4");
  await q(`INSERT INTO $S.retired_billing_subscriptions VALUES ('sub_Term4', $1, NULL, 'expired', 'ALREADY_FINAL', now())`, [other.id]);
  const clash = await mappingRow("sub_Term4");
  const before = await subRow(c.id);
  assert.equal((await deliver(body({ type: "subscription.charged", status: "active", rid: "sub_Term4", createdAt: T0 + 50, notes: c.notes }), "evt_T4")).body.outcome, "ignored");
  assert.deepEqual(await event("evt_T4"), { outcome: "IGNORED", reason: "reconciliation_conflict", userId: null });
  assert.deepEqual(await mappingRow("sub_Term4"), clash);
  assert.deepEqual(await subRow(c.id), before);

  const r = await addTerminalPaid("sub_Term5");
  const failing = prismaBillingWebhookStore(prisma, (tx) => ({ ...tx, async finishEvent() { throw new Error("injected"); } }));
  const charge = body({ type: "subscription.charged", status: "active", rid: "sub_Term5", createdAt: T0 + 50, notes: r.notes });
  assert.equal((await deliver(charge, "evt_T5", failing)).status, 503);
  assert.deepEqual(await mappingRow("sub_Term5"), []);
  assert.equal(await event("evt_T5"), undefined);
  assert.equal((await deliver(charge, "evt_T5")).body.outcome, "reconciliation_required");
  assert.equal((await mappingRow("sub_Term5")).length, 1);
});

test("repeated concurrent delivery: late charges, duplicates and replacement checkouts on terminal subscriptions (no deadlock, nothing lost)", { skip }, async () => {
  for (let round = 0; round < 5; round++) {
    const users = await Promise.all(Array.from({ length: 4 }, (_, i) => addTerminalPaid(`sub_TermStorm${round}x${i}`)));
    const work = [];
    users.forEach((u, i) => {
      const rid = `sub_TermStorm${round}x${i}`;
      for (let k = 0; k < 3; k++) {
        const charge = body({ type: "subscription.charged", status: "active", rid, createdAt: T0 + 10 + k, notes: u.notes });
        work.push(deliver(charge, `evt_TS${round}_${i}_${k}`), deliver(charge, `evt_TS${round}_${i}_${k}`)); // each also duplicated
      }
      work.push(deliver(body({ type: "subscription.cancelled", status: "cancelled", rid, createdAt: T0 + 20, paid: 4, notes: u.notes }), `evt_TSc${round}_${i}`));
      if (i % 2 === 0) work.push(startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider: createOnly(`sub_TermStormNew${round}x${i}`) }));
    });
    const results = await Promise.all(work);
    assert.ok(results.every((r) => r.status === undefined || r.status === 200), `round ${round}: no webhook needed a retry`);
    for (const [i, u] of users.entries()) {
      const rid = `sub_TermStorm${round}x${i}`;
      const { rows } = await q(`SELECT id, outcome, reason, "userId" FROM $S.billing_webhook_events WHERE "razorpaySubscriptionId" = $1 AND "eventType" = 'subscription.charged'`, [rid]);
      assert.equal(rows.length, 3, `round ${round} user ${i}: every distinct charge recorded once`);
      assert.ok(rows.every((e) => e.outcome === "RECONCILIATION_REQUIRED" && e.userId === u.id), JSON.stringify(rows));
      assert.deepEqual((await retiredRows(u.id)).map((m) => [m.razorpaySubscriptionId, m.checkoutAttemptId]), [[rid, u.attempt]]);
      assert.equal((await event(`evt_TSc${round}_${i}`)).outcome, "IGNORED"); // historical paid_count: not a charge
      const s = await subRow(u.id);
      assert.equal(s.razorpaySubscriptionId, i % 2 === 0 ? `sub_TermStormNew${round}x${i}` : rid);
      assert.equal(computeEntitlement(s, new Date()).plan, "FREE");
    }
  }
});

// ---------------------------------------------------------------------------
// Payload notes disagreeing with the local owner
// ---------------------------------------------------------------------------

test("exact current ID, notes name another user: flagged for the local owner; notes user gets nothing; idempotent", { skip }, async () => {
  const a = attempt();
  const u = await addUser({ sub: { rid: "sub_NotesCur1", status: "authenticated", attempt: a } });
  const other = await addUser({ sub: { rid: "sub_NotesOther1", status: "created", attempt: attempt() } });
  const [ub, ob] = [await subRow(u.id), await subRow(other.id)];
  const charge = body({ type: "subscription.charged", status: "active", rid: "sub_NotesCur1", createdAt: nowSec(), notes: { userId: other.id, checkoutAttemptId: a } });
  const rs = await Promise.all(Array.from({ length: 8 }, () => deliver(charge, "evt_NotesCur1")));
  assert.deepEqual(rs.map((r) => r.body.outcome).sort(), [...Array(7).fill("duplicate"), "reconciliation_required"].sort());
  assert.deepEqual(await event("evt_NotesCur1"), { outcome: "RECONCILIATION_REQUIRED", reason: "notes_user_mismatch", userId: u.id });
  assert.deepEqual([await subRow(u.id), await subRow(other.id)], [ub, ob]);
  for (const id of [u.id, other.id]) assert.equal(computeEntitlement(await subRow(id), new Date()).plan, "FREE");
  assert.deepEqual([await retiredRows(u.id), await retiredRows(other.id)], [[], []]);
});

test("early webhook with only the attempt reservation and mismatched notes: never bound; flagged for the reservation owner", { skip }, async () => {
  const a = attempt();
  const u = await addUser({ sub: { attempt: a, checkoutStatus: "RESERVED" } });
  const other = await addUser();
  const before = await subRow(u.id);
  const r = await deliver(body({ type: "subscription.charged", status: "active", rid: "sub_NotesEarly1", createdAt: nowSec(), notes: { userId: other.id, checkoutAttemptId: a } }), "evt_NotesEarly1");
  assert.equal(r.body.outcome, "reconciliation_required");
  assert.deepEqual(await event("evt_NotesEarly1"), { outcome: "RECONCILIATION_REQUIRED", reason: "notes_user_mismatch", userId: u.id });
  assert.deepEqual(await subRow(u.id), before); // still unbound: notes never create a mapping
  assert.equal(await subRow(other.id), null);
  // The checkout's own bind (Razorpay's create response for this attempt) remains the mapping source.
  assert.equal(await prismaCheckoutStore(prisma).bind(a, "sub_NotesEarly1"), true);
  assert.equal((await subRow(u.id)).razorpaySubscriptionId, "sub_NotesEarly1");
});

test("exact retired ID, notes name another user: flagged for the stored original owner; newer subscription untouched", { skip }, async () => {
  const u = await addStaleCheckout("sub_NotesRet1");
  const other = await addUser();
  assert.equal((await startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider: unpaidProvider("sub_NotesRet2") })).ok, true);
  const newer = await subRow(u.id);
  const r = await deliver(body({ type: "subscription.charged", status: "active", rid: "sub_NotesRet1", createdAt: nowSec(), notes: { userId: other.id, checkoutAttemptId: u.attempt } }), "evt_NotesRet1");
  assert.equal(r.body.outcome, "reconciliation_required");
  assert.deepEqual(await event("evt_NotesRet1"), { outcome: "RECONCILIATION_REQUIRED", reason: "notes_user_mismatch", userId: u.id });
  assert.deepEqual(await subRow(u.id), newer);
  assert.equal(await subRow(other.id), null);
  assert.equal((await retiredRows(u.id))[0].userId, u.id); // mapping unchanged
});

test("concurrent mismatched and matching charges with checkouts, repeated: no deadlock, every event attributed to the local owner", { skip }, async () => {
  for (let round = 0; round < 5; round++) {
    const users = await Promise.all(Array.from({ length: 4 }, async (_, i) => {
      const a = attempt();
      const u = await addUser({ sub: { rid: `sub_NotesStorm${round}x${i}`, status: i % 2 ? "cancelled" : "authenticated", attempt: a, lastEventAt: T0 } });
      return { ...u, attempt: a, rid: `sub_NotesStorm${round}x${i}` };
    }));
    const stranger = await addUser();
    const work = [];
    for (const [i, u] of users.entries()) {
      for (let k = 0; k < 3; k++) {
        const notes = { userId: k === 0 ? stranger.id : u.id, checkoutAttemptId: u.attempt };
        const ev = body({ type: "subscription.charged", status: "active", rid: u.rid, createdAt: T0 + 10 + k, notes });
        work.push(deliver(ev, `evt_NS${round}_${i}_${k}`), deliver(ev, `evt_NS${round}_${i}_${k}`));
      }
      work.push(startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider: createOnly(`sub_NotesStormNew${round}x${i}`) }));
    }
    const results = await Promise.all(work);
    assert.ok(results.every((r) => r.status === undefined || r.status === 200), `round ${round}: no webhook needed a retry`);
    for (const [i, u] of users.entries()) {
      const { rows } = await q(`SELECT id, outcome, reason, "userId" FROM $S.billing_webhook_events WHERE "razorpaySubscriptionId" = $1 ORDER BY id`, [u.rid]);
      assert.equal(rows.length, 3, `round ${round} user ${i}`);
      for (const e of rows) assert.ok(e.userId === null || e.userId === u.id, JSON.stringify(e)); // never the stranger
      const mismatched = rows.find((e) => e.id.endsWith("_0"));
      // Before or after archival, the local (current or stored) owner is flagged, never the stranger.
      assert.deepEqual([mismatched.outcome, mismatched.reason, mismatched.userId], ["RECONCILIATION_REQUIRED", "notes_user_mismatch", u.id]);
    }
    assert.equal(await subRow(stranger.id), null);
  }
});

// ---------------------------------------------------------------------------
// Lifecycle events whose notes disagree with the exact local owner
// ---------------------------------------------------------------------------

const atSec = (sec) => new Date(sec * 1000);
async function addPaid(rid) {
  const a = attempt();
  const u = await addUser({ sub: { rid, status: "active", attempt: a, paid: true, lastEventAt: T0 } });
  return { ...u, attempt: a, rid };
}
const mmBody = (type, status, rid, createdAt, notesUser, extra = {}) => JSON.stringify({ event: type, created_at: createdAt, contains: ["subscription"],
  payload: { subscription: { entity: { id: rid, status, paid_count: 1, current_end: T0 + 90 * DAY, notes: { userId: notesUser }, ...extra } } } });

test("mismatched cancelled revokes access for the exact local owner; the notes user is untouched", { skip }, async () => {
  const u = await addPaid("sub_LifeCan1");
  const other = await addPaid("sub_LifeOther1");
  const otherBefore = await subRow(other.id);
  assert.equal(computeEntitlement(await subRow(u.id), atSec(T0 + 20)).plan, "PRO");
  const r = await deliver(mmBody("subscription.cancelled", "cancelled", u.rid, T0 + 10, other.id, { ended_at: T0 + 10 }), "evt_LifeCan1");
  assert.equal(r.body.outcome, "processed");
  assert.deepEqual(await event("evt_LifeCan1"), { outcome: "PROCESSED", reason: "applied_notes_user_mismatch", userId: u.id });
  const s = await subRow(u.id);
  assert.deepEqual([s.status, s.endedAt?.getTime()], ["cancelled", (T0 + 10) * 1000]);
  assert.equal(computeEntitlement(s, atSec(T0 + 20)).plan, "FREE");
  assert.deepEqual(await subRow(other.id), otherBefore);
});

test("each event class with mismatched notes, in the database", { skip }, async () => {
  const stranger = await addUser();
  const end = T0 + 30 * DAY;
  // Restrictive: applied, paid-through preserved (not extended).
  for (const [type, status] of [["subscription.halted", "halted"], ["subscription.paused", "paused"], ["subscription.completed", "completed"]]) {
    const u = await addPaid(`sub_LifeR${status}`);
    await deliver(mmBody(type, status, u.rid, T0 + 10, stranger.id), `evt_LifeR_${status}`);
    assert.deepEqual(await event(`evt_LifeR_${status}`), { outcome: "PROCESSED", reason: "applied_notes_user_mismatch", userId: u.id }, type);
    const s = await subRow(u.id);
    assert.deepEqual([s.status, s.currentPeriodEnd.getTime()], [status, end * 1000], type);
    assert.equal(computeEntitlement(s, atSec(T0 + 20)).plan, "FREE", type);
  }
  // Completed stays terminal even against a later matching resume.
  const done = await subRow((await q(`SELECT "userId" FROM $S.subscriptions WHERE "razorpaySubscriptionId" = 'sub_LifeRcompleted'`)).rows[0].userId);
  await deliver(body({ type: "subscription.resumed", status: "active", rid: "sub_LifeRcompleted", createdAt: T0 + 20, notes: { userId: done.userId } }), "evt_LifeR_resume");
  assert.deepEqual(await event("evt_LifeR_resume"), { outcome: "IGNORED", reason: "terminal_state", userId: null });
  // Access-increasing: flagged, nothing restored.
  const p = await addPaid("sub_LifeResume");
  await q(`UPDATE $S.subscriptions SET status = 'paused', "lastEventType" = 'subscription.paused' WHERE "userId" = $1`, [p.id]);
  const pBefore = await subRow(p.id);
  await deliver(mmBody("subscription.resumed", "active", p.rid, T0 + 10, stranger.id), "evt_LifeResume");
  assert.deepEqual(await event("evt_LifeResume"), { outcome: "RECONCILIATION_REQUIRED", reason: "notes_user_mismatch", userId: p.id });
  assert.deepEqual(await subRow(p.id), pBefore);
  assert.equal(computeEntitlement(pBefore, atSec(T0 + 20)).plan, "FREE");
  // Pending: not applied, so no grace past the stored paid-through date.
  const q2 = await addPaid("sub_LifePending");
  await deliver(mmBody("subscription.pending", "pending", q2.rid, end - 60, stranger.id), "evt_LifePending");
  assert.deepEqual(await event("evt_LifePending"), { outcome: "IGNORED", reason: "notes_user_mismatch", userId: q2.id });
  const ps = await subRow(q2.id);
  assert.deepEqual([ps.status, computeEntitlement(ps, atSec(end - 1)).plan, computeEntitlement(ps, atSec(end)).plan], ["active", "PRO", "FREE"]);
  // Charge: flagged, paid-through unchanged.
  const c = await addPaid("sub_LifeCharge");
  await deliver(mmBody("subscription.charged", "active", c.rid, T0 + 10, stranger.id), "evt_LifeCharge");
  assert.deepEqual(await event("evt_LifeCharge"), { outcome: "RECONCILIATION_REQUIRED", reason: "notes_user_mismatch", userId: c.id });
  assert.equal((await subRow(c.id)).currentPeriodEnd.getTime(), end * 1000);
  assert.equal(await subRow(stranger.id), null);
});

test("concurrent mismatched and matching lifecycle events, repeated: deadlock-free, terminal wins, nothing attributed to the notes user", { skip }, async () => {
  for (let round = 0; round < 5; round++) {
    const stranger = await addUser();
    const users = await Promise.all(Array.from({ length: 4 }, (_, i) => addPaid(`sub_LifeStorm${round}x${i}`)));
    const work = [];
    for (const [i, u] of users.entries()) {
      const evs = [
        [mmBody("subscription.cancelled", "cancelled", u.rid, T0 + 50, stranger.id, { ended_at: T0 + 50 }), "c"],
        [mmBody("subscription.resumed", "active", u.rid, T0 + 60, stranger.id), "r"],
        [mmBody("subscription.paused", "paused", u.rid, T0 + 20, stranger.id), "p"],
        [mmBody("subscription.pending", "pending", u.rid, T0 + 30, stranger.id), "pe"],
        [mmBody("subscription.charged", "active", u.rid, T0 + 40, stranger.id), "ch"],
        [body({ type: "subscription.halted", status: "halted", rid: u.rid, createdAt: T0 + 50, notes: { userId: u.id } }), "h"], // same second as the cancel
      ];
      for (const [b, k] of evs) work.push(deliver(b, `evt_LS${round}_${i}_${k}`), deliver(b, `evt_LS${round}_${i}_${k}`));
      work.push(startCheckout({ clerkUserId: u.clerkId, store: prismaCheckoutStore(prisma), provider: createOnly(`sub_LifeStormNew${round}x${i}`) }));
    }
    const results = await Promise.all(work);
    assert.ok(results.every((r) => r.status === undefined || r.status === 200), `round ${round}: no webhook needed a retry`);
    for (const [i, u] of users.entries()) {
      const { rows } = await q(`SELECT id, outcome, reason, "userId" FROM $S.billing_webhook_events WHERE "razorpaySubscriptionId" = $1`, [u.rid]);
      assert.equal(rows.length, 6, `round ${round} user ${i}: each distinct event recorded once`);
      for (const e of rows) assert.ok(e.userId === null || e.userId === u.id, JSON.stringify(e));
      const byKey = Object.fromEntries(rows.map((e) => [e.id.split("_").pop(), e]));
      assert.equal(byKey.ch.outcome, "RECONCILIATION_REQUIRED");
      // A mismatched resume is flagged while still mapped; once the cancelled subscription has been archived
      // by the replacement checkout it resolves through the retired mapping (ignored). Either way: local owner.
      assert.ok(["RECONCILIATION_REQUIRED", "IGNORED"].includes(byKey.r.outcome) && byKey.r.reason === "notes_user_mismatch" && byKey.r.userId === u.id, JSON.stringify(byKey.r));
      const s = await subRow(u.id);
      // Whatever the arrival order, the cancellation (newest restrictive event, terminal) ends it,
      // or the row was archived and replaced by a new checkout after the cancellation applied.
      if (s.razorpaySubscriptionId === u.rid) assert.equal(s.status, "cancelled", `round ${round} user ${i}`);
      else assert.deepEqual((await retiredRows(u.id)).map((m) => m.providerStatus), ["cancelled"]);
      assert.equal(computeEntitlement(s, atSec(T0 + 100)).plan, "FREE");
    }
    assert.equal(await subRow(stranger.id), null);
  }
});
