// Razorpay billing logic, free of Next/Clerk/Prisma so it can be unit tested.
//
//   computeEntitlement   stored subscription state + explicit `now` -> FREE/PRO
//   handleRazorpayWebhook raw bytes -> signature check -> idempotent, ordered apply
//   startCheckout        local reservation BEFORE the Razorpay create call
//
// Persistence goes through the WebhookStore / CheckoutStore interfaces
// (Prisma implementations in billing-store.ts). Lock order, shared by every
// writer: the users row (checkout only, FOR NO KEY UPDATE) FIRST, then the
// subscriptions row. The webhook never row-locks a users row; inserting a
// retired mapping takes only the foreign key's KEY SHARE lock on it, which
// FOR NO KEY UPDATE does not block, so the two paths cannot deadlock.
//
// Privacy: nothing here logs or persists raw bodies, signatures, payment or
// card details, customer contact data, secrets, or Razorpay identifiers in logs.
import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";

// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

/** Subscription statuses documented by Razorpay (SDK 2.9.8 types plus "paused"). */
export const SUBSCRIPTION_STATUSES = [
  "created", "authenticated", "active", "pending", "halted", "paused", "cancelled", "completed", "expired",
] as const;
export type SubscriptionStatus = (typeof SUBSCRIPTION_STATUSES)[number];

/** Final states: Razorpay cannot restart them, so no later event may revive them. */
export const TERMINAL_STATUSES: ReadonlySet<string> = new Set(["cancelled", "completed", "expired"]);

export type CancelMode = "IMMEDIATE" | "CYCLE_END";
export type CheckoutStatus = "RESERVED" | "CREATED" | "FAILED" | "UNRESOLVED";
export type Plan = "FREE" | "PRO";

/** The stored fields entitlement depends on. */
export interface BillingState {
  status: string;
  razorpaySubscriptionId: string | null;
  currentPeriodEnd: Date | null;
  paymentConfirmedAt: Date | null;
  cancelMode: CancelMode | null;
  cancelSubscriptionId: string | null;
}

/** A locked local subscription row, as the stores return it. */
export interface StoredSubscription extends BillingState {
  id: string;
  userId: string;
  lastEventAt: Date | null;
  lastEventType: string | null;
  checkoutAttemptId: string | null;
  checkoutStatus: CheckoutStatus | null;
  checkoutStartedAt: Date | null;
}

// ---------------------------------------------------------------------------
// Entitlement
// ---------------------------------------------------------------------------

/** Product rule (not a Razorpay setting): renewal-payment grace while pending. */
export const PENDING_GRACE_MS = 72 * 60 * 60 * 1000;

export type EntitlementReason =
  | "no_subscription" | "active" | "active_lapsed" | "pending_grace" | "pending_grace_over"
  | "unpaid" | "cancel_at_cycle_end" | "cancelled" | "halted" | "paused" | "ended" | "not_active";

export interface Entitlement {
  plan: Plan;
  reason: EntitlementReason;
  /** Instant at which PRO access ends (exclusive), when PRO. */
  accessUntil: Date | null;
}

const validDate = (d: Date | null | undefined): d is Date => d instanceof Date && !Number.isNaN(d.getTime());

/**
 * Effective plan for `state` at `now`. The stored `plan` column is never
 * consulted. A paid-through date counts only when backed by confirmed payment.
 * Every boundary is exclusive: at exactly `accessUntil` the user is FREE.
 */
export function computeEntitlement(state: BillingState | null | undefined, now: Date): Entitlement {
  const free = (reason: EntitlementReason): Entitlement => ({ plan: "FREE", reason, accessUntil: null });
  if (!state) return free("no_subscription");
  const paidThrough = state.paymentConfirmedAt && validDate(state.currentPeriodEnd) ? state.currentPeriodEnd : null;
  const until = (end: Date, reason: EntitlementReason, lapsed: EntitlementReason): Entitlement =>
    now.getTime() < end.getTime() ? { plan: "PRO", reason, accessUntil: end } : free(lapsed);

  switch (state.status) {
    case "active":
      return paidThrough ? until(paidThrough, "active", "active_lapsed") : free("unpaid");
    case "pending":
      // A renewal charge is being retried. Only a previously established
      // paid-through date earns grace; an initial checkout never does.
      return paidThrough ? until(new Date(paidThrough.getTime() + PENDING_GRACE_MS), "pending_grace", "pending_grace_over") : free("unpaid");
    case "cancelled":
      // Retained only for an application-recorded cycle-end cancellation of
      // THIS subscription; a provider cancellation alone is treated as immediate.
      if (paidThrough && state.cancelMode === "CYCLE_END" && state.razorpaySubscriptionId !== null
          && state.cancelSubscriptionId === state.razorpaySubscriptionId) {
        return until(paidThrough, "cancel_at_cycle_end", "cancelled");
      }
      return free("cancelled");
    case "halted":
      return free("halted");
    case "paused":
      return free("paused");
    case "completed":
    case "expired":
      return free("ended");
    default: // created, authenticated, unknown
      return free("not_active");
  }
}

// ---------------------------------------------------------------------------
// Signature and event identity
// ---------------------------------------------------------------------------

const SIGNATURE_HEX = /^[0-9a-fA-F]{64}$/; // HMAC-SHA256 = 32 bytes
const PROVIDER_EVENT_ID = /^[A-Za-z0-9_.:-]{1,100}$/;
export const BODY_HASH_ID_PREFIX = "body-sha256:";

