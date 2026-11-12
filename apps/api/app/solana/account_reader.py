"""Read small byte ranges of Solana accounts over JSON-RPC.

Used by the on-chain security adapter. Every failure is raised as an
``AccountReadError`` carrying a short, sanitised ``code`` (for example
``HTTP_429``, ``TIMEOUT`` or ``RPC_-32602``) so callers can record why a
measurement is missing without ever echoing the RPC URL, which may embed an
API key.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

PUBLIC_MAINNET_RPC_URL = "https://api.mainnet-beta.solana.com"


class AccountReadError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AccountSlice:
    address: str
    context_slot: int
    owner: str
    executable: bool
    data: bytes  # only the requested slice


@dataclass(frozen=True)
class AccountLookup:
    """``account`` is None when the address holds no account at ``context_slot``."""

    context_slot: int
    account: AccountSlice | None


def resolve_security_rpc_url() -> str | None:
    """The RPC endpoint for security reads, or None if none may be used.

    ``SOLANA_RPC_URL`` always wins. Without it, the public mainnet endpoint is
    used only in DEBUG (development); a non-debug deployment must configure a
    reliable provider explicitly.
    """
    if settings.SOLANA_RPC_URL:
        return settings.SOLANA_RPC_URL
    if settings.DEBUG:
        return PUBLIC_MAINNET_RPC_URL
    return None


class AccountReader:
    def __init__(self, rpc_url: str, client: httpx.AsyncClient, commitment: str = "confirmed"):
        self._rpc_url = rpc_url
        self._client = client
        self._commitment = commitment

    async def _rpc(self, method: str, params: list) -> Any:
        try:
            response = await self._client.post(
                self._rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
        except httpx.TimeoutException:
            raise AccountReadError("TIMEOUT") from None
        except httpx.HTTPError:
            raise AccountReadError("NETWORK") from None
        if response.status_code != 200:
            raise AccountReadError(f"HTTP_{response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise AccountReadError("MALFORMED_RESPONSE") from None
        if not isinstance(payload, dict):
            raise AccountReadError("MALFORMED_RESPONSE")
        if "error" in payload:
            error = payload["error"]
            code = error.get("code") if isinstance(error, dict) else None
            raise AccountReadError(f"RPC_{code}" if isinstance(code, int) else "RPC_ERROR")
        if "result" not in payload:
            raise AccountReadError("MALFORMED_RESPONSE")
        return payload["result"]

    async def get_account_slice(self, address: str, offset: int, length: int) -> AccountLookup:
        result = await self._rpc(
            "getAccountInfo",
            [
                address,
                {
                    "encoding": "base64",
                    "commitment": self._commitment,
                    "dataSlice": {"offset": offset, "length": length},
                },
            ],
        )
        try:
            context_slot = int(result["context"]["slot"])
            value = result["value"]
            if value is None:
                return AccountLookup(context_slot=context_slot, account=None)
            encoded, encoding = value["data"]
            if encoding != "base64":
                raise AccountReadError("MALFORMED_RESPONSE")
            account = AccountSlice(
                address=address,
                context_slot=context_slot,
                owner=str(value["owner"]),
                executable=bool(value["executable"]),
                data=base64.b64decode(encoded, validate=True),
            )
        except AccountReadError:
            raise
        except (KeyError, TypeError, ValueError):
            raise AccountReadError("MALFORMED_RESPONSE") from None
        return AccountLookup(context_slot=context_slot, account=account)
