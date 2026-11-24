// Run with: pnpm --filter @solanaguard/web test
// Fixtures are hand-built in the shape of the real flow_context contract
// (apps/api/app/ml/flow_context.py); they are not live data.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  describeFlowContext,
  formatLog,
  formatLogAsPct,
  formatPct,
  formatPoints,
  groupReasons,
  parseFlowContext,
} from "./flow-context-view.ts";
import { loadIntelligence, parseIntelligence } from "./intelligence-view.ts";

const SOURCE = { source: "defillama", adapters: ["defillama_tvl_history@1", "defillama_token_history@1"], metrics: ["tvl.usd.daily", "tokens.qty.daily", "tokens.usd.daily", "tvl.borrowed.usd.daily"] };
const DISCLAIMER = "Estimated from DefiLlama token quantities valued at the previous day's DefiLlama prices. It is not direct evidence of user deposits or withdrawals.";
const NOT_APPLICABLE = { field: "utilisation", code: "not_applicable", detail: "No borrowed-value history exists for this protocol." };

function base(overrides = {}) {
  return {
    feature_version: "features-daily-v1", measurement_day: "2026-11-13", previous_day: "2026-11-12", regime: "normal",
    available: false, flow: null, coverage: null, non_flow_effect: null, tvl_log_return: null, sol_log_return: -0.018795,
    lending: null, contributions: null, unavailable: [], source: SOURCE, disclaimer: DISCLAIMER, ...overrides,
  };
}

function lendingContext(overrides = {}) {
  return base({
    available: true,
    flow: { log_change: 0.029705, pct_change: 0.03015, robust_z: 3.7652, valuation: "previous-day DefiLlama USD prices" },
    coverage: { matched_share: 1.0, removed_share: 0.0, added_share: 0.000306, unpriced_assets: 0, minimum_matched_share: 0.9 },
    non_flow_effect: { log_change: -0.0131 },
    tvl_log_return: 0.016604,
    lending: { borrowed_usd: 810382661.0, borrowed_log_change: 0.02478, utilisation: 0.406996, utilisation_change: 0.001972, utilisation_change_z: 0.9236, definition: "borrowed / (borrowed + available liquidity)" },
    contributions: {
      unit: "share of the previous day's matched USD value (simple, not log)",
      positive: [{ symbol: "USDC", share: 0.001564 }, { symbol: "USDG", share: 0.029132 }, { symbol: "PYUSD", share: 0.001296 }, { symbol: "CBBTC", share: 0.000982 }, { symbol: "REUSD", share: 0.000554 }],
      negative: [{ symbol: "SOL", share: -0.001227 }, { symbol: "USDE", share: -0.001679 }, { symbol: "SUSDAI", share: -0.000564 }, { symbol: "ONYC", share: -0.000231 }, { symbol: "SYRUPUSDC", share: -0.00023 }],
      unchanged: [], listed_total: 0.029595, flow_total: 0.03015, other_assets: 0.000555, max_listed: 10,
    },
    ...overrides,
  });
}

const residual = () => base({
  regime: "residual",
  coverage: { matched_share: 1.0, removed_share: 0.0, added_share: 0.0, unpriced_assets: 0, minimum_matched_share: 0.9 },
  unavailable: [
    { field: "tvl_log_return", code: "regime_break", detail: "The TVL series changed meaning." },
    { field: "flow_log", code: "below_min_base", detail: "The previous-day base is below USD 10M, too small for a meaningful percentage change." },
    { field: "flow_z", code: "below_min_base", detail: "The previous-day base is below USD 10M, too small for a meaningful percentage change." },
    NOT_APPLICABLE,
  ],
});

const noTokens = () => base({
  tvl_log_return: -0.013837,
  unavailable: [
    { field: "flow_log", code: "no_token_data", detail: "DefiLlama publishes no per-asset token history for this protocol." },
    { field: "flow_z", code: "no_token_data", detail: "DefiLlama publishes no per-asset token history for this protocol." },
    NOT_APPLICABLE,
  ],
});

const view = (ctx, name = "Kamino") => describeFlowContext(parseFlowContext(ctx).data, name);

// --- parsing ----------------------------------------------------------------