/** HMAC-SHA256 over the exact received bytes, compared in constant time. */
export function verifyRazorpaySignature(rawBody: Uint8Array, signature: string | null, secret: string): boolean {
  if (!secret || typeof signature !== "string" || !SIGNATURE_HEX.test(signature)) return false;
  const expected = createHmac("sha256", secret).update(rawBody).digest();
  const received = Buffer.from(signature, "hex");
  return received.length === expected.length && timingSafeEqual(received, expected);
}

export const sha256Hex = (bytes: Uint8Array): string => createHash("sha256").update(bytes).digest("hex");

export type EventIdSource = "PROVIDER_HEADER" | "BODY_SHA256";

/** x-razorpay-event-id, or a namespaced body digest when the header is absent. Null if the header is malformed. */
export function eventIdentity(header: string | null, bodySha256: string): { id: string; source: EventIdSource } | null {
  if (header === null || header === "") return { id: `${BODY_HASH_ID_PREFIX}${bodySha256}`, source: "BODY_SHA256" };
  return PROVIDER_EVENT_ID.test(header) ? { id: header, source: "PROVIDER_HEADER" } : null;
}

// ---------------------------------------------------------------------------
// Event parsing (only after the signature is verified)
// ---------------------------------------------------------------------------

export interface SubscriptionEntity {
  id: string;
  status: string;
  paidCount: number | null;
  currentEnd: Date | null;
  endedAt: Date | null;
  notes: { userId?: string; checkoutAttemptId?: string };
}

export interface ParsedEvent {
  type: string;
  createdAt: Date;
  subscription: SubscriptionEntity | null;
}

const EVENT_TYPE = /^[a-z_]+(\.[a-z_]+)*$/;
const SUBSCRIPTION_ID = /^sub_[A-Za-z0-9]{1,40}$/;
const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const unixSeconds = (v: unknown): Date | null =>
  typeof v === "number" && Number.isSafeInteger(v) && v > 0 ? new Date(v * 1000) : null;

/** Webhook events that carry a subscription entity, and the entity status each implies. */
export const SUBSCRIPTION_EVENTS: Readonly<Record<string, SubscriptionStatus>> = {
  "subscription.authenticated": "authenticated",
  "subscription.activated": "active",
  "subscription.charged": "active",
  "subscription.pending": "pending",
  "subscription.halted": "halted",
  "subscription.paused": "paused",
  "subscription.resumed": "active",
  "subscription.cancelled": "cancelled",
  "subscription.completed": "completed",
};

export type ParseResult = { ok: true; event: ParsedEvent } | { ok: false; reason: "malformed_json" | "invalid_event" };

export function parseWebhookEvent(rawBody: Uint8Array): ParseResult {
  let json: unknown;
  try {
    json = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(rawBody));
  } catch {
    return { ok: false, reason: "malformed_json" };
  }
  if (!isObject(json)) return { ok: false, reason: "invalid_event" };
  const type = json.event;
  const createdAt = unixSeconds(json.created_at);
  if (typeof type !== "string" || type.length > 64 || !EVENT_TYPE.test(type) || !createdAt) return { ok: false, reason: "invalid_event" };
  if (json.payload !== undefined && !isObject(json.payload)) return { ok: false, reason: "invalid_event" };

  const wrapper = isObject(json.payload) ? json.payload.subscription : undefined;
  if (wrapper === undefined) {
    // Subscription lifecycle events must carry the entity; other events need not.
    return type in SUBSCRIPTION_EVENTS ? { ok: false, reason: "invalid_event" } : { ok: true, event: { type, createdAt, subscription: null } };
  }
  const entity = isObject(wrapper) ? wrapper.entity : undefined;
  if (!isObject(entity) || typeof entity.id !== "string" || !SUBSCRIPTION_ID.test(entity.id) || typeof entity.status !== "string") {
    return { ok: false, reason: "invalid_event" };
  }
  // Razorpay sends notes as an object, or as [] when empty.
  const rawNotes = isObject(entity.notes) ? entity.notes : {};
  const notes: SubscriptionEntity["notes"] = {};
  if (typeof rawNotes.userId === "string") notes.userId = rawNotes.userId;
  if (typeof rawNotes.checkoutAttemptId === "string") notes.checkoutAttemptId = rawNotes.checkoutAttemptId;
  const paidCount = typeof entity.paid_count === "number" && Number.isSafeInteger(entity.paid_count) && entity.paid_count >= 0 ? entity.paid_count : null;
  return {
    ok: true,
    event: {
      type,
      createdAt,
      subscription: { id: entity.id, status: entity.status, paidCount, currentEnd: unixSeconds(entity.current_end), endedAt: unixSeconds(entity.ended_at), notes },
    },
  };
}

// ---------------------------------------------------------------------------
// Transitions and ordering
// ---------------------------------------------------------------------------

/**
 * Explicit lifecycle edges: which subscription events may follow a stored
 * status when both carry the SAME created_at second (Razorpay timestamps are
 * whole seconds and delivery order is not guaranteed). Newer events are applied
 * regardless (the provider's newer snapshot wins), except into terminal rows.
 */
