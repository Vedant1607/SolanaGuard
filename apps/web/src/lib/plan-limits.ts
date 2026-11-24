export const FREE_PROTOCOL_LIMIT = 5;

/** How many protocol cards a plan may open; the rest render as locked cards. */
export function visibleProtocolCount(plan: string, total: number): number {
  return plan === "PRO" ? total : FREE_PROTOCOL_LIMIT;
}
