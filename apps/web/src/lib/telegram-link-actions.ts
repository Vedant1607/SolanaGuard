"use server";

import { auth } from "@clerk/nextjs/server";
import { db } from "@solanaguard/database";
import { issueTelegramLink, type TelegramLinkResult } from "@/lib/telegram-link-core";
import { prismaTelegramLinkStore } from "@/lib/telegram-link-store";

/**
 * Issue a single-use, 10-minute Telegram link for the signed-in user. Takes no
 * arguments: the account is resolved from the Clerk session, never from the
 * client. The plaintext token exists only in the returned URL; never log it.
 */
export async function createTelegramLink(): Promise<TelegramLinkResult> {
  const { isAuthenticated, userId } = await auth();
  return issueTelegramLink({
    clerkUserId: isAuthenticated ? userId : null,
    botUsername: process.env.NEXT_PUBLIC_TELEGRAM_BOT_USERNAME,
    store: prismaTelegramLinkStore(db),
  });
}
