"""
Protocol adapters — trimmed to the 6 confirmed launch protocols.
Data sources: DefiLlama (TVL) + Helius (tx counts).
Add more protocols here as the launch list grows post-MVP.

Program IDs and DefiLlama slugs verified directly against official
docs/GitHub/Solscan as of this file's last edit — two of the original
six (Kamino, Marginfi) were wrong and have been corrected.
"""

import httpx
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from app.solana.client import get_solana_client

logger = logging.getLogger(__name__)

# slug -> (Solana program ID, DefiLlama slug)
LAUNCH_PROTOCOLS = {
    "raydium":  {"program_id": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", "llama_slug": "raydium"},
    "orca":     {"program_id": "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", "llama_slug": "orca"},
    "kamino":   {"program_id": "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD", "llama_slug": "kamino-lend"},
    "marginfi": {"program_id": "MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA", "llama_slug": "marginfi"},
    "marinade": {"program_id": "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD", "llama_slug": "marinade-liquid-staking"},
    "jito":     {"program_id": "Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb", "llama_slug": "jito-liquid-staking"},
}


@dataclass
class ProtocolMetrics:
    slug: str
    tvl_usd: float
    tx_count_24h: int | None  # None = unavailable, never a genuine zero


async def fetch_tvl(llama_slug: str) -> float | None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(f"https://api.llama.fi/protocol/{llama_slug}")
            if resp.status_code == 200:
                tvl_series = resp.json().get("tvl", [])
                if tvl_series:
                    tvl_usd = tvl_series[-1].get("totalLiquidityUSD")
                    if isinstance(tvl_usd, (int, float)) and tvl_usd > 0:
                        return float(tvl_usd)
        except Exception as e:
            logger.warning(f"DefiLlama fetch failed for {llama_slug}: {e}")
    return None


def _describe_rpc_error(error: Exception) -> str:
    """Summarise an RPC failure without echoing the request URL, which embeds the API key."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, httpx.HTTPError):
        return type(error).__name__
    return f"{type(error).__name__}: {error}"


async def get_protocol_metrics(slug: str) -> Optional[ProtocolMetrics]:
    cfg = LAUNCH_PROTOCOLS.get(slug)
    if not cfg:
        return None

    tvl_usd = await fetch_tvl(cfg["llama_slug"])
    if tvl_usd is None:
        logger.warning(f"No valid TVL returned for {slug}, skipping snapshot")
        return None

    tx_count_24h: int | None
    try:
        client = get_solana_client()
        window_start = datetime.now(timezone.utc) - timedelta(hours=24)
        tx_count_24h = await client.count_signatures_since(
            cfg["program_id"],
            int(window_start.timestamp()),
        )
    except Exception as e:
        tx_count_24h = None
        logger.warning(
            f"Helius 24-hour transaction count unavailable for {slug} "
            f"({_describe_rpc_error(e)}); storing TVL-only snapshot with txCount24h=NULL"
        )

    return ProtocolMetrics(slug=slug, tvl_usd=tvl_usd, tx_count_24h=tx_count_24h)