export const SAME_SECOND_EDGES: Readonly<Record<string, ReadonlySet<string>>> = {
  created: new Set(["subscription.authenticated", "subscription.activated", "subscription.charged", "subscription.pending",
    "subscription.halted", "subscription.paused", "subscription.cancelled", "subscription.completed"]),
  authenticated: new Set(["subscription.activated", "subscription.charged", "subscription.pending", "subscription.halted",
    "subscription.cancelled", "subscription.completed"]),
  active: new Set(["subscription.activated", "subscription.charged", "subscription.pending", "subscription.halted",
    "subscription.paused", "subscription.cancelled", "subscription.completed"]),
  pending: new Set(["subscription.charged", "subscription.pending", "subscription.halted", "subscription.cancelled", "subscription.completed"]),
  halted: new Set(["subscription.charged", "subscription.cancelled", "subscription.completed"]),
  paused: new Set(["subscription.resumed", "subscription.cancelled", "subscription.completed"]),
};

/**
 * Same-second precedence: event E is superseded when the last applied event at
 * that second is one that can only come AFTER E (a resume needs a pause first;
 * a recovery charge needs the pending/halt first; activation follows
 * authentication). Such an E is a late predecessor, not a new transition.
 */
export const SUPERSEDED_BY: Readonly<Record<string, ReadonlySet<string>>> = {
  "subscription.paused": new Set(["subscription.resumed"]),
  "subscription.pending": new Set(["subscription.charged"]),
  "subscription.halted": new Set(["subscription.charged"]),
  "subscription.authenticated": new Set(["subscription.activated", "subscription.charged", "subscription.pending", "subscription.halted"]),
  "subscription.activated": new Set(["subscription.charged"]),
};

export type SubscriptionPatch = Partial<{
  status: string;
  plan: Plan;
  lastEventAt: Date;
  lastEventType: string;
  paymentConfirmedAt: Date;
  currentPeriodEnd: Date;
  endedAt: Date;
  razorpaySubscriptionId: string;
  checkoutStatus: CheckoutStatus;
}>;

export type Transition = { apply: true; patch: SubscriptionPatch } | { apply: false; reason: string };

/** Successful-payment evidence: active, at least one paid cycle, and a paid-through date after the event. */
export function hasPaymentEvidence(sub: SubscriptionEntity, eventAt: Date): boolean {
  return sub.status === "active" && sub.paidCount !== null && sub.paidCount >= 1
    && sub.currentEnd !== null && sub.currentEnd.getTime() > eventAt.getTime();
}

/** Pure decision for one subscription event against the locked local row. */
export function decideTransition(row: StoredSubscription, event: ParsedEvent, now: Date): Transition {
  const sub = event.subscription;
  const target = SUBSCRIPTION_EVENTS[event.type];
  if (!sub || !target) return { apply: false, reason: "unsupported_event" };
  if (TERMINAL_STATUSES.has(row.status)) return { apply: false, reason: "terminal_state" };
  if (sub.status !== target) return { apply: false, reason: "status_mismatch" };
  if (row.lastEventAt) {
    const delta = event.createdAt.getTime() - row.lastEventAt.getTime();
    if (delta < 0) return { apply: false, reason: "stale_event" };
    if (delta === 0) {
      if (row.lastEventType && SUPERSEDED_BY[event.type]?.has(row.lastEventType)) return { apply: false, reason: "stale_event" };
      if (!SAME_SECOND_EDGES[row.status]?.has(event.type)) return { apply: false, reason: "stale_event" };
    }
  }

  const patch: SubscriptionPatch = { status: target, lastEventAt: event.createdAt, lastEventType: event.type };
  if (target === "active" && hasPaymentEvidence(sub, event.createdAt)) {
    patch.paymentConfirmedAt = event.createdAt;
    patch.currentPeriodEnd = sub.currentEnd!;
  }
  if ((target === "cancelled" || target === "completed") && sub.endedAt) patch.endedAt = sub.endedAt;
  // pending / halted / paused keep the previously established paid-through date.
  patch.plan = computeEntitlement({ ...row, ...patch }, now).plan; // snapshot only
  return { apply: true, patch };
}

// ---------------------------------------------------------------------------
// Webhook processing
// ---------------------------------------------------------------------------

export interface EventClaim {
  id: string;
  idSource: EventIdSource;
  eventType: string;
  razorpaySubscriptionId: string | null;
  /** Null only for a signed body that could not be parsed. */
  providerCreatedAt: Date | null;
  receivedAt: Date;
  bodySha256: string;
}
/** RECONCILIATION_REQUIRED: a payment arrived for a retired subscription; an operator must act (no automatic access or refund). */
export type EventOutcome = "PROCESSED" | "IGNORED" | "RECONCILIATION_REQUIRED";

export type RetirementReason = "APP_CANCELLED" | "ALREADY_FINAL";

/**
 * Durable, insert-only record of a Razorpay subscription detached from its
 * owner's current row by checkout retirement. Never updated or overwritten.
 */
export interface RetiredSubscription {
  razorpaySubscriptionId: string;
  userId: string;
  checkoutAttemptId: string | null;
  /** Provider status that justified retirement: cancelled / completed / expired. */
  providerStatus: string;
  reason: RetirementReason;
  retiredAt: Date;
}

