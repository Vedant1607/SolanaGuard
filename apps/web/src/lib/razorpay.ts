import Razorpay from "razorpay";

// The Razorpay SDK throws at construction when key_id is missing, so the
// client is created lazily and only when every billing variable is present.
const REQUIRED_BILLING_ENV = [
  "RAZORPAY_KEY_ID",
  "RAZORPAY_KEY_SECRET",
  "RAZORPAY_PLAN_ID",
  "NEXT_PUBLIC_RAZORPAY_KEY_ID",
] as const;

export function isBillingConfigured(): boolean {
  return REQUIRED_BILLING_ENV.every((name) => Boolean(process.env[name]));
}

let client: Razorpay | null = null;

export function getRazorpay(): Razorpay | null {
  if (!isBillingConfigured()) return null;
  client ??= new Razorpay({
    key_id: process.env.RAZORPAY_KEY_ID!,
    key_secret: process.env.RAZORPAY_KEY_SECRET!,
  });
  return client;
}
