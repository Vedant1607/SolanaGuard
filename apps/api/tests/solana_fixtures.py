"""Replay recorded public mainnet getAccountInfo responses through httpx.

``tests/fixtures/solana/mainnet_accounts.json`` holds real responses recorded
by ``scripts/record_solana_fixtures.py`` (header slices only, public data).
Edge cases that cannot be recorded on demand (an authority change, an
immutable program, a malformed header) are derived from those recordings in
the tests themselves and are labelled as such.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "solana" / "mainnet_accounts.json"
FAKE_RPC_URL = "https://rpc.example.test/?api-key=fixture-secret-key"


def load_recorded() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def recorded_responses() -> dict[tuple[str, int, int], dict]:
    return {
        (e["address"], e["offset"], e["length"]): e["response"] for e in load_recorded()["exchanges"]
    }


def account_value(response: dict) -> dict | None:
    return response["result"]["value"]


def with_data(response: dict, data: bytes, owner: str | None = None) -> dict:
    """Copy of a recorded response with replaced account bytes (derived fixture)."""
    copy = json.loads(json.dumps(response))
    copy["result"]["value"]["data"] = [base64.b64encode(data).decode(), "base64"]
    if owner is not None:
        copy["result"]["value"]["owner"] = owner
    return copy


def replay_transport(
    responses: dict[tuple[str, int, int], dict] | None = None,
    *,
    status_overrides: dict[str, int] | None = None,
    raise_for: dict[str, Exception] | None = None,
    calls: list | None = None,
) -> httpx.MockTransport:
    responses = recorded_responses() if responses is None else responses
    status_overrides = status_overrides or {}
    raise_for = raise_for or {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["method"] == "getAccountInfo"
        address, options = body["params"]
        key = (address, options["dataSlice"]["offset"], options["dataSlice"]["length"])
        if calls is not None:
            calls.append(key)
        if address in raise_for:
            raise raise_for[address]
        if address in status_overrides:
            return httpx.Response(status_overrides[address], json={"error": "denied"}, request=request)
        if key not in responses:
            raise AssertionError(f"no recorded response for {key}")
        return httpx.Response(200, json=responses[key], request=request)

    return httpx.MockTransport(handler)
