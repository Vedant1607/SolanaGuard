"use client";

import { useState } from "react";
import Script from "next/script";
import { createSubscription } from "@/lib/billing-actions";

export function UpgradeButton() {
  const [loading, setLoading] = useState(false);

  async function handleUpgrade() {
    setLoading(true);
    try {
      const { subscriptionId, keyId } = await createSubscription();

      const options = {
        key: keyId,
        subscription_id: subscriptionId,
        name: "SolanaGuard",
        description: "Pro plan — monthly",
        handler: function () {
          // Razorpay confirms success client-side here, but the webhook
          // (HMAC-verified server-side) is the actual source of truth for
          // upgrading the account — this just gets the user back to a
          // page that'll reflect the upgrade once the webhook lands.
          window.location.href = "/pricing?success=true";
        },
        theme: { color: "#06b6d4" },
      };

      // @ts-expect-error Razorpay is loaded globally via the script tag below
      const rzp = new window.Razorpay(options);
      rzp.open();
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <Script src="https://checkout.razorpay.com/v1/checkout.js" strategy="lazyOnload" />
      <button
        onClick={handleUpgrade}
        disabled={loading}
        className="w-full bg-cyan-500 text-slate-900 font-bold text-sm py-2 rounded hover:bg-cyan-400 transition-colors disabled:opacity-50"
      >
        {loading ? "Loading..." : "Upgrade to Pro"}
      </button>
    </>
  );
}