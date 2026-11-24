"""Backfill closed-day DefiLlama per-asset token history into metric_observations.

Usage (from apps/api, with DATABASE_URL pointing at the intended database):

    uv run python scripts/backfill_defillama_tokens.py [--slug kamino ...]

Writes ``tokens.qty.daily`` (unit "token"), ``tokens.usd.daily`` (unit "usd")
for Jito, Marinade, Kamino and Drift, and ``tvl.borrowed.usd.daily`` for
Kamino, all as ``defillama_token_history@1``. Idempotent: re-running inserts
only keys that are not stored yet, never updates or deletes, and reports
conflicts instead of overwriting them. Prints a JSON report; exits 1 if any
protocol failed or did not verify.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.db import close_pool  # noqa: E402
from app.log_redaction import install_log_redaction  # noqa: E402
from app.tasks.defillama_token_backfill import run_token_backfill  # noqa: E402


async def main(slugs: list[str] | None) -> int:
    try:
        report = await run_token_backfill(slugs=slugs)
    finally:
        await close_pool()
    print(json.dumps(report.as_dict(), indent=2))
    totals = report.totals
    return 1 if totals["failed"] or totals["unverified"] else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    install_log_redaction()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slug", action="append", help="limit to these SolanaGuard slugs")
    raise SystemExit(asyncio.run(main(parser.parse_args().slug)))
