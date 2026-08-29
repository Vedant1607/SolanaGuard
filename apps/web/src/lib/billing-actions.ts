"use server";

import { auth } from "@clerk/nextjs/server";
import { db } from "@solanaguard/database";
import { razorpay } from "@/lib/razorpay";

export async function createSubscription() {
  const { isAuthenticated, userId } = await auth();
  if (!isAuthenticated || !userId) {
    throw new Error("Must be signed in to upgrade");
  }

  const user = await db.user.findUnique({ where: { clerkId: userId } });
  if (!user) throw new Error("User record not found — try reloading the page");

  const subscription = await razorpay.subscriptions.create({
    plan_id: process.env.RAZORPAY_PLAN_ID!,
    customer_notify: 1,
    total_count: 120, // Razorpay requires a fixed cycle count, no "until cancelled" option — 120 months (10yr) stands in for effectively indefinite monthly billing
    notes: { userId: user.id },
  });

  return { subscriptionId: subscription.id, keyId: process.env.NEXT_PUBLIC_RAZORPAY_KEY_ID! };
}