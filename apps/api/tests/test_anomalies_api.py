import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.anomalies import router


class FakeConnection:
    def __init__(self, rows=None, total=0, error=None):
        self.rows = rows or []
        self.total = total
        self.error = error
        self.fetch_args = None
        self.fetchval_args = None

    async def fetch(self, query, *args):
        self.fetch_args = (query, args)
        if self.error:
            raise self.error
        return self.rows

    async def fetchval(self, query, *args):
        self.fetchval_args = (query, args)
        if self.error:
            raise self.error
        return self.total


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return FakeAcquire(self.connection)


def anomaly_row(**overrides):
    row = {
        "id": "anomaly-1",
        "type": "TX_SPIKE",
        "severity": "HIGH",
        "description": "Transaction activity spiked.",
        "detected_at": datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc),
        "calibrated_score": 99.7,
        "raw_score": 0.73,
        "threshold": 0.61,
        "detector_version": "iforest-v2-time-window",
        "detection_method": "isolation_forest",
        "protocol_id": "protocol-1",
        "protocol_name": "Jupiter",
        "protocol_slug": "jupiter",
    }
    row.update(overrides)
    return row


class AnomaliesApiTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1/anomalies")
        self.client = TestClient(app)

    def request(self, connection, path="/api/v1/anomalies"):
        async def get_pool():
            return FakePool(connection)

        with patch("app.routers.anomalies.get_pool", get_pool):
            return self.client.get(path)

    def test_returns_persisted_anomalies_and_pagination(self):
        connection = FakeConnection(rows=[anomaly_row()], total=1)

        response = self.request(connection)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "data": [{
                "id": "anomaly-1", "type": "TX_SPIKE", "severity": "HIGH",
                "description": "Transaction activity spiked.",
                "detected_at": "2026-10-17T12:00:00Z",
                "protocol": {"id": "protocol-1", "name": "Jupiter", "slug": "jupiter"},
                "calibrated_score": 99.7, "raw_score": 0.73, "threshold": 0.61,
                "detector": {"version": "iforest-v2-time-window", "method": "isolation_forest"},
            }],
            "pagination": {"limit": 50, "offset": 0, "total": 1},
        })
        self.assertIn("JOIN protocols AS p", connection.fetch_args[0])
        self.assertEqual(connection.fetch_args[1], (50, 0))

    def test_returns_empty_results(self):
        response = self.request(FakeConnection())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "data": [], "pagination": {"limit": 50, "offset": 0, "total": 0}
        })

    def test_applies_pagination(self):
        connection = FakeConnection(rows=[anomaly_row()], total=7)

        response = self.request(connection, "/api/v1/anomalies?limit=2&offset=4")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pagination"], {"limit": 2, "offset": 4, "total": 7})
        self.assertEqual(connection.fetch_args[1], (2, 4))

    def test_applies_protocol_and_severity_filters(self):
        connection = FakeConnection(rows=[anomaly_row()], total=1)

        response = self.request(
            connection,
            "/api/v1/anomalies?protocol_slug=jupiter&severity=HIGH&limit=2&offset=4",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("p.slug = $1", connection.fetch_args[0])
        self.assertIn('a.severity = $2::"RiskLevel"', connection.fetch_args[0])
        self.assertEqual(connection.fetch_args[1], ("jupiter", "HIGH", 2, 4))
        self.assertEqual(connection.fetchval_args[1], ("jupiter", "HIGH"))

    def test_rejects_invalid_query_parameters(self):
        for query in ("limit=0", "limit=101", "offset=-1", "severity=UNKNOWN"):
            with self.subTest(query=query):
                response = self.client.get(f"/api/v1/anomalies?{query}")
                self.assertEqual(response.status_code, 422)

    def test_returns_generic_error_when_database_query_fails(self):
        response = self.request(FakeConnection(error=RuntimeError("database unavailable")))

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Unable to retrieve anomalies"})