/** Operations available inside the single webhook transaction. */
export interface WebhookTx {
  /** Insert the event row; false when this identity already exists (duplicate). Waits on a concurrent claim. */
  claimEvent(claim: EventClaim): Promise<boolean>;
  /** SELECT ... FOR UPDATE by exact Razorpay subscription ID. */
  lockByProviderId(razorpaySubscriptionId: string): Promise<StoredSubscription | null>;
  /** SELECT ... FOR UPDATE by exact checkout attempt ID. */
  lockByCheckoutAttempt(checkoutAttemptId: string): Promise<StoredSubscription | null>;
  /** Retired mapping by exact Razorpay subscription ID (insert-only rows; no lock needed). */
  findRetired(razorpaySubscriptionId: string): Promise<RetiredSubscription | null>;
  /** Insert-only; called with the owning subscription row locked. */
  insertRetired(mapping: RetiredSubscription): Promise<void>;
  updateSubscription(rowId: string, patch: SubscriptionPatch): Promise<void>;
  /** `userId` is set only for events resolved through a retired mapping (its original owner). */
  finishEvent(id: string, outcome: EventOutcome, reason: string, processedAt: Date, userId?: string | null): Promise<void>;
}
export interface WebhookStore {
  /** Runs `fn` in one database transaction; any throw rolls everything back. */
  transaction<T>(fn: (tx: WebhookTx) => Promise<T>): Promise<T>;
}

export interface WebhookResponse {
  status: number;
  body: { received: boolean; outcome?: string; error?: string };
}

export const CHECKOUT_ATTEMPT_ID = /^ca_[A-Za-z0-9_-]{32}$/;

/**
 * What a late event for an ENDED subscription (retired, or terminal but still
 * mapped) establishes, using only parsed fields:
 *   payment            subscription.charged whose entity is active with a paid
 *                      cycle running past the event (hasPaymentEvidence)
 *   unverified_charge  subscription.charged without that evidence (still a
 *                      charge notification: flagged, never trusted)
 *   authentication     subscription.authenticated (mandate only)
 *   status             every other lifecycle event. paid_count is CUMULATIVE,
 *                      so on activated / resumed / pending / halted / paused /
 *                      cancelled / completed it is history, not a new charge;
 *                      each successful charge has its own subscription.charged.
 */
export type EndedEventKind = "payment" | "unverified_charge" | "authentication" | "status";

export function classifyEndedEvent(event: ParsedEvent): EndedEventKind {
  const sub = event.subscription;
  if (event.type === "subscription.charged") return sub && hasPaymentEvidence(sub, event.createdAt) ? "payment" : "unverified_charge";
  if (event.type === "subscription.authenticated") return "authentication";
  return "status";
}

/**
 * An event for a retired subscription never touches any current row. A charge
 * notification is flagged for an operator; nothing grants access or refunds.
 */
export function decideRetiredEvent(event: ParsedEvent): { outcome: EventOutcome; reason: string } {
  switch (classifyEndedEvent(event)) {
    case "payment": return { outcome: "RECONCILIATION_REQUIRED", reason: "retired_payment" };
    case "unverified_charge": return { outcome: "RECONCILIATION_REQUIRED", reason: "retired_charge_unverified" };
    case "authentication": return { outcome: "IGNORED", reason: "retired_authenticated" };
    default: return { outcome: "IGNORED", reason: "retired_subscription" };
  }
}

/**
 * A charge for a subscription that is terminal but still the user's current
 * mapping (no replacement checkout yet). Its ownership is archived NOW, in this
 * transaction, while the row is locked, so the flag carries the original owner
 * and attempt; the row itself is never changed (terminal stays terminal, no Pro).
 */
async function flagTerminalCharge(tx: WebhookTx, row: StoredSubscription, sub: SubscriptionEntity, now: Date,
  kind: "payment" | "unverified_charge"): Promise<Applied> {
  const intended: RetiredSubscription = {
    razorpaySubscriptionId: sub.id, userId: row.userId, checkoutAttemptId: row.checkoutAttemptId,
    providerStatus: row.status, reason: "ALREADY_FINAL", retiredAt: now,
  };
  const existing = await tx.findRetired(sub.id);
  if (existing && !sameRetiredOwnership(existing, intended)) return { outcome: "IGNORED", reason: "reconciliation_conflict" };
  if (!existing) await tx.insertRetired(intended);
  return { outcome: "RECONCILIATION_REQUIRED", reason: kind === "payment" ? "terminal_payment" : "terminal_charge_unverified", userId: row.userId };
}

type Applied = { outcome: EventOutcome; reason: string; userId?: string };

/**
 * Payload notes are cross-checks against the exact LOCAL mapping, which is the
 * only ownership source. Returns the disagreement, if any.
 */
function notesConflict(notes: SubscriptionEntity["notes"], owner: { userId: string; checkoutAttemptId: string | null }):
  NotesConflict | null {
  if (notes.userId !== undefined && notes.userId !== owner.userId) return "notes_user_mismatch";
  if (owner.checkoutAttemptId && notes.checkoutAttemptId !== undefined && notes.checkoutAttemptId !== owner.checkoutAttemptId) return "attempt_mismatch";
  return null;
}

/** Lifecycle events that can only reduce access; safe to apply to the exact local row whatever the notes say. */
export const RESTRICTIVE_EVENTS: ReadonlySet<string> = new Set([
  "subscription.cancelled", "subscription.completed", "subscription.halted", "subscription.paused",
]);
/** Events that could grant, restore or prepare access; never applied on contradictory notes. */
export const ACCESS_INCREASING_EVENTS: ReadonlySet<string> = new Set([
  "subscription.activated", "subscription.authenticated", "subscription.resumed",
]);

type NotesConflict = "notes_user_mismatch" | "attempt_mismatch";

