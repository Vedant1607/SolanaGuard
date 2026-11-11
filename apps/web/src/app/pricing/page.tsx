import { getSubscription } from "@/lib/get-subscription";
import { UpgradeButton } from "@/components/UpgradeButton";
import { isBillingConfigured } from "@/lib/razorpay";
import { unstable_rethrow } from "next/navigation";

export default async function PricingPage() {
  const { plan } = await getSubscription().catch((error: unknown) => {
    unstable_rethrow(error); // let Next.js dynamic-rendering signals through
    console.error("[pricing] Subscription lookup failed; showing Free plan", error instanceof Error ? error.message : error);
    return { plan: "FREE" as const };
  });
  const billingEnabled = isBillingConfigured();

  return (
    <div className="min-h-screen bg-slate-950 text-slate-200 font-mono p-6">
      <div className="max-w-2xl mx-auto">
        <h1 className="text-2xl font-bold text-white mb-8 text-center">Plans</h1>
        <div className="grid grid-cols-2 gap-4">
          <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-6">
            <h2 className="font-bold text-white mb-1">Free</h2>
            <div className="text-2xl font-bold mb-4">₹0</div>
            <ul className="text-sm text-slate-400 space-y-1 mb-6">
              <li>5 protocols monitored</li>
              <li>Email + Telegram alerts</li>
            </ul>
            {plan === "FREE" && (
              <div className="text-xs text-emerald-400 text-center">Current plan</div>
            )}
          </div>
          <div className="rounded-lg border border-cyan-500/50 bg-slate-900/50 p-6">
            <h2 className="font-bold text-white mb-1">Pro</h2>
            <div className="text-2xl font-bold mb-4">₹2,000<span className="text-sm text-slate-500">/mo</span></div>
            <ul className="text-sm text-slate-400 space-y-1 mb-6">
              <li>All protocols monitored</li>
              <li>Email + Telegram alerts</li>
              <li>Priority support</li>
            </ul>
            {plan === "PRO" ? (
              <div className="text-xs text-emerald-400 text-center">Current plan</div>
            ) : (
              <UpgradeButton enabled={billingEnabled} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}