"use server";

import { auth } from "@clerk/nextjs/server";
import { db } from "@solanaguard/database";
import { getRazorpay } from "@/lib/razorpay";

export type CreateSubscriptionResult =
  | { ok: true; subscriptionId: string; keyId: string }
  | { ok: false; message: string };

// Expected failures are returned as values (not thrown) so the client can show them.
export async function createSubscription(): Promise<CreateSubscriptionResult> {
  const { isAuthenticated, userId } = await auth();
  if (!isAuthenticated || !userId) {
    return { ok: false, message: "Sign in to upgrade." };
  }

  const razorpay = getRazorpay();
  if (!razorpay) {
    return { ok: false, message: "Upgrades are not available in this environment." };
  }

  try {
    const user = await db.user.findUnique({ where: { clerkId: userId } });
    if (!user) return { ok: false, message: "User record not found — try reloading the page." };

    const subscription = await razorpay.subscriptions.create({
      plan_id: process.env.RAZORPAY_PLAN_ID!,
      customer_notify: 1,
      total_count: 120, // Razorpay requires a fixed cycle count, no "until cancelled" option — 120 months (10yr) stands in for effectively indefinite monthly billing
      notes: { userId: user.id },
    });

    return { ok: true, subscriptionId: subscription.id, keyId: process.env.NEXT_PUBLIC_RAZORPAY_KEY_ID! };
  } catch (error) {
    console.error("[createSubscription] Failed to start Razorpay subscription", error instanceof Error ? error.message : error);
    return { ok: false, message: "Could not start checkout. Please try again later." };
  }
}
