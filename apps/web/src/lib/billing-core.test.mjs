// Run with: pnpm --filter @solanaguard/web test. In-memory stores and a fake
// Razorpay client only; no network, no database. Identifiers are fake.
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";

import {
  PENDING_GRACE_MS, computeEntitlement, decideCheckout, decideTransition, eventIdentity, handleRazorpayWebhook,
  decideRetiredEvent, isDefiniteRejection, newCheckoutAttemptId, parseWebhookEvent, sha256Hex, startCheckout, verifyRazorpaySignature,
} from "./billing-core.ts";

const SECRET = "whsec_test_only_value_0123456789";
const T0 = 1_790_000_000; // event seconds
const at = (s) => new Date(s * 1000);
const DAY = 86_400;
const enc = (s) => new TextEncoder().encode(s);
const sign = (bytes, secret = SECRET) => createHmac("sha256", secret).update(bytes).digest("hex");
const ATTEMPT_A = "ca_" + "A".repeat(32);
const ATTEMPT_B = "ca_" + "B".repeat(32);

function headers(map) {
  const lower = Object.fromEntries(Object.entries(map).map(([k, v]) => [k.toLowerCase(), v]));
  return { get: (name) => lower[name.toLowerCase()] ?? null };
}

function eventBody({ type, createdAt = T0, id = "sub_Old1", status, paid = 1, end = T0 + 30 * DAY, endedAt, notes = {} }) {
  const entity = { id, entity: "subscription", status, paid_count: paid, current_start: createdAt, current_end: end, notes };
  if (endedAt) entity.ended_at = endedAt;
  return JSON.stringify({ entity: "event", account_id: "acc_Test", event: type, contains: ["subscription"], payload: { subscription: { entity } }, created_at: createdAt });
}

function row(overrides = {}) {
  return {
    id: "row1", userId: "user_1", status: "created", razorpaySubscriptionId: null, currentPeriodEnd: null,
    paymentConfirmedAt: null, cancelMode: null, cancelSubscriptionId: null, lastEventAt: null, lastEventType: null,
    checkoutAttemptId: null, checkoutStatus: null, checkoutStartedAt: null, ...overrides,
  };
}

/** Transactional in-memory store: serialised transactions, full rollback on throw. */
function memoryWebhookStore(subs = [], { failAfterClaim = false, retired = [] } = {}) {
  const state = { subs, retired, events: new Map() };
  let queue = Promise.resolve();
  const store = {
    state,
    transaction(fn) {
      const run = queue.then(async () => {
        const snapshot = structuredClone({ subs: state.subs, events: [...state.events], retired: state.retired });
        const tx = {
          async claimEvent(claim) {
            if (state.events.has(claim.id)) return false;
            state.events.set(claim.id, { ...claim, outcome: null, reason: null, processedAt: null, userId: null });
            if (failAfterClaim) throw new Error("injected failure");
            return true;
          },
          async lockByProviderId(id) { const r = state.subs.find((s) => s.razorpaySubscriptionId === id); return r ? { ...r } : null; },
          async lockByCheckoutAttempt(a) { const r = state.subs.find((s) => s.checkoutAttemptId === a); return r ? { ...r } : null; },
          async findRetired(id) { const r = state.retired.find((x) => x.razorpaySubscriptionId === id); return r ? { ...r } : null; },
          async insertRetired(m) {
            if (state.retired.some((x) => x.razorpaySubscriptionId === m.razorpaySubscriptionId)) throw new Error("unique violation");
            state.retired.push({ ...m });
          },
          async updateSubscription(id, patch) { Object.assign(state.subs.find((s) => s.id === id), patch); },
          async finishEvent(id, outcome, reason, processedAt, userId = null) { Object.assign(state.events.get(id), { outcome, reason, processedAt, userId }); },
        };
        try {
          return await fn(tx);
        } catch (e) {
          state.subs.splice(0, state.subs.length, ...snapshot.subs);
          state.retired.splice(0, state.retired.length, ...snapshot.retired);
          state.events = new Map(snapshot.events);
          throw e;
        }
      });
      queue = run.catch(() => {});
      return run;
    },
  };
  return store;
}

/** What the Prisma store passes to `decide`: ownership of an existing mapping for the row's provider ID. */
function archivedFor(retired, existing) {
  const m = existing?.razorpaySubscriptionId ? retired.find((x) => x.razorpaySubscriptionId === existing.razorpaySubscriptionId) : null;
  return m ? { userId: m.userId, checkoutAttemptId: m.checkoutAttemptId } : null;
}

async function deliver(store, body, { eventId = `evt_${sha256Hex(enc(body)).slice(0, 14)}`, secret = SECRET, now = at(T0 + 60), logs = [] } = {}) {
  const raw = enc(body);
  const h = { "x-razorpay-signature": sign(raw) };
  if (eventId !== null) h["x-razorpay-event-id"] = eventId;
  return handleRazorpayWebhook({ rawBody: raw, headers: headers(h), secret, store, now: () => now, log: (l) => logs.push(l) });
}

// ---------------------------------------------------------------------------
describe("signature", () => {
  const body = eventBody({ type: "subscription.charged", status: "active" });
  const raw = enc(body);

  test("correct signature verifies", () => assert.equal(verifyRazorpaySignature(raw, sign(raw), SECRET), true));
  test("missing signature", () => assert.equal(verifyRazorpaySignature(raw, null, SECRET), false));
  test("wrong secret", () => assert.equal(verifyRazorpaySignature(raw, sign(raw, "other"), SECRET), false));
  test("tampered body", () => assert.equal(verifyRazorpaySignature(enc(body.replace("active", "halted")), sign(raw), SECRET), false));
  test("malformed hex / wrong length", () => {
    for (const s of ["zz".repeat(32), sign(raw).slice(0, 62), sign(raw) + "00", "", " " + sign(raw).slice(1)]) {
      assert.equal(verifyRazorpaySignature(raw, s, SECRET), false, s);
    }
  });
  test("uppercase hex of the right digest is accepted", () => assert.equal(verifyRazorpaySignature(raw, sign(raw).toUpperCase(), SECRET), true));
  test("verification is over exact bytes, not a re-serialization", () => {
    const spaced = enc('{ "event" : "x",\n  "name": "\\u00e9" }');
    const reserialized = enc(JSON.stringify(JSON.parse(new TextDecoder().decode(spaced))));
    assert.notDeepEqual(spaced, reserialized);
    assert.equal(verifyRazorpaySignature(spaced, sign(spaced), SECRET), true);
    assert.equal(verifyRazorpaySignature(reserialized, sign(spaced), SECRET), false);
  });

  test("handler: missing secret -> 503, before anything else", async () => {
    const store = memoryWebhookStore();
    const r = await handleRazorpayWebhook({ rawBody: raw, headers: headers({ "x-razorpay-signature": sign(raw) }), secret: undefined, store });
    assert.equal(r.status, 503);
    assert.equal(store.state.events.size, 0);
  });
  test("handler: missing / wrong / malformed signature -> 401 and nothing stored", async () => {
    const store = memoryWebhookStore();
    for (const sig of [undefined, sign(raw, "other"), "nothex"]) {
      const h = sig === undefined ? {} : { "x-razorpay-signature": sig };
      const r = await handleRazorpayWebhook({ rawBody: raw, headers: headers(h), secret: SECRET, store });
      assert.equal(r.status, 401);
    }
    assert.equal(store.state.events.size, 0);
  });
  test("handler: signed but malformed JSON is recorded as ignored and acknowledged with 200", async () => {
    const store = memoryWebhookStore();
    const logs = [];
    for (const bad of [enc('{"secret_field":"not json'), new Uint8Array([0x7b, 0xff, 0xfe, 0x7d]) /* invalid UTF-8 */]) {
      const signature = sign(bad);
      const r = await handleRazorpayWebhook({ rawBody: bad, headers: headers({ "x-razorpay-signature": signature }), secret: SECRET, store, log: (l) => logs.push(l) });
      assert.deepEqual([r.status, r.body.outcome], [200, "ignored"]);
      const ev = store.state.events.get(`body-sha256:${sha256Hex(bad)}`);
      assert.deepEqual([ev.eventType, ev.outcome, ev.reason, ev.providerCreatedAt, ev.razorpaySubscriptionId], ["unparsed", "IGNORED", "malformed_json", null, null]);
      assert.ok(!logs.join("\n").includes(signature));
      // A redelivery of the same bytes is a duplicate, not a second failure.
      const again = await handleRazorpayWebhook({ rawBody: bad, headers: headers({ "x-razorpay-signature": signature }), secret: SECRET, store });
      assert.deepEqual([again.status, again.body.outcome], [200, "duplicate"]);
    }
    assert.ok(!logs.join("\n").includes("secret_field"));
    assert.equal(store.state.events.size, 2);
  });
  test("handler: signed but structurally invalid events are recorded as ignored with 200 and change nothing", async () => {
    const store = memoryWebhookStore([row({ razorpaySubscriptionId: "sub_Old1", status: "authenticated" })]);
    const before = structuredClone(store.state.subs);
    const invalid = [
      "[]", '"x"', JSON.stringify({ event: "subscription.charged" }), // no created_at
      JSON.stringify({ event: "subscription.charged", created_at: T0, payload: {} }), // no entity
      JSON.stringify({ event: "subscription.charged", created_at: T0, payload: { subscription: { entity: { id: "not-a-sub", status: "active" } } } }),
      JSON.stringify({ event: "Bad Type!", created_at: T0, payload: {} }),
      JSON.stringify({ event: "subscription.charged", created_at: -5, payload: {} }),
    ];
    for (const body of invalid) {
      const r = await deliver(store, body);
      assert.deepEqual([r.status, r.body.outcome], [200, "ignored"], body);
    }
    assert.equal(store.state.events.size, invalid.length);
    assert.ok([...store.state.events.values()].every((e) => e.reason === "invalid_event" && e.eventType === "unparsed"));
    assert.deepEqual(store.state.subs, before);
  });
  test("handler: a database failure while recording a malformed event still returns 503", async () => {
    const bad = enc("{not json");
    const r = await handleRazorpayWebhook({ rawBody: bad, headers: headers({ "x-razorpay-signature": sign(bad) }), secret: SECRET,
      store: memoryWebhookStore([], { failAfterClaim: true }), log: () => {} });
    assert.equal(r.status, 503);
  });
  test("handler: a malformed event-id header falls back to the body digest", async () => {
    const store = memoryWebhookStore([row({ razorpaySubscriptionId: "sub_Old1", status: "authenticated" })]);
    const logs = [];
    const r = await deliver(store, body, { eventId: "bad id with spaces", logs });
    assert.deepEqual([r.status, r.body.outcome], [200, "processed"]);
    assert.equal([...store.state.events.values()][0].idSource, "BODY_SHA256");
    assert.match(logs[0], /id_header=invalid/);
  });
});

// ---------------------------------------------------------------------------
describe("event identity and idempotency", () => {
  test("provider header is the identity", () => {
    assert.deepEqual(eventIdentity("evt_ABC123", "f".repeat(64)), { id: "evt_ABC123", source: "PROVIDER_HEADER" });
  });
  test("absent header falls back to a namespaced body digest", () => {
    assert.deepEqual(eventIdentity(null, "a".repeat(64)), { id: `body-sha256:${"a".repeat(64)}`, source: "BODY_SHA256" });
  });

  const activeSub = () => row({ razorpaySubscriptionId: "sub_Old1", checkoutAttemptId: ATTEMPT_A, checkoutStatus: "CREATED", status: "authenticated" });

  test("sequential duplicate is acknowledged without reapplying", async () => {
    const store = memoryWebhookStore([activeSub()]);
    const body = eventBody({ type: "subscription.charged", status: "active" });
    const first = await deliver(store, body, { eventId: "evt_dup1" });
    const after1 = structuredClone(store.state.subs[0]);
    store.state.subs[0].status = "sentinel"; // any reapplication would overwrite this
    const second = await deliver(store, body, { eventId: "evt_dup1" });
    assert.deepEqual([first.status, first.body.outcome, second.status, second.body.outcome], [200, "processed", 200, "duplicate"]);
    assert.equal(store.state.subs[0].status, "sentinel");
    store.state.subs[0].status = after1.status;
    assert.equal(store.state.events.size, 1);
  });

  test("concurrent duplicates apply once", async () => {
    const store = memoryWebhookStore([activeSub()]);
    const body = eventBody({ type: "subscription.charged", status: "active" });
    const rs = await Promise.all(Array.from({ length: 5 }, () => deliver(store, body, { eventId: "evt_dup2" })));
    assert.deepEqual(rs.map((r) => r.body.outcome).sort(), ["duplicate", "duplicate", "duplicate", "duplicate", "processed"]);
  });

  test("body-hash fallback dedupes retries without the header and records its source", async () => {
    const store = memoryWebhookStore([activeSub()]);
    const body = eventBody({ type: "subscription.charged", status: "active" });
    const a = await deliver(store, body, { eventId: null });
    const b = await deliver(store, body, { eventId: null });
    assert.deepEqual([a.body.outcome, b.body.outcome], ["processed", "duplicate"]);
    const [ev] = store.state.events.values();
    assert.equal(ev.idSource, "BODY_SHA256");
    assert.equal(ev.id, `body-sha256:${sha256Hex(enc(body))}`);
  });

  test("failure after event insertion rolls back everything and returns 503; retry then succeeds", async () => {
    const store = memoryWebhookStore([activeSub()], { failAfterClaim: true });
    const body = eventBody({ type: "subscription.charged", status: "active" });
    const before = structuredClone(store.state.subs);
    const logs = [];
    const r = await deliver(store, body, { eventId: "evt_fail", logs });
    assert.equal(r.status, 503);
    assert.equal(store.state.events.size, 0);
    assert.deepEqual(store.state.subs, before);
    const healthy = memoryWebhookStore(store.state.subs);
    assert.equal((await deliver(healthy, body, { eventId: "evt_fail" })).body.outcome, "processed");
  });

  test("unknown but valid events are recorded as ignored with 200", async () => {
    const store = memoryWebhookStore();
    const body = JSON.stringify({ event: "payment.captured", created_at: T0, payload: { payment: { entity: { id: "pay_X", email: "x@example.test" } } } });
    const r = await deliver(store, body, { eventId: "evt_pay" });
    assert.deepEqual([r.status, r.body.outcome], [200, "ignored"]);
    assert.equal(store.state.events.get("evt_pay").reason, "unsupported_event");
  });
});