test("the real contract shapes parse; absent and malformed are distinct", () => {
  for (const ctx of [lendingContext(), residual(), noTokens()]) assert.equal(parseFlowContext(ctx).state, "ok");
  assert.deepEqual(parseFlowContext(undefined), { state: "absent" });
  assert.deepEqual(parseFlowContext(null), { state: "absent" });
  assert.deepEqual(parseFlowContext("x"), { state: "malformed" });
  assert.deepEqual(parseFlowContext(lendingContext({ flow: { pct_change: "3%" } })), { state: "malformed" });
  assert.deepEqual(parseFlowContext(lendingContext({ contributions: { ...lendingContext().contributions, positive: [{ symbol: "USDG", share: null }] } })), { state: "malformed" });
  assert.deepEqual(parseFlowContext(base({ unavailable: [{ field: "flow_log" }] })), { state: "malformed" });
  // "available" must agree with the presence of a flow.
  assert.deepEqual(parseFlowContext(lendingContext({ available: false })), { state: "malformed" });
  assert.deepEqual(parseFlowContext(base({ available: true })), { state: "malformed" });
});

test("a malformed flow_context never hides the TVL behaviour classification", () => {
  const report = {
    protocol: { slug: "kamino", name: "Kamino", is_active: true },
    model: { name: "tvl-robust", version: "tvl-robust-v1", method: "robust z", min_history_days: 90, target_flag_rate: 0.01 },
    evaluated_at: "2026-11-14T10:00:00+00:00", classification: "NORMAL", robust_z: 0.4, threshold: 8, direction: null, drawdown_30d: null,
    excess_log_return: 0.01, daily_log_return: 0.016, market: { peer_median_return: 0, market_z: 0, market_wide: false },
    latest_closed_day: "2026-11-13", latest_age_days: 1,
    observation_window: { baseline_start: "2026-08-14", baseline_end: "2026-11-12", baseline_days: 90, baseline_points: 90, history_days_before_latest: 400, peers: 5 },
    source: { source: "defillama", adapter: "defillama_tvl_history@1", metric_key: "tvl.usd.daily", resolution: "closed UTC days" },
    supporting_observations: [], explanation: [], insufficient_reasons: [], disclaimer: "d",
  };
  const parsed = parseIntelligence({ ...report, flow_context: { broken: true } });
  assert.equal(parsed.classification, "NORMAL");
  assert.equal(parseFlowContext(parsed.flow_context).state, "malformed");
  assert.equal(parseFlowContext(parseIntelligence(report).flow_context).state, "absent");
});

test("flow_context is requested only when asked for, alongside history", async () => {
  const calls = [];
  const fetchImpl = async (url) => { calls.push(url); return { ok: true, status: 200, json: async () => ({}) }; };
  await loadIntelligence(fetchImpl, "http://api/x/intelligence");
  await loadIntelligence(fetchImpl, "http://api/x/intelligence", 180);
  await loadIntelligence(fetchImpl, "http://api/x/intelligence", 180, undefined, { flowContext: true });
  await loadIntelligence(fetchImpl, "http://api/x/intelligence", 0, undefined, { flowContext: true });
  assert.deepEqual(calls, [
    "http://api/x/intelligence",
    "http://api/x/intelligence?history_days=180",
    "http://api/x/intelligence?history_days=180&flow_context=true",
    "http://api/x/intelligence?flow_context=true",
  ]);
});

// --- formatting -------------------------------------------------------------

test("percentages, log changes and contribution points keep their API units", () => {
  assert.equal(formatPct(0.03015), "+3.02%"); // pct_change is already a simple ratio: no expm1
  assert.equal(formatPct(-0.001497), "−0.15%");
  assert.equal(formatPct(0), "0.00%");
  assert.equal(formatPct(0.000306, { signed: false }), "0.03%");
  assert.equal(formatPct(0.00000004), "+<0.01%"); // tiny non-zero is never shown as zero
  assert.equal(formatPct(null), null);
  assert.equal(formatLogAsPct(-0.0131), "−1.30%"); // log -> expm1, not -1.31%
  assert.equal(formatLogAsPct(0.016604), "+1.67%");
  assert.equal(formatLog(-0.0131), "−0.0131");
  assert.equal(formatLog(0.00001), "0.0000"); // rounds to zero: no sign
  assert.equal(formatPoints(0.029132), "+2.91 pts");
  assert.equal(formatPoints(-0.00023), "−0.02 pts");
});

