"use server";

import { auth } from "@clerk/nextjs/server";
import { db } from "@solanaguard/database";
import { getRazorpay } from "@/lib/razorpay";
import { startCheckout, type CheckoutResult } from "@/lib/billing-core";
import { prismaCheckoutStore } from "@/lib/billing-store";

export type CreateSubscriptionResult =
  | { ok: true; subscriptionId: string; keyId: string }
  | { ok: false; message: string };

const MESSAGES: Record<Exclude<CheckoutResult, { ok: true }>["reason"], string> = {
  unauthenticated: "Sign in to upgrade.",
  no_account: "User record not found — try reloading the page.",
  already_pro: "You already have an active Pro subscription.",
  in_progress: "A checkout is already in progress. Please finish it or try again shortly.",
  unresolved: "We could not confirm your previous checkout. Please contact support before trying again so you are not charged twice.",
  needs_attention: "Your existing subscription needs attention. Please contact support.",
  provider_rejected: "Could not start checkout. Please try again later.",
  provider_unconfirmed: "We could not confirm that checkout started. Please contact support before trying again so you are not charged twice.",
};

// Expected failures are returned as values (not thrown) so the client can show them.
// The account comes from the Clerk session only; a local reservation is
// committed before Razorpay is called (see startCheckout in billing-core.ts).
export async function createSubscription(): Promise<CreateSubscriptionResult> {
  const { isAuthenticated, userId } = await auth();
  if (!isAuthenticated || !userId) {
    return { ok: false, message: MESSAGES.unauthenticated };
  }

  const razorpay = getRazorpay();
  if (!razorpay) {
    return { ok: false, message: "Upgrades are not available in this environment." };
  }

  try {
    const result = await startCheckout({
      clerkUserId: userId,
      store: prismaCheckoutStore(db),
      provider: {
        create: (notes) => razorpay.subscriptions.create({
          plan_id: process.env.RAZORPAY_PLAN_ID!,
          customer_notify: 1,
          total_count: 120, // Razorpay requires a fixed cycle count, no "until cancelled" option — 120 months (10yr) stands in for effectively indefinite monthly billing
          notes,
        }),
        fetch: (id) => razorpay.subscriptions.fetch(id),
        cancel: (id) => razorpay.subscriptions.cancel(id, false), // immediate: only ever used on a never-authenticated checkout
      },
    });
    if (!result.ok) {
      if (result.reason === "provider_unconfirmed" || result.reason === "provider_rejected") {
        console.error(`[createSubscription] Checkout not started: ${result.reason}`);
      }
      return { ok: false, message: MESSAGES[result.reason] };
    }
    return { ok: true, subscriptionId: result.subscriptionId, keyId: process.env.NEXT_PUBLIC_RAZORPAY_KEY_ID! };
  } catch (error) {
    // Database failure. If a reservation was committed it stays open (RESERVED or
    // UNRESOLVED) and blocks another remote creation until reconciled.
    console.error("[createSubscription] Could not reserve checkout", error instanceof Error ? error.name : "unknown");
    return { ok: false, message: "Could not start checkout. Please try again later." };
  }
}
