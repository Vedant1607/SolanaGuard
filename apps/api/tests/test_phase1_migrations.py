"""Static checks on the Phase 1 migrations and seed, plus an opt-in DB test.

The static tests never touch a database. The integration test runs only when
SOLANAGUARD_TEST_DATABASE_URL points at a DISPOSABLE database that already has
all migrations applied; every statement runs in a transaction that is rolled
back.
"""

import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from app.solana.base58 import is_valid_pubkey
from tests.solana_fixtures import load_recorded

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "packages" / "database" / "prisma" / "migrations"
SCHEMA_MIGRATION = MIGRATIONS / "20261112120000_phase1_registry_observations" / "migration.sql"
DATA_MIGRATION = MIGRATIONS / "20261112120100_phase1_catalogue_data" / "migration.sql"
SEED = ROOT / "packages" / "database" / "prisma" / "seed.ts"

DESTRUCTIVE = [
    r"\bDROP\b",
    r"\bTRUNCATE\b",
    r"\bDELETE\s+FROM\b",
    r"\bALTER\s+COLUMN\b[^;]*\bTYPE\b",
    r"\bRENAME\b",
    r"\bSET\s+NOT\s+NULL\b",
    r"\bON\s+DELETE\s+CASCADE\b",
]


def sql_without_comments(path: Path) -> str:
    return "\n".join(line.split("--", 1)[0] for line in path.read_text().splitlines())


def registry_rows(sql: str):
    return re.findall(
        r"SELECT gen_random_uuid\(\)::text, p\.\"id\", '(\w+)', '(\w+)'::\"AddressKind\", '([^']+)', (true|false),\s+"
        r"'(\w+)', 'VERIFIED'::\"AddressVerification\", TIMESTAMP '[^']+',\s+'[^']+', (true|false)\s+FROM \"protocols\" p WHERE p\.\"slug\" = '(\w+)'",
        sql,
    )


class MigrationSafetyTests(unittest.TestCase):
    def test_no_destructive_statements(self):
        for path in (SCHEMA_MIGRATION, DATA_MIGRATION):
            sql = sql_without_comments(path)
            for pattern in DESTRUCTIVE:
                with self.subTest(file=path.parent.name, pattern=pattern):
                    self.assertIsNone(re.search(pattern, sql, re.IGNORECASE))

    def test_schema_migration_is_additive(self):
        sql = sql_without_comments(SCHEMA_MIGRATION)
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        allowed = ("CREATE TYPE", "CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX", "ALTER TYPE", "ALTER TABLE")
        for statement in statements:
            with self.subTest(statement=statement[:60]):
                self.assertTrue(statement.startswith(allowed))
                if statement.startswith("ALTER TYPE"):
                    self.assertIn("ADD VALUE IF NOT EXISTS 'PERPS'", statement)
                if statement.startswith("ALTER TABLE"):
                    self.assertRegex(statement, r"^ALTER TABLE \"\w+\" ADD (COLUMN|CONSTRAINT)")
        self.assertIn('"supportLevel" "SupportLevel" NOT NULL DEFAULT \'PARTIAL\'', sql)

    def test_new_enum_value_is_not_used_in_the_migration_that_adds_it(self):
        sql = sql_without_comments(SCHEMA_MIGRATION)
        self.assertEqual(sql.count("PERPS"), 1)

    def test_integrity_constraints_present(self):
        sql = sql_without_comments(SCHEMA_MIGRATION)
        self.assertIn(
            'FOREIGN KEY ("addressId", "protocolId") REFERENCES "protocol_addresses"("id", "protocolId") '
            "ON DELETE RESTRICT",
            sql,
        )
        self.assertIn('UNIQUE INDEX "protocol_addresses_id_protocolId_key"', sql)
        self.assertEqual(sql.count("ON DELETE RESTRICT"), 3)
        for name in (
            "metric_observations_value_matches_status_chk",
            "metric_observations_error_has_code_chk",
            "protocol_addresses_address_base58_chk",
        ):
            self.assertIn(name, sql)

    def test_data_migration_updates_are_guarded(self):
        sql = sql_without_comments(DATA_MIGRATION)
        updates = re.findall(r"UPDATE \"protocols\"[^;]+;", sql)
        self.assertEqual(len(updates), 3)
        self.assertIn("\"isActive\" = false\nWHERE \"slug\" = 'marginfi'", updates[0])
        for update in updates[1:]:
            self.assertIn('AND "programId" = ', update)  # only replaces the known-invalid value
            self.assertIn("NOT EXISTS", update)
        self.assertNotIn("UPDATE \"protocol_snapshots\"", sql)
        self.assertNotIn("UPDATE \"risk_scores\"", sql)
        self.assertNotIn("UPDATE \"anomalies\"", sql)
        self.assertIn("ON CONFLICT DO NOTHING", sql)
        self.assertEqual(sql.count('ON CONFLICT ("protocolId", "address") DO NOTHING'), 14)

    def test_registry_contents(self):
        rows = registry_rows(DATA_MIGRATION.read_text())
        self.assertEqual(len(rows), 14)
        by_label = {r[2]: r for r in rows}
        for address, kind, label, shared, owner, active, slug in rows:
            with self.subTest(label=label):
                self.assertTrue(is_valid_pubkey(address))
                self.assertTrue(is_valid_pubkey(owner))
        # Jito: the stake pool is an account owned by the SPL Stake Pool program,
        # which is registered separately as a shared PROGRAM dependency.
        self.assertEqual(by_label["Jito stake pool"][1], "STAKE_POOL")
        self.assertEqual(by_label["Jito stake pool"][4], "SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy")
        self.assertEqual(by_label["SPL Stake Pool program"][1:4], ("PROGRAM", "SPL Stake Pool program", "true"))
        raydium = sorted(r[2] for r in rows if r[6] == "raydium")
        self.assertEqual(raydium, ["Raydium AMM v4", "Raydium CLMM", "Raydium CPMM"])
        self.assertEqual(by_label["Drift v2"][6], "drift")
        self.assertEqual(by_label["Marginfi v2"][5], "false")
        self.assertEqual([r[2] for r in rows if r[5] == "false"], ["Marginfi v2"])

    def test_registry_matches_recorded_mainnet_owners(self):
        owners = {
            e["address"]: e["response"]["result"]["value"]["owner"]
            for e in load_recorded()["exchanges"]
            if e["response"].get("result", {}).get("value")
        }
        for address, kind, label, _shared, owner, _active, _slug in registry_rows(DATA_MIGRATION.read_text()):
            with self.subTest(label=label):
                self.assertEqual(owners[address], owner)

    def test_seed_matches_data_migration(self):
        seed = SEED.read_text()
        for address, kind, label, shared, owner, active, slug in registry_rows(DATA_MIGRATION.read_text()):
            with self.subTest(label=label):
                self.assertIn(
                    f'{{ slug: "{slug}", address: "{address}", kind: "{kind}" as const, label: "{label}", '
                    f'isSharedDependency: {shared}, expectedOwner: "{owner}", isActive: {active},',
                    seed,
                )
        self.assertNotIn("KLend2g3cP87fffoy8q1mQqGKjrL9jnmujyAASHa3tsF", seed)
        self.assertNotIn("MFv2hWf31Z9kbCa1snEPdcgp168vLLAZnkTHsMgGgAB", seed)
        self.assertRegex(seed, r'slug: "marginfi".*isActive: false')
        self.assertRegex(seed, r'slug: "drift",\s+name: "Drift",\s+category: "PERPS"')


