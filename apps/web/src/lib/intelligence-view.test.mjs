// Run with: pnpm --filter @solanaguard/web test
// All reports below are hand-built test fixtures, not model output.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  MAX_FRESH_AGE_DAYS,
  buildExcessChart,
  chartDayTitle,
  dayAge,
  dayRanges,
  formatLogReturn,
  loadIntelligence,
  parseIntelligence,
  summarizeIntelligence,
} from "./intelligence-view.ts";
import { openProtocolSlugs, sortByDataAvailability } from "./protocol-availability.ts";
import { visibleProtocolCount } from "./plan-limits.ts";

const NOW = Date.parse("2026-11-12T10:00:00Z");

function report(overrides = {}) {
  return {
    protocol: { slug: "orca", name: "Orca", is_active: true },
    model: { name: "tvl-robust", version: "tvl-robust-v1", method: "robust z", min_history_days: 90, target_flag_rate: 0.01 },
    evaluated_at: "2026-11-12T10:00:00+00:00",
    classification: "NORMAL",
    robust_z: -0.08,
    threshold: 8,
    direction: "decline",
    drawdown_30d: -0.131,
    excess_log_return: -0.001,
    daily_log_return: -0.02,
    market: { peer_median_return: -0.019, market_z: -1.7, market_wide: false },
    latest_closed_day: "2026-11-11",
    latest_age_days: 1,
    observation_window: { baseline_start: "2026-08-13", baseline_end: "2026-11-10", baseline_days: 90, baseline_points: 90, history_days_before_latest: 2037, peers: 5 },
    source: { source: "defillama", adapter: "defillama_tvl_history@1", metric_key: "tvl.usd.daily", resolution: "closed UTC days" },
    supporting_observations: [{ id: "obs-1", role: "latest_day", day: "2026-11-11", value_usd: 1, source_observed_at: "2026-11-11T00:00:00Z", collected_at: "2026-11-12T09:50:57Z" }],
    explanation: ["line"],
    insufficient_reasons: [],
    disclaimer: "Unusualness relative to this protocol's own history and to its peers. It is not a probability of an exploit, compromise or loss.",
    history_window: null,
    history: [],
    ...overrides,
  };
}

function day(d, overrides = {}) {
  return { day: d, observation_id: `o-${d}`, excess_log_return: 0.01, daily_log_return: 0.01, peer_median_return: 0, robust_z: 0.5, unusual: false, market_wide: false, reasons: [], ...overrides };
}

// --- classification states -------------------------------------------------

test("NORMAL is shown as normal with its z-score when fresh", () => {
  const s = summarizeIntelligence(parseIntelligence(report()), NOW);
  assert.equal(s.classification, "NORMAL");
  assert.equal(s.zText, "−0.08");
  assert.equal(s.stale, false);
  assert.equal(s.ageDays, 1);
  assert.equal(s.drawdownText, "−13.1%");
});

test("UNUSUAL is preserved", () => {
  const s = summarizeIntelligence(parseIntelligence(report({ classification: "UNUSUAL", robust_z: -12.4 })), NOW);
  assert.equal(s.classification, "UNUSUAL");
  assert.equal(s.label, "Unusual");
  assert.equal(s.zText, "−12.40");
});

test("INSUFFICIENT_HISTORY carries the API's reasons and no score", () => {
  const r = report({ classification: "INSUFFICIENT_HISTORY", robust_z: null, latest_closed_day: null, latest_age_days: null, insufficient_reasons: [{ code: "no_observations", detail: "No closed daily TVL observations are stored for this protocol." }] });
  const s = summarizeIntelligence(parseIntelligence(r), NOW);
  assert.equal(s.classification, "INSUFFICIENT_HISTORY");
  assert.equal(s.zText, "—");
  assert.deepEqual(s.reasons.map((x) => x.code), ["no_observations"]);
  assert.equal(s.freshnessText, "No closed daily observation stored");
});

