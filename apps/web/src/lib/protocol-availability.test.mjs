// Run with: pnpm --filter @solanaguard/web test  (Node >= 23.6 loads the .ts modules directly)
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DATA_UNAVAILABLE_LABEL,
  UNAVAILABLE_LABEL,
  availabilityRank,
  protocolMetricLabels,
  sortByDataAvailability,
} from "./protocol-availability.ts";
import { FREE_PROTOCOL_LIMIT, visibleProtocolCount } from "./plan-limits.ts";

function protocol(slug, overrides = {}) {
  return {
    slug,
    name: slug[0].toUpperCase() + slug.slice(1),
    category: "DEX",
    tvlUsd: 1_000_000,
    txCount24h: 100,
    snapshotAt: "2026-11-12T10:00:00",
    previousSnapshotAt: "2026-11-12T09:55:00",
    overallScore: 40,
    riskLevel: "MEDIUM",
    explanation: "rule-based",
    scoredAt: "2026-11-12T10:00:00",
    ...overrides,
  };
}

const noData = { tvlUsd: null, txCount24h: null, snapshotAt: null, previousSnapshotAt: null, overallScore: null, riskLevel: null, explanation: null, scoredAt: null };

// What GET /api/v1/protocols returns after Phase 1: active protocols ordered
// by name. Drift is catalogued (security-only) and has no TVL or score.
const API_ORDER = [
  protocol("drift", { category: "PERPS", ...noData }),
  protocol("jito", { category: "LIQUID_STAKING" }),
  protocol("kamino", { name: "Kamino Lend", category: "LENDING" }),
  protocol("marinade", { category: "LIQUID_STAKING" }),
  protocol("orca"),
  protocol("raydium"),
];

const slugs = (list) => list.map((p) => p.slug);

test("protocols with data come before protocols without data", () => {
  assert.deepEqual(slugs(sortByDataAvailability(API_ORDER)), ["jito", "kamino", "marinade", "orca", "raydium", "drift"]);
});

test("a protocol without data does not take a Free slot when enough data-backed protocols exist", () => {
  const ordered = sortByDataAvailability(API_ORDER);
  const visible = ordered.slice(0, visibleProtocolCount("FREE", ordered.length));
  assert.equal(visible.length, FREE_PROTOCOL_LIMIT);
  assert.ok(!slugs(visible).includes("drift"));
  assert.ok(visible.every((p) => availabilityRank(p) === 0));
});

test("no-data protocols stay visible (listed last), never dropped", () => {
  const ordered = sortByDataAvailability(API_ORDER);
  assert.equal(ordered.length, API_ORDER.length);
  assert.deepEqual(new Set(slugs(ordered)), new Set(slugs(API_ORDER)));
  assert.equal(ordered.at(-1).slug, "drift");
});

test("a no-data protocol still fills a Free slot when there are too few data-backed protocols", () => {
  const few = [protocol("alpha", noData), protocol("beta"), protocol("gamma")];
  const ordered = sortByDataAvailability(few);
  assert.deepEqual(slugs(ordered), ["beta", "gamma", "alpha"]);
  assert.deepEqual(slugs(ordered.slice(0, visibleProtocolCount("FREE", ordered.length))), ["beta", "gamma", "alpha"]);
});

test("missing TVL with a valid risk score ranks the same as TVL without a score", () => {
  const riskOnly = protocol("riskonly", { tvlUsd: null });
  const tvlOnly = protocol("tvlonly", { overallScore: null, riskLevel: null });
  assert.equal(availabilityRank(riskOnly), 1);
  assert.equal(availabilityRank(tvlOnly), 1);
  const ordered = sortByDataAvailability([protocol("none", noData), tvlOnly, riskOnly, protocol("full")]);
  // full data first, then both partial ones in their original order, then none.
  assert.deepEqual(slugs(ordered), ["full", "tvlonly", "riskonly", "none"]);
});

test("a score without a risk level is not treated as complete risk data", () => {
  assert.equal(availabilityRank(protocol("x", { riskLevel: null })), 1);
});

