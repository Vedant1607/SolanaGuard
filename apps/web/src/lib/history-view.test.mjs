// Run with: pnpm --filter @solanaguard/web test. Statuses are hand-built fixtures.
import { test } from "node:test";
import assert from "node:assert/strict";

import { describeHistory } from "./history-view.ts";

const NOW = Date.parse("2026-11-14T10:00:00Z");

function status({ refresh = {}, availability } = {}) {
  return {
    evaluated_at: "2026-11-14T10:00:00Z",
    refresh: { enabled: true, state: "waiting", running: false, interval_seconds: 21600, last_attempt_started_at: null,
      last_attempt_finished_at: null, last_outcome: null, last_success_at: null, last_failure: null, next_run_at: null, ...refresh },
    availability: availability ?? { ok: true, available_through: "2026-11-13", age_days: 1,
      metrics: { "tvl.usd.daily": { latest_day: "2026-11-13", lagging_protocols: [], protocols: { raydium: "2026-11-13" } } } },
  };
}

test("availability date and refresh run are separate facts", () => {
  const v = describeHistory(status({ refresh: { last_outcome: "succeeded", last_attempt_finished_at: "2026-11-14T09:00:00+00:00", last_success_at: "2026-11-14T09:00:00+00:00", next_run_at: "2026-11-14T15:00:00+00:00" } }), NOW);
  assert.equal(v.availableThrough, "2026-11-13");
  assert.match(v.availability.text, /available through 2026-11-13/);
  assert.match(v.refresh.text, /succeeded 1 h ago\. Next run in ~5 h/);
  assert.equal(v.refresh.tone, "ok");
});

test("after an API restart nothing is claimed as successful", () => {
  const v = describeHistory(status(), NOW);
  assert.match(v.refresh.text, /No refresh has completed since the API started/);
  assert.notEqual(v.refresh.tone, "ok");
});

test("failed, partial, identity-blocked and skipped runs are never shown as success", () => {
  const at = "2026-11-14T08:00:00Z";
  for (const [outcome, pattern] of [["failed", /failed/], ["skipped_identity", /failed/], ["partial", /partly failed/], ["skipped_locked", /skipped/]]) {
    const v = describeHistory(status({ refresh: { last_outcome: outcome, last_attempt_finished_at: at, last_failure: "tvl_history: failed (RuntimeError)" } }), NOW);
    assert.match(v.refresh.text, pattern, outcome);
    assert.doesNotMatch(v.refresh.text, /succeeded/, outcome);
  }
});

test("running and disabled states", () => {
  assert.match(describeHistory(status({ refresh: { running: true, last_attempt_started_at: "2026-11-14T09:58:00Z" } }), NOW).refresh.text, /in progress \(started 2 min ago\)/);
  assert.match(describeHistory(status({ refresh: { enabled: false } }), NOW).refresh.text, /off on this server/);
});

test("stale history is flagged with the model's 3-day limit", () => {
  const v = describeHistory(status({ availability: { ok: true, available_through: "2026-11-10", age_days: 4, metrics: {} } }), NOW);
  assert.equal(v.availability.tone, "warning");
  assert.match(v.availability.text, /4 days old/);
  const edge = describeHistory(status({ availability: { ok: true, available_through: "2026-11-11", age_days: 3, metrics: {} } }), NOW);
  assert.equal(edge.availability.tone, "ok");
});

test("lagging protocols, missing data and an unreachable API", () => {
  const v = describeHistory(status({ availability: { ok: true, available_through: "2026-11-13", age_days: 1,
    metrics: { "tvl.usd.daily": { latest_day: "2026-11-13", lagging_protocols: ["drift"], protocols: { drift: "2026-11-12", raydium: "2026-11-13" } } } } }), NOW);
  assert.deepEqual(v.lagging, ["drift (2026-11-12)"]);
  assert.equal(describeHistory(status({ availability: { ok: true, available_through: null, age_days: null, metrics: {} } }), NOW).availability.tone, "warning");
  assert.match(describeHistory(status({ availability: { ok: false, reason: "unavailable" } }), NOW).availability.text, /could not be read/);
  const none = describeHistory(null, NOW);
  assert.equal(none.availableThrough, null);
  assert.match(none.availability.text, /could not be loaded/);
});
