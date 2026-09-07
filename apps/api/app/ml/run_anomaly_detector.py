"""Run the offline anomaly detector against the configured PostgreSQL database."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

# ``uv run python app/ml/run_anomaly_detector.py`` executes this file as a
# script, so add the API project root before importing the ``app`` package.
API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.db import close_pool
from app.ml.anomaly_detector import run_detector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main() -> int:
    try:
        report = await run_detector()
        print(json.dumps(report.__dict__, indent=2, sort_keys=True, default=str))
        return 0
    except Exception:
        logger.exception("Anomaly detector failed")
        return 1
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