// ---------------------------------------------------------------------------
describe("mapping", () => {
  test("existing subscription ID (legacy row without attempt ID)", async () => {
    const store = memoryWebhookStore([row({ razorpaySubscriptionId: "sub_Old1", status: "active" })]);
    const r = await deliver(store, eventBody({ type: "subscription.charged", status: "active", notes: { userId: "user_1" } }));
    assert.equal(r.body.outcome, "processed");
    assert.equal(computeEntitlement(store.state.subs[0], at(T0 + 60)).plan, "PRO");
  });

  test("early webhook binds by exact checkout attempt ID", async () => {
    const store = memoryWebhookStore([row({ checkoutAttemptId: ATTEMPT_A, checkoutStatus: "RESERVED" })]);
    const r = await deliver(store, eventBody({ type: "subscription.authenticated", status: "authenticated", id: "sub_New1", paid: 0, notes: { userId: "user_1", checkoutAttemptId: ATTEMPT_A } }));
    assert.equal(r.body.outcome, "processed");
    assert.equal(store.state.subs[0].razorpaySubscriptionId, "sub_New1");
    assert.equal(store.state.subs[0].checkoutStatus, "CREATED");
    assert.equal(store.state.events.values().next().value.reason, "applied_bound");
  });

  test("wrong notes user cannot remap or change an account (the charge is flagged for the LOCAL owner)", async () => {
    const original = row({ razorpaySubscriptionId: "sub_Old1", status: "authenticated", checkoutAttemptId: ATTEMPT_A, checkoutStatus: "CREATED" });
    const store = memoryWebhookStore([structuredClone(original)]);
    const r = await deliver(store, eventBody({ type: "subscription.charged", status: "active", notes: { userId: "user_2" } }));
    assert.equal(r.body.outcome, "reconciliation_required");
    assert.equal([...store.state.events.values()][0].userId, "user_1");
    assert.deepEqual(store.state.subs[0], original);
    // notes.userId alone never maps an unknown subscription either
    const r2 = await deliver(store, eventBody({ type: "subscription.charged", status: "active", id: "sub_Other", notes: { userId: "user_1" } }));
    assert.equal(store.state.events.get(`evt_${sha256Hex(enc(eventBody({ type: "subscription.charged", status: "active", id: "sub_Other", notes: { userId: "user_1" } }))).slice(0, 14)}`).reason, "unknown_subscription");
    assert.equal(r2.body.outcome, "ignored");
    assert.deepEqual(store.state.subs[0], original);
  });

  test("attempt ID in notes cannot bind a reservation owned by another user", async () => {
    const store = memoryWebhookStore([row({ checkoutAttemptId: ATTEMPT_A, checkoutStatus: "RESERVED" })]);
    const r = await deliver(store, eventBody({ type: "subscription.activated", status: "active", id: "sub_X", notes: { userId: "user_2", checkoutAttemptId: ATTEMPT_A } }));
    assert.equal(r.body.outcome, "ignored");
    assert.equal(store.state.subs[0].razorpaySubscriptionId, null);
  });

  test("old subscription events cannot affect a new checkout", async () => {
    // The user's row was re-reserved for ATTEMPT_B; the old Razorpay ID was cleared.
    const store = memoryWebhookStore([row({ checkoutAttemptId: ATTEMPT_B, checkoutStatus: "RESERVED" })]);
    for (const [type, status] of [["subscription.charged", "active"], ["subscription.activated", "active"], ["subscription.cancelled", "cancelled"]]) {
      const r = await deliver(store, eventBody({ type, status, id: "sub_Old1", notes: { userId: "user_1", checkoutAttemptId: ATTEMPT_A } }));
      assert.equal(r.body.outcome, "ignored");
    }
    assert.deepEqual(store.state.subs[0], row({ checkoutAttemptId: ATTEMPT_B, checkoutStatus: "RESERVED" }));
  });

  test("unknown subscription is safely ignored", async () => {
    const store = memoryWebhookStore();
    const r = await deliver(store, eventBody({ type: "subscription.charged", status: "active", id: "sub_Nobody" }));
    assert.deepEqual([r.status, r.body.outcome], [200, "ignored"]);
  });
});

// ---------------------------------------------------------------------------
describe("ordering", () => {
  const live = (o = {}) => row({ razorpaySubscriptionId: "sub_Old1", ...o });
  const ev = (type, status, createdAt, extra = {}) => parseWebhookEvent(enc(eventBody({ type, status, createdAt, ...extra }))).event;
  const now = at(T0 + 10);

  test("stale charge after cancellation is rejected (terminal)", () => {
    const r = live({ status: "cancelled", lastEventAt: at(T0 + 5) });
    assert.deepEqual(decideTransition(r, ev("subscription.charged", "active", T0), now), { apply: false, reason: "terminal_state" });
  });
  test("stale activation after halt is rejected", () => {
    const r = live({ status: "halted", lastEventAt: at(T0 + 5) });
    assert.deepEqual(decideTransition(r, ev("subscription.activated", "active", T0), now), { apply: false, reason: "stale_event" });
  });
  test("pending after active is applied and keeps the paid-through date", () => {
    const r = live({ status: "active", lastEventAt: at(T0), paymentConfirmedAt: at(T0), currentPeriodEnd: at(T0 + 30 * DAY) });
    const t = decideTransition(r, ev("subscription.pending", "pending", T0 + 30 * DAY + 60), at(T0 + 30 * DAY + 120));
    assert.equal(t.apply, true);
    assert.equal(t.patch.status, "pending");
    assert.equal(t.patch.currentPeriodEnd, undefined);
    assert.equal(t.patch.plan, "PRO"); // within grace
  });
  test("same second: terminal wins over nonterminal in either arrival order", () => {
    const activeRow = live({ status: "active", lastEventAt: at(T0) });
    assert.equal(decideTransition(activeRow, ev("subscription.cancelled", "cancelled", T0), now).apply, true);
    const cancelledRow = live({ status: "cancelled", lastEventAt: at(T0) });
    assert.equal(decideTransition(cancelledRow, ev("subscription.charged", "active", T0), now).apply, false);
    const haltedRow = live({ status: "halted", lastEventAt: at(T0) });
    assert.deepEqual(decideTransition(haltedRow, ev("subscription.activated", "active", T0), now), { apply: false, reason: "stale_event" });
  });
  test("terminal subscription is never revived, even by newer events", () => {
    for (const status of ["cancelled", "completed", "expired"]) {
      const r = live({ status, lastEventAt: at(T0) });
      for (const [type, s] of [["subscription.charged", "active"], ["subscription.activated", "active"], ["subscription.resumed", "active"]]) {
        assert.deepEqual(decideTransition(r, ev(type, s, T0 + DAY), now), { apply: false, reason: "terminal_state" });
      }
    }
  });
  test("event/entity status mismatch is ignored", () => {
    assert.deepEqual(decideTransition(live(), ev("subscription.charged", "halted", T0), now), { apply: false, reason: "status_mismatch" });
  });
  test("end-to-end: cancel then a late, older charge leaves the account FREE", async () => {
    const store = memoryWebhookStore([live({ status: "active", paymentConfirmedAt: at(T0), currentPeriodEnd: at(T0 + 30 * DAY), lastEventAt: at(T0) })]);
    await deliver(store, eventBody({ type: "subscription.cancelled", status: "cancelled", createdAt: T0 + 100, endedAt: T0 + 100 }));
    const late = await deliver(store, eventBody({ type: "subscription.charged", status: "active", createdAt: T0 + 50 }));
    assert.equal(late.body.outcome, "reconciliation_required"); // flagged for an operator, never applied
    assert.equal(store.state.subs[0].status, "cancelled");
    assert.equal(computeEntitlement(store.state.subs[0], at(T0 + 200)).plan, "FREE");
  });
  test("authentication or activation without a paid cycle never grants PRO", () => {
    const t1 = decideTransition(live(), ev("subscription.authenticated", "authenticated", T0, { paid: 0 }), now);
    const t2 = decideTransition(live(), ev("subscription.activated", "active", T0, { paid: 0 }), now);
    assert.equal(t1.patch.plan, "FREE");
    assert.equal(t2.patch.plan, "FREE");
    assert.equal(t2.patch.paymentConfirmedAt, undefined);
  });
  test("charge with a paid-through date not after the event is not payment evidence", () => {
    const t = decideTransition(live(), ev("subscription.charged", "active", T0, { end: T0 }), now);
    assert.equal(t.patch.paymentConfirmedAt, undefined);
  });
});