test("stale observations are never shown as NORMAL or UNUSUAL, even if the response said so", () => {
  for (const classification of ["NORMAL", "UNUSUAL"]) {
    const r = parseIntelligence(report({ classification, robust_z: classification === "UNUSUAL" ? 9 : 0.3, latest_closed_day: "2026-11-07" }));
    const s = summarizeIntelligence(r, NOW);
    assert.equal(dayAge("2026-11-07", NOW), 5);
    assert.equal(s.classification, "INSUFFICIENT_HISTORY");
    assert.equal(s.stale, true);
    assert.equal(s.zText, "—");
    assert.ok(s.reasons.some((x) => x.code === "stale_history"));
    assert.match(s.freshnessText, /stale/);
  }
  // exactly at the limit is still fresh, matching the model's MAX_LATEST_AGE_DAYS
  const edge = summarizeIntelligence(parseIntelligence(report({ latest_closed_day: "2026-11-09" })), NOW);
  assert.equal(edge.ageDays, MAX_FRESH_AGE_DAYS);
  assert.equal(edge.classification, "NORMAL");
});

test("the stale reason is not duplicated when the API already reported it", () => {
  const r = report({ classification: "INSUFFICIENT_HISTORY", robust_z: 0.4, latest_closed_day: "2026-11-01", insufficient_reasons: [{ code: "stale_history", detail: "x" }] });
  assert.equal(summarizeIntelligence(parseIntelligence(r), NOW).reasons.filter((x) => x.code === "stale_history").length, 1);
});

// --- validation and API errors ----------------------------------------------

test("malformed or inconsistent responses are rejected", () => {
  assert.equal(parseIntelligence(null), null);
  assert.equal(parseIntelligence([]), null);
  assert.equal(parseIntelligence(report({ classification: "LOW" })), null);
  assert.equal(parseIntelligence(report({ classification: "NORMAL", robust_z: null })), null);
  assert.equal(parseIntelligence(report({ threshold: "8" })), null);
  assert.equal(parseIntelligence(report({ disclaimer: "" })), null);
  assert.equal(parseIntelligence(report({ latest_closed_day: "yesterday" })), null);
  assert.equal(parseIntelligence(report({ history: [{ day: "2026-11-11" }] })), null);
  assert.ok(parseIntelligence(report()));
  assert.deepEqual(parseIntelligence({ ...report(), history: undefined, history_window: undefined }).history, []);
});

const omit = (obj, key) => Object.fromEntries(Object.entries(obj).filter(([k]) => k !== key));

test("malformed nested fields the UI formats are rejected instead of crashing the page", () => {
  const base = report();
  const withWindow = (w) => ({ ...base, observation_window: w });
  const noHistoryDays = omit(base.observation_window, "history_days_before_latest");
  assert.equal(parseIntelligence(withWindow(noHistoryDays)), null);
  assert.equal(parseIntelligence(withWindow({ ...base.observation_window, history_days_before_latest: "2037" })), null);
  assert.equal(parseIntelligence(withWindow({ ...base.observation_window, peers: null })), null);
  assert.equal(parseIntelligence(withWindow({ ...base.observation_window, baseline_start: "Aug 13" })), null);

  const support = base.supporting_observations[0];
  const withSupport = (o) => ({ ...base, supporting_observations: [o] });
  const noId = omit(support, "id");
  const noCollected = omit(support, "collected_at");
  assert.equal(parseIntelligence(withSupport(noId)), null);
  assert.equal(parseIntelligence(withSupport({ ...support, id: "" })), null);
  assert.equal(parseIntelligence(withSupport(noCollected)), null);
  assert.equal(parseIntelligence(withSupport({ ...support, collected_at: 1731405057 })), null);
  assert.equal(parseIntelligence(withSupport({ ...support, value_usd: "1.16e9" })), null);
  assert.equal(parseIntelligence({ ...base, supporting_observations: [null] }), null);
});

