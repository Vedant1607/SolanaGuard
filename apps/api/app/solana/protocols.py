import httpx
import logging
from dataclasses import dataclass
from typing import Optional
from app.solana.client import get_solana_client

logger = logging.getLogger(__name__)

LAUNCH_PROTOCOLS = {
    "raydium":  {"program_id": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", "llama_slug": "raydium"},
    "orca":     {"program_id": "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", "llama_slug": "orca"},
    "kamino":   {"program_id": "KLend2g3cP87fffoy8q1mQqGKjrL9jnmujyAASHa3tsF", "llama_slug": "kamino-lending"},
    "marginfi": {"program_id": "MFv2hWf31Z9kbCa1snEPdcgp168vLLAZnkTHsMgGgAB", "llama_slug": "marginfi"},
    "marinade": {"program_id": "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD", "llama_slug": "marinade-finance"},
    "jito":     {"program_id": "Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb", "llama_slug": "jito-liquid-staking"},
}


@dataclass
class ProtocolMetrics:
    slug: str
    tvl_usd: float
    tx_count_24h: int


async def fetch_tvl(llama_slug: str) -> float:
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(f"https://api.llama.fi/protocol/{llama_slug}")
            if resp.status_code == 200:
                tvl_series = resp.json().get("tvl", [])
                if tvl_series:
                    return tvl_series[-1].get("totalLiquidityUSD", 0.0)
        except Exception as e:
            logger.warning(f"DefiLlama fetch failed for {llama_slug}: {e}")
    return 0.0


async def get_protocol_metrics(slug: str) -> Optional[ProtocolMetrics]:
    cfg = LAUNCH_PROTOCOLS.get(slug)
    if not cfg:
        return None

    tvl_usd = await fetch_tvl(cfg["llama_slug"])

    tx_count_24h = 0
    try:
        client = get_solana_client()
        sigs = await client.get_recent_signatures(cfg["program_id"], limit=100)
        tx_count_24h = len(sigs)
    except Exception as e:
        logger.warning(f"Helius tx fetch failed for {slug}: {e}")

    return ProtocolMetrics(slug=slug, tvl_usd=tvl_usd, tx_count_24h=tx_count_24h)