/**
 * Notes disagree with the LOCAL owner (exact provider-ID row, or the exact
 * attempt reservation when `bind`). Ownership never moves to the notes user,
 * nothing is ever bound, and every outcome is attributed to the local owner:
 *
 *   charged              RECONCILIATION_REQUIRED, row untouched (no Pro granted or extended)
 *   activated/authenticated/resumed
 *                        RECONCILIATION_REQUIRED, row untouched (no Pro granted or restored)
 *   cancelled/completed/halted/paused
 *                        applied through the normal ordering/terminal rules
 *                        (decideTransition), so access is revoked; recorded
 *                        PROCESSED "applied_<conflict>" as an integrity warning.
 *                        Rejected by those rules (stale, terminal, entity
 *                        status mismatch): IGNORED "<conflict>".
 *   pending              IGNORED "<conflict>", not applied: moving active ->
 *                        pending would open the 72h grace beyond the paid-through
 *                        date, i.e. EXTEND access. Left active, access still
 *                        ends at the stored paid-through date.
 *   any event on an early (unbound) reservation
 *                        charge: RECONCILIATION_REQUIRED; otherwise IGNORED.
 *                        Never bound, never applied.
 *
 * Operator query: outcome = 'RECONCILIATION_REQUIRED' OR reason LIKE '%_mismatch'.
 */
async function applyWithNotesConflict(tx: WebhookTx, row: StoredSubscription, bind: boolean, event: ParsedEvent,
  conflict: NotesConflict, now: Date): Promise<Applied> {
  const flag: Applied = { outcome: "RECONCILIATION_REQUIRED", reason: conflict, userId: row.userId };
  const ignore: Applied = { outcome: "IGNORED", reason: conflict, userId: row.userId };
  if (event.type === "subscription.charged") return flag;
  if (bind) return ignore;
  if (ACCESS_INCREASING_EVENTS.has(event.type)) return flag;
  if (RESTRICTIVE_EVENTS.has(event.type)) {
    const transition = decideTransition(row, event, now);
    if (!transition.apply) return ignore;
    // Restrictive targets never carry payment fields; dropped anyway so a mismatched event can never extend access.
    const patch: SubscriptionPatch = { ...transition.patch };
    delete patch.paymentConfirmedAt;
    delete patch.currentPeriodEnd;
    await tx.updateSubscription(row.id, patch);
    return { outcome: "PROCESSED", reason: `applied_${conflict}`, userId: row.userId };
  }
  return ignore; // subscription.pending (see above)
}

/** For a retired subscription: the stored original owner wins; nothing current is touched. */
function onRetiredNotesConflict(event: ParsedEvent, reason: NotesConflict, originalOwner: string): Applied {
  return event.type === "subscription.charged"
    ? { outcome: "RECONCILIATION_REQUIRED", reason, userId: originalOwner }
    : { outcome: "IGNORED", reason, userId: originalOwner };
}

async function applySubscriptionEvent(tx: WebhookTx, event: ParsedEvent, now: Date): Promise<Applied> {
  const sub = event.subscription;
  if (!sub || !(event.type in SUBSCRIPTION_EVENTS)) return { outcome: "IGNORED", reason: "unsupported_event" };

  // 1. Exact, already-persisted Razorpay subscription ID (covers legacy rows).
  let row = await tx.lockByProviderId(sub.id);
  let bind = false;
  // 2. Early webhook: the exact checkout attempt of a current, still-unbound reservation.
  if (!row && sub.notes.checkoutAttemptId && CHECKOUT_ATTEMPT_ID.test(sub.notes.checkoutAttemptId)) {
    const candidate = await tx.lockByCheckoutAttempt(sub.notes.checkoutAttemptId);
    if (candidate && candidate.razorpaySubscriptionId === null
        && (candidate.checkoutStatus === "RESERVED" || candidate.checkoutStatus === "UNRESOLVED")) {
      row = candidate;
      bind = true;
    }
  }
  // 3. Retired mapping. Retirement replaces the attempt ID in the same
  // transaction, so step 2 cannot match a retired subscription; the check on a
  // bind candidate is defence in depth: a retired ID is never bound again.
  if (!row || bind) {
    const retired = await tx.findRetired(sub.id);
    if (retired) {
      // The stored original owner wins over notes.
      const conflict = notesConflict(sub.notes, retired);
      if (conflict) return onRetiredNotesConflict(event, conflict, retired.userId);
      return { ...decideRetiredEvent(event), userId: retired.userId };
    }
  }
  if (!row) return { outcome: "IGNORED", reason: "unknown_subscription" };
  // The exact provider ID (or exact attempt reservation) is authoritative. An
  // early webhook whose notes disagree is never bound: the checkout's own bind
  // (from Razorpay's create response) maps it, and later events flag again.
  const conflict = notesConflict(sub.notes, row);
  if (conflict) return applyWithNotesConflict(tx, row, bind, event, conflict, now);
  if (!bind && TERMINAL_STATUSES.has(row.status)) {
    const kind = classifyEndedEvent(event);
    if (kind === "payment" || kind === "unverified_charge") return flagTerminalCharge(tx, row, sub, now, kind);
  }

  const transition = decideTransition(row, event, now);
  const patch: SubscriptionPatch = transition.apply ? { ...transition.patch } : {};
  if (bind) Object.assign(patch, { razorpaySubscriptionId: sub.id, checkoutStatus: "CREATED" as const });
  if (Object.keys(patch).length > 0) await tx.updateSubscription(row.id, patch);
  return transition.apply ? { outcome: "PROCESSED", reason: bind ? "applied_bound" : "applied" } : { outcome: "IGNORED", reason: transition.reason };
}

