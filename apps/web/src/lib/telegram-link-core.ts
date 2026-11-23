// Telegram link-token issuance, free of Next/Clerk/Prisma so it can be unit tested.
// The token is 32 random bytes as URL-safe base64 (43 chars, inside Telegram's
// 64-char /start payload limit). Only its SHA-256 hex digest is persisted; the
// plaintext is returned once, inside the deep link, to the signed-in caller.
// The API (apps/api/app/telegram_linking.py) hashes the same way when consuming.
import { createHash, randomBytes } from "node:crypto";

export const TELEGRAM_LINK_TTL_MS = 10 * 60 * 1000;
export const TELEGRAM_LINK_TOKEN_BYTES = 32;
const TELEGRAM_START_PAYLOAD_MAX = 64;
// Telegram bot usernames: 5-32 chars of letters, digits and underscores.
const BOT_USERNAME = /^[A-Za-z0-9_]{5,32}$/;

export type TelegramLinkFailure = "unauthenticated" | "not_configured" | "no_account";
export type TelegramLinkResult =
  | { ok: true; url: string; expiresAt: string }
  | { ok: false; reason: TelegramLinkFailure };

export interface TelegramLinkStore {
  /** Application user ID for a Clerk user ID (never looked up by email). */
  findUserIdByClerkId(clerkId: string): Promise<string | null>;
  /** In one transaction: revoke the user's unused tokens, then store the new hash. */
  replaceTokens(userId: string, tokenHash: string, createdAt: Date, expiresAt: Date): Promise<void>;
}

export function hashLinkToken(token: string): string {
  return createHash("sha256").update(token, "utf8").digest("hex");
}

export function generateLinkToken(random: (size: number) => Buffer = randomBytes): string {
  const token = random(TELEGRAM_LINK_TOKEN_BYTES).toString("base64url");
  if (token.length > TELEGRAM_START_PAYLOAD_MAX) throw new Error("link token exceeds Telegram's payload limit");
  return token;
}

/** The configured bot username without a leading "@", or null when missing/invalid. */
export function normalizeBotUsername(raw: string | undefined | null): string | null {
  const name = (raw ?? "").trim().replace(/^@/, "");
  return BOT_USERNAME.test(name) ? name : null;
}

export function buildDeepLink(botUsername: string, token: string): string {
  return `https://t.me/${botUsername}?start=${token}`; // both are URL-safe by construction
}

export async function issueTelegramLink({
  clerkUserId,
  botUsername,
  store,
  now = () => new Date(),
  random = randomBytes,
}: {
  clerkUserId: string | null | undefined;
  botUsername: string | undefined | null;
  store: TelegramLinkStore;
  now?: () => Date;
  random?: (size: number) => Buffer;
}): Promise<TelegramLinkResult> {
  if (!clerkUserId) return { ok: false, reason: "unauthenticated" };
  const bot = normalizeBotUsername(botUsername);
  if (!bot) return { ok: false, reason: "not_configured" };
  const userId = await store.findUserIdByClerkId(clerkUserId);
  if (!userId) return { ok: false, reason: "no_account" };

  const token = generateLinkToken(random);
  const createdAt = now();
  const expiresAt = new Date(createdAt.getTime() + TELEGRAM_LINK_TTL_MS);
  await store.replaceTokens(userId, hashLinkToken(token), createdAt, expiresAt);
  return { ok: true, url: buildDeepLink(bot, token), expiresAt: expiresAt.toISOString() };
}