// ---------------------------------------------------------------------------
describe("same-second transitions (explicit edges)", () => {
  const RID = "sub_Same1";
  const base = (o = {}) => row({ razorpaySubscriptionId: RID, checkoutAttemptId: ATTEMPT_A, checkoutStatus: "CREATED", ...o });
  const paidActive = (o = {}) => base({ status: "active", paymentConfirmedAt: at(T0 - DAY), currentPeriodEnd: at(T0 + 10 * DAY), ...o });
  const send = (store, type, status, createdAt, eventId, extra = {}) =>
    deliver(store, eventBody({ type, status, id: RID, createdAt, end: createdAt + 30 * DAY, ...extra }), { eventId, now: at(createdAt + 5) });
  const S = T0 + 1000;

  test("resumed applies after paused with the SAME timestamp, restoring PRO", async () => {
    const store = memoryWebhookStore([paidActive({ lastEventAt: at(T0), lastEventType: "subscription.charged" })]);
    assert.equal((await send(store, "subscription.paused", "paused", S, "evt_P1")).body.outcome, "processed");
    assert.equal(computeEntitlement(store.state.subs[0], at(S + 1)).plan, "FREE");
    assert.equal((await send(store, "subscription.resumed", "active", S, "evt_R1")).body.outcome, "processed");
    assert.equal(store.state.subs[0].status, "active");
    assert.equal(computeEntitlement(store.state.subs[0], at(S + 1)).plan, "PRO");
  });

  test("a same-second paused arriving AFTER its resume is a late predecessor and is ignored", async () => {
    const store = memoryWebhookStore([paidActive({ lastEventAt: at(T0), lastEventType: "subscription.charged" })]);
    await send(store, "subscription.resumed", "active", S, "evt_R2");
    const late = await send(store, "subscription.paused", "paused", S, "evt_P2");
    assert.equal(late.body.outcome, "ignored");
    assert.equal(store.state.events.get("evt_P2").reason, "stale_event");
    assert.equal(store.state.subs[0].status, "active");
  });

  test("charged restores pending -> active with the SAME timestamp, and extends paid-through", async () => {
    const store = memoryWebhookStore([paidActive({ lastEventAt: at(T0), lastEventType: "subscription.charged" })]);
    assert.equal((await send(store, "subscription.pending", "pending", S, "evt_Pe1")).body.outcome, "processed");
    assert.equal((await send(store, "subscription.charged", "active", S, "evt_C1", { paid: 2 })).body.outcome, "processed");
    assert.equal(store.state.subs[0].status, "active");
    assert.deepEqual(store.state.subs[0].currentPeriodEnd, at(S + 30 * DAY));
  });

  test("a same-second pending arriving AFTER the recovery charge is ignored", async () => {
    const store = memoryWebhookStore([paidActive({ status: "pending", lastEventAt: at(T0), lastEventType: "subscription.pending" })]);
    await send(store, "subscription.charged", "active", S, "evt_C2", { paid: 2 });
    assert.equal((await send(store, "subscription.pending", "pending", S, "evt_Pe2")).body.outcome, "ignored");
    assert.equal(store.state.subs[0].status, "active");
  });

  test("halted -> charged in the same second is allowed (recovery); halted -> activated is not", async () => {
    const store = memoryWebhookStore([paidActive({ status: "halted", lastEventAt: at(S), lastEventType: "subscription.halted" })]);
    assert.equal((await send(store, "subscription.activated", "active", S, "evt_A3")).body.outcome, "ignored");
    assert.equal((await send(store, "subscription.charged", "active", S, "evt_C3", { paid: 2 })).body.outcome, "processed");
    assert.equal(store.state.subs[0].status, "active");
  });

  test("terminal states stay terminal for the same subscription ID: same second, newer, any event", async () => {
    for (const [type, status] of [["subscription.cancelled", "cancelled"], ["subscription.completed", "completed"]]) {
      const store = memoryWebhookStore([paidActive({ lastEventAt: at(T0), lastEventType: "subscription.charged" })]);
      assert.equal((await send(store, type, status, S, `evt_T_${status}`)).body.outcome, "processed");
      for (const [t2, s2, when] of [["subscription.charged", "active", S], ["subscription.resumed", "active", S], ["subscription.activated", "active", S + DAY],
        ["subscription.pending", "pending", S + DAY], ["subscription.charged", "active", S + 2 * DAY]]) {
        const r = await send(store, t2, s2, when, `evt_${status}_${t2}_${when}`);
        // A charge is flagged for reconciliation; nothing is applied either way.
        assert.equal(r.body.outcome, t2 === "subscription.charged" ? "reconciliation_required" : "ignored");
      }
      assert.equal(store.state.subs[0].status, status);
      assert.equal(computeEntitlement(store.state.subs[0], at(S + 3 * DAY)).plan, "FREE");
    }
    // expired is terminal too (Razorpay sends no expired webhook; it is only ever a stored state)
    const t = decideTransition(base({ status: "expired", lastEventAt: at(T0) }),
      parseWebhookEvent(enc(eventBody({ type: "subscription.charged", status: "active", id: RID, createdAt: S }))).event, at(S));
    assert.deepEqual(t, { apply: false, reason: "terminal_state" });
  });

  test("duplicates and strictly older events around a same-second pair", async () => {
    const store = memoryWebhookStore([paidActive({ lastEventAt: at(T0), lastEventType: "subscription.charged" })]);
    await send(store, "subscription.paused", "paused", S, "evt_P4");
    await send(store, "subscription.resumed", "active", S, "evt_R4");
    assert.equal((await send(store, "subscription.resumed", "active", S, "evt_R4")).body.outcome, "duplicate");
    assert.equal((await send(store, "subscription.paused", "paused", S, "evt_P4")).body.outcome, "duplicate");
    assert.equal((await send(store, "subscription.paused", "paused", S - 1, "evt_P5")).body.outcome, "ignored"); // older
    assert.equal(store.state.subs[0].status, "active");
    assert.equal(store.state.subs[0].lastEventType, "subscription.resumed");
  });

  test("edges table: every same-second event not listed for a state is rejected", () => {
    const ev = (type, status) => parseWebhookEvent(enc(eventBody({ type, status, id: RID, createdAt: S }))).event;
    assert.equal(decideTransition(base({ status: "paused", lastEventAt: at(S) }), ev("subscription.charged", "active"), at(S)).apply, false);
    assert.equal(decideTransition(base({ status: "pending", lastEventAt: at(S) }), ev("subscription.activated", "active"), at(S)).apply, false);
    assert.equal(decideTransition(base({ status: "active", lastEventAt: at(S) }), ev("subscription.resumed", "active"), at(S)).apply, false);
    assert.equal(decideTransition(base({ status: "active", lastEventAt: at(S) }), ev("subscription.authenticated", "authenticated"), at(S)).apply, false);
  });
});

// ---------------------------------------------------------------------------
describe("entitlement", () => {
  const paid = (o = {}) => row({ razorpaySubscriptionId: "sub_Old1", status: "active", paymentConfirmedAt: at(T0), currentPeriodEnd: at(T0 + 30 * DAY), ...o });
  const end = at(T0 + 30 * DAY).getTime();
  const plan = (state, ms) => computeEntitlement(state, new Date(ms)).plan;

  test("no subscription", () => assert.equal(computeEntitlement(null, new Date()).plan, "FREE"));
  test("active paid subscription", () => assert.deepEqual(computeEntitlement(paid(), at(T0 + DAY)), { plan: "PRO", reason: "active", accessUntil: at(T0 + 30 * DAY) }));
  test("active with stale paid-through date", () => assert.equal(plan(paid(), end), "FREE"));
  test("active with missing paid-through date or without payment evidence", () => {
    assert.equal(plan(paid({ currentPeriodEnd: null }), T0 * 1000), "FREE");
    assert.equal(plan(paid({ paymentConfirmedAt: null }), T0 * 1000), "FREE");
    assert.equal(plan(paid({ currentPeriodEnd: new Date(NaN) }), T0 * 1000), "FREE");
  });
  test("the stored plan column is ignored", () => assert.equal(plan({ ...paid({ status: "halted" }), plan: "PRO" }, T0 * 1000), "FREE"));
  test("pending renewal within grace, and the exact 72h boundary", () => {
    const p = paid({ status: "pending" });
    assert.equal(PENDING_GRACE_MS, 72 * 3600 * 1000);
    assert.equal(plan(p, end + 1), "PRO");
    assert.equal(plan(p, end + PENDING_GRACE_MS - 1), "PRO");
    assert.equal(plan(p, end + PENDING_GRACE_MS), "FREE");
    assert.equal(plan(p, end + PENDING_GRACE_MS + 1), "FREE");
  });
  test("pending initial checkout (no confirmed payment) is never PRO", () => {
    assert.equal(plan(row({ status: "pending", currentPeriodEnd: at(T0 + 30 * DAY) }), T0 * 1000), "FREE");
  });
  test("halted and paused are FREE immediately", () => {
    assert.equal(plan(paid({ status: "halted" }), T0 * 1000), "FREE");
    assert.equal(plan(paid({ status: "paused" }), T0 * 1000), "FREE");
  });
  test("resumed is PRO only once an eligible active state is confirmed", () => {
    // resumed with an old paid-through date: FREE until a new charge
    assert.equal(plan(paid({ currentPeriodEnd: at(T0 - DAY) }), T0 * 1000), "FREE");
    const t = decideTransition(paid({ status: "paused", lastEventAt: at(T0) }),
      parseWebhookEvent(enc(eventBody({ type: "subscription.resumed", status: "active", createdAt: T0 + 10, end: T0 + 40 * DAY }))).event, at(T0 + 20));
    assert.equal(t.patch.plan, "PRO");
  });
  test("cycle-end cancellation keeps PRO until the paid-through date", () => {
    const c = paid({ status: "cancelled", cancelMode: "CYCLE_END", cancelSubscriptionId: "sub_Old1" });
    assert.equal(plan(c, end - 1), "PRO");
    assert.equal(plan(c, end), "FREE");
  });
  test("cycle-end intent bound to a different subscription does not apply", () => {
    assert.equal(plan(paid({ status: "cancelled", cancelMode: "CYCLE_END", cancelSubscriptionId: "sub_Other" }), T0 * 1000), "FREE");
  });
  test("immediate or provider-side cancellation is FREE immediately", () => {
    assert.equal(plan(paid({ status: "cancelled", cancelMode: "IMMEDIATE", cancelSubscriptionId: "sub_Old1" }), T0 * 1000), "FREE");
    assert.equal(plan(paid({ status: "cancelled" }), T0 * 1000), "FREE");
  });
  test("completed, expired, created and authenticated are FREE", () => {
    for (const status of ["completed", "expired", "created", "authenticated"]) assert.equal(plan(paid({ status }), T0 * 1000), "FREE", status);
  });
});