export interface WebhookInput {
  rawBody: Uint8Array;
  headers: { get(name: string): string | null };
  secret: string | undefined;
  store: WebhookStore;
  now?: () => Date;
  log?: (line: string) => void;
}

/**
 * Verify, identify, then process one Razorpay delivery inside ONE transaction
 * (claim event -> lock/resolve subscription -> validate -> apply -> record).
 * 200: processed, ignored (including signed but unparseable bodies) or duplicate.
 * 401: missing/invalid signature. 503: not configured, or a failure that
 * rolled everything back (Razorpay retries).
 */
export async function handleRazorpayWebhook({ rawBody, headers, secret, store, now = () => new Date(), log = console.info }: WebhookInput): Promise<WebhookResponse> {
  if (!secret) return { status: 503, body: { received: false, error: "not_configured" } };
  if (!verifyRazorpaySignature(rawBody, headers.get("x-razorpay-signature"), secret)) {
    return { status: 401, body: { received: false, error: "invalid_signature" } };
  }
  const bodySha256 = sha256Hex(rawBody);
  const header = headers.get("x-razorpay-event-id");
  // A malformed event-id header cannot be trusted as identity; the body digest still dedupes retries.
  const identity = eventIdentity(header, bodySha256) ?? eventIdentity(null, bodySha256)!;
  const headerNote = header && identity.source === "BODY_SHA256" ? " id_header=invalid" : "";
  const parsed = parseWebhookEvent(rawBody);
  // A correctly signed body that cannot be parsed is irrecoverable: Razorpay
  // would resend the same bytes. It is recorded (no payload) and acknowledged
  // with 200 so retries cannot exhaust and disable the webhook.
  const event = parsed.ok ? parsed.event : null;
  const eventType = event?.type ?? "unparsed";

  let result: { outcome: EventOutcome | "DUPLICATE"; reason: string };
  try {
    result = await store.transaction(async (tx) => {
      const claimed = await tx.claimEvent({
        id: identity.id,
        idSource: identity.source,
        eventType,
        razorpaySubscriptionId: event?.subscription?.id ?? null,
        providerCreatedAt: event?.createdAt ?? null,
        receivedAt: now(),
        bodySha256,
      });
      if (!claimed) return { outcome: "DUPLICATE" as const, reason: "duplicate" };
      const applied: Applied = parsed.ok
        ? await applySubscriptionEvent(tx, parsed.event, now())
        : { outcome: "IGNORED", reason: parsed.reason };
      await tx.finishEvent(identity.id, applied.outcome, applied.reason, now(), applied.userId ?? null);
      return { outcome: applied.outcome, reason: applied.reason };
    });
  } catch (error) {
    // Rolled back: no event row, no subscription change. Log the class only.
    log(`[razorpay-webhook] failed type=${eventType} error=${error instanceof Error ? error.name : "unknown"}`);
    return { status: 503, body: { received: false, error: "retry" } };
  }
  log(`[razorpay-webhook] ${result.outcome.toLowerCase()} type=${eventType} reason=${result.reason} id_source=${identity.source}${headerNote}`);
  return { status: 200, body: { received: true, outcome: result.outcome.toLowerCase() } };
}

// ---------------------------------------------------------------------------
// Checkout reservation
// ---------------------------------------------------------------------------

/**
 * A bound, never-authenticated checkout is reopened (same Razorpay ID) for this
 * long. After that it is RETIRED before a new one is created: the create call
 * sets no expire_by, and local age proves nothing about whether its link can
 * still take payment, so the old ID stays mapped until Razorpay itself reports
 * the subscription cancelled/expired/completed.
 */
export const CHECKOUT_REUSE_WINDOW_MS = 24 * 60 * 60 * 1000;
/** After this the create call is treated as unresolved (it may still have succeeded). */
export const PROVIDER_CREATE_TIMEOUT_MS = 15_000;

export type CheckoutBlock = "already_pro" | "in_progress" | "unresolved" | "needs_attention";
export type CheckoutDecision =
  /**
   * `retire`: the mapping for the old subscription ID, written in the SAME
   * transaction that detaches it. `retireRecorded`: an identical-ownership
   * mapping already exists and is kept as is (never overwritten).
   */
  | { kind: "reserve"; retire?: RetiredSubscription; retireRecorded?: true }
  | { kind: "reuse"; subscriptionId: string }
  | { kind: "retire"; subscriptionId: string }
  | { kind: "blocked"; reason: CheckoutBlock };

const LIVE_STATUSES: ReadonlySet<string> = new Set(["authenticated", "active", "pending", "halted", "paused"]);

/** What Razorpay confirmed, outside any transaction, about a stale checkout. */
export interface ProviderRetirement {
  subscriptionId: string;
  providerStatus: string;
  reason: RetirementReason;
}

/**
 * Pure decision, made while the user's row and subscription row are locked.
 * `retired` is a Razorpay subscription the provider has just confirmed can no
 * longer accept payment and showed no paid cycle. It is detached only if the
 * row still holds that exact ID with no confirmed payment; a webhook that got
 * the lock first (payment, authentication) changes the decision.
 */
