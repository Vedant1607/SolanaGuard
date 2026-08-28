import { auth } from "@clerk/nextjs/server";
import { db } from "@solanaguard/database";

export async function getAppUser() {
  const { isAuthenticated, userId } = await auth();
  if (!isAuthenticated || !userId) return null;
  return db.user.findUnique({ where: { clerkId: userId }, select: { id: true, telegramChatId: true } });
}