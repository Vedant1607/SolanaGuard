import httpx
from typing import Any
from app.config import settings


class SolanaClient:
    def __init__(self):
        self.rpc_url = f"{settings.HELIUS_RPC_URL}{settings.HELIUS_API_KEY}"
        self.timeout = httpx.Timeout(20.0)

    async def _rpc(self, method: str, params: list) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise ValueError(f"RPC error: {data['error']}")
            return data["result"]

    async def get_recent_signatures(self, program_id: str, limit: int = 100) -> list:
        result = await self._rpc("getSignaturesForAddress", [program_id, {"limit": limit}])
        return result or []


_client: SolanaClient | None = None


def get_solana_client() -> SolanaClient:
    global _client
    if _client is None:
        _client = SolanaClient()
    return _client