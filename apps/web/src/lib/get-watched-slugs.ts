import { auth } from "@clerk/nextjs/server";
import { db } from "@solanaguard/database";

export async function getWatchedSlugs(): Promise<Set<string>> {
  const { isAuthenticated, userId } = await auth();
  if (!isAuthenticated || !userId) return new Set();

  const user = await db.user.findUnique({ where: { clerkId: userId } });
  if (!user) return new Set();

  const items = await db.watchlistItem.findMany({
    where: { userId: user.id },
    include: { protocol: { select: { slug: true } } },
  });

  return new Set(items.map((i) => i.protocol.slug));
}