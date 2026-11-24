import { auth } from "@clerk/nextjs/server";
import { db } from "@solanaguard/database";
import { computeEntitlement } from "@/lib/billing-core";

/** The signed-in user's effective plan, computed from stored state (never the `plan` column alone). */
export async function getSubscription() {
  const { isAuthenticated, userId } = await auth();
  if (!isAuthenticated || !userId) return { plan: "FREE" as const };

  const user = await db.user.findUnique({
    where: { clerkId: userId },
    include: { subscription: true },
  });

  return { plan: computeEntitlement(user?.subscription, new Date()).plan };
}