// --- available flow -----------------------------------------------------------

test("an available flow shows the API percentage, z-score, log split and day", () => {
  const v = view(lendingContext());
  assert.equal(v.status, "available");
  assert.match(v.headline, /inflow of 3\.02% by quantity/);
  assert.deepEqual(v.flow, { pct: "+3.02%", log: "+0.0297", z: "+3.77", valuation: "previous-day DefiLlama USD prices" });
  assert.deepEqual(v.otherEffect, { pct: "−1.30%", log: "−0.0131" });
  assert.deepEqual(v.tvlChange, { pct: "+1.67%", log: "+0.0166" });
  assert.equal(v.identity, "+0.0166 = +0.0297 flow − 0.0131 other");
  assert.equal(v.measurementDay, "2026-11-13");
  assert.equal(v.previousDay, "2026-11-12");
  assert.deepEqual(v.notes, []);
  assert.equal(v.coverage.matched, "100.0%");
  assert.equal(v.coverage.added, "0.03%");
  assert.equal(v.coverage.removed, "0.00%");
  assert.equal(v.coverage.unpriced, "0 assets");
  assert.equal(v.coverage.belowMinimum, false);
});

test("a flow without a defined z-score says so instead of showing 0", () => {
  const v = view(lendingContext({ flow: { log_change: 0.01, pct_change: 0.01005, robust_z: null, valuation: "x" },
    unavailable: [{ field: "flow_z", code: "zero_dispersion", detail: "d" }] }));
  assert.equal(v.flow.z, null);
  assert.equal(v.notes[0].code, "zero_dispersion");
  assert.match(v.notes[0].text, /z-score is undefined/);
});

test("outflow headline", () => {
  const v = view(lendingContext({ flow: { log_change: -0.001498, pct_change: -0.001497, robust_z: -4.6404, valuation: "x" }, contributions: null, lending: null }), "Jito");
  assert.match(v.headline, /outflow of 0\.15%/);
  assert.equal(v.flow.z, "−4.64");
});

// --- contributions -----------------------------------------------------------

test("contributions are split, ordered by size and keep the API aggregation", () => {
  const c = view(lendingContext()).contributions;
  assert.deepEqual(c.positive.map((r) => r.symbol), ["USDG", "USDC", "PYUSD", "CBBTC", "REUSD"]);
  assert.deepEqual(c.negative.map((r) => r.symbol), ["USDE", "SOL", "SUSDAI", "ONYC", "SYRUPUSDC"]);
  assert.equal(c.positive[0].text, "+2.91 pts");
  assert.equal(c.positive[0].scale, 1);
  assert.ok(c.negative[0].scale > 0 && c.negative[0].scale < 1);
  assert.equal(c.listed, "+2.96 pts");
  assert.equal(c.other, "+0.06 pts");
  assert.equal(c.total, "+3.02 pts");
  assert.equal(c.truncated, true); // 10 listed of max 10: the list is not the whole portfolio
  assert.equal(c.listedCount, 10);
});

test("a short list is not marked truncated; unchanged assets and a zero remainder are kept", () => {
  const ctx = lendingContext();
  ctx.contributions = { ...ctx.contributions, positive: [{ symbol: "USDG", share: 0.03015 }], negative: [], unchanged: ["JITOSOL"], listed_total: 0.03015, other_assets: 0.0 };
  const c = view(ctx).contributions;
  assert.equal(c.truncated, false);
  assert.deepEqual(c.unchanged, ["JITOSOL"]);
  assert.equal(c.other, "0.00 pts");
  assert.deepEqual(c.negative, []);
});

// --- lending -------------------------------------------------------------------

test("lending metrics are shown when present, in their own units", () => {
  const l = view(lendingContext()).lending;
  assert.equal(l.borrowed, "$810.4M");
  assert.equal(l.borrowedChange, "+2.51%");
  assert.equal(l.utilisation, "40.70%");
  assert.equal(l.utilisationChange, "+0.20 pts");
  assert.equal(l.utilisationZ, "+0.92");
  assert.match(l.definition, /available liquidity/);
});

