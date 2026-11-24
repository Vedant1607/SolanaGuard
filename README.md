# SolanaGuard

SolanaGuard is an AI-assisted DeFi risk-monitoring platform for Solana. It combines protocol activity monitoring, evidence-based on-chain security assessment, historical TVL intelligence, anomaly detection, watchlists, and alerting in a web dashboard.

> **Project status (25 November 2026):** The core application and most feature implementations are in the repository. The remaining focus is production data initialization, integration verification, billing lifecycle completion, automated checks, model validation, and documentation. The FastAPI backend has not yet been deployed; its hosting target is intentionally still open.

## Live application

- **Web dashboard:** [solanaguard-seven.vercel.app](https://solanaguard-seven.vercel.app)
- **Database:** Neon PostgreSQL
- **Backend API:** Not yet publicly deployed. The web application expects the API base URL through `API_URL`.

## Current implementation status

| Area | Status |
|---|---|
| Monorepo and core dashboard | Implemented |
| Protocol ingestion and rule-based activity scoring | Implemented; production data flow still needs verification |
| Clerk authentication and watchlists | Implemented; end-to-end production verification pending |
| Email alerts | Implemented; delivery verification pending |
| Telegram linking, webhook and alerts | Implemented; live webhook/delivery verification pending |
| On-chain security assessment and address registry | Implemented; collector/data coverage needs verification |
| Historical TVL and token observations | Backfill and recurring refresh code implemented; production data initialization pending |
| Time-window anomaly detector | Implemented; meaningful results depend on sufficient continuous history |
| Category-based ML risk engine | Implemented separately; feature coverage and evaluation remain limited |
| Razorpay checkout and reliable webhook processing | Implemented; live test-mode verification and customer cancellation flow remain |
| CI and release-readiness checks | Not yet established as a repository workflow |
| Production API hosting | Not yet selected or deployed |

## Monitored protocols

The current catalogue contains six active protocols:

- **DEX:** Raydium, Orca
- **Lending:** Kamino
- **Liquid staking:** Marinade, Jito
- **Perpetuals:** Drift

Marginfi remains in the database to preserve historical relationships, but it is currently inactive and is not part of the active dashboard catalogue. The Free plan displays up to five protocols; Pro can display all active protocols.

## How the risk systems differ

SolanaGuard intentionally keeps its analytical dimensions separate. Do not combine their scores or describe them as interchangeable.

1. **Rule-based activity score** — the primary user-facing activity/risk score, based on protocol snapshot changes such as TVL and transaction counts. It is not a smart-contract security verdict.
2. **On-chain security assessment** — evaluates registered addresses and collected observations against versioned rules. Missing, stale, or failed observations produce an unknown/insufficient assessment, not a LOW result.
3. **Historical TVL intelligence (`tvl-robust-v1`)** — evaluates daily TVL behaviour relative to a protocol's history and peers. It reports unusualness, not the probability of an exploit.
4. **Time-window anomaly detector (`iforest-v2-time-window`)** — persists calibrated anomaly results exposed through the anomaly feed.
5. **Category ML risk engine (`ml-risk-category-iforest-v1`)** — stores its scores and anomalies separately. Its score is not blended into the rule-based activity score, and its anomaly rows are excluded from the calibrated anomaly feed.

### Interpretation and safety rules

- Missing, stale, or failed data must remain **UNKNOWN / INSUFFICIENT_HISTORY**. Never convert missing data into LOW/NORMAL.
- An upgradeable Solana program is not, by itself, evidence of compromise.
- ML anomaly scores are not supervised exploit predictions. Do not claim precision, recall, or exploit-prediction performance without a suitable labelled dataset and evaluation.
- Some category-ML inputs (including volume, unique-wallet, and liquidity-depth measurements) are not currently collected reliably. The category model has known placeholder/unmeasured inputs and may abstain when required data is unavailable.
- The historical TVL model needs at least 90 days of usable observations. The time-window detector needs roughly 18 days of continuous five-minute snapshots before it can assess a protocol meaningfully.
- An empty anomaly feed is not an all-clear; it may mean that no protocol has enough usable history to be assessed.

## Architecture

| Component | Technology | Location |
|---|---|---|
| Web application | Next.js 16 App Router, React 19, TypeScript, Tailwind CSS | `apps/web` |
| Backend API | FastAPI, Python 3.14, asyncpg, Pydantic Settings | `apps/api` |
| Database | Neon PostgreSQL 18, Prisma 7 schema and migrations | `packages/database` |
| Authentication | Clerk | Web application |
| Payments | Razorpay subscriptions and signed webhooks | Web application |
| Email notifications | Resend | API |
| Telegram notifications | Telegram Bot API and webhook | API + web application |
| Solana data | Helius / Solana JSON-RPC | API adapters |
| TVL and token history | DefiLlama | API adapters and backfill jobs |

### Data and background jobs

The API currently runs protocol ingestion and the time-window anomaly detector as lifespan-managed tasks. The on-chain security collector is configurable. Historical DefiLlama refresh is implemented as an idempotent script for a scheduled runner; it should not be assumed to run automatically merely because the API process is running.

PostgreSQL advisory locks coordinate ingestion, detection, security collection, and historical refresh work across instances. The historical refresh script verifies the target database name before writing.

## API surface

The FastAPI application exposes, among others:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Basic process health |
| `GET /api/v1/protocols` | Active protocol snapshots and latest rule-based scores |
| `GET /api/v1/protocols/{slug}/risk-score` | Latest rule-based score for a protocol |
| `GET /api/v1/protocols/{slug}/security` | Address registry, observations and security evidence |
| `GET /api/v1/security/collector-status` | Security collector heartbeat and latest stored batches |
| `GET /api/v1/protocols/{slug}/intelligence` | Historical TVL intelligence |
| `GET /api/v1/anomalies` | Persisted calibrated anomaly feed |
| `GET /api/v1/history/refresh-status` | Historical observation availability and refresh status |
| `POST /api/v1/telegram/webhook` | Telegram bot webhook |
| `POST /api/v1/protocols/ingest` | Internal, authenticated ingestion trigger |

Internal write/test endpoints require `X-Internal-Api-Secret`. The Telegram webhook requires Telegram's configured secret-token header. See the corresponding router and authentication modules before integrating new callers.

## Local development

### Prerequisites

- Node.js 24
- pnpm 9 (the version declared by the repository)
- Python 3.14 and [uv](https://docs.astral.sh/uv/)
- PostgreSQL for local development; Docker Compose is available at the repository root

### Install and configure

```bash
git clone https://github.com/Vedant1607/SolanaGuard.git
cd SolanaGuard

pnpm install

# Install the API dependencies
cd apps/api
uv sync
cd ../..
```

Configure environment variables in the appropriate local environment files. Do not commit secrets. At a minimum, the API needs a PostgreSQL `DATABASE_URL`; Helius/Solana RPC settings are needed for live Solana collection. The web application needs `API_URL` and the Clerk settings used by the app. Razorpay, Resend, and Telegram variables are needed only for the corresponding integrations.

The root `.env.example` is currently a legacy starter and does not yet reflect every current integration (notably Razorpay and all API settings). See [the teammate handoff](docs/TEAMMATE_HANDOFF.md) for the environment-file cleanup task; do not treat the starter file as a complete production configuration.

### Database

```bash
docker compose up -d
pnpm db:migrate
pnpm db:seed
```

Use `pnpm db:migrate` only against a local development database. Production migrations have already been applied to the current Neon database; do not run development/reset commands against production.

### Run the applications

Terminal 1 — API:

```bash
cd apps/api
uv run fastapi dev app/main.py
```

Terminal 2 — web:

```bash
pnpm dev
```

- Web: http://localhost:3000
- API: http://localhost:8000
- API OpenAPI docs: http://localhost:8000/docs

## Tests and quality checks

Run from the repository root unless noted:

```bash
# API tests
cd apps/api
uv run pytest
cd ../..

# Web unit tests
pnpm --filter @solanaguard/web test

# Monorepo checks
pnpm lint
pnpm check-types
pnpm build
```

The repository contains API and web test suites. Their presence does not imply that every check currently passes; run the full suite against the current commit before declaring a release candidate. Database integration tests are opt-in and must use a disposable test database, never production.

## Environment configuration reference

Keep secrets in the relevant local environment file or service secret store; never add real values to Git.

### API

Common settings include:

- `DATABASE_URL`
- `ENVIRONMENT` (`development` or `production`)
- `DEBUG` (must be false in production)
- `ALLOWED_ORIGINS` (explicit HTTPS origins in production)
- `INTERNAL_API_SECRET` (at least 32 characters when configured)
- `HELIUS_API_KEY`, `HELIUS_RPC_URL`, `SOLANA_RPC_URL`
- `INGEST_INTERVAL_SECONDS`, `ANOMALY_DETECTOR_INTERVAL_SECONDS`
- `SECURITY_COLLECTOR_ENABLED`, `SECURITY_COLLECTOR_INTERVAL_SECONDS`
- `RESEND_API_KEY`, `ALERT_FROM_EMAIL`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`

The hosted historical refresh script separately requires `DEFILLAMA_REFRESH_EXPECTED_DATABASE` so it can fail closed when pointed at the wrong database. Its schedule must be configured by the chosen hosting/scheduling environment.

### Web application

Common settings include:

- `API_URL`
- `NEXT_PUBLIC_APP_URL`
- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`
- `NEXT_PUBLIC_TELEGRAM_BOT_USERNAME`
- `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_PLAN_ID`
- `NEXT_PUBLIC_RAZORPAY_KEY_ID`, `RAZORPAY_WEBHOOK_SECRET`

## Repository layout

```text
apps/
  api/       FastAPI service, adapters, background tasks, ML, API tests
  web/       Next.js dashboard, authentication, billing and web tests
packages/
  database/ Prisma schema, migrations, seed and database client
  eslint-config/
docs/
  TEAMMATE_HANDOFF.md
```

## Next steps

The ordered remaining-work plan, acceptance criteria, known limitations, and handoff boundaries are maintained in [`docs/TEAMMATE_HANDOFF.md`](docs/TEAMMATE_HANDOFF.md). The immediate priorities are:

1. Initialize and verify production historical and on-chain data.
2. Complete integration and billing lifecycle verification.
3. Establish CI and run full regression/build checks.
4. Validate ML feature coverage and model behaviour.
5. Finish environment/documentation cleanup and final product QA.
