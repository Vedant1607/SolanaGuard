import { auth, currentUser } from "@clerk/nextjs/server";
import { db } from "@solanaguard/database";
import { CLERK_CONFIG_HINT, clerkConfigIssue, primaryEmail, reconcileUser, type SyncUserStore } from "./sync-user-core";

const store: SyncUserStore<Awaited<ReturnType<typeof db.user.create>>> = {
  findByClerkId: (clerkId) => db.user.findUnique({ where: { clerkId } }),
  findByEmail: (email) => db.user.findUnique({ where: { email }, select: { id: true, clerkId: true } }),
  updateEmail: (clerkId, email) => db.user.update({ where: { clerkId }, data: { email } }),
  create: (clerkId, email) => db.user.create({ data: { clerkId, email } }),
};

export async function syncUser() {
  const { isAuthenticated, userId } = await auth();
  if (!isAuthenticated || !userId) return null;

  const user = await currentUser();
  if (!user) return null;

  const email = primaryEmail(user);
  if (!email) {
    console.error("[syncUser] Current Clerk user has no primary email.");
    return null;
  }

  const outcome = await reconcileUser(userId, email, store);
  if (outcome.status === "email_owned_by_other_account" || outcome.status === "conflict") {
    const issue = clerkConfigIssue(process.env);
    console.error(
      outcome.status === "conflict"
        ? "[syncUser] User could not be synced because of a uniqueness conflict."
        : "[syncUser] Email belongs to a different Clerk account. Account reconciliation is required before syncing.",
      issue ? `Likely cause: ${CLERK_CONFIG_HINT[issue]}` : "",
    );
    return null;
  }
  return outcome.user;
}