@unittest.skipUnless(
    os.environ.get("SOLANAGUARD_TEST_DATABASE_URL"),
    "set SOLANAGUARD_TEST_DATABASE_URL to a disposable, fully migrated database to run",
)
class ConstraintIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg

        self.asyncpg = asyncpg
        self.conn = await asyncpg.connect(os.environ["SOLANAGUARD_TEST_DATABASE_URL"])
        self.tx = self.conn.transaction()
        await self.tx.start()
        self.p1 = await self.conn.fetchval("INSERT INTO protocols (id, slug, name, category, \"programId\") VALUES ('t-p1','t-p1','T1','DEX','11111111111111111111111111111112') RETURNING id")
        self.p2 = await self.conn.fetchval("INSERT INTO protocols (id, slug, name, category, \"programId\") VALUES ('t-p2','t-p2','T2','DEX','11111111111111111111111111111113') RETURNING id")
        self.a1 = await self.conn.fetchval(
            "INSERT INTO protocol_addresses (id, \"protocolId\", address, kind, label) "
            "VALUES ('t-a1', $1, '11111111111111111111111111111114', 'PROGRAM', 'x') RETURNING id", self.p1)

    async def asyncTearDown(self):
        await self.tx.rollback()
        await self.conn.close()

    async def insert(self, protocol_id, address_id, status, numeric=None, error=None):
        sp = self.conn.transaction()
        await sp.start()
        try:
            await self.conn.execute(
                'INSERT INTO metric_observations (id, "protocolId", "addressId", "metricKey", status, '
                '"valueNumeric", unit, source, "adapterKey", "adapterVersion", "errorCode") '
                "VALUES (gen_random_uuid()::text, $1, $2, 'm', $3::\"ObservationStatus\", $4, 'slot', 's', 'a', '1', $5)",
                protocol_id, address_id, status, numeric, error,
            )
        except Exception:
            await sp.rollback()
            raise
        await sp.commit()

    async def test_address_must_belong_to_the_same_protocol(self):
        await self.insert(self.p1, self.a1, "OK", 1)
        await self.insert(self.p2, None, "OK", 1)  # protocol-level metric
        with self.assertRaises(self.asyncpg.exceptions.ForeignKeyViolationError):
            await self.insert(self.p2, self.a1, "OK", 1)

    async def test_status_value_checks(self):
        with self.assertRaises(self.asyncpg.exceptions.CheckViolationError):
            await self.insert(self.p1, self.a1, "MISSING", 0)
        with self.assertRaises(self.asyncpg.exceptions.CheckViolationError):
            await self.insert(self.p1, self.a1, "ERROR")
        with self.assertRaises(self.asyncpg.exceptions.CheckViolationError):
            await self.insert(self.p1, self.a1, "OK")
        await self.insert(self.p1, self.a1, "ERROR", error="TIMEOUT")

    async def test_protocol_with_registry_rows_cannot_be_deleted(self):
        # ON DELETE RESTRICT raises restrict_violation (23001), not
        # foreign_key_violation (23503) as NO ACTION would.
        with self.assertRaises(self.asyncpg.exceptions.RestrictViolationError):
            await self.conn.execute("DELETE FROM protocols WHERE id = $1", self.p1)


if __name__ == "__main__":
    unittest.main()
