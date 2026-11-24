"""Record public mainnet getAccountInfo responses used as test fixtures.

Usage (from apps/api, network access required, read-only):

    python scripts/record_solana_fixtures.py [RPC_URL]

RPC_URL defaults to the public mainnet endpoint. Only header byte ranges are
requested (no program code), and only public chain data is stored. The RPC
URL itself is never written to the fixture file, so a keyed endpoint can be
used safely.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.solana.loader import (  # noqa: E402
    PROGRAM_HEADER_LEN,
    PROGRAMDATA_HEADER_LEN,
    BPF_UPGRADEABLE_LOADER,
    decode_program_account,
)
import base64  # noqa: E402

PUBLIC_MAINNET_RPC_URL = "https://api.mainnet-beta.solana.com"
OUTPUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "solana" / "mainnet_accounts.json"

PROGRAMS = [
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpools
    "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD",  # Kamino Lend
    "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD",  # Marinade
    "SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy",  # SPL Stake Pool (Jito dependency)
    "dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH",  # Drift v2
    "MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA",  # Marginfi v2
]
NON_PROGRAMS = [
    "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF",  # Kamino main market
    "8szGkuLTAux9XMgZ2vtY39jVSowEcpBfFfD8hXSEqdGC",  # Marinade state
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL mint
    "Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb",  # Jito stake pool
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # JitoSOL mint
]
# Deliberately invalid / absent keys, to record real RPC failure shapes.
NEGATIVE = [
    ("KLend2g3cP87fffoy8q1mQqGKjrL9jnmujyAASHa3tsF", 0, 0),  # old seed value (33 bytes)
    ("MFv2hWf31Z9kbCa1snEPdcgp168vLLAZnkTHsMgGgAB", 0, 0),  # old seed value (no account)
]


async def fetch(client: httpx.AsyncClient, url: str, address: str, offset: int, length: int) -> dict:
    params = [
        address,
        {"encoding": "base64", "commitment": "confirmed", "dataSlice": {"offset": offset, "length": length}},
    ]
    response = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": params})
    response.raise_for_status()
    await asyncio.sleep(0.25)  # stay well inside public rate limits
    return {"address": address, "offset": offset, "length": length, "response": response.json()}


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else PUBLIC_MAINNET_RPC_URL
    exchanges = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for program in PROGRAMS:
            exchange = await fetch(client, url, program, 0, PROGRAM_HEADER_LEN)
            exchanges.append(exchange)
            value = exchange["response"]["result"]["value"]
            if value and value["owner"] == BPF_UPGRADEABLE_LOADER:
                programdata = decode_program_account(base64.b64decode(value["data"][0]))
                exchanges.append(await fetch(client, url, programdata, 0, PROGRAMDATA_HEADER_LEN))
        for address in NON_PROGRAMS:
            exchanges.append(await fetch(client, url, address, 0, 0))
        for address, offset, length in NEGATIVE:
            exchanges.append(await fetch(client, url, address, offset, length))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "description": "Recorded public Solana mainnet getAccountInfo responses (header slices only).",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "endpoint": "public mainnet" if url == PUBLIC_MAINNET_RPC_URL else "configured provider",
        "exchanges": exchanges,
    }
    OUTPUT.write_text(json.dumps(document, indent=2) + "\n")
    print(f"wrote {len(exchanges)} exchanges to {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