// ---------------------------------------------------------------------------
describe("checkout", () => {
  /** Mimics the Prisma store: per-user serialised reserve, conditional updates. */
  function memoryCheckoutStore(users = { clerk_1: "user_1" }, rows = [], retired = []) {
    let queue = Promise.resolve();
    const store = {
      rows, retired,
      reserve(clerkUserId, attemptId, now, decide) {
        const run = queue.then(() => {
          const userId = users[clerkUserId];
          if (!userId) return { kind: "no_account" };
          const existing = rows.find((r) => r.userId === userId) ?? null;
          const d = decide(existing ? { ...existing } : null, archivedFor(retired, existing));
          if (d.kind === "reserve") {
            if (d.retire && !d.retireRecorded) {
              // Insert-only, unique provider ID: a duplicate aborts before anything changes.
              if (retired.some((x) => x.razorpaySubscriptionId === d.retire.razorpaySubscriptionId)) throw new Error("unique violation");
              retired.push({ ...d.retire });
            }
            const fresh = row({ id: `row_${userId}`, userId, checkoutAttemptId: attemptId, checkoutStatus: "RESERVED", checkoutStartedAt: now });
            if (existing) Object.assign(existing, fresh); else rows.push(fresh);
          }
          return { ...d, userId };
        });
        queue = run.catch(() => {});
        return run;
      },
      async bind(a, id) {
        const r = rows.find((x) => x.checkoutAttemptId === a);
        if (r && ["RESERVED", "UNRESOLVED"].includes(r.checkoutStatus) && (r.razorpaySubscriptionId === null || r.razorpaySubscriptionId === id)) {
          Object.assign(r, { razorpaySubscriptionId: id, checkoutStatus: "CREATED" });
          return true;
        }
        return r?.razorpaySubscriptionId === id;
      },
      async markFailed(a) { const r = rows.find((x) => x.checkoutAttemptId === a && ["RESERVED", "UNRESOLVED"].includes(x.checkoutStatus) && !x.razorpaySubscriptionId); if (r) r.checkoutStatus = "FAILED"; },
      async markUnresolved(a) { const r = rows.find((x) => x.checkoutAttemptId === a && x.checkoutStatus === "RESERVED"); if (r) r.checkoutStatus = "UNRESOLVED"; },
    };
    return store;
  }
  function fakeProvider(behaviour, { fetch = async () => { throw new Error("unexpected fetch"); }, cancel = async () => { throw new Error("unexpected cancel"); } } = {}) {
    const calls = [];
    const ops = [];
    return {
      calls, ops,
      create: async (notes) => { calls.push(notes); return behaviour(calls.length, notes); },
      fetch: async (id) => { ops.push(`fetch:${id}`); return fetch(id); },
      cancel: async (id) => { ops.push(`cancel:${id}`); return cancel(id); },
    };
  }
  const now = () => at(T0);

  test("attempt IDs are random and well-formed", () => {
    const a = newCheckoutAttemptId(), b = newCheckoutAttemptId();
    assert.match(a, /^ca_[A-Za-z0-9_-]{32}$/);
    assert.notEqual(a, b);
  });

  test("unauthenticated and unknown users never reach Razorpay", async () => {
    const provider = fakeProvider(() => ({ id: "sub_X" }));
    const store = memoryCheckoutStore();
    assert.deepEqual(await startCheckout({ clerkUserId: null, store, provider }), { ok: false, reason: "unauthenticated" });
    assert.deepEqual(await startCheckout({ clerkUserId: "clerk_ghost", store, provider }), { ok: false, reason: "no_account" });
    assert.equal(provider.calls.length, 0);
  });

  test("two simultaneous requests create one reservation and one remote subscription", async () => {
    let release;
    const gate = new Promise((r) => { release = r; });
    const provider = fakeProvider(async (n) => { await gate; return { id: `sub_N${n}` }; });
    const store = memoryCheckoutStore();
    const both = Promise.all([startCheckout({ clerkUserId: "clerk_1", store, provider, now }), startCheckout({ clerkUserId: "clerk_1", store, provider, now })]);
    await new Promise((r) => setTimeout(r, 10));
    release();
    const results = await both;
    assert.equal(provider.calls.length, 1);
    assert.deepEqual(results.map((r) => r.ok ? "ok" : r.reason).sort(), ["in_progress", "ok"]);
    assert.equal(store.rows.length, 1);
    assert.equal(store.rows[0].razorpaySubscriptionId, "sub_N1");
    assert.equal(provider.calls[0].checkoutAttemptId, store.rows[0].checkoutAttemptId);
    assert.equal(provider.calls[0].userId, "user_1");
  });

  test("an existing active subscription rejects another checkout", async () => {
    const provider = fakeProvider(() => ({ id: "sub_X" }));
    const store = memoryCheckoutStore(undefined, [row({ razorpaySubscriptionId: "sub_Old1", status: "active", paymentConfirmedAt: at(T0 - DAY), currentPeriodEnd: at(T0 + DAY) })]);
    assert.deepEqual(await startCheckout({ clerkUserId: "clerk_1", store, provider, now }), { ok: false, reason: "already_pro" });
    assert.equal(provider.calls.length, 0);
  });

  test("live but unentitled subscriptions (halted/paused/pending) block a second remote subscription", () => {
    for (const status of ["halted", "paused", "pending", "authenticated"]) {
      assert.deepEqual(decideCheckout(row({ status, razorpaySubscriptionId: "sub_Old1" }), at(T0)), { kind: "blocked", reason: "needs_attention" }, status);
    }
  });

  test("a recent bound-but-unpaid checkout is reopened, not duplicated", async () => {
    const provider = fakeProvider(() => ({ id: "sub_X" }));
    const store = memoryCheckoutStore(undefined, [row({ razorpaySubscriptionId: "sub_Open", checkoutAttemptId: ATTEMPT_A, checkoutStatus: "CREATED", checkoutStartedAt: at(T0 - 60) })]);
    assert.deepEqual(await startCheckout({ clerkUserId: "clerk_1", store, provider, now }), { ok: true, subscriptionId: "sub_Open" });
    assert.equal(provider.calls.length, 0);
  });

  test("ended subscriptions allow a new checkout, which clears the old mapping", async () => {
    const provider = fakeProvider(() => ({ id: "sub_New" }));
    const store = memoryCheckoutStore(undefined, [row({ razorpaySubscriptionId: "sub_Old1", status: "cancelled", lastEventAt: at(T0 - DAY) })]);
    assert.deepEqual(await startCheckout({ clerkUserId: "clerk_1", store, provider, now }), { ok: true, subscriptionId: "sub_New" });
    assert.equal(store.rows[0].lastEventAt, null);
    assert.equal(store.rows.length, 1);
    assert.deepEqual(store.retired.map((r) => [r.razorpaySubscriptionId, r.reason]), [["sub_Old1", "ALREADY_FINAL"]]); // archived, not dropped
  });

  test("a creation timeout leaves the reservation unresolved and never creates again", async () => {
    let resolveLate;
    const provider = fakeProvider(() => new Promise((r) => { resolveLate = r; }));
    const store = memoryCheckoutStore();
    let late;
    const r1 = await startCheckout({ clerkUserId: "clerk_1", store, provider, now, timeoutMs: 5, background: (p) => { late = p; } });
    assert.deepEqual(r1, { ok: false, reason: "provider_unconfirmed" });
    assert.equal(store.rows[0].checkoutStatus, "UNRESOLVED");
    const r2 = await startCheckout({ clerkUserId: "clerk_1", store, provider, now });
    assert.deepEqual(r2, { ok: false, reason: "unresolved" });
    assert.equal(provider.calls.length, 1);
    // The original call succeeds later: its ID is recorded, still no second create.
    resolveLate({ id: "sub_Late" });
    await late;
    assert.equal(store.rows[0].razorpaySubscriptionId, "sub_Late");
    assert.equal(store.rows[0].checkoutStatus, "CREATED");
    assert.equal(provider.calls.length, 1);
  });

  test("network errors and 5xx are ambiguous: unresolved, not failed", async () => {
    for (const err of [new TypeError("socket hang up"), { statusCode: 502, error: {} }, { statusCode: 408, error: {} }]) {
      const store = memoryCheckoutStore();
      const r = await startCheckout({ clerkUserId: "clerk_1", store, provider: fakeProvider(() => { throw err; }), now });
      assert.deepEqual(r, { ok: false, reason: "provider_unconfirmed" });
      assert.equal(store.rows[0].checkoutStatus, "UNRESOLVED");
    }
  });

  test("a provider 4xx rejection marks the reservation failed and allows a clean retry", async () => {
    const store = memoryCheckoutStore();
    const provider = fakeProvider((n) => { if (n === 1) throw { statusCode: 400, error: { code: "BAD_REQUEST_ERROR" } }; return { id: "sub_Retry" }; });
    assert.deepEqual(await startCheckout({ clerkUserId: "clerk_1", store, provider, now }), { ok: false, reason: "provider_rejected" });
    assert.equal(store.rows[0].checkoutStatus, "FAILED");
    assert.equal(store.rows[0].razorpaySubscriptionId, null);
    assert.deepEqual(await startCheckout({ clerkUserId: "clerk_1", store, provider, now }), { ok: true, subscriptionId: "sub_Retry" });
    assert.equal(store.rows.length, 1);
  });

  test("a malformed provider response is unresolved", async () => {
    const store = memoryCheckoutStore();
    const r = await startCheckout({ clerkUserId: "clerk_1", store, provider: fakeProvider(() => ({ id: "weird" })), now });
    assert.deepEqual(r, { ok: false, reason: "provider_unconfirmed" });
    assert.equal(store.rows[0].checkoutStatus, "UNRESOLVED");
  });

  // --- stale (> 24h) unpaid checkout: never detached on local age alone ---
  const STALE_AT = T0 - 2 * DAY;
  const staleRow = () => row({ id: "row_user_1", razorpaySubscriptionId: "sub_Stale1", status: "created", checkoutAttemptId: ATTEMPT_A, checkoutStatus: "CREATED", checkoutStartedAt: at(STALE_AT) });

  test("the create request sets no provider-side expiry (expire_by), so local age proves nothing", () => {
    // Documented by the decision: a stale created checkout is retired, never directly re-reserved.
    assert.deepEqual(decideCheckout(staleRow(), at(T0)), { kind: "retire", subscriptionId: "sub_Stale1" });
    assert.deepEqual(decideCheckout(staleRow(), at(STALE_AT + DAY - 1)), { kind: "reuse", subscriptionId: "sub_Stale1" });
  });

  test("stale checkout still 'created' at Razorpay: cancelled first, then replaced", async () => {
    const store = memoryCheckoutStore(undefined, [staleRow()]);
    const provider = fakeProvider(() => ({ id: "sub_Fresh1" }), { fetch: async () => ({ status: "created", paid_count: 0 }), cancel: async () => ({ status: "cancelled", paid_count: 0 }) });
    assert.deepEqual(await startCheckout({ clerkUserId: "clerk_1", store, provider, now }), { ok: true, subscriptionId: "sub_Fresh1" });
    assert.deepEqual(provider.ops, ["fetch:sub_Stale1", "cancel:sub_Stale1"]);
    assert.equal(provider.calls.length, 1);
    assert.equal(store.rows[0].razorpaySubscriptionId, "sub_Fresh1");
  });

  test("stale checkout already expired at Razorpay: replaced without a cancel call", async () => {
    const store = memoryCheckoutStore(undefined, [staleRow()]);
    const provider = fakeProvider(() => ({ id: "sub_Fresh2" }), { fetch: async () => ({ status: "expired", paid_count: 0 }) });
    assert.deepEqual(await startCheckout({ clerkUserId: "clerk_1", store, provider, now }), { ok: true, subscriptionId: "sub_Fresh2" });
    assert.deepEqual(provider.ops, ["fetch:sub_Stale1"]);
  });

  test("stale checkout that Razorpay says was paid keeps its mapping; no new subscription", async () => {
    for (const status of ["authenticated", "active"]) {
      const store = memoryCheckoutStore(undefined, [staleRow()]);
      const provider = fakeProvider(() => ({ id: "sub_Never" }), { fetch: async () => ({ status }) });
      assert.deepEqual(await startCheckout({ clerkUserId: "clerk_1", store, provider, now }), { ok: false, reason: "needs_attention" });
      assert.equal(provider.calls.length, 0);
      assert.deepEqual(store.rows[0], staleRow());
    }
  });

  test("fetch/cancel failure, timeout or a non-final cancel result keeps the mapping", async () => {
    const cases = [
      { fetch: async () => { throw new TypeError("network"); }, expect: "provider_unconfirmed" },
      { fetch: () => new Promise(() => {}), expect: "provider_unconfirmed" },
      { fetch: async () => ({ status: "created", paid_count: 0 }), cancel: async () => { throw { statusCode: 400, error: {} }; }, expect: "provider_unconfirmed" },
      { fetch: async () => ({ status: "created", paid_count: 0 }), cancel: async () => ({ status: "authenticated" }), expect: "needs_attention" },
      // Payment evidence (or a missing count) in the provider response is never an ordinary unpaid retirement.
      { fetch: async () => ({ status: "created", paid_count: 0 }), cancel: async () => ({ status: "cancelled", paid_count: 1 }), expect: "needs_attention" },
      { fetch: async () => ({ status: "created", paid_count: 0 }), cancel: async () => ({ status: "cancelled" }), expect: "needs_attention" },
      { fetch: async () => ({ status: "cancelled", paid_count: 1 }), expect: "needs_attention" },
      { fetch: async () => ({ status: "expired" }), expect: "needs_attention" },
      { fetch: async () => ({ status: "created", paid_count: 1 }), expect: "needs_attention" },
    ];
    for (const c of cases) {
      const store = memoryCheckoutStore(undefined, [staleRow()]);
      const provider = fakeProvider(() => ({ id: "sub_Never" }), c);
      assert.deepEqual(await startCheckout({ clerkUserId: "clerk_1", store, provider, now, timeoutMs: 10 }), { ok: false, reason: c.expect });
      assert.equal(provider.calls.length, 0);
      assert.deepEqual(store.rows[0], staleRow());
    }
  });

  test("late payment on the old checkout while a new checkout starts: the payment wins, nothing is detached", async () => {
    // Shared rows: the webhook and checkout stores operate on the same table.
    // The cancel response either still shows no paid cycle (provider lag: caught
    // by the re-decision under the lock) or already shows it (caught before that).
    for (const [cancelPaid, expected] of [[0, "already_pro"], [1, "needs_attention"]]) {
      const rows = [staleRow()];
      const retired = [];
      const webhooks = memoryWebhookStore(rows, { retired });
      const store = memoryCheckoutStore(undefined, rows, retired);
      const paid = eventBody({ type: "subscription.charged", status: "active", id: "sub_Stale1", createdAt: T0, notes: { userId: "user_1", checkoutAttemptId: ATTEMPT_A } });
      const provider = fakeProvider(() => ({ id: "sub_Never" }), {
        fetch: async () => ({ status: "created", paid_count: 0 }),
        // The customer completes the OLD link just before our cancel lands; its webhook arrives first.
        cancel: async () => { await deliver(webhooks, paid, { eventId: "evt_LatePay1", now: at(T0 + 1) }); return { status: "cancelled", paid_count: cancelPaid }; },
      });
      const r = await startCheckout({ clerkUserId: "clerk_1", store, provider, now });
      assert.deepEqual(r, { ok: false, reason: expected });
      assert.equal(provider.calls.length, 0);
      assert.equal(rows[0].razorpaySubscriptionId, "sub_Stale1");
      assert.equal(computeEntitlement(rows[0], at(T0 + 2)).plan, "PRO");
      assert.deepEqual(retired, []); // a paid subscription is never retired
    }
  });

  test("isDefiniteRejection classification", () => {
    assert.equal(isDefiniteRejection({ statusCode: 400 }), true);
    assert.equal(isDefiniteRejection({ statusCode: 429 }), true);
    for (const e of [{ statusCode: 408 }, { statusCode: 500 }, new Error("x"), null, undefined]) assert.equal(isDefiniteRejection(e), false);
  });
});

