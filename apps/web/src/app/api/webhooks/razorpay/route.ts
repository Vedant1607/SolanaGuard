import crypto from "crypto";
import { NextResponse } from "next/server";
import { db } from "@solanaguard/database";

export async function POST(req: Request) {
  const body = await req.text();
  const signature = req.headers.get("x-razorpay-signature");

  const expectedSignature = crypto
    .createHmac("sha256", process.env.RAZORPAY_WEBHOOK_SECRET!)
    .update(body)
    .digest("hex");

  if (signature !== expectedSignature) {
    return new NextResponse("Invalid webhook signature", { status: 400 });
  }

  const event = JSON.parse(body);
  const subscriptionEntity = event.payload?.subscription?.entity;
  if (!subscriptionEntity) return NextResponse.json({ received: true });

  const userId = subscriptionEntity.notes?.userId;
  if (!userId) return NextResponse.json({ received: true });

  switch (event.event) {
    case "subscription.activated":
    case "subscription.charged": {
      await db.subscription.upsert({
        where: { userId },
        create: {
          userId,
          razorpaySubscriptionId: subscriptionEntity.id,
          plan: "PRO",
          status: subscriptionEntity.status,
          currentPeriodEnd: subscriptionEntity.current_end
            ? new Date(subscriptionEntity.current_end * 1000)
            : null,
        },
        update: {
          razorpaySubscriptionId: subscriptionEntity.id,
          plan: "PRO",
          status: subscriptionEntity.status,
          currentPeriodEnd: subscriptionEntity.current_end
            ? new Date(subscriptionEntity.current_end * 1000)
            : null,
        },
      });
      break;
    }
    case "subscription.cancelled":
    case "subscription.completed":
    case "subscription.halted": {
      await db.subscription.updateMany({
        where: { razorpaySubscriptionId: subscriptionEntity.id },
        data: { plan: "FREE", status: subscriptionEntity.status },
      });
      break;
    }
  }

  return NextResponse.json({ received: true });
}