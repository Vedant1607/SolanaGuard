// Run with: pnpm --filter @solanaguard/web test. In-memory store; identifiers are fake.
import { test } from "node:test";
import assert from "node:assert/strict";

import { clerkConfigIssue, isUniqueViolation, normalizeEmail, primaryEmail, reconcileUser } from "./sync-user-core.ts";

const unique = () => Object.assign(new Error("Unique constraint failed"), { code: "P2002" });

/** Mimics the users table: clerkId and email are both unique. */
function memoryStore(rows = [], { onCreate } = {}) {
  const calls = [];
  const store = {
    rows,
    calls,
    async findByClerkId(clerkId) { calls.push("findByClerkId"); return rows.find((r) => r.clerkId === clerkId) ?? null; },
    async findByEmail(email) { calls.push("findByEmail"); const r = rows.find((x) => x.email === email); return r ? { id: r.id, clerkId: r.clerkId } : null; },
    async updateEmail(clerkId, email) {
      calls.push("updateEmail");
      if (rows.some((r) => r.email === email && r.clerkId !== clerkId)) throw unique();
      const r = rows.find((x) => x.clerkId === clerkId);
      r.email = email;
      return r;
    },
    async create(clerkId, email) {
      calls.push("create");
      onCreate?.(rows);
      if (rows.some((r) => r.email === email || r.clerkId === clerkId)) throw unique();
      const r = { id: `u${rows.length + 1}`, clerkId, email };
      rows.push(r);
      return r;
    },
  };
  return store;
}

test("an existing Clerk ID gets its email updated", async () => {
  const store = memoryStore([{ id: "u1", clerkId: "user_a", email: "old@example.test" }]);
  const out = await reconcileUser("user_a", "new@example.test", store);
  assert.equal(out.status, "updated");
  assert.equal(out.user.email, "new@example.test");
  assert.equal(store.rows.length, 1);
});

test("an unchanged email is not rewritten", async () => {
  const store = memoryStore([{ id: "u1", clerkId: "user_a", email: "a@example.test" }]);
  const out = await reconcileUser("user_a", "a@example.test", store);
  assert.equal(out.status, "unchanged");
  assert.equal(out.user.id, "u1");
  assert.deepEqual(store.calls, ["findByClerkId"]);
});

test("an email owned by a different Clerk ID is never taken over", async () => {
  const store = memoryStore([{ id: "u1", clerkId: "user_old", email: "same@example.test" }]);
  const out = await reconcileUser("user_new", "same@example.test", store);
  assert.deepEqual(out, { status: "email_owned_by_other_account" });
  assert.deepEqual(store.rows, [{ id: "u1", clerkId: "user_old", email: "same@example.test" }]); // untouched
  assert.ok(!store.calls.includes("create") && !store.calls.includes("updateEmail"));
});

test("an unrelated email creates a new user", async () => {
  const store = memoryStore([{ id: "u1", clerkId: "user_old", email: "other@example.test" }]);
  const out = await reconcileUser("user_new", "fresh@example.test", store);
  assert.equal(out.status, "created");
  assert.deepEqual(out.user, { id: "u2", clerkId: "user_new", email: "fresh@example.test" });
});

test("changing to an email held by another account is a conflict, not an overwrite", async () => {
  const store = memoryStore([{ id: "u1", clerkId: "user_a", email: "a@example.test" }, { id: "u2", clerkId: "user_b", email: "b@example.test" }]);
  const out = await reconcileUser("user_a", "b@example.test", store);
  assert.deepEqual(out, { status: "email_owned_by_other_account" });
  assert.equal(store.rows[0].email, "a@example.test");
  assert.equal(store.rows[1].clerkId, "user_b");
});

test("a concurrent create for the same Clerk user returns the existing row", async () => {
  const store = memoryStore([], { onCreate: (rows) => rows.length === 0 && rows.push({ id: "u9", clerkId: "user_a", email: "a@example.test" }) });
  const out = await reconcileUser("user_a", "a@example.test", store);
  assert.equal(out.status, "unchanged");
  assert.equal(out.user.id, "u9");
  assert.equal(store.rows.length, 1);
});

test("a concurrent create of the same email by another account is refused", async () => {
  const store = memoryStore([], { onCreate: (rows) => rows.length === 0 && rows.push({ id: "u9", clerkId: "user_other", email: "a@example.test" }) });
  assert.deepEqual(await reconcileUser("user_a", "a@example.test", store), { status: "email_owned_by_other_account" });
  assert.equal(store.rows[0].clerkId, "user_other");
});

test("an unattributable unique violation is a conflict; other errors propagate", async () => {
  const store = memoryStore();
  store.create = async () => { throw unique(); };
  assert.deepEqual(await reconcileUser("user_a", "a@example.test", store), { status: "conflict" });
  const broken = memoryStore();
  broken.create = async () => { throw new Error("connection lost"); };
  await assert.rejects(reconcileUser("user_a", "a@example.test", broken), /connection lost/);
});

test("emails are normalised and only the primary address is used", () => {
  assert.equal(normalizeEmail("  Mixed@Example.TEST "), "mixed@example.test");
  assert.equal(normalizeEmail("   "), null);
  const user = { primaryEmailAddressId: "e2", emailAddresses: [{ id: "e1", emailAddress: "secondary@example.test" }, { id: "e2", emailAddress: "Primary@Example.test" }] };
  assert.equal(primaryEmail(user), "primary@example.test");
  assert.equal(primaryEmail({ ...user, primaryEmailAddressId: null }), null);
  assert.equal(isUniqueViolation({ code: "P2002" }), true);
  assert.equal(isUniqueViolation(new Error("x")), false);
});

test("Clerk configuration problems are detected instead of silently using keyless mode", () => {
  assert.equal(clerkConfigIssue({}), "keys_missing");
  assert.equal(clerkConfigIssue({ NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: "", CLERK_SECRET_KEY: "" }), "keys_missing"); // declared but empty, as in the root .env
  assert.equal(clerkConfigIssue({ NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: "pk_test_x" }), "keys_missing");
  assert.equal(clerkConfigIssue({ NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: "pk_test_x", CLERK_SECRET_KEY: "sk_live_y" }), "mode_mismatch");
  assert.equal(clerkConfigIssue({ NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: "pk_live_x", CLERK_SECRET_KEY: "sk_test_y" }), "mode_mismatch");
  assert.equal(clerkConfigIssue({ NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: "abc", CLERK_SECRET_KEY: "sk_test_y" }), "invalid_format");
  assert.equal(clerkConfigIssue({ NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: "pk_test_x", CLERK_SECRET_KEY: "sk_test_y" }), null);
});
