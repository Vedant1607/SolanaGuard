"use server";

import { auth } from "@clerk/nextjs/server";
import { db } from "@solanaguard/database";
import { revalidatePath } from "next/cache";

export async function toggleWatchlist(slug: string) {
  const { isAuthenticated, userId } = await auth();
  if (!isAuthenticated || !userId) {
    throw new Error("Must be signed in to use the watchlist");
  }

  const user = await db.user.findUnique({ where: { clerkId: userId } });
  if (!user) {
    throw new Error("User record not found — try reloading the page");
  }

  const protocol = await db.protocol.findUnique({ where: { slug } });
  if (!protocol) {
    throw new Error(`Unknown protocol: ${slug}`);
  }

  const key = { userId_protocolId: { userId: user.id, protocolId: protocol.id } };
  const existing = await db.watchlistItem.findUnique({ where: key });

  if (existing) {
    await db.watchlistItem.delete({ where: key });
  } else {
    await db.watchlistItem.create({ data: { userId: user.id, protocolId: protocol.id } });
  }

  revalidatePath("/");
}