test("a reported TVL of exactly 0 is a real measurement, not missing data", () => {
  assert.equal(availabilityRank(protocol("zero", { tvlUsd: 0 })), 0);
  assert.notEqual(protocolMetricLabels(protocol("zero", { tvlUsd: 0 })).tvl, UNAVAILABLE_LABEL);
});

test("sorting is stable: the API's order is kept within each availability rank", () => {
  const input = [protocol("zeta"), protocol("alpha", noData), protocol("mu"), protocol("beta", noData), protocol("eta")];
  assert.deepEqual(slugs(sortByDataAvailability(input)), ["zeta", "mu", "eta", "alpha", "beta"]);
});

test("sorting does not mutate the API response", () => {
  const input = [...API_ORDER];
  sortByDataAvailability(input);
  assert.deepEqual(slugs(input), slugs(API_ORDER));
});

test("plan limits are unchanged: Free opens 5 cards, Pro opens all", () => {
  assert.equal(FREE_PROTOCOL_LIMIT, 5);
  assert.equal(visibleProtocolCount("FREE", 6), 5);
  assert.equal(visibleProtocolCount("FREE", 50), 5);
  assert.equal(visibleProtocolCount("PRO", 6), 6);
  assert.equal(visibleProtocolCount("PRO", 50), 50);
  // Unknown plan values fall back to the Free limit, as before.
  assert.equal(visibleProtocolCount("ENTERPRISE?", 6), 5);
});

test("null metrics are shown as unavailable, never as zero or LOW", () => {
  const labels = protocolMetricLabels(API_ORDER[0]);
  assert.equal(labels.tvl, UNAVAILABLE_LABEL);
  assert.equal(labels.riskScore, UNAVAILABLE_LABEL);
  assert.equal(labels.riskLevel, null);
  assert.equal(labels.dataUnavailable, true);
  assert.equal(DATA_UNAVAILABLE_LABEL, "Data unavailable");
  for (const value of [labels.tvl, labels.riskScore]) {
    assert.doesNotMatch(value, /\$0|^0|LOW/);
  }
});

test("available metrics are shown as reported", () => {
  const labels = protocolMetricLabels(protocol("orca", { tvlUsd: 1_250_000, overallScore: 0, riskLevel: "LOW" }));
  assert.equal(labels.tvl, "$1.3M");
  assert.equal(labels.riskScore, "0/100"); // a genuine score of 0 is shown, not hidden
  assert.equal(labels.riskLevel, "LOW");
  assert.equal(labels.dataUnavailable, false);
});

test("partially available protocols are not flagged as having no data", () => {
  assert.equal(protocolMetricLabels(protocol("p", { tvlUsd: null })).dataUnavailable, false);
});

test("activity sources name only what actually backs the data", async () => {
  const { activitySourceText } = await import("./protocol-availability.ts");
  // Drift-like: catalogued for security only, never ingested — no source is claimed.
  const none = activitySourceText({ tvlUsd: null, txCount24h: null, snapshotAt: null });
  assert.equal(none, "No TVL or transaction data has been ingested for this protocol.");
  assert.doesNotMatch(none, /Helius|DefiLlama/);
  // TVL ingested, transaction count unavailable (e.g. over the signature cap).
  assert.equal(
    activitySourceText({ tvlUsd: 942_682_378, txCount24h: null, snapshotAt: "2026-11-12T08:56:18" }),
    "TVL from DefiLlama · 24h transactions unavailable in the latest snapshot",
  );
  assert.equal(
    activitySourceText({ tvlUsd: 848_450_017, txCount24h: 8859, snapshotAt: "2026-11-12T08:56:41" }),
    "TVL from DefiLlama · 24h transactions from Helius",
  );
  // A snapshot without TVL does not claim DefiLlama.
  assert.match(activitySourceText({ tvlUsd: null, txCount24h: 12, snapshotAt: "2026-11-12T08:56:41" }), /^TVL unavailable · 24h transactions from Helius$/);
});