// ---------------------------------------------------------------------------
describe("retired subscriptions", () => {
  const STALE = row({ id: "row_user_1", razorpaySubscriptionId: "sub_Stale1", status: "created", checkoutAttemptId: ATTEMPT_A, checkoutStatus: "CREATED", checkoutStartedAt: at(T0 - 2 * DAY) });
  const NOTES = { userId: "user_1", checkoutAttemptId: ATTEMPT_A };
  const fixedNow = () => at(T0);

  /** Webhook and checkout stores sharing one subscriptions table and one retired table. */
  function world({ fetch = async () => ({ status: "created", paid_count: 0 }), cancel = async () => ({ status: "cancelled", paid_count: 0 }), freshId = "sub_Fresh1" } = {}) {
    const rows = [structuredClone(STALE)];
    const retired = [];
    const webhooks = memoryWebhookStore(rows, { retired });
    const ops = [];
    const provider = {
      creates: 0,
      async create() { this.creates++; return { id: freshId }; },
      async fetch(id) { ops.push(`fetch:${id}`); return fetch(id); },
      async cancel(id) { ops.push(`cancel:${id}`); return cancel(id); },
      // No refund method exists on the provider at all; recorded if anything tried.
      refund() { ops.push("refund"); throw new Error("refunds are never automatic"); },
    };
    const checkout = (p = provider) => startCheckout({ clerkUserId: "clerk_1", store: memoryCheckoutStoreShared(rows, retired), provider: p, now: fixedNow });
    return { rows, retired, webhooks, provider, ops, checkout };
  }
  // Same semantics as the checkout suite's store (kept local: that helper is scoped to its describe).
  function memoryCheckoutStoreShared(rows, retired) {
    return {
      async reserve(clerkUserId, attemptId, now, decide) {
        const existing = rows.find((r) => r.userId === "user_1") ?? null;
        const d = decide(existing ? { ...existing } : null, archivedFor(retired, existing));
        if (d.kind === "reserve") {
          if (d.retire && !d.retireRecorded) {
            if (retired.some((x) => x.razorpaySubscriptionId === d.retire.razorpaySubscriptionId)) throw new Error("unique violation");
            retired.push({ ...d.retire });
          }
          Object.assign(existing, row({ id: "row_user_1", userId: "user_1", checkoutAttemptId: attemptId, checkoutStatus: "RESERVED", checkoutStartedAt: now }));
        }
        return { ...d, userId: "user_1" };
      },
      async bind(a, id) { const r = rows.find((x) => x.checkoutAttemptId === a && x.razorpaySubscriptionId === null); if (!r) return false; Object.assign(r, { razorpaySubscriptionId: id, checkoutStatus: "CREATED" }); return true; },
      async markFailed() {}, async markUnresolved() {},
    };
  }
  const late = (w, type, status, eventId, extra = {}) =>
    deliver(w.webhooks, eventBody({ type, status, id: "sub_Stale1", createdAt: T0 + 30, notes: NOTES, ...extra }), { eventId });

  test("1. retirement preserves the original subscription, user and checkout attempt", async () => {
    const w = world();
    assert.deepEqual(await w.checkout(), { ok: true, subscriptionId: "sub_Fresh1" });
    assert.deepEqual(w.ops, ["fetch:sub_Stale1", "cancel:sub_Stale1"]);
    assert.deepEqual(w.retired, [{ razorpaySubscriptionId: "sub_Stale1", userId: "user_1", checkoutAttemptId: ATTEMPT_A, providerStatus: "cancelled", reason: "APP_CANCELLED", retiredAt: at(T0) }]);
    assert.notEqual(w.rows[0].checkoutAttemptId, ATTEMPT_A);
    assert.equal(w.rows[0].razorpaySubscriptionId, "sub_Fresh1");
  });

  test("1b. already-final at Razorpay: mapped with ALREADY_FINAL and no cancel call", async () => {
    const w = world({ fetch: async () => ({ status: "expired", paid_count: 0 }) });
    assert.equal((await w.checkout()).ok, true);
    assert.deepEqual(w.ops, ["fetch:sub_Stale1"]);
    assert.deepEqual([w.retired[0].reason, w.retired[0].providerStatus], ["ALREADY_FINAL", "expired"]);
  });

  test("2-5. late charge on a retired subscription: RECONCILIATION_REQUIRED for the original owner; current row, access and money untouched", async () => {
    const w = world();
    await w.checkout();
    const before = structuredClone(w.rows);
    const logs = [];
    const raw = eventBody({ type: "subscription.charged", status: "active", id: "sub_Stale1", createdAt: T0 + 30, notes: NOTES });
    const r = await deliver(w.webhooks, raw, { eventId: "evt_RetiredPay1", logs });
    assert.deepEqual([r.status, r.body.outcome], [200, "reconciliation_required"]);
    const e = w.webhooks.state.events.get("evt_RetiredPay1");
    assert.deepEqual([e.outcome, e.reason, e.userId, e.razorpaySubscriptionId, e.eventType],
      ["RECONCILIATION_REQUIRED", "retired_payment", "user_1", "sub_Stale1", "subscription.charged"]);
    assert.deepEqual(w.rows, before);                                                   // 3. current row untouched
    assert.equal(computeEntitlement(w.rows[0], at(T0 + 60)).plan, "FREE");             // 4. no automatic Pro
    assert.ok(!w.ops.includes("refund"));                                               // 5. no automatic refund
    assert.deepEqual(w.ops, ["fetch:sub_Stale1", "cancel:sub_Stale1"]);                 //    and no provider call at all from the webhook
    assert.ok(!logs.join("\n").includes("sub_Stale1") && !logs.join("\n").includes("user_1"));
  });

  test("2b. only a charge notification is flagged; historical paid_count on status events is not a new charge", () => {
    const parse = (o) => parseWebhookEvent(enc(eventBody({ id: "sub_Stale1", ...o }))).event;
    const cases = [
      [{ type: "subscription.charged", status: "active" }, "RECONCILIATION_REQUIRED", "retired_payment"],
      // A charge notification without full evidence is still flagged (uncertain, never trusted for access).
      [{ type: "subscription.charged", status: "active", paid: 0 }, "RECONCILIATION_REQUIRED", "retired_charge_unverified"],
      [{ type: "subscription.charged", status: "cancelled" }, "RECONCILIATION_REQUIRED", "retired_charge_unverified"],
      [{ type: "subscription.charged", status: "active", end: T0 }, "RECONCILIATION_REQUIRED", "retired_charge_unverified"],
      // paid_count is cumulative: on status / terminal notifications it is history.
      [{ type: "subscription.cancelled", status: "cancelled", paid: 3 }, "IGNORED", "retired_subscription"],
      [{ type: "subscription.completed", status: "completed", paid: 120 }, "IGNORED", "retired_subscription"],
      [{ type: "subscription.activated", status: "active", paid: 1 }, "IGNORED", "retired_subscription"],
      [{ type: "subscription.halted", status: "halted", paid: 2 }, "IGNORED", "retired_subscription"],
      [{ type: "subscription.cancelled", status: "cancelled", paid: 0 }, "IGNORED", "retired_subscription"],
      [{ type: "subscription.authenticated", status: "authenticated", paid: 0 }, "IGNORED", "retired_authenticated"],
    ];
    for (const [o, outcome, reason] of cases) assert.deepEqual(decideRetiredEvent(parse(o)), { outcome, reason }, JSON.stringify(o));
  });

  test("6. late activation / authentication / cancellation cannot revive or overwrite the current subscription", async () => {
    const w = world();
    await w.checkout();
    // The new subscription is paid and active.
    await deliver(w.webhooks, eventBody({ type: "subscription.charged", status: "active", id: "sub_Fresh1", createdAt: T0 + 10, notes: { userId: "user_1", checkoutAttemptId: w.rows[0].checkoutAttemptId } }), { eventId: "evt_FreshPay" });
    const before = structuredClone(w.rows);
    assert.equal(computeEntitlement(before[0], at(T0 + 60)).plan, "PRO");
    for (const [type, status, paid] of [["subscription.activated", "active", 0], ["subscription.authenticated", "authenticated", 0], ["subscription.cancelled", "cancelled", 0],
      ["subscription.halted", "halted", 0], ["subscription.completed", "completed", 0]]) {
      const r = await late(w, type, status, `evt_Late_${status}`, { paid });
      assert.equal(r.body.outcome, "ignored", type);
      assert.equal(w.webhooks.state.events.get(`evt_Late_${status}`).userId, "user_1");
    }
    assert.deepEqual(w.rows, before);
    assert.equal(computeEntitlement(w.rows[0], at(T0 + 60)).plan, "PRO");
  });

  test("7. duplicate late events are idempotent", async () => {
    const w = world();
    await w.checkout();
    const results = await Promise.all([1, 2, 3].map(() => late(w, "subscription.charged", "active", "evt_DupRetired")));
    assert.deepEqual(results.map((r) => r.body.outcome).sort(), ["duplicate", "duplicate", "reconciliation_required"]);
    assert.equal((await late(w, "subscription.charged", "active", "evt_DupRetired")).body.outcome, "duplicate");
    assert.equal([...w.webhooks.state.events.values()].filter((e) => e.outcome === "RECONCILIATION_REQUIRED").length, 1);
  });

  test("8. unknown provider subscription IDs stay ignored with no user association", async () => {
    const w = world();
    await w.checkout();
    const r = await deliver(w.webhooks, eventBody({ type: "subscription.charged", status: "active", id: "sub_Nobody9", notes: { userId: "user_1" } }), { eventId: "evt_Nobody" });
    assert.equal(r.body.outcome, "ignored");
    const e = w.webhooks.state.events.get("evt_Nobody");
    assert.deepEqual([e.reason, e.userId], ["unknown_subscription", null]);
  });

  test("9. webhook first: a payment recorded before retirement re-decides blocks detachment", async () => {
    // fetch/cancel still say unpaid (provider lag), but the charge webhook took the row first.
    const w = world({ cancel: async () => { await late(w, "subscription.charged", "active", "evt_First"); return { status: "cancelled", paid_count: 0 }; } });
    assert.deepEqual(await w.checkout(), { ok: false, reason: "already_pro" });
    assert.deepEqual([w.rows[0].razorpaySubscriptionId, w.rows[0].checkoutAttemptId, w.retired.length], ["sub_Stale1", ATTEMPT_A, 0]);
    assert.equal(w.webhooks.state.events.get("evt_First").outcome, "PROCESSED");
  });

  test("10. retirement first: the payment event that follows is flagged, not applied", async () => {
    const w = world();
    await w.checkout();
    assert.equal((await late(w, "subscription.charged", "active", "evt_After")).body.outcome, "reconciliation_required");
    assert.equal(w.rows[0].razorpaySubscriptionId, "sub_Fresh1");
  });

  test("10b. our own cancel's webhook landing before the retirement transaction still yields a mapping", async () => {
    const w = world({ cancel: async () => { await late(w, "subscription.cancelled", "cancelled", "evt_OwnCancel", { paid: 0 }); return { status: "cancelled", paid_count: 0 }; } });
    assert.equal((await w.checkout()).ok, true);
    assert.equal(w.webhooks.state.events.get("evt_OwnCancel").outcome, "PROCESSED"); // applied while still mapped
    assert.deepEqual(w.retired.map((r) => [r.razorpaySubscriptionId, r.checkoutAttemptId]), [["sub_Stale1", ATTEMPT_A]]);
  });

  test("11. a payment already recorded on the row (paid, then cancelled) is never silently detached", () => {
    const paidThenCancelled = { ...STALE, status: "cancelled", paymentConfirmedAt: at(T0 - DAY), currentPeriodEnd: at(T0 - 1) };
    const retirement = { subscriptionId: "sub_Stale1", providerStatus: "cancelled", reason: "APP_CANCELLED" };
    assert.deepEqual(decideCheckout(paidThenCancelled, at(T0), retirement), { kind: "blocked", reason: "needs_attention" });
    // Without the confirmed payment the same row is retired, with its mapping.
    assert.equal(decideCheckout({ ...paidThenCancelled, paymentConfirmedAt: null }, at(T0), retirement).retire.razorpaySubscriptionId, "sub_Stale1");
  });

  test("12. a second checkout (and its own retirement) never overwrites the first mapping", async () => {
    const w = world();
    await w.checkout();
    const first = structuredClone(w.retired[0]);
    // The new checkout goes stale too and is retired in turn.
    Object.assign(w.rows[0], { checkoutStartedAt: at(T0 - 3 * DAY) });
    const secondAttempt = w.rows[0].checkoutAttemptId;
    const p2 = { ...w.provider, creates: 0, async create() { return { id: "sub_Fresh2" }; } };
    assert.deepEqual(await w.checkout(p2), { ok: true, subscriptionId: "sub_Fresh2" });
    assert.deepEqual(w.retired[0], first);
    assert.deepEqual(w.retired.map((r) => [r.razorpaySubscriptionId, r.checkoutAttemptId]), [["sub_Stale1", ATTEMPT_A], ["sub_Fresh1", secondAttempt]]);
    // The row points at an already-mapped ID again, with a DIFFERENT attempt: conflicting ownership,
    // nothing written, no provider call.
    Object.assign(w.rows[0], { ...STALE, checkoutAttemptId: ATTEMPT_B });
    const conflicted = structuredClone(w.rows[0]);
    const p3 = { ...w.provider, async create() { throw new Error("must not be reached"); } };
    assert.deepEqual(await w.checkout(p3), { ok: false, reason: "needs_attention" });
    assert.deepEqual(w.rows[0], conflicted);
    assert.deepEqual(w.retired[0], first);
    assert.equal(w.retired.length, 2);
  });

  test("an early-bind candidate is never bound to a retired provider ID", async () => {
    const retired = [{ razorpaySubscriptionId: "sub_Stale1", userId: "user_1", checkoutAttemptId: ATTEMPT_A, providerStatus: "cancelled", reason: "APP_CANCELLED", retiredAt: at(T0) }];
    // Defensive scenario: a reservation that (somehow) still carries the retired attempt ID.
    const rows = [row({ id: "row_user_1", checkoutAttemptId: ATTEMPT_A, checkoutStatus: "RESERVED" })];
    const webhooks = memoryWebhookStore(rows, { retired });
    const r = await deliver(webhooks, eventBody({ type: "subscription.charged", status: "active", id: "sub_Stale1", notes: NOTES }), { eventId: "evt_NoBind" });
    assert.equal(r.body.outcome, "reconciliation_required");
    assert.equal(rows[0].razorpaySubscriptionId, null);
  });
});

