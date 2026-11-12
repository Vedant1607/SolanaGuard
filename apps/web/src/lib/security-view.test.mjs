// Run with: pnpm --filter @solanaguard/web test
import { test } from "node:test";
import assert from "node:assert/strict";

import { RULE_CONTEXT, explorerUrl, ruleTitle, shortKey, sortEvidence, summarizeSecurity } from "./security-view.ts";
import { openProtocolSlugs } from "./protocol-availability.ts";
import { visibleProtocolCount } from "./plan-limits.ts";

function obs(id, overrides = {}) {
  return {
    id, metric_key: "security.program_upgradeable", status: "OK", value: "true", unit: "bool",
    source: "solana-rpc", source_slot: 100, source_observed_at: null,
    collected_at: "2026-11-12T08:51:16", adapter: "onchain_security@1", error_code: null, ...overrides,
  };
}

function evidence(rule_id, level, observations, overrides = {}) {
  return { rule_id, indicator: rule_id, level, explanation: `${rule_id} explanation`, address_id: "a1", address_label: "Program", observations, rule_version: "security-rules/1", ...overrides };
}

function assessment(evidenceList, dimension = {}) {
  return {
    protocol: { slug: "x", name: "X", category: "DEX", is_active: true, support_level: "PARTIAL" },
    evaluated_at: "2026-11-12T09:00:00+00:00",
    dimension: { dimension: "security", level: "UNKNOWN", explanation: "", assessed_indicators: 3, total_indicators: 4, rule_version: "security-rules/1", ...dimension },
    evidence: evidenceList,
    addresses: [],
  };
}

test("summary reports the newest cited observation, sources, adapters and unknowns", () => {
  const a = assessment([
    evidence("SEC-01", "MEDIUM", [obs("o1"), obs("o2", { collected_at: "2026-11-12T08:36:16" })]),
    evidence("SEC-02", "UNKNOWN", [obs("o1")]),
    evidence("REG-01", "LOW", [obs("o3", { collected_at: "2026-11-12T08:51:17", metric_key: "registry.owner" })]),
  ]);
  const s = summarizeSecurity(a);
  assert.equal(s.latestObservationAt, Date.parse("2026-11-12T08:51:17Z")); // naive API time read as UTC
  assert.deepEqual(s.sources, ["solana-rpc"]);
  assert.deepEqual(s.adapters, ["onchain_security@1"]);
  assert.deepEqual(s.unknown.map((e) => e.rule_id), ["SEC-02"]);
  assert.equal(s.coverageText, "3 of 4 indicators assessed");
  assert.equal(s.hasEvidence, true);
});

test("failed and missing readings are surfaced, not hidden", () => {
  const a = assessment([
    evidence("SEC-01", "UNKNOWN", [obs("e1", { status: "ERROR", value: null, error_code: "HTTP_429" })]),
    evidence("REG-01", "UNKNOWN", [obs("m1", { status: "MISSING", value: null, error_code: "ACCOUNT_NOT_FOUND" })]),
  ]);
  const s = summarizeSecurity(a);
  assert.deepEqual(s.failedObservations.map((o) => o.id).sort(), ["e1", "m1"]);
});

test("no evidence at all is reported as not evaluated, with no observation time", () => {
  const s = summarizeSecurity(assessment([], { assessed_indicators: 0, total_indicators: 0 }));
  assert.equal(s.coverageText, "No indicators evaluated");
  assert.equal(s.latestObservationAt, null);
  assert.equal(s.hasEvidence, false);
});

test("evidence is ordered most severe first, UNKNOWN above LOW and INFO, stable within a level", () => {
  const list = [
    evidence("REG-01", "LOW", []), evidence("SEC-01", "INFO", []), evidence("SEC-02", "UNKNOWN", []),
    evidence("SEC-03", "HIGH", []), evidence("SEC-01", "MEDIUM", [], { address_id: "b" }), evidence("REG-01", "LOW", [], { address_id: "c" }),
  ];
  const sorted = sortEvidence(list);
  assert.deepEqual(sorted.map((e) => e.level), ["HIGH", "MEDIUM", "UNKNOWN", "LOW", "LOW", "INFO"]);
  assert.equal(sorted[3].address_id, "a1"); // original order kept between the two LOWs
  assert.equal(list[0].level, "LOW"); // input untouched
});

test("every rule emitted by security-rules/1 has plain-language context", () => {
  for (const id of ["REG-01", "SEC-01", "SEC-02", "SEC-03"]) {
    assert.ok(RULE_CONTEXT[id].why.length > 20, id);
    assert.notEqual(ruleTitle(id), id);
  }
  assert.equal(ruleTitle("SEC-99"), "SEC-99"); // unknown rules are shown by id, not hidden
  assert.match(RULE_CONTEXT["SEC-03"].why, /not evidence of compromise/);
});

test("explorer links and short keys", () => {
  assert.equal(explorerUrl("KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"), "https://explorer.solana.com/address/KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD");
  assert.equal(shortKey("KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"), "KLen…gmjD");
  assert.equal(shortKey(null), "—");
});

test("detail-page access matches the dashboard grid: a Free plan cannot open the sixth protocol", () => {
  const p = (slug, data = true) => ({ slug, name: slug, category: "DEX", tvlUsd: data ? 1 : null, txCount24h: null, snapshotAt: null, overallScore: data ? 15 : null, riskLevel: data ? "LOW" : null, explanation: null, scoredAt: null });
  const list = [p("drift", false), p("jito"), p("kamino"), p("marinade"), p("orca"), p("raydium")];
  const free = openProtocolSlugs(list, visibleProtocolCount("FREE", list.length));
  assert.equal(free.size, 5);
  assert.ok(!free.has("drift"));
  const pro = openProtocolSlugs(list, visibleProtocolCount("PRO", list.length));
  assert.equal(pro.size, 6);
  assert.ok(pro.has("drift"));
  assert.equal(openProtocolSlugs(list, -1).size, 0);
});
