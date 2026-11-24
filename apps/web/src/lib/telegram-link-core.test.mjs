// Run with: pnpm --filter @solanaguard/web test. In-memory store; identifiers are fake.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  TELEGRAM_LINK_TTL_MS, buildDeepLink, generateLinkToken, hashLinkToken, issueTelegramLink, normalizeBotUsername,
} from "./telegram-link-core.ts";

// Shared with apps/api/tests/test_telegram_linking.py: both sides must hash identically.
const VECTOR_TOKEN = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
const VECTOR_HASH = "ea866a757e4c38babfa8127cbe9a409d3e1f93a00ff1488ff735fcf917afffd0";
const fixedRandom = (size) => Buffer.from(Array.from({ length: size }, (_, i) => i));
const NOW = new Date("2026-11-20T12:00:00.000Z");

/** Mimics telegram_link_tokens: revoke-unused + insert happen together. */
function memoryStore(users = { user_clerk_a: "u1", user_clerk_b: "u2" }) {
  const tokens = [];
  const calls = [];
  return {
    tokens,
    calls,
    async findUserIdByClerkId(clerkId) { calls.push(["findUserIdByClerkId", clerkId]); return users[clerkId] ?? null; },
    async replaceTokens(userId, tokenHash, createdAt, expiresAt) {
      calls.push(["replaceTokens", userId]);
      for (const t of tokens) if (t.userId === userId && !t.consumedAt && !t.revokedAt) t.revokedAt = createdAt;
      tokens.push({ userId, tokenHash, createdAt, expiresAt, consumedAt: null, revokedAt: null });
    },
  };
}

const issue = (store, overrides = {}) => issueTelegramLink({
  clerkUserId: "user_clerk_a", botUsername: "SolanaGuardBot", store, now: () => NOW, random: fixedRandom, ...overrides,
});

test("an authenticated user gets a deep link carrying a fresh token", async () => {
  const store = memoryStore();
  const out = await issue(store);
  assert.equal(out.ok, true);
  assert.equal(out.url, `https://t.me/SolanaGuardBot?start=${VECTOR_TOKEN}`);
  assert.equal(out.expiresAt, new Date(NOW.getTime() + TELEGRAM_LINK_TTL_MS).toISOString());
});

test("unauthenticated callers are rejected before any lookup or token generation", async () => {
  for (const clerkUserId of [null, undefined, ""]) {
    const store = memoryStore();
    let generated = false;
    const out = await issue(store, { clerkUserId, random: (n) => { generated = true; return fixedRandom(n); } });
    assert.deepEqual(out, { ok: false, reason: "unauthenticated" });
    assert.deepEqual(store.calls, []);
    assert.equal(generated, false);
  }
});

test("the application user is resolved by Clerk ID only", async () => {
  const store = memoryStore();
  await issue(store, { clerkUserId: "user_clerk_b" });
  assert.deepEqual(store.calls, [["findUserIdByClerkId", "user_clerk_b"], ["replaceTokens", "u2"]]);
  const unknown = memoryStore();
  assert.deepEqual(await issue(unknown, { clerkUserId: "user_clerk_unknown" }), { ok: false, reason: "no_account" });
  assert.equal(unknown.tokens.length, 0);
});

test("only the SHA-256 hash is stored, never the plaintext token", async () => {
  const store = memoryStore();
  const out = await issue(store);
  const token = new URL(out.url).searchParams.get("start");
  assert.equal(store.tokens.length, 1);
  assert.equal(store.tokens[0].tokenHash, VECTOR_HASH);
  assert.match(store.tokens[0].tokenHash, /^[0-9a-f]{64}$/);
  assert.ok(!JSON.stringify(store.tokens).includes(token));
});

test("tokens expire ten minutes after issue", async () => {
  const store = memoryStore();
  await issue(store);
  assert.equal(store.tokens[0].expiresAt.getTime() - store.tokens[0].createdAt.getTime(), 10 * 60 * 1000);
});

test("issuing a new token revokes the user's previous unused tokens only", async () => {
  const store = memoryStore();
  await issue(store);
  await issue(store, { clerkUserId: "user_clerk_b" });
  const later = new Date(NOW.getTime() + 60_000);
  await issue(store, { now: () => later, random: () => Buffer.alloc(32, 7) });
  const [first, other, second] = store.tokens;
  assert.equal(first.revokedAt?.getTime(), later.getTime());
  assert.equal(other.revokedAt, null); // another user's token is untouched
  assert.equal(second.revokedAt, null);
});

test("missing or invalid bot configuration fails cleanly without issuing", async () => {
  for (const botUsername of [undefined, null, "", "  ", "bad name", "ab", "x".repeat(33), "evil.com/x"]) {
    const store = memoryStore();
    assert.deepEqual(await issue(store, { botUsername }), { ok: false, reason: "not_configured" });
    assert.equal(store.tokens.length, 0);
  }
  assert.equal(normalizeBotUsername("@SolanaGuardBot"), "SolanaGuardBot");
});

test("real tokens are 43 URL-safe characters, unique, and match the API's hash", () => {
  const tokens = new Set(Array.from({ length: 200 }, () => generateLinkToken()));
  assert.equal(tokens.size, 200);
  for (const t of tokens) assert.match(t, /^[A-Za-z0-9_-]{43}$/);
  assert.equal(hashLinkToken(VECTOR_TOKEN), VECTOR_HASH);
  assert.ok(buildDeepLink("SolanaGuardBot", VECTOR_TOKEN).split("start=")[1].length <= 64);
});