// ---------------------------------------------------------------------------
describe("archiving subscriptions that ended via webhooks", () => {
  const ENDED = (status, extra = {}) => row({ id: "row_user_1", razorpaySubscriptionId: "sub_Ended1", status, checkoutAttemptId: ATTEMPT_A,
    checkoutStatus: "CREATED", checkoutStartedAt: at(T0 - 40 * DAY), lastEventAt: at(T0 - DAY), lastEventType: `subscription.${status}`, ...extra });
  const NOTES = { userId: "user_1", checkoutAttemptId: ATTEMPT_A };

  function world(start, retired = []) {
    const rows = [structuredClone(start)];
    const webhooks = memoryWebhookStore(rows, { retired });
    const ops = [];
    const provider = {
      async create() { ops.push("create"); return { id: "sub_Next1" }; },
      async fetch() { ops.push("fetch"); throw new Error("an ended subscription needs no provider check"); },
      async cancel() { ops.push("cancel"); throw new Error("an ended subscription is never cancelled again"); },
    };
    const store = {
      async reserve(clerkUserId, attemptId, now, decide) {
        const existing = rows[0];
        const d = decide({ ...existing }, archivedFor(retired, existing));
        if (d.kind === "reserve") {
          if (d.retire && !d.retireRecorded) {
            if (retired.some((x) => x.razorpaySubscriptionId === d.retire.razorpaySubscriptionId)) throw new Error("unique violation");
            retired.push({ ...d.retire });
          }
          Object.assign(existing, row({ id: "row_user_1", userId: "user_1", checkoutAttemptId: attemptId, checkoutStatus: "RESERVED", checkoutStartedAt: now }));
        }
        return { ...d, userId: "user_1" };
      },
      async bind(a, id) { const r = rows[0]; if (r.checkoutAttemptId !== a || r.razorpaySubscriptionId) return false; Object.assign(r, { razorpaySubscriptionId: id, checkoutStatus: "CREATED" }); return true; },
      async markFailed() {}, async markUnresolved() {},
    };
    const checkout = () => startCheckout({ clerkUserId: "clerk_1", store, provider, now: () => at(T0) });
    return { rows, retired, webhooks, ops, checkout };
  }

  test("cancelled / completed / expired: archived as ALREADY_FINAL with original owner and attempt, in the reservation itself", async () => {
    for (const status of ["cancelled", "completed", "expired"]) {
      const w = world(ENDED(status));
      assert.deepEqual(await w.checkout(), { ok: true, subscriptionId: "sub_Next1" });
      assert.deepEqual(w.retired, [{ razorpaySubscriptionId: "sub_Ended1", userId: "user_1", checkoutAttemptId: ATTEMPT_A,
        providerStatus: status, reason: "ALREADY_FINAL", retiredAt: at(T0) }], status);
      assert.deepEqual(w.ops, ["create"], status); // no provider fetch/cancel: the status came from verified webhooks
      assert.deepEqual([w.rows[0].razorpaySubscriptionId, w.rows[0].checkoutAttemptId === ATTEMPT_A], ["sub_Next1", false]);
    }
  });

  test("legacy ended row without a checkout attempt is archived with a null attempt", async () => {
    const w = world(ENDED("cancelled", { checkoutAttemptId: null, checkoutStatus: null }));
    assert.equal((await w.checkout()).ok, true);
    assert.equal(w.retired[0].checkoutAttemptId, null);
  });

  test("a formerly paid, now ended subscription is archived too (cycle-end access over)", async () => {
    const w = world(ENDED("cancelled", { paymentConfirmedAt: at(T0 - 40 * DAY), currentPeriodEnd: at(T0 - 10 * DAY) }));
    assert.equal((await w.checkout()).ok, true);
    assert.equal(w.retired[0].razorpaySubscriptionId, "sub_Ended1");
  });

  test("late charged for the archived subscription: flagged against the old account; new subscription untouched; no Pro", async () => {
    const w = world(ENDED("cancelled"));
    await w.checkout();
    const before = structuredClone(w.rows);
    const charge = eventBody({ type: "subscription.charged", status: "active", id: "sub_Ended1", createdAt: T0 + 30, notes: NOTES });
    const results = await Promise.all([1, 2].map(() => deliver(w.webhooks, charge, { eventId: "evt_EndedPay" })));
    assert.deepEqual(results.map((r) => r.body.outcome).sort(), ["duplicate", "reconciliation_required"]);
    assert.equal((await deliver(w.webhooks, charge, { eventId: "evt_EndedPay" })).body.outcome, "duplicate");
    const e = w.webhooks.state.events.get("evt_EndedPay");
    assert.deepEqual([e.outcome, e.reason, e.userId, e.razorpaySubscriptionId], ["RECONCILIATION_REQUIRED", "retired_payment", "user_1", "sub_Ended1"]);
    assert.equal((await deliver(w.webhooks, eventBody({ type: "subscription.activated", status: "active", id: "sub_Ended1", paid: 0, createdAt: T0 + 40, notes: NOTES }), { eventId: "evt_EndedAct" })).body.outcome, "ignored");
    assert.deepEqual(w.rows, before);
    assert.equal(computeEntitlement(w.rows[0], at(T0 + 60)).plan, "FREE");
    assert.deepEqual(w.ops, ["create"]); // no refund or any other provider call from webhooks
  });

  test("an existing mapping with the same owner and attempt is kept as is, never overwritten", async () => {
    const original = { razorpaySubscriptionId: "sub_Ended1", userId: "user_1", checkoutAttemptId: ATTEMPT_A, providerStatus: "cancelled", reason: "APP_CANCELLED", retiredAt: at(T0 - 5 * DAY) };
    const w = world(ENDED("expired"), [structuredClone(original)]);
    assert.equal((await w.checkout()).ok, true);
    assert.deepEqual(w.retired, [original]); // still APP_CANCELLED / cancelled / its own timestamp
    assert.equal(w.rows[0].razorpaySubscriptionId, "sub_Next1");
  });

  test("conflicting ownership (other user or other attempt) fails safely: nothing written, nothing created", async () => {
    for (const conflict of [{ userId: "user_2" }, { checkoutAttemptId: ATTEMPT_B }, { checkoutAttemptId: null }]) {
      const mapping = { razorpaySubscriptionId: "sub_Ended1", userId: "user_1", checkoutAttemptId: ATTEMPT_A, providerStatus: "cancelled", reason: "ALREADY_FINAL", retiredAt: at(T0 - DAY), ...conflict };
      const w = world(ENDED("cancelled"), [structuredClone(mapping)]);
      const before = structuredClone(w.rows);
      assert.deepEqual(await w.checkout(), { ok: false, reason: "needs_attention" }, JSON.stringify(conflict));
      assert.deepEqual(w.rows, before);
      assert.deepEqual(w.retired, [mapping]);
      assert.deepEqual(w.ops, []);
    }
  });

  test("non-terminal subscriptions are never archived by this path, whatever their local age", () => {
    const ancient = at(T0 + 3650 * DAY);
    for (const status of ["authenticated", "active", "pending", "halted", "paused"]) {
      const d = decideCheckout(ENDED(status), ancient);
      assert.deepEqual(d, { kind: "blocked", reason: "needs_attention" }, status);
    }
    // "created" is only ever retired through the provider check, never archived on age.
    assert.deepEqual(decideCheckout(ENDED("created"), ancient), { kind: "retire", subscriptionId: "sub_Ended1" });
    // Still entitled under a cycle-end cancellation: blocked, not archived.
    const cycleEnd = ENDED("cancelled", { paymentConfirmedAt: at(T0 - DAY), currentPeriodEnd: at(T0 + 10 * DAY), cancelMode: "CYCLE_END", cancelSubscriptionId: "sub_Ended1" });
    assert.deepEqual(decideCheckout(cycleEnd, at(T0)), { kind: "blocked", reason: "already_pro" });
    // Nothing remote (failed checkout): plain reservation, nothing to archive.
    assert.deepEqual(decideCheckout(row({ checkoutAttemptId: ATTEMPT_A, checkoutStatus: "FAILED" }), at(T0)), { kind: "reserve" });
    // Open reservations are never touched.
    assert.equal(decideCheckout(ENDED("cancelled", { checkoutStatus: "UNRESOLVED" }), at(T0)).kind, "blocked");
  });
});

