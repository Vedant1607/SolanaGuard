"""One DefiLlama daily-history refresh for hosted deployments (Render Cron Job).

Usage (from apps/api, with DATABASE_URL pointing at the intended database):

    DEFILLAMA_REFRESH_EXPECTED_DATABASE=<db name> uv run python scripts/refresh_defillama_history.py

Runs the same two idempotent backfills as the local scheduler, in order
(TVL history, then token history), under the same session advisory lock
(``REFRESH_CYCLE_LOCK_ID``) via ``run_locked_jobs``; each backfill keeps its own
per-protocol ``pg_advisory_xact_lock``. Nothing is updated or deleted: only
missing days are inserted and conflicting source revisions are reported.

Target check (fails closed): ``current_database()`` must equal
``--expected-database`` / ``DEFILLAMA_REFRESH_EXPECTED_DATABASE``. This is an
explicit guard against a mis-pointed DATABASE_URL; it does not replace the
local scheduler's cluster-identity gate, which hosted Postgres may not support.

Each run is a fresh process, so nothing depends on API restarts or in-memory
state. Prints a JSON summary (no credentials) and exits:

    0  both jobs succeeded, or another refresh holds the lock (skipped, logged)
    1  a job failed or was partial (some protocols failed or did not verify)
    2  target check failed or not configured; no backfill was run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.db import close_pool, get_pool  # noqa: E402
from app.log_redaction import install_log_redaction  # noqa: E402
from app.tasks.defillama_refresh import run_locked_jobs  # noqa: E402

logger = logging.getLogger("refresh_defillama_history")

EXIT_OK, EXIT_JOB_FAILED, EXIT_TARGET = 0, 1, 2
EXPECTED_DATABASE_ENV = "DEFILLAMA_REFRESH_EXPECTED_DATABASE"


async def check_target(pool, expected_database: str) -> tuple[bool, str]:
    if not expected_database:
        return False, "expected_database_not_configured"
    try:
        async with pool.acquire() as conn:
            actual = await conn.fetchval("SELECT current_database()")
    except Exception as error:
        return False, f"target_check_failed:{type(error).__name__}"
    return (True, "verified") if actual == expected_database else (False, "database_mismatch")


async def refresh(expected_database: str, *, pool=None, jobs=run_locked_jobs) -> tuple[int, dict]:
    """(exit code, summary). ``pool``/``jobs`` are injectable for tests."""
    summary: dict = {"target": None, "outcome": None, "jobs": []}
    owns_pool = pool is None
    try:
        if not expected_database:  # configuration problem: fail before opening any connection
            ok, reason = False, "expected_database_not_configured"
        elif pool is None:
            try:
                pool = await get_pool()
            except Exception as error:  # type only: connection errors can echo connection details
                ok, reason = False, f"target_check_failed:{type(error).__name__}"
            else:
                ok, reason = await check_target(pool, expected_database)
        else:
            ok, reason = await check_target(pool, expected_database)
        summary["target"] = {"expected_database": expected_database or None, "verified": ok, "reason": reason}
        if not ok:
            logger.error("DefiLlama history refresh blocked: target database not verified (%s); no backfill was run", reason)
            summary["outcome"] = "skipped_target"
            return EXIT_TARGET, summary
        outcome, job_statuses = await jobs(pool)
        summary["outcome"] = outcome
        summary["jobs"] = [asdict(j) for j in job_statuses]
    finally:
        if owns_pool:
            await close_pool()
    if outcome == "succeeded":
        return EXIT_OK, summary
    if outcome == "skipped_locked":
        logger.warning("DefiLlama history refresh skipped: another refresh holds the lock")
        return EXIT_OK, summary
    logger.error("DefiLlama history refresh finished with outcome %s", outcome)
    return EXIT_JOB_FAILED, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--expected-database", default=os.environ.get(EXPECTED_DATABASE_ENV, ""),
                        help=f"required current_database() of the target (default: ${EXPECTED_DATABASE_ENV})")
    args = parser.parse_args(argv)
    code, summary = asyncio.run(refresh(args.expected_database))
    print(json.dumps(summary, indent=2))
    return code


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    install_log_redaction()
    raise SystemExit(main())
