import { auth } from "@clerk/nextjs/server";
import { db } from "@solanaguard/database";

export async function getSubscription() {
  const { isAuthenticated, userId } = await auth();
  if (!isAuthenticated || !userId) return { plan: "FREE" as const };

  const user = await db.user.findUnique({
    where: { clerkId: userId },
    include: { subscription: true },
  });

  return { plan: user?.subscription?.plan ?? ("FREE" as const) };
}