test("valid nested fields, including null baseline dates and no supporting observations, are still accepted", () => {
  const insufficient = report({
    classification: "INSUFFICIENT_HISTORY", robust_z: null, latest_closed_day: null, latest_age_days: null, supporting_observations: [],
    observation_window: { baseline_start: null, baseline_end: null, baseline_days: 90, baseline_points: 0, history_days_before_latest: 0, peers: 0 },
    insufficient_reasons: [{ code: "no_observations", detail: "x" }],
  });
  assert.ok(parseIntelligence(insufficient));
  assert.ok(parseIntelligence(report()));
});

test("malformed nested responses from the API map to the existing malformed result", async () => {
  const noHistoryDays = omit(report().observation_window, "history_days_before_latest");
  assert.deepEqual(await loadIntelligence(fakeFetch(res(200, report({ observation_window: noHistoryDays }))), "u"), { ok: false, reason: "malformed" });
});

function fakeFetch(response, calls = []) {
  return async (url, init) => {
    calls.push({ url, init });
    if (response instanceof Error) throw response;
    return response;
  };
}
const res = (status, body, badJson = false) => ({ ok: status >= 200 && status < 300, status, json: async () => { if (badJson) throw new SyntaxError("bad"); return body; } });

test("API failures map to reasons, never to a classification", async () => {
  assert.deepEqual(await loadIntelligence(fakeFetch(res(404, {})), "u"), { ok: false, reason: "not_found" });
  assert.deepEqual(await loadIntelligence(fakeFetch(res(503, {})), "u"), { ok: false, reason: "not_ready" });
  assert.deepEqual(await loadIntelligence(fakeFetch(res(500, {})), "u"), { ok: false, reason: "unavailable" });
  assert.deepEqual(await loadIntelligence(fakeFetch(new TypeError("fetch failed")), "u"), { ok: false, reason: "unavailable" });
  assert.deepEqual(await loadIntelligence(fakeFetch(new DOMException("timed out", "TimeoutError")), "u"), { ok: false, reason: "unavailable" });
  assert.deepEqual(await loadIntelligence(fakeFetch(res(200, null, true)), "u"), { ok: false, reason: "malformed" });
  assert.deepEqual(await loadIntelligence(fakeFetch(res(200, { classification: "NORMAL" })), "u"), { ok: false, reason: "malformed" });
  const ok = await loadIntelligence(fakeFetch(res(200, report())), "u");
  assert.equal(ok.ok, true);
});

test("a slow API is abandoned after the timeout", async () => {
  const hanging = (_url, init) => new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(init.signal.reason)));
  assert.deepEqual(await loadIntelligence(hanging, "u", 0, 20), { ok: false, reason: "unavailable" });
});

test("history is requested only when asked for, and capped at 180 days", async () => {
  const calls = [];
  await loadIntelligence(fakeFetch(res(200, report()), calls), "http://api/x/intelligence");
  await loadIntelligence(fakeFetch(res(200, report()), calls), "http://api/x/intelligence", 180);
  await loadIntelligence(fakeFetch(res(200, report()), calls), "http://api/x/intelligence", 999);
  assert.deepEqual(calls.map((c) => c.url), ["http://api/x/intelligence", "http://api/x/intelligence?history_days=180", "http://api/x/intelligence?history_days=180"]);
  assert.equal(calls[0].init.cache, "no-store");
});

// --- chart transformation ---------------------------------------------------

test("every calendar day appears once; missing dates are gaps with null values, not zero returns", () => {
  const history = [day("2026-11-01", { excess_log_return: 0 }), day("2026-11-02"), day("2026-11-05", { excess_log_return: -0.02 })];
  const chart = buildExcessChart(history, { start: "2026-11-01", end: "2026-11-06" });
  assert.deepEqual(chart.days.map((d) => d.day), ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]);
  assert.deepEqual(chart.missingDays, ["2026-11-03", "2026-11-04", "2026-11-06"]);
  for (const d of chart.days.filter((x) => x.kind === "missing")) {
    assert.equal(d.value, null);
    assert.equal(d.observationId, null);
  }
  assert.equal(chart.days[0].kind, "scored");
  assert.equal(chart.days[0].value, 0); // a real zero stays a zero
  assert.match(chartDayTitle(chart.days[2], 8), /no observation stored \(not a zero return\)/);
  assert.equal(chart.scoredCount, 3);
});

