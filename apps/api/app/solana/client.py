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

    async def count_signatures_since(self, program_id: str, since_unix_seconds: int) -> int:
        """Count program-address signatures from ``since_unix_seconds`` onward.

        RPC returns newest signatures first and limits each page to 1,000.
        The method paginates until it has reached a signature older than the
        requested boundary. Missing ``blockTime`` values make the count
        unverifiable, so callers receive an error instead of a truncated
        value masquerading as a 24-hour count.
        """
        before: str | None = None
        seen_before: set[str] = set()
        count = 0

        while True:
            options: dict[str, Any] = {"limit": 1_000}
            if before is not None:
                options["before"] = before
            page = await self._rpc("getSignaturesForAddress", [program_id, options]) or []
            if not page:
                return count

            oldest_block_time: int | None = None
            for signature_info in page:
                block_time = signature_info.get("blockTime")
                if not isinstance(block_time, int):
                    raise ValueError("RPC signature response is missing blockTime")
                if block_time >= since_unix_seconds:
                    count += 1
                oldest_block_time = block_time

            if oldest_block_time is None or oldest_block_time < since_unix_seconds:
                return count

            before = page[-1].get("signature")
            if not isinstance(before, str) or before in seen_before:
                raise ValueError("RPC signature pagination did not advance")
            seen_before.add(before)


_client: SolanaClient | None = None


def get_solana_client() -> SolanaClient:
    global _client
    if _client is None:
        _client = SolanaClient()
    return _client
