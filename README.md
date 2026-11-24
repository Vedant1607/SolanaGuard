# SolanaGuard

AI-powered DeFi risk monitor for Solana. Real-time protocol monitoring, risk scoring, and alerting — built as a SaaS platform.

## Status

* **Phase 0** — Setup ✅
* **Phase 1** — MVP Core ✅ — 6 protocols on real Helius + DefiLlama data, rule-based risk engine, public dashboard, deployed
* **Phase 2** — Auth + Alerts ✅ (mostly) — Clerk auth, watchlists, email alerts on risk escalation. Telegram alerts still pending.
* **Phase 3** — Billing — not started
* **Phase 4** — ML layer — in progress on `feature/ml-risk-engine` (teammate)
* **Phase 5** — Harden & scale — not started

## Live

* **Web:** `<your-vercel-url>`
* **API:** `<your-render-url>/health`

## Monitored protocols

Raydium, Orca (DEX) · Kamino, Marginfi (Lending) · Marinade, Jito (Liquid Staking)

## Team split

* **Infra, backend, blockchain** (Vedant) — `apps/web`, `apps/api` shell/routers/solana adapters, deployment, billing
* **AI/ML** (teammate, `feature/ml-risk-engine` branch) — `apps/api/app/ml/`, `apps/api/app/services/sentiment_analyzer.py`, `apps/api/app/services/ai_narrator.py`

## Tech stack

* **Web:** Next.js 16 (App Router, Turbopack), Tailwind, Clerk auth
* **API:** FastAPI (Python 3.14, uv), direct asyncpg queries
* **Database:** PostgreSQL 18 via Prisma 7 (`packages/database`)
* **Data sources:** Helius (Solana RPC), DefiLlama (TVL)
* **Alerts:** Resend (email)
* **Deploy:** Vercel (web), Render (API + history cron, `render.yaml`), Neon (Postgres)

## Repo structure

```text
apps/
├── web/        Next.js dashboard + auth + watchlist
└── api/        FastAPI: ingestion, rule-based risk engine, alerts

packages/
├── database/   Prisma schema + client
└── types/      Shared TypeScript types
```

## Local development

### Prerequisites

* Node.js 24 LTS, pnpm 11
* Python 3.14, uv
* Docker (for local Postgres + Redis)

### Setup

```bash
git clone https://github.com/Vedant1607/SolanaGuard.git

cd solanaguard

pnpm install

cp .env.example .env

docker compose up -d

cd packages/database

npx prisma migrate dev

npx prisma db seed

cd ../..

cd apps/api

uv sync

cd ../..
```

Fill in `.env`, `apps/api/.env`, and `apps/web/.env.local` with your own Helius, Clerk, and Resend keys — see `.env.example` for the full list.

### Run

```bash
# terminal 1
cd apps/api && uv run fastapi dev app/main.py

# terminal 2
pnpm dev
```

Web: http://localhost:3000 · API: http://localhost:8000/docs

## Deployment

`main` is the production branch. Feature work happens on separate branches (e.g. `feature/phase-2`) and merges into `main` when ready to ship.

* **Web — Vercel** (`apps/web`): auto-deploys on push.
* **API — Render**, from the Blueprint in [`render.yaml`](render.yaml): one always-on Docker web service (`apps/api/Dockerfile`, health check `/health`) and a cron job, `scripts/refresh_defillama_history.py`, that refreshes DefiLlama TVL and token history every 6 hours. `autoDeploy` is off until the first deployment has been verified. `apps/api/railway.json` is the previous Railway setup and is kept until the Render deployment is validated.
* **Database — Neon.** Use the **direct** (non-pooled) connection string with `sslmode=require` for the API and the cron job (session advisory locks). Apply migrations with Prisma (`prisma migrate deploy` in `packages/database`).

Production API settings (`ENVIRONMENT=production`; the API refuses to start otherwise):

* `DEBUG=false`, `ALLOWED_ORIGINS` set explicitly to the web app's https origin(s), `DEFILLAMA_HISTORY_REFRESH_ENABLED=false` (the identity-gated scheduler is for local development; hosted refresh is the cron job).
* `INTERNAL_API_SECRET` (≥ 32 characters): required in the `X-Internal-Api-Secret` header by `POST /api/v1/protocols/ingest`, `POST /api/v1/protocols/{slug}/test-alert` and `POST /api/v1/telegram/test-message`. Without it those endpoints return 503.
* `TELEGRAM_WEBHOOK_SECRET`: must match the `secret_token` passed to Telegram's `setWebhook`; without it the webhook rejects every request.
* `DATABASE_URL`, `HELIUS_API_KEY`, `SOLANA_RPC_URL`; optional `RESEND_API_KEY`, `ALERT_FROM_EMAIL`, `TELEGRAM_BOT_TOKEN`. The cron job also needs `DEFILLAMA_REFRESH_EXPECTED_DATABASE` (the Neon database name it must find before writing).

Locally the API defaults to `ENVIRONMENT=development`; set `INTERNAL_API_SECRET` in `apps/api/.env` if you want to call the internal endpoints.