export function decideCheckout(row: StoredSubscription | null, now: Date, retired?: ProviderRetirement,
  archived?: ArchivedOwnership | null): CheckoutDecision {
  if (!row) return { kind: "reserve" };
  const archive = (intended: RetiredSubscription): CheckoutDecision => {
    if (!archived) return { kind: "reserve", retire: intended };
    return sameRetiredOwnership(archived, intended)
      ? { kind: "reserve", retire: intended, retireRecorded: true }
      : { kind: "blocked", reason: "needs_attention" }; // conflicting ownership: nothing is written
  };
  if (row.checkoutStatus === "RESERVED") return { kind: "blocked", reason: "in_progress" };
  if (row.checkoutStatus === "UNRESOLVED") return { kind: "blocked", reason: "unresolved" };
  if (computeEntitlement(row, now).plan === "PRO") return { kind: "blocked", reason: "already_pro" };
  // A remote subscription that can (still) charge: never create a second one.
  if (LIVE_STATUSES.has(row.status)) return { kind: "blocked", reason: "needs_attention" };
  if (retired && row.razorpaySubscriptionId === retired.subscriptionId) {
    // Our own cancel's webhook may already have marked it cancelled: still retired, still mapped.
    if (row.paymentConfirmedAt || !(row.status === "created" || TERMINAL_STATUSES.has(row.status))) {
      return { kind: "blocked", reason: "needs_attention" }; // a recorded payment is never silently detached
    }
    return archive({ razorpaySubscriptionId: retired.subscriptionId, userId: row.userId, checkoutAttemptId: row.checkoutAttemptId,
      providerStatus: retired.providerStatus, reason: retired.reason, retiredAt: now });
  }
  if (row.status === "created" && row.razorpaySubscriptionId) {
    // Its link may still be payable: never detach it on local age alone.
    const started = row.checkoutStartedAt?.getTime();
    if (started !== undefined && now.getTime() - started < CHECKOUT_REUSE_WINDOW_MS) {
      return { kind: "reuse", subscriptionId: row.razorpaySubscriptionId };
    }
    return { kind: "retire", subscriptionId: row.razorpaySubscriptionId };
  }
  if (row.razorpaySubscriptionId && TERMINAL_STATUSES.has(row.status)) {
    // Ended per an applied, signature-verified webhook (status, never local age):
    // archive its ownership in the same transaction that replaces the mapping, so
    // its late events resolve to the original owner instead of unknown_subscription.
    return archive({ razorpaySubscriptionId: row.razorpaySubscriptionId, userId: row.userId, checkoutAttemptId: row.checkoutAttemptId,
      providerStatus: row.status, reason: "ALREADY_FINAL", retiredAt: now });
  }
  return { kind: "reserve" }; // none, or failed (nothing remote)
}

/** Ownership recorded by an existing retired mapping for the row's provider ID. */
export type ArchivedOwnership = Pick<RetiredSubscription, "userId" | "checkoutAttemptId">;

/**
 * An existing mapping for the same provider ID is acceptable only when it records
 * the same owner and checkout attempt; it is then kept as is (never overwritten).
 * Anything else is conflicting ownership: the reservation is abandoned unchanged.
 */
export function sameRetiredOwnership(existing: ArchivedOwnership, intended: RetiredSubscription): boolean {
  return existing.userId === intended.userId && existing.checkoutAttemptId === intended.checkoutAttemptId;
}

export interface CheckoutStore {
  /**
   * One short transaction: lock the user row (by Clerk ID), then its subscription
   * row, and any retired mapping already recorded for that row's provider ID;
   * decide; reserve if decided. A `reserve` carrying `retire` (and not
   * `retireRecorded`) inserts that mapping in the same transaction that detaches
   * the old ID. Existing mappings are never overwritten.
   */
  reserve(clerkUserId: string, attemptId: string, now: Date,
    decide: (row: StoredSubscription | null, archived: ArchivedOwnership | null) => CheckoutDecision):
    Promise<{ kind: "no_account" } | (CheckoutDecision & { userId: string })>;
  /** Conditional: binds only the same, still-unbound (or same-ID) reservation. */
  bind(attemptId: string, razorpaySubscriptionId: string): Promise<boolean>;
  /** Conditional on RESERVED/UNRESOLVED and unbound. */
  markFailed(attemptId: string): Promise<void>;
  /** Conditional on RESERVED. */
  markUnresolved(attemptId: string): Promise<void>;
}

/** The subscription entity fields retirement relies on (Razorpay returns the full entity). */
export interface ProviderSubscriptionState { status: string; paid_count?: number | null }

export interface SubscriptionProvider {
  create(notes: { userId: string; checkoutAttemptId: string }): Promise<{ id: string }>;
  /** Current provider-side state of one subscription. */
  fetch(razorpaySubscriptionId: string): Promise<ProviderSubscriptionState>;
  /** Immediate cancellation of one subscription; resolves to the updated entity. */
  cancel(razorpaySubscriptionId: string): Promise<ProviderSubscriptionState>;
}

/** Provider states in which a subscription can no longer accept any payment. */
const UNPAYABLE_STATUSES: ReadonlySet<string> = new Set(["cancelled", "completed", "expired"]);

/**
 * Final AND explicitly reports zero paid cycles. paid_count >= 1, or a missing
 * count, is evidence (or doubt) of payment: never an ordinary unpaid retirement.
 * A zero count does not prove no money moved (e.g. a charge still settling),
 * which is why the retired mapping exists.
 */
const unpaidFinal = (s: ProviderSubscriptionState | undefined): boolean =>
  !!s && UNPAYABLE_STATUSES.has(s.status) && s.paid_count === 0;

