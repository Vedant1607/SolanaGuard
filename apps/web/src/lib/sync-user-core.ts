// Pure core of syncUser: reconciles the signed-in Clerk user with the local
// users table through an injected store. It never links two identities just
// because their email strings match, and a uniqueness conflict is reported,
// never resolved by overwriting another account. Kept free of runtime imports
// so `node --test` can load it directly.

export interface SyncUserStore<U> {
  findByClerkId(clerkId: string): Promise<(U & { email: string }) | null>;
  findByEmail(email: string): Promise<{ id: string; clerkId: string } | null>;
  updateEmail(clerkId: string, email: string): Promise<U>;
  create(clerkId: string, email: string): Promise<U>;
}

export type SyncOutcome<U> =
  | { status: "unchanged" | "updated" | "created"; user: U }
  /** The email already belongs to a user with a different Clerk ID: reconciliation is required. */
  | { status: "email_owned_by_other_account" }
  /** A unique constraint failed and the row could not be attributed safely. */
  | { status: "conflict" };

export function normalizeEmail(email: string | null | undefined): string | null {
  const value = email?.trim().toLowerCase();
  return value ? value : null;
}

export function primaryEmail(user: { primaryEmailAddressId: string | null; emailAddresses: { id: string; emailAddress: string }[] }): string | null {
  return normalizeEmail(user.emailAddresses.find((a) => a.id === user.primaryEmailAddressId)?.emailAddress);
}

/** Prisma's unique-constraint violation (P2002), detected structurally. */
export function isUniqueViolation(error: unknown): boolean {
  return typeof error === "object" && error !== null && (error as { code?: unknown }).code === "P2002";
}

export async function reconcileUser<U>(clerkId: string, email: string, store: SyncUserStore<U>): Promise<SyncOutcome<U>> {
  const byClerkId = await store.findByClerkId(clerkId);
  if (byClerkId) {
    if (byClerkId.email === email) return { status: "unchanged", user: byClerkId };
    try {
      return { status: "updated", user: await store.updateEmail(clerkId, email) };
    } catch (error) {
      // The new email is already stored on another user: never take it over.
      if (isUniqueViolation(error)) return { status: "email_owned_by_other_account" };
      throw error;
    }
  }

  const byEmail = await store.findByEmail(email);
  if (byEmail) return { status: "email_owned_by_other_account" };

  try {
    return { status: "created", user: await store.create(clerkId, email) };
  } catch (error) {
    if (!isUniqueViolation(error)) throw error;
    // A concurrent request for the same Clerk user may have created the row first.
    const raced = await store.findByClerkId(clerkId);
    if (raced) return { status: "unchanged", user: raced };
    return (await store.findByEmail(email)) ? { status: "email_owned_by_other_account" } : { status: "conflict" };
  }
}

// --- Clerk configuration ------------------------------------------------------

export type ClerkConfigIssue = "keys_missing" | "mode_mismatch" | "invalid_format";

const KEY_MODE = { publishable: /^pk_(test|live)_/, secret: /^sk_(test|live)_/ };

/**
 * Detects a Clerk configuration that would silently switch identity provider:
 * missing keys make @clerk/nextjs fall back to a keyless (temporary) instance in
 * development, whose user IDs differ from the configured instance's. Values are
 * only inspected for their prefix and never returned.
 */
export function clerkConfigIssue(env: Record<string, string | undefined>): ClerkConfigIssue | null {
  const pk = env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY?.trim();
  const sk = env.CLERK_SECRET_KEY?.trim();
  if (!pk || !sk) return "keys_missing";
  const pkMode = KEY_MODE.publishable.exec(pk)?.[1];
  const skMode = KEY_MODE.secret.exec(sk)?.[1];
  if (!pkMode || !skMode) return "invalid_format";
  return pkMode === skMode ? null : "mode_mismatch";
}

export const CLERK_CONFIG_HINT: Record<ClerkConfigIssue, string> = {
  keys_missing: "Clerk keys are not set for this app, so Clerk is running in keyless mode with a temporary instance whose user IDs differ from the configured instance. Set NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY and CLERK_SECRET_KEY in apps/web/.env.local.",
  mode_mismatch: "The Clerk publishable and secret keys are from different environments (test vs live).",
  invalid_format: "A Clerk key does not have the expected pk_/sk_ format.",
};