test("stored but unscored days keep their reasons and are not marked unusual", () => {
  const chart = buildExcessChart([day("2026-11-02", { excess_log_return: null, robust_z: null, reasons: ["previous_day_missing"], unusual: true })], { start: "2026-11-02", end: "2026-11-02" });
  assert.equal(chart.days[0].kind, "unscored");
  assert.equal(chart.days[0].unusual, false);
  assert.match(chartDayTitle(chart.days[0], 8), /previous_day_missing/);
});

test("unusual days are marked exactly as the API flagged them", () => {
  const history = [day("2026-11-01"), day("2026-11-02", { excess_log_return: -0.4, robust_z: -15, unusual: true }), day("2026-11-03", { robust_z: 7.9 })];
  const chart = buildExcessChart(history, { start: "2026-11-01", end: "2026-11-03" });
  assert.deepEqual(chart.unusualDays.map((d) => d.day), ["2026-11-02"]);
  assert.match(chartDayTitle(chart.unusualDays[0], 8), /UNUSUAL \(\|z\| ≥ 8\)/);
});

test("an extreme day is clipped and flagged instead of flattening the chart; the real value stays in the tooltip", () => {
  const history = Array.from({ length: 60 }, (_, i) => day(`2026-09-${String((i % 30) + 1).padStart(2, "0")}`, { excess_log_return: (i % 7 - 3) / 1000 }));
  const unique = new Map(history.map((h) => [h.day, h]));
  unique.set("2026-09-15", day("2026-09-15", { excess_log_return: -2.1, robust_z: -150, unusual: true }));
  const chart = buildExcessChart([...unique.values()].sort((a, b) => a.day.localeCompare(b.day)), { start: "2026-09-01", end: "2026-09-30" });
  const crash = chart.days.find((d) => d.day === "2026-09-15");
  assert.ok(chart.limit < 0.1, `limit ${chart.limit}`);
  assert.equal(crash.clipped, true);
  assert.equal(crash.value, -2.1);
  assert.equal(chart.clippedCount, 1);
  assert.match(chartDayTitle(crash, 8), /−87\.8% vs peer median.*clipped/);
});

test("no clipping when nothing is extreme", () => {
  const chart = buildExcessChart([day("2026-11-01", { excess_log_return: 0.03 }), day("2026-11-02", { excess_log_return: -0.01 })], { start: "2026-11-01", end: "2026-11-02" });
  assert.equal(chart.clippedCount, 0);
  assert.equal(chart.limit, 0.03);
});

test("missing-day ranges are collapsed for listing", () => {
  assert.deepEqual(dayRanges(["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-10"]), [
    { start: "2026-06-01", end: "2026-06-03", count: 3 },
    { start: "2026-06-10", end: "2026-06-10", count: 1 },
  ]);
});

test("log returns are shown as simple percentage changes", () => {
  assert.equal(formatLogReturn(Math.log(0.9)), "−10.0%");
  assert.equal(formatLogReturn(0), "0.0%");
  assert.equal(formatLogReturn(null), "—");
});

// --- Free/Pro gating is unchanged --------------------------------------------

test("Free plan still opens only the first five data-ordered protocols; intelligence follows the same open set", () => {
  const protocols = ["a", "b", "c", "d", "e", "f", "g"].map((slug, i) => ({ slug, tvlUsd: i === 6 ? null : 1, overallScore: i === 6 ? null : 1, txCount24h: 1 }));
  const sorted = sortByDataAvailability(protocols);
  const open = openProtocolSlugs(sorted, visibleProtocolCount("FREE", sorted.length));
  assert.equal(open.size, 5);
  assert.equal(openProtocolSlugs(sorted, visibleProtocolCount("PRO", sorted.length)).size, 7);
});