test("lending with missing values shows them as unavailable, never zero", () => {
  const ctx = lendingContext({ lending: { borrowed_usd: 5e8, borrowed_log_change: null, utilisation: null, utilisation_change: null, utilisation_change_z: null, definition: "d" },
    unavailable: [{ field: "utilisation", code: "previous_day_missing", detail: "d" }, { field: "utilisation_change_z", code: "insufficient_baseline", detail: "d" }] });
  const v = view(ctx);
  assert.equal(v.lending.utilisation, null);
  assert.equal(v.lending.utilisationChange, null);
  assert.deepEqual(v.lendingNotes.map((n) => n.code), ["previous_day_missing", "insufficient_baseline"]);
  assert.deepEqual(v.notes, []); // lending reasons are not mixed into the flow notes
});

test("protocols without borrowed history get no lending block, only a not-applicable note", () => {
  const v = view(noTokens(), "Orca");
  assert.equal(v.lending, null);
  assert.deepEqual(v.lendingNotes.map((n) => n.code), ["not_applicable"]);
  assert.match(v.lendingNotes[0].text, /Not applicable/);
  assert.doesNotMatch(v.lendingNotes[0].text, /0%/);
});

// --- unavailable states ---------------------------------------------------------

test("Drift: the residual regime is explained, not compared", () => {
  const v = view(residual(), "Drift");
  assert.equal(v.status, "residual");
  assert.match(v.headline, /residual regime/);
  assert.equal(v.flow, null);
  assert.equal(v.otherEffect, null);
  assert.equal(v.tvlChange, null);
  assert.equal(v.identity, null);
  assert.equal(v.contributions, null);
  assert.deepEqual(v.notes.map((n) => n.code), ["regime_break", "below_min_base"]);
  assert.match(v.notes[0].text, /Drift's stored TVL series changed meaning after a regime break/);
  assert.deepEqual(v.notes[1].fields, ["Estimated token flow", "Flow z-score"]); // grouped, not repeated
  assert.match(v.notes[1].text, /below USD 10M/); // threshold comes from the API detail
  assert.equal(v.coverage.matched, "100.0%"); // real coverage is still reported
});

test("Raydium/Orca: no token history at the source, TVL change still shown", () => {
  for (const name of ["Raydium", "Orca"]) {
    const v = view(noTokens(), name);
    assert.equal(v.status, "no_token_data");
    assert.match(v.headline, /No per-asset token history/);
    assert.equal(v.flow, null);
    assert.equal(v.coverage, null);
    assert.equal(v.contributions, null);
    assert.equal(v.tvlChange.pct, "−1.37%");
    assert.equal(v.notes.length, 1);
    assert.match(v.notes[0].text, new RegExp(`does not publish per-asset token history for ${name}`));
  }
});

test("other unavailable codes use clear text, falling back to the API detail", () => {
  const ctx = base({
    coverage: { matched_share: 0.82, removed_share: 0.18, added_share: 0.05, unpriced_assets: 2, minimum_matched_share: 0.9 },
    tvl_log_return: 0.02,
    unavailable: [{ field: "flow_log", code: "asset_set_changed", detail: "Assets present on both days cover less than 90% of the previous day's value." },
      { field: "flow_log", code: "previous_day_missing", detail: "x" }, { field: "flow_log", code: "mystery", detail: "Server text." }],
  });
  const v = view(ctx);
  assert.equal(v.status, "unavailable");
  assert.equal(v.coverage.belowMinimum, true);
  assert.equal(v.coverage.unpriced, "2 assets");
  assert.match(v.notes[0].text, /less than 90%/);
  assert.match(v.notes[1].text, /previous day has no stored token observation/);
  assert.equal(v.notes[2].text, "Server text.");
  assert.equal(groupReasons(ctx.unavailable, "X", { lending: true }).length, 0);
});

test("missing measurement day and absent contributions are handled", () => {
  const v = view(base({ measurement_day: null, previous_day: null, sol_log_return: null, unavailable: [{ field: "flow_log", code: "no_observation", detail: "d" }] }));
  assert.equal(v.measurementDay, null);
  assert.equal(v.solChange, null);
  assert.equal(v.status, "unavailable");
  assert.match(v.notes[0].text, /No token observation is stored/);
});