async function withTimeout<T>(task: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error("timeout")), ms); });
  try {
    return await Promise.race([task, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Make a stale, never-authenticated checkout unpayable before it is detached.
 * True only when Razorpay reports it cancelled/completed/expired (as is, or
 * after an immediate cancel of a still-"created" subscription). Any other
 * state or any error keeps the existing mapping.
 */
async function retireStaleCheckout(provider: SubscriptionProvider, id: string, timeoutMs: number):
  Promise<{ ok: true; retirement: ProviderRetirement } | { ok: false; reason: "needs_attention" | "provider_unconfirmed" }> {
  try {
    const current = await withTimeout(provider.fetch(id), timeoutMs);
    if (unpaidFinal(current)) return { ok: true, retirement: { subscriptionId: id, providerStatus: current.status, reason: "ALREADY_FINAL" } };
    // Paid, authenticated, active, or final with a paid cycle: kept mapped for its webhooks and an operator.
    if (current?.status !== "created" || current.paid_count !== 0) return { ok: false, reason: "needs_attention" };
    const cancelled = await withTimeout(provider.cancel(id), timeoutMs);
    return unpaidFinal(cancelled) && cancelled.status === "cancelled"
      ? { ok: true, retirement: { subscriptionId: id, providerStatus: cancelled.status, reason: "APP_CANCELLED" } }
      : { ok: false, reason: "needs_attention" };
  } catch {
    return { ok: false, reason: "provider_unconfirmed" };
  }
}

export type CheckoutResult =
  | { ok: true; subscriptionId: string }
  | { ok: false; reason: "unauthenticated" | "no_account" | CheckoutBlock | "provider_rejected" | "provider_unconfirmed" };

export const newCheckoutAttemptId = (): string => `ca_${randomBytes(24).toString("base64url")}`;

/**
 * The Razorpay SDK rethrows HTTP errors as { statusCode, error }. A 4xx (other
 * than 408) is a definite rejection: nothing was created. Anything else
 * (timeouts, network errors, 5xx) is ambiguous.
 */
export function isDefiniteRejection(error: unknown): boolean {
  const code = isObject(error) ? error.statusCode : undefined;
  return typeof code === "number" && code >= 400 && code < 500 && code !== 408;
}

const TIMEOUT = Symbol("timeout");

export interface CheckoutInput {
  clerkUserId: string | null;
  store: CheckoutStore;
  provider: SubscriptionProvider;
  now?: () => Date;
  newAttemptId?: () => string;
  timeoutMs?: number;
  /** Receives the late-settlement task after a timeout (tests await it). */
  background?: (task: Promise<void>) => void;
}

export async function startCheckout({
  clerkUserId, store, provider, now = () => new Date(), newAttemptId = newCheckoutAttemptId,
  timeoutMs = PROVIDER_CREATE_TIMEOUT_MS, background = () => {},
}: CheckoutInput): Promise<CheckoutResult> {
  if (!clerkUserId) return { ok: false, reason: "unauthenticated" };
  const attemptId = newAttemptId();
  let reservation = await store.reserve(clerkUserId, attemptId, now(), (row, archived) => decideCheckout(row, now(), undefined, archived));
  if (reservation.kind === "retire") {
    // Outside any transaction. The decision is re-made under the row lock, so a
    // webhook that changed the row meanwhile (e.g. a late payment) wins.
    const retired = await retireStaleCheckout(provider, reservation.subscriptionId, timeoutMs);
    if (!retired.ok) return { ok: false, reason: retired.reason };
    reservation = await store.reserve(clerkUserId, attemptId, now(), (row, archived) => decideCheckout(row, now(), retired.retirement, archived));
    if (reservation.kind === "retire") return { ok: false, reason: "needs_attention" }; // a different stale ID appeared
  }
  if (reservation.kind === "no_account") return { ok: false, reason: "no_account" };
  if (reservation.kind === "blocked") return { ok: false, reason: reservation.reason };
  if (reservation.kind === "reuse") return { ok: true, subscriptionId: reservation.subscriptionId };

  // Reservation committed; the provider call happens outside any transaction.
  const call = provider.create({ userId: reservation.userId, checkoutAttemptId: attemptId });
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<typeof TIMEOUT>((resolve) => { timer = setTimeout(() => resolve(TIMEOUT), timeoutMs); });
  let outcome: { id: string } | typeof TIMEOUT;
  try {
    outcome = await Promise.race([call, timeout]);
  } catch (error) {
    clearTimeout(timer);
    if (isDefiniteRejection(error)) {
      await store.markFailed(attemptId);
      return { ok: false, reason: "provider_rejected" };
    }
    await store.markUnresolved(attemptId);
    return { ok: false, reason: "provider_unconfirmed" };
  }
  clearTimeout(timer);

  if (outcome === TIMEOUT) {
    await store.markUnresolved(attemptId);
    // Never retried. If the original call settles later, record what it says.
    background(call.then(
      async (sub) => { if (SUBSCRIPTION_ID.test(sub?.id ?? "")) await store.bind(attemptId, sub.id); },
      async (error) => { if (isDefiniteRejection(error)) await store.markFailed(attemptId); },
    ).catch(() => {}));
    return { ok: false, reason: "provider_unconfirmed" };
  }
  if (!SUBSCRIPTION_ID.test(outcome?.id ?? "") || !(await store.bind(attemptId, outcome.id))) {
    await store.markUnresolved(attemptId);
    return { ok: false, reason: "provider_unconfirmed" };
  }
  return { ok: true, subscriptionId: outcome.id };
}
