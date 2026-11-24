# SolanaGuard — Teammate Handoff Plan

**Prepared:** 25 November 2026  
**Repository:** [Vedant1607/SolanaGuard](https://github.com/Vedant1607/SolanaGuard)  
**Documentation baseline:** `c5667e9eefdf39904d811548d5e318dc0cf1b574`  
**Purpose:** Finish and verify the existing product so it is reliable, demonstrable, and safe to operate.

> **Scope boundary:** This plan deliberately excludes selecting a backend host or deploying the FastAPI service. Do not make hosting-provider changes as part of this handoff. Work on application code, data readiness, integrations, tests, and documentation only.

## 1. Current project state

### Implemented capabilities

- Next.js dashboard and protocol detail pages.
- FastAPI ingestion API and rule-based activity scoring.
- Clerk authentication and per-user watchlists.
- Email alert delivery through Resend.
- Telegram deep-link account linking, webhook handling, and alert delivery.
- On-chain security assessment based on a protocol-address registry and collected observations.
- Historical TVL intelligence (`tvl-robust-v1`).
- Time-window anomaly detection (`iforest-v2-time-window`).
- Separate category ML risk engine (`ml-risk-category-iforest-v1`).
- Razorpay subscription checkout and signed, idempotent webhook processing.
- Neon PostgreSQL schema, migration history, seed data, and Prisma client.

### Production/database facts from the latest audit

- The production Neon database has the full 11-migration chain applied.
- The active protocol catalogue contains six protocols: Raydium, Orca, Kamino, Marinade, Jito, and Drift.
- Marginfi remains in the database but is inactive.
- The protocol address registry contains 14 verified addresses.
- At the time of the database audit, historical `metric_observations` and `protocol_snapshots` data had not yet been initialized. Re-check before acting in case someone has since populated them.
- At the time of the audit, there were no production user or subscription records, so billing backfill had no eligible records.
- A pre-migration Neon backup branch was retained. Do not delete or overwrite it.

These are point-in-time findings, not a guarantee of the current live state. Re-query before any production data operation.

### Repository test inventory

- 25 API test modules under `apps/api/tests`.
- 10 web test modules under `apps/web/src/lib`.
- No GitHub Actions workflow was present in the reviewed repository tree.
- The test files exist, but the full suite/build status at the current commit still needs to be established.

## 2. Priority order

| Priority | Workstream | Outcome |
|---|---|---|
| P0 | Production data readiness | Fresh, sufficient, observable data without misleading defaults |
| P0 | End-to-end integration verification | Auth, watchlists, email, Telegram and billing verified in test/safe modes |
| P1 | CI and regression baseline | Reproducible quality checks on every pull request |
| P1 | Billing lifecycle | Users can understand and manage/cancel subscriptions safely |
| P1 | ML data coverage and validation | Model inputs and outputs are measurable and honestly represented |
| P2 | Operational hardening | Reliable alerting, job visibility, failure handling and privacy |
| P2 | Documentation and release QA | Maintainer-ready setup, configuration and acceptance evidence |

Do not begin optional new product features until P0 and P1 are complete or explicitly descoped.

---

## 3. P0 — Production data readiness

### 3.1 Re-audit the Neon database first

**Relevant areas:** `packages/database/prisma/schema.prisma`, migration history, `apps/api/app/routers/history.py`, `apps/api/app/routers/security.py`.

Before writing anything, record current row counts and latest timestamps for:

- `protocol_snapshots`
- `risk_scores` (separate `rule_based` and `ml` methods)
- `anomalies` by detector version
- `metric_observations` by adapter, protocol, metric, and status
- `protocol_addresses` by active/verification state
- Users, subscriptions, Telegram link tokens, and billing webhook events

Confirm the 11 migration records are complete and there are no failed/incomplete migration entries. Compare the live protocol catalogue with the migration/seed expectations.

**Acceptance criteria**
- A dated audit summary records counts, latest data timestamps, protocol coverage, and any missing/failed data.
- No unexpected schema drift or incomplete migrations.
- No credentials, full connection strings, webhook secrets, or personal data are copied into the handoff report.

### 3.2 Initialize historical DefiLlama observations

**Code**
- `apps/api/scripts/backfill_defillama_tvl.py`
- `apps/api/scripts/backfill_defillama_tokens.py`
- `apps/api/scripts/refresh_defillama_history.py`
- `apps/api/app/tasks/defillama_backfill.py`
- `apps/api/app/tasks/defillama_token_backfill.py`

Run only after verifying the target database and checking the current row counts. These jobs are designed to be idempotent and insert missing observations rather than overwrite source history.

Tasks:
1. Run the TVL history backfill for the intended active protocols.
2. Run token history for the protocols supported by the token adapter; do not assume every protocol has every metric.
3. Inspect each script's JSON report, including failed and unverified protocols and source conflicts.
4. Verify the latest closed UTC day stored for each protocol/metric.
5. Establish a recurring invocation for the refresh script in the eventual operations environment. The API's local development scheduler is disabled by default and is not a substitute for a durable hosted schedule.

**Acceptance criteria**
- Each supported active protocol has an explained TVL history coverage result.
- The historical model has at least 90 usable days before it is expected to classify a protocol; otherwise it returns insufficient history.
- Token metrics are populated only where the adapter supports them.
- Refresh runs are repeatable, target-checked, observable, and do not overwrite conflicting historical observations.

### 3.3 Verify live protocol ingestion and rule-based scoring

**Code:** `apps/api/app/tasks/ingest.py`, `apps/api/app/scoring/rule_based.py`, `apps/api/app/routers/protocols.py`.

Tasks:
- Confirm Helius/Solana RPC connectivity and DefiLlama source availability.
- Verify snapshots arrive for all active protocols and have plausible timestamps/values.
- Verify `txCount24h` remains NULL when unavailable; never coerce missing transaction counts to zero.
- Verify rule-based risk scores are written only when the required snapshot baseline exists.
- Verify only rule-based scores drive the primary dashboard score and HIGH/CRITICAL escalation alerts.
- Check that multiple instances do not duplicate ingestion runs; PostgreSQL advisory-lock coordination must remain intact.

**Acceptance criteria**
- Each active protocol has current snapshots and a documented data freshness result.
- A score is not fabricated when a baseline or required data is missing.
- Dashboard freshness indicators match the underlying timestamps.

### 3.4 Verify on-chain security collection

**Code:** `apps/api/app/tasks/security_collector.py`, `apps/api/app/adapters/onchain_security.py`, `apps/api/app/routers/security.py`.

Tasks:
- Confirm a reliable Solana RPC endpoint is configured for non-debug operation.
- Run/observe at least one collector cycle.
- Review `GET /api/v1/security/collector-status` for each active protocol's latest batch, freshness, OK/MISSING/STALE/ERROR counts, and error codes.
- Inspect evidence for representative program, market, mint, and stake-pool addresses.
- Confirm that unsupported or missing indicators remain UNKNOWN and that program upgradeability is not presented as proof of compromise.

**Acceptance criteria**
- Collector status distinguishes disabled, missing RPC, starting, active, and failing states.
- Address/observation provenance is visible and traceable.
- No protocol receives a LOW security verdict merely because evidence is absent.

---

## 4. P0 — End-to-end integration verification

Use test accounts and provider test/sandbox modes wherever available. Do not send real charges or production alerts to real users during testing.

### 4.1 Clerk and account synchronization

**Code:** `apps/web/src/lib/sync-user.ts`, `apps/web/src/lib/get-app-user.ts`, `apps/web/src/proxy.ts`.

Test:
- New user sign-up and sign-in.
- The authenticated Clerk identity is mapped to exactly one local user row.
- User-specific data is not accessible across accounts.
- Sign-out, expired sessions, and anonymous dashboard access behave correctly.

**Acceptance:** documented happy-path and failure-path results; no client-supplied user ID is trusted as the account identity.

### 4.2 Watchlists

**Code:** `apps/web/src/lib/watchlist-actions.ts`, `apps/web/src/components/WatchlistButton.tsx`.

Test add/remove, refresh persistence, unknown protocol rejection, and user isolation. Inspect the optimistic button update: it currently flips local state before the server action finishes and should restore the previous state if the action fails.

**Acceptance:** failed requests do not leave the UI showing a state that was not saved.

### 4.3 Email alerts

**Code:** `apps/api/app/alerts/email.py`, `apps/api/app/tasks/ingest.py`.

Verify:
- Resend credentials and sender/domain configuration.
- Correct recipient resolution for a user's watched protocols.
- Only rule-based transitions into HIGH/CRITICAL cause escalation alerts.
- Delivery failure is visible to operators and does not falsely report success.
- Email content escapes or safely formats untrusted protocol/explanation text before embedding it in HTML.

**Acceptance:** a controlled test escalation reaches the intended test mailbox; no duplicate alert is generated for the same score transition.

### 4.4 Telegram linking and alerts

**Code:** `apps/web/src/lib/telegram-link-core.ts`, `apps/web/src/lib/telegram-link-store.ts`, `apps/api/app/telegram_linking.py`, `apps/api/app/routers/telegram.py`, `apps/api/app/alerts/telegram.py`.

Verify:
- Bot username and token configuration.
- Telegram webhook is configured with the same secret token as the API.
- Linking token is single-use, short-lived, revocable, and never logged in plaintext.
- Only private chats are linked.
- A Telegram chat cannot be attached to two different users.
- A test HIGH/CRITICAL escalation reaches only users watching that protocol.
- API errors and Telegram delivery failures are observable without logging bot tokens or private token URLs.

**Acceptance:** a fresh test account can link, receive a controlled alert, and reject expired/reused/conflicting tokens.

### 4.5 Razorpay billing

**Code:** `apps/web/src/lib/billing-core.ts`, `apps/web/src/lib/billing-store.ts`, `apps/web/src/lib/billing-actions.ts`, `apps/web/src/app/api/webhooks/razorpay/route.ts`.

Test in Razorpay test mode:
- Checkout initiation and repeated-click protection.
- Valid, invalid, duplicated, delayed, and out-of-order signed webhook events.
- Subscription status and entitlement updates, including initial unpaid, active, pending/grace, halted, paused, cancelled, completed, and expired states.
- Checkout timeout/unknown outcome handling and reconciliation.
- A browser success redirect alone must never grant PRO; the verified webhook is authoritative.
- Database event idempotency and reconciliation-required cases.

**Acceptance:** repeatable tests prove no duplicate subscription creation, no forged/duplicate webhook grants access, and entitlement matches confirmed payment state.

---

## 5. P1 — Finish the customer billing lifecycle

The repository contains checkout and webhook handling, but the reviewed web application did not expose a complete customer-facing subscription management/cancellation flow.

Tasks:
1. Decide the cancellation policy: immediate or end-of-cycle, and explain the policy in the UI.
2. Implement an authenticated server-side cancellation action bound to the signed-in user's local subscription.
3. Call Razorpay using the stored subscription identifier; never accept an arbitrary provider subscription ID from the browser.
4. Handle provider timeout/unknown outcomes safely; do not claim cancellation until reconciled.
5. Display current plan, payment/renewal status, paid-through date, and cancellation state.
6. Revalidate entitlements from confirmed provider events and local state.
7. Add tests for races between cancellation and incoming webhooks.

**Acceptance**
- Users can see what plan they have and whether access is paid through a date.
- Users can initiate the documented cancellation policy securely.
- Cancellation and webhook races cannot revive a terminal subscription or grant extra access.
- Free plan limits (five visible protocols) and Pro access (all active protocols) match the UI and server-side behaviour.

---

## 6. P1 — CI and regression baseline

There was no GitHub Actions workflow in the reviewed tree.

Add CI for pull requests and pushes to the maintained branches. Suggested jobs:

**API**
- Set up Python 3.14 and uv.
- Install from `uv.lock` in frozen mode.
- Run `uv run pytest`.
- If database integration tests are enabled, provision a disposable test database and apply migrations there only.

**Web/monorepo**
- Set up the Node version used by the project and pnpm version declared by the repository.
- Install with a frozen lockfile.
- Run web unit tests, ESLint, TypeScript checks, and production build.

Commands currently exposed by the repository:

```bash
cd apps/api && uv sync --frozen && uv run pytest
cd ../.. 
pnpm install --frozen-lockfile
pnpm --filter @solanaguard/web test
pnpm lint
pnpm check-types
pnpm build
```

Confirm command behaviour locally before codifying it in CI; adjust workspace filters only when verified.

**Acceptance**
- CI runs automatically on PRs.
- A failing test, lint, type check, or build blocks merge.
- No production database or real provider secret is required for ordinary CI.
- Test reports are retained as workflow artifacts when useful.

---

## 7. P1 — ML feature coverage and model validation

### 7.1 Separate the two anomaly systems

Keep these systems independent:
- `iforest-v2-time-window`: calibrated anomaly results used by the anomaly feed.
- `ml-risk-category-iforest-v1`: category-based risk score/anomaly records kept separate from the calibrated feed.

Do not merge their score scales or feed eligibility without a separate design decision.

### 7.2 Improve measurement coverage

**Code:** `apps/api/app/ml/`, `apps/api/app/tasks/ml_risk.py`, adapter and ingestion modules.

The category ML engine currently has known data limitations: volume, unique-wallet, liquidity-depth and related fields are not all collected as real measurements. Some components use placeholder/default inputs, and missing transaction counts cause scoring to be skipped.

Tasks:
- Build an explicit feature-availability matrix by protocol and feature.
- Collect additional inputs only where a trustworthy source and adapter can be implemented.
- Ensure unmeasured values are not presented as genuine zero values.
- Keep the current abstention behaviour when required inputs/history are missing.
- Reassess the feature windows and the assumption that snapshots are evenly spaced.
- Verify bundled `.joblib` artifacts load using the pinned scikit-learn version declared in `pyproject.toml`.

### 7.3 Evaluate honestly

- Build a reproducible offline evaluation set from recorded historical observations.
- Inspect score distributions, anomaly frequency, stability, and false-positive examples with manual review.
- Add labelled exploit examples only when source, timestamp, protocol, and label quality can be documented.
- Do not claim precision/recall or exploit prediction without a fit-for-purpose labelled dataset and evaluation methodology.
- Record model version, feature version, training window, thresholds, and known limitations.

**Acceptance**
- A model report describes the data used and the failure/abstention cases.
- Unsupported features are explicitly marked unavailable rather than silently filled with zero.
- ML output remains separate from the primary rule-based activity score.

---

## 8. P2 — Operational hardening

Tasks to evaluate after the core integrations pass:

- **Persistent job health:** in-memory job heartbeats reset after process restart. Decide whether durable run history/heartbeats are needed for operators.
- **Alert reliability:** consider a persistent outbox/retry strategy for provider timeouts and transient notification failures, with deduplication keys.
- **Observability:** centralize structured logs and capture sanitized exception context, job durations, failure counts, and provider response categories.
- **Credential hygiene:** check that Helius URLs, Telegram bot URLs, database URLs, webhook signatures, and tokens are redacted from logs and error pages.
- **API protection:** review rate limits and abuse controls for public read endpoints and internal write endpoints. Internal endpoints must continue to fail closed when their secret is absent.
- **Data correctness:** monitor stale snapshots, historical source lag, missing observation ratios, and RPC fallback usage.
- **Backups/recovery:** document Neon backup/PITR expectations and a tested recovery procedure before changes to production data.
- **Dependency hygiene:** review outdated dependencies and update only with lockfile, tests, and compatibility checks.

**Acceptance:** a short runbook explains how to identify a stale collector, failed refresh, failed alert delivery, billing reconciliation case, and database connectivity issue.

---

## 9. P2 — Documentation cleanup

The root README has been refreshed, but the repo's configuration samples and some component-level documentation need alignment.

Tasks:
- Replace the legacy root `.env.example` entries (it still mentions Stripe and older API variable names) with the real current Clerk, Helius, Resend, Telegram, Razorpay, `API_URL`, and database variable names.
- Add comments for required vs optional variables and which app consumes each one.
- Add a small `apps/api/README.md` covering API setup, configuration, endpoints, scheduled scripts, and tests.
- Add an architecture/data-flow diagram: sources → adapters → observations/snapshots → scoring/assessment → API → web/alerts.
- Document how to run safe local/test-mode provider integrations.
- Ensure docs do not advertise disabled/inactive protocols as active or imply that unconfigured features are live.

**Acceptance:** a new contributor can configure and run the app from a clean clone using only the docs and example environment files, without access to production secrets.

---

## 10. P2 — Final product QA

Run the full journey on a clean test account at desktop and mobile widths:

1. Open dashboard anonymously and inspect data-unavailable states.
2. Sign in and create an account record.
3. Add/remove watchlist protocols and refresh.
4. Open each active protocol detail page.
5. Inspect security evidence, registry addresses, data freshness, and TVL intelligence.
6. Inspect anomaly feed empty, populated, and unavailable states.
7. Link Telegram and verify a controlled test notification.
8. Verify email escalation using a test mailbox.
9. Complete Razorpay test-mode checkout; verify webhook-driven access.
10. Cancel a test subscription after cancellation flow is implemented.
11. Verify Free/Pro limits and account isolation.
12. Inspect browser console, server logs, keyboard navigation, loading states, and error states.

**Acceptance:** attach a concise QA matrix with environment, test account type (no personal data), result, evidence, and known failures.

---

## 11. Important engineering invariants

Do not weaken these behaviours while completing the work:

- Missing/stale/error data must never become LOW/NORMAL by default.
- Security assessment, rule-based activity score, historical TVL intelligence, and ML anomaly outputs are separate dimensions.
- Rule-based activity scoring remains the primary user-facing activity score unless a deliberate, reviewed product decision changes it.
- ML anomaly rows are not silently promoted into the calibrated anomaly feed.
- An upgradeable Solana program is not proof of compromise.
- Never claim supervised exploit prediction or performance metrics without suitable labelled data.
- Keep advisory-lock coordination and idempotent write behaviour for scheduled/background work.
- Never log secrets, raw webhook signatures, full secret-bearing RPC URLs, plaintext Telegram link tokens, or raw billing payloads.
- Do not run `prisma migrate dev`, `prisma db push`, `migrate reset`, destructive SQL, or seed scripts against production as a shortcut. Production migrations have already been applied; inspect state and use a reviewed, additive migration for future schema changes.
- Use a disposable test database for destructive/integration tests.
- Keep the retained Neon backup branch intact.

## 12. Suggested execution / handoff protocol

1. Start from the current `main` branch and create a separate branch for each coherent workstream.
2. Post the initial production data audit before running any backfill or modifying production data.
3. Submit small PRs with scope, implementation notes, commands/tests run, and screenshots/log excerpts where useful.
4. Do not combine data initialization, billing lifecycle changes, and ML redesign into one large PR.
5. Ask for review before any production data mutation, provider webhook change, or billing behaviour change.
6. Keep this handoff document updated as items are completed; mark an item DONE only when its acceptance criteria have evidence.
7. Backend hosting/provider selection and deployment remain outside this plan.

## 13. Definition of done

SolanaGuard is ready for the next product phase when:
- Historical and live data are populated and their freshness/coverage is visible.
- Security collection is working and UNKNOWN is preserved for missing evidence.
- Auth, watchlists, email, Telegram, and Razorpay pass end-to-end test-mode checks.
- Billing cancellation and entitlement reconciliation are complete.
- CI is green and full API/web/build checks pass.
- ML limitations and evaluation evidence are documented.
- The environment examples, API docs, architecture docs, and QA matrix are current.
- No critical/high-severity unresolved defect blocks a normal user journey.

Backend deployment is tracked separately and is not a completion criterion in this handoff.
