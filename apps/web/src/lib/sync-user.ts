import { auth, currentUser } from "@clerk/nextjs/server";
import { db } from "@solanaguard/database";

export async function syncUser() {
  const { isAuthenticated, userId } = await auth();
  if (!isAuthenticated || !userId) return null;

  const user = await currentUser();
  if (!user) return null;

  const email = user.emailAddresses
    .find((address) => address.id === user.primaryEmailAddressId)
    ?.emailAddress?.trim()
    .toLowerCase();

  if (!email) {
    console.error("[syncUser] Current Clerk user has no primary email.");
    return null;
  }

  const existingByClerkId = await db.user.findUnique({
    where: { clerkId: userId },
  });

  if (existingByClerkId) {
    return db.user.update({
      where: { clerkId: userId },
      data: { email },
    });
  }

  const existingByEmail = await db.user.findUnique({
    where: { email },
    select: { id: true, clerkId: true },
  });

  if (existingByEmail) {
    console.error(
      "[syncUser] Email belongs to a different Clerk account. " +
        "Account reconciliation is required before syncing.",
    );
    return null;
  }

  return db.user.create({
    data: { clerkId: userId, email },
  });
}
