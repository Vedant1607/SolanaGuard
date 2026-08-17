import { auth, currentUser } from "@clerk/nextjs/server";
import { db } from "@solanaguard/database";

export async function syncUser() {
  const { isAuthenticated, userId } = await auth();
  if (!isAuthenticated || !userId) return null;

  const user = await currentUser();
  if (!user) return null;

  const email = user.emailAddresses[0]?.emailAddress ?? "";

  return db.user.upsert({
    where: { clerkId: userId },
    create: { clerkId: userId, email },
    update: { email },
  });
}