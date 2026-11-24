"use client";

import { useState } from "react";
import Script from "next/script";
import { createSubscription } from "@/lib/billing-actions";

export function UpgradeButton({ enabled }: { enabled: boolean }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!enabled) {
    return (
      <div className="text-center">
        <button disabled className="w-full cursor-not-allowed rounded bg-slate-800 py-2 text-sm font-bold text-slate-500">
          Upgrade to Pro
        </button>
        <p className="mt-2 text-xs text-slate-500">Upgrades are not available in this environment.</p>
      </div>
    );
  }

  async function handleUpgrade() {
    setLoading(true);
    setError(null);
    try {
      const result = await createSubscription();
      if (!result.ok) {
        setError(result.message);
        return;
      }

      // @ts-expect-error Razorpay is loaded globally via the script tag below
      const RazorpayCheckout = window.Razorpay;
      if (typeof RazorpayCheckout !== "function") {
        setError("Checkout could not be loaded. Please try again.");
        return;
      }

      const rzp = new RazorpayCheckout({
        key: result.keyId,
        subscription_id: result.subscriptionId,
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
      });
      rzp.open();
    } catch {
      setError("Could not start checkout. Please try again.");
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
      {error && <p role="alert" className="mt-2 text-center text-xs text-amber-300">{error}</p>}
    </>
  );
}
