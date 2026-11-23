// Row-lock behaviour of Telegram link issuance against REAL PostgreSQL (opt-in).
//
// Runs only when SOLANAGUARD_TEST_DATABASE_URL points at a DISPOSABLE database;
// skipped otherwise. Never touches existing tables: it creates its own schema,
// a minimal users table, applies the real 20261120120000 migration there, and
// drops the schema afterwards. Identifiers are fake.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { randomBytes, randomUUID } from "node:crypto";

import { hashLinkToken, issueTelegramLink } from "./telegram-link-core.ts";
import { prismaTelegramLinkStore } from "./telegram-link-store.ts";

const URL_ = process.env.SOLANAGUARD_TEST_DATABASE_URL;
const skip = URL_ ? false : "set SOLANAGUARD_TEST_DATABASE_URL to a DISPOSABLE database to run";
const DB_PKG = new URL("../../../../packages/database/", import.meta.url);
const MIGRATION = new URL("prisma/migrations/20261120120000_telegram_link_tokens/migration.sql", DB_PKG);

const schema = `tg_issue_test_${randomUUID().replaceAll("-", "").slice(0, 12)}`;
let admin, prisma, pg;

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
  await admin.query(readFileSync(MIGRATION, "utf8"));
  await admin.query(`SET search_path TO public`);
  // Prisma's own queries use `schema`; the raw FOR UPDATE uses search_path, as in production (public).
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

async function addUser() {
  const id = `u-${randomUUID()}`;
  await admin.query(`INSERT INTO "${schema}".users (id, "clerkId", email) VALUES ($1, $2, $3)`, [id, `clerk_${id}`, `${id}@example.test`]);
  return { id, clerkId: `clerk_${id}` };
}

/** Issue through the real code path; returns the plaintext token's hash. */
async function issue(store, clerkUserId) {
  const out = await issueTelegramLink({ clerkUserId, botUsername: "SolanaGuardBot", store });
  assert.equal(out.ok, true);
  return hashLinkToken(new URL(out.url).searchParams.get("start"));
}

/** Tokens the webhook would accept (same predicate as apps/api/app/telegram_linking.py). */
async function usable(userId) {
  const { rows } = await admin.query(
    `SELECT "tokenHash" FROM "${schema}".telegram_link_tokens WHERE "userId" = $1 AND "consumedAt" IS NULL
       AND "revokedAt" IS NULL AND "expiresAt" > (now() AT TIME ZONE 'UTC')`, [userId]);
  return rows.map((r) => r.tokenHash);
}

async function waitForLockWaiters(count) {
  for (let i = 0; i < 200; i++) {
    const { rows } = await admin.query(
      `SELECT count(*)::int AS n FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'`);
    if (rows[0].n >= count) return;
    await new Promise((r) => setTimeout(r, 10));
  }
  throw new Error(`expected ${count} lock waiter(s)`);
}

test("simultaneous issuance for one user leaves exactly one usable token: the later one", { skip }, async () => {
  const store = prismaTelegramLinkStore(prisma);
  const a = await addUser();
  const b = await addUser();
  const blocker = new pg.Client({ connectionString: URL_ });
  await blocker.connect();
  try {
    await blocker.query("BEGIN");
    await blocker.query(`SELECT id FROM "${schema}".users WHERE id = $1 FOR UPDATE`, [a.id]);

    const first = issue(store, a.clerkId);
    await waitForLockWaiters(1);                        // first is queued on user A's row lock
    const second = issue(store, a.clerkId);
    await waitForLockWaiters(2);                        // second queued behind it

    // A different user is not serialised behind user A.
    const other = await issue(store, b.clerkId);
    assert.deepEqual(await usable(b.id), [other]);

    await blocker.query("ROLLBACK");                    // release: FIFO lock queue, first then second
    const [firstHash, secondHash] = await Promise.all([first, second]);

    assert.deepEqual(await usable(a.id), [secondHash]); // second remains usable
    const { rows } = await admin.query(
      `SELECT "revokedAt" IS NOT NULL AS revoked FROM "${schema}".telegram_link_tokens WHERE "tokenHash" = $1`, [firstHash]);
    assert.deepEqual(rows, [{ revoked: true }]);       // first was revoked by second
    assert.deepEqual(await usable(b.id), [other]);      // other user untouched
  } finally {
    await blocker.end();
  }
});

test("a burst of concurrent issuances per user still leaves one usable token each", { skip }, async () => {
  const store = prismaTelegramLinkStore(prisma);
  const a = await addUser();
  const b = await addUser();
  const hashes = await Promise.all([
    ...Array.from({ length: 10 }, () => issue(store, a.clerkId)),
    ...Array.from({ length: 5 }, () => issue(store, b.clerkId)),
  ]);
  const [ua, ub] = [await usable(a.id), await usable(b.id)];
  assert.equal(ua.length, 1);
  assert.equal(ub.length, 1);
  assert.ok(hashes.slice(0, 10).includes(ua[0]));
  assert.ok(hashes.slice(10).includes(ub[0]));
  const { rows } = await admin.query(`SELECT count(*)::int AS n FROM "${schema}".telegram_link_tokens WHERE "userId" = ANY($1)`, [[a.id, b.id]]);
  assert.equal(rows[0].n, 15);                          // every request stored its own hash; extras revoked
});

test("control: the previous array transaction leaves two usable tokens under the same race", { skip }, async () => {
  const lockless = {
    ...prismaTelegramLinkStore(prisma),
    async replaceTokens(userId, tokenHash, createdAt, expiresAt) {
      await prisma.$transaction([
        prisma.telegramLinkToken.updateMany({ where: { userId, consumedAt: null, revokedAt: null }, data: { revokedAt: createdAt } }),
        prisma.telegramLinkToken.create({ data: { userId, tokenHash, createdAt, expiresAt } }),
      ]);
    },
  };
  const a = await addUser();
  const oldHash = await issue(lockless, a.clerkId);    // an existing unused token
  const blocker = new pg.Client({ connectionString: URL_ });
  await blocker.connect();
  try {
    // Both old-style transactions queue on the existing token's row in updateMany.
    // After release, the second re-checks that row (already revoked) but cannot
    // see the first's newly inserted token, so it revokes nothing new.
    await blocker.query("BEGIN");
    await blocker.query(`SELECT 1 FROM "${schema}".telegram_link_tokens WHERE "tokenHash" = $1 FOR UPDATE`, [oldHash]);
    const racing = [issue(lockless, a.clerkId)];
    await waitForLockWaiters(1);
    racing.push(issue(lockless, a.clerkId));
    await waitForLockWaiters(2);
    await blocker.query("ROLLBACK");
    await Promise.all(racing);
  } finally {
    await blocker.end();
  }
  assert.equal((await usable(a.id)).length, 2);
});

test("stored values are hashes only", { skip }, async () => {
  const store = prismaTelegramLinkStore(prisma);
  const a = await addUser();
  const out = await issueTelegramLink({ clerkUserId: a.clerkId, botUsername: "SolanaGuardBot", store, random: () => randomBytes(32) });
  const token = new URL(out.url).searchParams.get("start");
  const { rows } = await admin.query(`SELECT * FROM "${schema}".telegram_link_tokens WHERE "userId" = $1`, [a.id]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].tokenHash, hashLinkToken(token));
  assert.ok(!JSON.stringify(rows).includes(token));
  assert.equal(rows[0].expiresAt.getTime() - rows[0].createdAt.getTime(), 10 * 60 * 1000);
});
