import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

import asyncpg
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.risk.security_assessment import (
    ADDRESSES_QUERY,
    BY_ID_QUERY,
    CHANGES_QUERY,
    FIRST_OK_QUERY,
    LATEST_QUERY,
)
from app.routers.security import PROTOCOL_QUERY, router

UPG = "BPFLoaderUpgradeab1e11111111111111111111111"
AUTH = "FytDrVzDybM1TwFQPGb8qaxZR7dBCzNeqT3vtQsceZQK"


def now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def obs_row(id, metric, *, status="OK", numeric=None, text=None, age=timedelta(minutes=5), slot=468_000_000, error=None, unit="pubkey", **extra):
    row = {
        "id": id, "address_id": "addr-1", "metric_key": metric, "status": status,
        "value_numeric": None if numeric is None else Decimal(numeric), "value_text": text,
        "unit": unit, "source": "solana-rpc", "source_slot": slot, "source_observed_at": None,
        "collected_at": now_naive() - age, "adapter_key": "onchain_security", "adapter_version": "1",
        "error_code": error,
    }
    row.update(extra)
    return row


PROTOCOL = {"id": "p-1", "slug": "raydium", "name": "Raydium", "category": "DEX", "is_active": True, "support_level": "PARTIAL"}
ADDRESS = {
    "id": "addr-1", "address": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", "kind": "PROGRAM",
    "label": "Raydium AMM v4", "is_shared_dependency": False, "expected_owner": UPG,
    "verification": "VERIFIED", "verified_at": datetime(2026, 11, 11, 18, 56), "verification_note": "checked",
}


class FakeConnection:
    def __init__(self, protocol=PROTOCOL, addresses=(ADDRESS,), latest=(), first_ok=(), changes=(), by_id=(), error=None):
        self.results = {
            ADDRESSES_QUERY: list(addresses), LATEST_QUERY: list(latest), FIRST_OK_QUERY: list(first_ok),
            CHANGES_QUERY: list(changes), BY_ID_QUERY: list(by_id),
        }
        self.protocol, self.error = protocol, error
        self.queries = []

    async def fetchrow(self, query, *args):
        assert query == PROTOCOL_QUERY
        if self.error:
            raise self.error
        return self.protocol

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return self.results[query]


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return Acquire()


class SecurityApiTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1/protocols")
        self.client = TestClient(app)

    def get(self, conn, slug="raydium"):
        with patch("app.routers.security.get_pool", AsyncMock(return_value=FakePool(conn))):
            return self.client.get(f"/api/v1/protocols/{slug}/security")

    def test_unknown_protocol_is_404(self):
        self.assertEqual(self.get(FakeConnection(protocol=None)).status_code, 404)

    def test_unmigrated_database_is_503_without_details(self):
        response = self.get(FakeConnection(error=asyncpg.exceptions.UndefinedColumnError("supportLevel")))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("supportLevel", response.text)

    def test_no_observations_yields_unknown_with_reasons(self):
        response = self.get(FakeConnection())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["dimension"]["level"], "UNKNOWN")
        self.assertEqual(body["dimension"]["assessed_indicators"], 0)
        self.assertEqual(body["dimension"]["total_indicators"], 4)
        self.assertTrue(all(e["level"] == "UNKNOWN" for e in body["evidence"]))
        self.assertTrue(all("no observation has been collected yet" in e["explanation"] for e in body["evidence"]))

    def test_full_contract_shape_with_observation_provenance(self):
        latest = [
            obs_row("o-owner", "registry.owner", text=UPG),
            obs_row("o-upg", "security.program_upgradeable", numeric=1, unit="bool"),
            obs_row("o-auth", "security.upgrade_authority", text=AUTH),
            obs_row("o-slot", "security.last_deploy_slot", numeric=445_763_204, unit="slot"),
        ]
        first_ok = [dict(r, id=r["id"] + "-first", ok_count=8, collected_at=now_naive() - timedelta(days=2)) for r in latest]
        conn = FakeConnection(latest=latest, first_ok=first_ok)
        body = self.get(conn).json()

        self.assertEqual(body["protocol"], {
            "slug": "raydium", "name": "Raydium", "category": "DEX", "is_active": True, "support_level": "PARTIAL",
        })
        self.assertEqual(body["dimension"]["level"], "MEDIUM")
        self.assertEqual(body["dimension"]["rule_version"], "security-rules/1")
        self.assertEqual((body["dimension"]["assessed_indicators"], body["dimension"]["total_indicators"]), (4, 4))
        self.assertEqual(body["addresses"][0]["verification"], "VERIFIED")
        self.assertTrue(body["addresses"][0]["verified_at"].endswith("Z") or "+00:00" in body["addresses"][0]["verified_at"])

        rules = {e["rule_id"]: e for e in body["evidence"]}
        self.assertEqual(set(rules), {"REG-01", "SEC-01", "SEC-02", "SEC-03"})
        self.assertEqual(rules["SEC-01"]["level"], "MEDIUM")
        self.assertEqual(rules["SEC-02"]["level"], "LOW")
        self.assertEqual(rules["SEC-03"]["level"], "LOW")
        sec01 = rules["SEC-01"]["observations"]
        self.assertEqual([o["id"] for o in sec01], ["o-upg", "o-auth"])
        self.assertEqual(sec01[0]["value"], "true")
        for o in sec01:
            self.assertEqual(o["source"], "solana-rpc")
            self.assertEqual(o["adapter"], "onchain_security@1")
            self.assertEqual(o["source_slot"], 468_000_000)
            self.assertIsNotNone(o["collected_at"])
        self.assertEqual(rules["SEC-03"]["observations"][0]["value"], "445763204")

        # Change detection is bounded to the history window and tracked metrics.
        changes_args = next(args for q, args in conn.queries if q == CHANGES_QUERY)
        self.assertEqual(changes_args[0], "p-1")
        self.assertEqual(set(changes_args[1]), {"security.program_upgradeable", "security.upgrade_authority"})

    def test_authority_change_is_high_and_cites_previous_observation(self):
        latest = [
            obs_row("o-owner", "registry.owner", text=UPG),
            obs_row("o-upg", "security.program_upgradeable", numeric=1, unit="bool"),
            obs_row("o-auth", "security.upgrade_authority", text="NewAuthority1111111111111111111111111111111"),
            obs_row("o-slot", "security.last_deploy_slot", numeric=445_763_204, unit="slot"),
        ]
        change = obs_row("o-auth", "security.upgrade_authority", text="NewAuthority1111111111111111111111111111111",
                         previous_id="o-prev", previous_text=AUTH, previous_numeric=None)
        previous = obs_row("o-prev", "security.upgrade_authority", text=AUTH, age=timedelta(minutes=20))
        body = self.get(FakeConnection(latest=latest, changes=[change], by_id=[previous])).json()
        self.assertEqual(body["dimension"]["level"], "HIGH")
        sec02 = next(e for e in body["evidence"] if e["rule_id"] == "SEC-02")
        self.assertEqual([o["id"] for o in sec02["observations"]], ["o-prev", "o-auth"])

    def test_errors_are_exposed_as_codes_and_unknown(self):
        latest = [obs_row("o-owner", "registry.owner", status="ERROR", error="HTTP_429")]
        body = self.get(FakeConnection(latest=latest)).json()
        reg = next(e for e in body["evidence"] if e["rule_id"] == "REG-01")
        self.assertEqual(reg["level"], "UNKNOWN")
        self.assertIn("HTTP_429", reg["explanation"])
        self.assertEqual(reg["observations"][0]["error_code"], "HTTP_429")
        self.assertIsNone(reg["observations"][0]["value"])

    def test_inactive_protocol_without_active_addresses_is_unknown(self):
        protocol = dict(PROTOCOL, slug="marginfi", is_active=False)
        body = self.get(FakeConnection(protocol=protocol, addresses=())).json()
        self.assertFalse(body["protocol"]["is_active"])
        self.assertEqual(body["dimension"]["level"], "UNKNOWN")
        self.assertEqual(body["evidence"], [])


if __name__ == "__main__":
    unittest.main()