// ---------------------------------------------------------------------------
describe("late charge on a terminal subscription that is still the current mapping", () => {
  const RID = "sub_Term1";
  const NOTES = { userId: "user_1", checkoutAttemptId: ATTEMPT_A };
  const paidActive = () => row({ id: "row_user_1", razorpaySubscriptionId: RID, status: "active", checkoutAttemptId: ATTEMPT_A, checkoutStatus: "CREATED",
    checkoutStartedAt: at(T0 - 40 * DAY), paymentConfirmedAt: at(T0 - 30 * DAY), currentPeriodEnd: at(T0 + DAY), lastEventAt: at(T0 - 30 * DAY), lastEventType: "subscription.charged" });
  const ev = (type, status, createdAt, extra = {}) => eventBody({ type, status, id: RID, createdAt, notes: NOTES, ...extra });

  /** A cancelled (via webhook), still-mapped subscription, plus a checkout store on the same tables. */
  async function ended(retired = []) {
    const rows = [paidActive()];
    const webhooks = memoryWebhookStore(rows, { retired });
    assert.equal((await deliver(webhooks, ev("subscription.cancelled", "cancelled", T0 + 100, { endedAt: T0 + 100 }), { eventId: "evt_Cancel" })).body.outcome, "processed");
    const checkoutStore = {
      async reserve(clerkUserId, attemptId, now, decide) {
        const existing = rows[0];
        const d = decide({ ...existing }, archivedFor(retired, existing));
        if (d.kind === "reserve") {
          if (d.retire && !d.retireRecorded) {
            if (retired.some((x) => x.razorpaySubscriptionId === d.retire.razorpaySubscriptionId)) throw new Error("unique violation");
            retired.push({ ...d.retire });
          }
          Object.assign(existing, row({ id: "row_user_1", userId: "user_1", checkoutAttemptId: attemptId, checkoutStatus: "RESERVED", checkoutStartedAt: now }));
        }
        return { ...d, userId: "user_1" };
      },
      async bind(a, id) { const r = rows[0]; if (r.checkoutAttemptId !== a || r.razorpaySubscriptionId) return false; Object.assign(r, { razorpaySubscriptionId: id, checkoutStatus: "CREATED" }); return true; },
      async markFailed() {}, async markUnresolved() {},
    };
    const ops = [];
    const provider = { async create() { ops.push("create"); return { id: "sub_Next9" }; }, async fetch() { ops.push("fetch"); }, async cancel() { ops.push("cancel"); } };
    const checkout = () => startCheckout({ clerkUserId: "clerk_1", store: checkoutStore, provider, now: () => at(T0 + 500) });
    return { rows, retired, webhooks, ops, checkout };
  }

  test("1-3, 5. terminal then late charged (before any replacement): flagged with the original owner + attempt archived; row untouched; no Pro", async () => {
    const w = await ended();
    const before = structuredClone(w.rows);
    const r = await deliver(w.webhooks, ev("subscription.charged", "active", T0 + 50), { eventId: "evt_LateCharge", now: at(T0 + 200) });
    assert.deepEqual([r.status, r.body.outcome], [200, "reconciliation_required"]);
    const e = w.webhooks.state.events.get("evt_LateCharge");
    assert.deepEqual([e.outcome, e.reason, e.userId, e.razorpaySubscriptionId], ["RECONCILIATION_REQUIRED", "terminal_payment", "user_1", RID]);
    assert.deepEqual(w.retired, [{ razorpaySubscriptionId: RID, userId: "user_1", checkoutAttemptId: ATTEMPT_A, providerStatus: "cancelled", reason: "ALREADY_FINAL", retiredAt: at(T0 + 200) }]);
    assert.deepEqual(w.rows, before);
    assert.equal(computeEntitlement(w.rows[0], at(T0 + 300)).plan, "FREE");
  });

  test("a charge newer than the cancellation is flagged the same way (terminal stays terminal)", async () => {
    const w = await ended();
    assert.equal((await deliver(w.webhooks, ev("subscription.charged", "active", T0 + 400, { end: T0 + 400 + 30 * DAY }), { eventId: "evt_Newer" })).body.outcome, "reconciliation_required");
    assert.equal(w.rows[0].status, "cancelled");
  });

  test("6. duplicates are idempotent; a second distinct charge reuses the one mapping", async () => {
    const w = await ended();
    const charge = ev("subscription.charged", "active", T0 + 50);
    const rs = await Promise.all([1, 2, 3].map(() => deliver(w.webhooks, charge, { eventId: "evt_DupTerm" })));
    assert.deepEqual(rs.map((r) => r.body.outcome).sort(), ["duplicate", "duplicate", "reconciliation_required"]);
    assert.equal((await deliver(w.webhooks, ev("subscription.charged", "active", T0 + 60), { eventId: "evt_SecondTerm" })).body.outcome, "reconciliation_required");
    assert.equal(w.retired.length, 1);
    assert.equal([...w.webhooks.state.events.values()].filter((e) => e.outcome === "RECONCILIATION_REQUIRED").length, 2);
  });

  test("2, 4. a later replacement checkout keeps the webhook-created mapping; later events never touch the newer subscription", async () => {
    const w = await ended();
    await deliver(w.webhooks, ev("subscription.charged", "active", T0 + 50), { eventId: "evt_PreReplace", now: at(T0 + 200) });
    const mapping = structuredClone(w.retired);
    assert.deepEqual(await w.checkout(), { ok: true, subscriptionId: "sub_Next9" });
    assert.deepEqual(w.ops, ["create"]);
    assert.deepEqual(w.retired, mapping); // recognised as the same ownership: kept, not re-inserted
    const newer = structuredClone(w.rows);
    const r = await deliver(w.webhooks, ev("subscription.charged", "active", T0 + 70), { eventId: "evt_PostReplace" });
    assert.equal(r.body.outcome, "reconciliation_required");
    assert.deepEqual([w.webhooks.state.events.get("evt_PostReplace").reason, w.webhooks.state.events.get("evt_PostReplace").userId], ["retired_payment", "user_1"]);
    assert.deepEqual(w.rows, newer);
  });

  test("7-8. cancellation with historical paid_count, authentication and status events stay terminal_state; no mapping created", async () => {
    const w = await ended();
    for (const [type, status, paid, id] of [["subscription.cancelled", "cancelled", 5, "evt_HistCancel"], ["subscription.completed", "completed", 5, "evt_HistDone"],
      ["subscription.authenticated", "authenticated", 0, "evt_Auth"], ["subscription.activated", "active", 1, "evt_Act"], ["subscription.halted", "halted", 2, "evt_Halt"]]) {
      const r = await deliver(w.webhooks, ev(type, status, T0 + 150, { paid }), { eventId: id });
      assert.equal(r.body.outcome, "ignored", type);
      assert.deepEqual([w.webhooks.state.events.get(id).reason, w.webhooks.state.events.get(id).userId], ["terminal_state", null], type);
    }
    assert.deepEqual(w.retired, []);
  });

  test("an unverified charge notification is still flagged, distinctly", async () => {
    const w = await ended();
    await deliver(w.webhooks, ev("subscription.charged", "active", T0 + 50, { paid: 0 }), { eventId: "evt_Unverified" });
    const e = w.webhooks.state.events.get("evt_Unverified");
    assert.deepEqual([e.outcome, e.reason, e.userId], ["RECONCILIATION_REQUIRED", "terminal_charge_unverified", "user_1"]);
  });

  test("conflicting existing mapping: recorded as reconciliation_conflict, nothing overwritten or applied", async () => {
    const clash = { razorpaySubscriptionId: RID, userId: "user_2", checkoutAttemptId: ATTEMPT_B, providerStatus: "expired", reason: "ALREADY_FINAL", retiredAt: at(T0) };
    const w = await ended([structuredClone(clash)]);
    const before = structuredClone(w.rows);
    assert.equal((await deliver(w.webhooks, ev("subscription.charged", "active", T0 + 50), { eventId: "evt_Clash" })).body.outcome, "ignored");
    assert.deepEqual([w.webhooks.state.events.get("evt_Clash").reason, w.webhooks.state.events.get("evt_Clash").userId], ["reconciliation_conflict", null]);
    assert.deepEqual(w.retired, [clash]);
    assert.deepEqual(w.rows, before);
  });

  test("notes naming another user: flagged for the LOCAL owner as notes_user_mismatch; nothing archived or applied", async () => {
    const w = await ended();
    const before = structuredClone(w.rows);
    const r = await deliver(w.webhooks, eventBody({ type: "subscription.charged", status: "active", id: RID, createdAt: T0 + 50, notes: { userId: "user_2" } }), { eventId: "evt_WrongNotes" });
    assert.equal(r.body.outcome, "reconciliation_required");
    const e = w.webhooks.state.events.get("evt_WrongNotes");
    assert.deepEqual([e.reason, e.userId], ["notes_user_mismatch", "user_1"]);
    assert.deepEqual(w.retired, []);
    assert.deepEqual(w.rows, before);
  });

  test("a failure after the mapping insert rolls the mapping back with the event; the retry flags it", async () => {
    const w = await ended();
    const failing = { transaction: (fn) => w.webhooks.transaction((tx) => fn({ ...tx, async finishEvent() { throw new Error("injected"); } })) };
    const charge = ev("subscription.charged", "active", T0 + 50);
    assert.equal((await deliver(failing, charge, { eventId: "evt_Rollback" })).status, 503);
    assert.deepEqual(w.retired, []);
    assert.equal(w.webhooks.state.events.has("evt_Rollback"), false);
    assert.equal((await deliver(w.webhooks, charge, { eventId: "evt_Rollback" })).body.outcome, "reconciliation_required");
    assert.equal(w.retired.length, 1);
  });
});

