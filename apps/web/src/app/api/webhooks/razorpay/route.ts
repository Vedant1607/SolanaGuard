import { NextResponse } from "next/server";
import { db } from "@solanaguard/database";
import { handleRazorpayWebhook } from "@/lib/billing-core";
import { prismaBillingWebhookStore } from "@/lib/billing-store";

// node:crypto (HMAC, timingSafeEqual) and Prisma require the Node.js runtime.
export const runtime = "nodejs";

export async function POST(req: Request) {
  // Exact received bytes: the signature is verified over these, never over a
  // decoded or re-serialized body.
  const rawBody = new Uint8Array(await req.arrayBuffer());
  const result = await handleRazorpayWebhook({
    rawBody,
    headers: req.headers,
    secret: process.env.RAZORPAY_WEBHOOK_SECRET,
    store: prismaBillingWebhookStore(db),
  });
  return NextResponse.json(result.body, { status: result.status });
}