// ---------------------------------------------------------------------------
describe("notes disagreeing with the local owner", () => {
  const RID = "sub_Notes1";
  const live = (extra = {}) => row({ id: "row_user_1", razorpaySubscriptionId: RID, status: "authenticated", checkoutAttemptId: ATTEMPT_A, checkoutStatus: "CREATED", ...extra });
  const otherUser = row({ id: "row_user_2", userId: "user_2", razorpaySubscriptionId: "sub_Notes2", status: "created", checkoutAttemptId: ATTEMPT_B, checkoutStatus: "CREATED" });
  const charge = (o = {}) => eventBody({ type: "subscription.charged", status: "active", id: RID, notes: { userId: "user_2", checkoutAttemptId: ATTEMPT_A }, ...o });

  test("current subscription matched by exact ID, notes user differs: RECONCILIATION_REQUIRED for the original owner; nobody gets Pro", async () => {
    const store = memoryWebhookStore([live(), structuredClone(otherUser)]);
    const before = structuredClone(store.state.subs);
    const r = await deliver(store, charge(), { eventId: "evt_N1" });
    assert.deepEqual([r.status, r.body.outcome], [200, "reconciliation_required"]);
    const e = store.state.events.get("evt_N1");
    assert.deepEqual([e.outcome, e.reason, e.userId, e.razorpaySubscriptionId], ["RECONCILIATION_REQUIRED", "notes_user_mismatch", "user_1", RID]);
    assert.deepEqual(store.state.subs, before); // neither the owner's row nor the notes user's row changes
    for (const s of store.state.subs) assert.equal(computeEntitlement(s, at(T0 + 60)).plan, "FREE");
    assert.deepEqual(store.state.retired, []);
  });

  test("attempt disagreement on the exact ID is flagged the same way, distinctly", async () => {
    const store = memoryWebhookStore([live()]);
    await deliver(store, charge({ notes: { userId: "user_1", checkoutAttemptId: ATTEMPT_B } }), { eventId: "evt_N2" });
    assert.deepEqual([store.state.events.get("evt_N2").reason, store.state.events.get("evt_N2").userId], ["attempt_mismatch", "user_1"]);
  });

  // --- lifecycle events with mismatched notes on the EXACT local mapping ---
  const X = T0 + 9 * DAY; // stored paid-through date
  const paid = (extra = {}) => live({ status: "active", paymentConfirmedAt: at(T0 - DAY), currentPeriodEnd: at(X), lastEventAt: at(T0 - DAY), lastEventType: "subscription.charged", ...extra });
  const mm = (type, status, createdAt, extra = {}) => eventBody({ type, status, id: RID, createdAt, notes: { userId: "user_2" }, ...extra });
  const got = (store, id) => { const e = store.state.events.get(id); return [e.outcome, e.reason, e.userId]; };

  test("cancelled with mismatched notes revokes access for the exact local owner (warning recorded); other users untouched", async () => {
    const store = memoryWebhookStore([paid(), structuredClone(otherUser)]);
    assert.equal(computeEntitlement(store.state.subs[0], at(T0 + 20)).plan, "PRO");
    const r = await deliver(store, mm("subscription.cancelled", "cancelled", T0 + 10, { endedAt: T0 + 10 }), { eventId: "evt_L1" });
    assert.deepEqual([r.status, r.body.outcome], [200, "processed"]);
    assert.deepEqual(got(store, "evt_L1"), ["PROCESSED", "applied_notes_user_mismatch", "user_1"]);
    assert.deepEqual([store.state.subs[0].status, store.state.subs[0].endedAt], ["cancelled", at(T0 + 10)]);
    assert.equal(computeEntitlement(store.state.subs[0], at(T0 + 20)).plan, "FREE");
    assert.deepEqual(store.state.subs[1], otherUser);
  });

  test("halted and paused with mismatched notes apply the restrictive transition; paid-through data is preserved, not extended", async () => {
    for (const [type, status] of [["subscription.halted", "halted"], ["subscription.paused", "paused"]]) {
      const store = memoryWebhookStore([paid()]);
      await deliver(store, mm(type, status, T0 + 10, { end: X + 60 * DAY }), { eventId: `evt_L2${status}` });
      assert.deepEqual(got(store, `evt_L2${status}`), ["PROCESSED", "applied_notes_user_mismatch", "user_1"], type);
      const s = store.state.subs[0];
      assert.deepEqual([s.status, s.currentPeriodEnd, s.paymentConfirmedAt], [status, at(X), at(T0 - DAY)], type);
      assert.equal(computeEntitlement(s, at(T0 + 20)).plan, "FREE", type);
    }
  });

  test("an attempt disagreement on a restrictive event is applied and warned distinctly", async () => {
    const store = memoryWebhookStore([paid()]);
    await deliver(store, eventBody({ type: "subscription.halted", status: "halted", id: RID, createdAt: T0 + 10, notes: { userId: "user_1", checkoutAttemptId: ATTEMPT_B } }), { eventId: "evt_L2a" });
    assert.deepEqual(got(store, "evt_L2a"), ["PROCESSED", "applied_attempt_mismatch", "user_1"]);
    assert.equal(store.state.subs[0].status, "halted");
  });

  test("completed applies and stays terminal; expired stays terminal; nothing mismatched (or matching) revives them", async () => {
    const store = memoryWebhookStore([paid()]);
    await deliver(store, mm("subscription.completed", "completed", T0 + 10, { endedAt: T0 + 10 }), { eventId: "evt_L3c" });
    assert.deepEqual(got(store, "evt_L3c"), ["PROCESSED", "applied_notes_user_mismatch", "user_1"]);
    for (const [type, status, when, notes, id] of [["subscription.resumed", "active", T0 + 10, { userId: "user_2" }, "evt_L3r"],
      ["subscription.activated", "active", T0 + 20, { userId: "user_2" }, "evt_L3a"], ["subscription.resumed", "active", T0 + 30, { userId: "user_1" }, "evt_L3rm"],
      ["subscription.cancelled", "cancelled", T0 + 40, { userId: "user_2" }, "evt_L3x"]]) {
      await deliver(store, eventBody({ type, status, id: RID, createdAt: when, notes }), { eventId: id });
    }
    assert.deepEqual(got(store, "evt_L3rm"), ["IGNORED", "terminal_state", null]);
    assert.deepEqual(got(store, "evt_L3x"), ["IGNORED", "notes_user_mismatch", "user_1"]); // terminal: not re-applied
    assert.equal(store.state.subs[0].status, "completed");
    assert.equal(computeEntitlement(store.state.subs[0], at(T0 + 50)).plan, "FREE");
    const expired = memoryWebhookStore([paid({ status: "expired" })]);
    await deliver(expired, mm("subscription.cancelled", "cancelled", T0 + 10), { eventId: "evt_L3e" });
    assert.deepEqual(got(expired, "evt_L3e"), ["IGNORED", "notes_user_mismatch", "user_1"]);
    assert.equal(expired.state.subs[0].status, "expired");
  });

  test("activated / resumed / authenticated with mismatched notes never grant or restore Pro; flagged for the local owner", async () => {
    const paused = memoryWebhookStore([paid({ status: "paused", lastEventType: "subscription.paused" })]);
    const before = structuredClone(paused.state.subs);
    await deliver(paused, mm("subscription.resumed", "active", T0 + 10), { eventId: "evt_L4r" });
    assert.deepEqual(got(paused, "evt_L4r"), ["RECONCILIATION_REQUIRED", "notes_user_mismatch", "user_1"]);
    assert.deepEqual(paused.state.subs, before);
    assert.equal(computeEntitlement(paused.state.subs[0], at(T0 + 20)).plan, "FREE");
    const created = memoryWebhookStore([live({ status: "created" })]);
    const createdBefore = structuredClone(created.state.subs);
    await deliver(created, mm("subscription.authenticated", "authenticated", T0 + 5, { paid: 0 }), { eventId: "evt_L4au" });
    await deliver(created, mm("subscription.activated", "active", T0 + 10, { end: T0 + 40 * DAY }), { eventId: "evt_L4ac" });
    assert.deepEqual([got(created, "evt_L4au"), got(created, "evt_L4ac")],
      [["RECONCILIATION_REQUIRED", "notes_user_mismatch", "user_1"], ["RECONCILIATION_REQUIRED", "notes_user_mismatch", "user_1"]]);
    assert.deepEqual(created.state.subs, createdBefore);
    assert.equal(computeEntitlement(created.state.subs[0], at(T0 + 20)).plan, "FREE");
  });

  test("charged with mismatched notes is flagged and never extends the paid-through date", async () => {
    const store = memoryWebhookStore([paid()]);
    await deliver(store, mm("subscription.charged", "active", T0 + 10, { end: X + 30 * DAY }), { eventId: "evt_L5" });
    assert.deepEqual(got(store, "evt_L5"), ["RECONCILIATION_REQUIRED", "notes_user_mismatch", "user_1"]);
    assert.equal(store.state.subs[0].currentPeriodEnd.getTime(), at(X).getTime());
    assert.equal(computeEntitlement(store.state.subs[0], at(X)).plan, "FREE"); // still ends at the stored date
  });

  test("pending with mismatched notes is not applied: no 72h grace, access still ends at the stored paid-through date", async () => {
    const store = memoryWebhookStore([paid()]);
    await deliver(store, mm("subscription.pending", "pending", X - 60), { eventId: "evt_L6" });
    assert.deepEqual(got(store, "evt_L6"), ["IGNORED", "notes_user_mismatch", "user_1"]);
    const s = store.state.subs[0];
    assert.equal(s.status, "active");
    assert.deepEqual([computeEntitlement(s, at(X - 1)).plan, computeEntitlement(s, at(X)).plan], ["PRO", "FREE"]);
    // Contrast: a MATCHING pending applies and earns the renewal grace.
    const matching = memoryWebhookStore([paid()]);
    await deliver(matching, eventBody({ type: "subscription.pending", status: "pending", id: RID, createdAt: X - 60, notes: { userId: "user_1" } }), { eventId: "evt_L6m" });
    assert.equal(computeEntitlement(matching.state.subs[0], at(X + 3600)).plan, "PRO");
  });

  test("same-second ordering, stale events and duplicates still hold with mismatched notes", async () => {
    // paused then resumed in the same second, both mismatched: the pause applies, the resume is only flagged.
    const store = memoryWebhookStore([paid()]);
    await deliver(store, mm("subscription.paused", "paused", T0 + 10), { eventId: "evt_L7p" });
    await deliver(store, mm("subscription.resumed", "active", T0 + 10), { eventId: "evt_L7r" });
    assert.deepEqual([got(store, "evt_L7p")[1], got(store, "evt_L7r")[0], store.state.subs[0].status], ["applied_notes_user_mismatch", "RECONCILIATION_REQUIRED", "paused"]);
    // A matching resume, then a mismatched same-second late pause: superseded, not applied.
    const s2 = memoryWebhookStore([paid({ status: "paused", lastEventAt: at(T0 + 10), lastEventType: "subscription.paused" })]);
    await deliver(s2, eventBody({ type: "subscription.resumed", status: "active", id: RID, createdAt: T0 + 20, notes: { userId: "user_1" } }), { eventId: "evt_L7mr" });
    await deliver(s2, mm("subscription.paused", "paused", T0 + 20), { eventId: "evt_L7lp" });
    assert.deepEqual([got(s2, "evt_L7lp"), s2.state.subs[0].status], [["IGNORED", "notes_user_mismatch", "user_1"], "active"]);
    // A strictly older mismatched cancellation is stale: not applied.
    const s3 = memoryWebhookStore([paid({ lastEventAt: at(T0 + 100) })]);
    await deliver(s3, mm("subscription.cancelled", "cancelled", T0 + 50), { eventId: "evt_L7s" });
    assert.deepEqual([got(s3, "evt_L7s"), s3.state.subs[0].status], [["IGNORED", "notes_user_mismatch", "user_1"], "active"]);
    // Concurrent duplicates of a mismatched cancellation: applied once.
    const s4 = memoryWebhookStore([paid()]);
    const rs = await Promise.all([1, 2, 3].map(() => deliver(s4, mm("subscription.cancelled", "cancelled", T0 + 10), { eventId: "evt_L7d" })));
    assert.deepEqual(rs.map((r) => r.body.outcome).sort(), ["duplicate", "duplicate", "processed"]);
    assert.equal(s4.state.events.size, 1);
  });

  test("early (unbound) reservation: mismatched restrictive and access events are never bound or applied", async () => {
    const reservation = row({ id: "row_user_1", checkoutAttemptId: ATTEMPT_A, checkoutStatus: "RESERVED" });
    const store = memoryWebhookStore([structuredClone(reservation)]);
    for (const [type, status, id] of [["subscription.cancelled", "cancelled", "evt_L8c"], ["subscription.activated", "active", "evt_L8a"]]) {
      await deliver(store, eventBody({ type, status, id: "sub_Early8", notes: { userId: "user_2", checkoutAttemptId: ATTEMPT_A } }), { eventId: id });
      assert.deepEqual(got(store, id), ["IGNORED", "notes_user_mismatch", "user_1"], type);
    }
    assert.deepEqual(store.state.subs[0], reservation);
  });

  test("duplicates of a mismatched charge are idempotent", async () => {
    const store = memoryWebhookStore([live()]);
    const rs = await Promise.all([1, 2, 3].map(() => deliver(store, charge(), { eventId: "evt_N4" })));
    assert.deepEqual(rs.map((r) => r.body.outcome).sort(), ["duplicate", "duplicate", "reconciliation_required"]);
    assert.equal(store.state.events.size, 1);
  });

  test("early webhook (attempt reservation only) with mismatched notes user: never bound; charge flagged for the reservation owner", async () => {
    const reservation = row({ id: "row_user_1", checkoutAttemptId: ATTEMPT_A, checkoutStatus: "RESERVED" });
    const store = memoryWebhookStore([structuredClone(reservation)]);
    const r = await deliver(store, eventBody({ type: "subscription.charged", status: "active", id: "sub_Early9", notes: { userId: "user_2", checkoutAttemptId: ATTEMPT_A } }), { eventId: "evt_N5" });
    assert.equal(r.body.outcome, "reconciliation_required");
    assert.deepEqual([store.state.events.get("evt_N5").reason, store.state.events.get("evt_N5").userId], ["notes_user_mismatch", "user_1"]);
    assert.deepEqual(store.state.subs[0], reservation); // not bound, not applied
    // A non-charge early event with mismatched notes: ignored, unbound.
    assert.equal((await deliver(store, eventBody({ type: "subscription.authenticated", status: "authenticated", id: "sub_Early9", paid: 0, notes: { userId: "user_2", checkoutAttemptId: ATTEMPT_A } }), { eventId: "evt_N5b" })).body.outcome, "ignored");
    assert.deepEqual(store.state.subs[0], reservation);
  });

  test("notes.userId alone never creates a mapping or attributes an unknown subscription", async () => {
    const store = memoryWebhookStore([row({ id: "row_user_1" })]);
    await deliver(store, eventBody({ type: "subscription.charged", status: "active", id: "sub_Unknown7", notes: { userId: "user_1" } }), { eventId: "evt_N6" });
    assert.deepEqual([store.state.events.get("evt_N6").reason, store.state.events.get("evt_N6").userId], ["unknown_subscription", null]);
    assert.equal(store.state.subs[0].razorpaySubscriptionId, null);
  });

  test("retired subscription matched by exact ID, notes user differs: flagged for the stored original owner; newer subscription untouched", async () => {
    const retired = [{ razorpaySubscriptionId: RID, userId: "user_1", checkoutAttemptId: ATTEMPT_A, providerStatus: "cancelled", reason: "APP_CANCELLED", retiredAt: at(T0) }];
    const newer = row({ id: "row_user_1", razorpaySubscriptionId: "sub_Newer1", status: "active", checkoutAttemptId: ATTEMPT_B, checkoutStatus: "CREATED",
      paymentConfirmedAt: at(T0), currentPeriodEnd: at(T0 + 30 * DAY), lastEventAt: at(T0) });
    const store = memoryWebhookStore([structuredClone(newer), structuredClone(otherUser)], { retired });
    const before = structuredClone(store.state.subs);
    assert.equal((await deliver(store, charge(), { eventId: "evt_N7" })).body.outcome, "reconciliation_required");
    assert.deepEqual([store.state.events.get("evt_N7").reason, store.state.events.get("evt_N7").userId], ["notes_user_mismatch", "user_1"]);
    // Non-charge: ignored, still recorded against the stored owner (never the notes user).
    assert.equal((await deliver(store, eventBody({ type: "subscription.cancelled", status: "cancelled", id: RID, paid: 0, notes: { userId: "user_2" } }), { eventId: "evt_N7c" })).body.outcome, "ignored");
    assert.deepEqual([store.state.events.get("evt_N7c").reason, store.state.events.get("evt_N7c").userId], ["notes_user_mismatch", "user_1"]);
    assert.deepEqual(store.state.subs, before);
    assert.equal(computeEntitlement(store.state.subs[1], at(T0 + 60)).plan, "FREE"); // the notes user gets nothing
  });

  test("conflicting local ownership (terminal row vs. another owner's mapping) is still rejected, even with matching notes", async () => {
    const clash = { razorpaySubscriptionId: RID, userId: "user_2", checkoutAttemptId: ATTEMPT_B, providerStatus: "cancelled", reason: "ALREADY_FINAL", retiredAt: at(T0) };
    const store = memoryWebhookStore([live({ status: "cancelled", lastEventAt: at(T0) })], { retired: [structuredClone(clash)] });
    const before = structuredClone(store.state.subs);
    assert.equal((await deliver(store, charge({ createdAt: T0 + 5, notes: { userId: "user_1", checkoutAttemptId: ATTEMPT_A } }), { eventId: "evt_N8" })).body.outcome, "ignored");
    assert.deepEqual([store.state.events.get("evt_N8").reason, store.state.events.get("evt_N8").userId], ["reconciliation_conflict", null]);
    assert.deepEqual(store.state.retired, [clash]);
    assert.deepEqual(store.state.subs, before);
  });
});

// ---------------------------------------------------------------------------
describe("privacy", () => {
  test("event records and logs carry no body, signature, secret, payment or contact data", async () => {
    const store = memoryWebhookStore([row({ razorpaySubscriptionId: "sub_Old1", status: "authenticated" })]);
    const body = JSON.stringify({
      event: "subscription.charged", created_at: T0, contains: ["subscription", "payment"],
      payload: {
        subscription: { entity: { id: "sub_Old1", status: "active", paid_count: 1, current_end: T0 + 30 * DAY, notes: { userId: "user_1" } } },
        payment: { entity: { id: "pay_SECRETPAY1", amount: 49900, email: "someone@example.test", contact: "+910000000000", card: { last4: "4242" } } },
      },
    });
    const raw = enc(body);
    const signature = sign(raw);
    const logs = [];
    const r = await handleRazorpayWebhook({ rawBody: raw, headers: headers({ "x-razorpay-signature": signature, "x-razorpay-event-id": "evt_Priv1" }), secret: SECRET, store, log: (l) => logs.push(l) });
    assert.equal(r.status, 200);
    const stored = JSON.stringify([...store.state.events.values()]);
    const logged = logs.join("\n");
    for (const forbidden of [SECRET, signature, "pay_SECRETPAY1", "someone@example.test", "+910000000000", "4242", "49900", '"payload"', "user_1"]) {
      assert.ok(!stored.includes(forbidden), `event row contains ${forbidden}`);
      assert.ok(!logged.includes(forbidden), `log contains ${forbidden}`);
    }
    assert.ok(!logged.includes("sub_Old1"), "logs carry no Razorpay identifiers");
    assert.deepEqual(Object.keys([...store.state.events.values()][0]).sort(),
      ["bodySha256", "eventType", "id", "idSource", "outcome", "processedAt", "providerCreatedAt", "razorpaySubscriptionId", "reason", "receivedAt", "userId"]);
  });
});
