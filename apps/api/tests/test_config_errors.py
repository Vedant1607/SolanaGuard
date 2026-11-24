"""Settings errors must never expose input values (secrets, connection strings).

A fake sentinel stands in for a secret; it must not appear in the exception
message, its repr, the formatted traceback, logs, or a real interpreter's
startup failure output.
"""

import io
import logging
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")  # import-time settings

from app.config import ConfigurationError, get_settings

API_ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "SENTINEL-not-a-real-secret-7f3a9c"
SENTINEL_URL = f"postgresql://user:{SENTINEL}@db.invalid/{SENTINEL}"


class SanitizedSettingsErrorTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)  # no .env here, so only the patched environment is read
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def load(self, env: dict) -> ConfigurationError:
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ConfigurationError) as caught:
            get_settings()
        return caught.exception

    def assert_clean(self, error: BaseException):
        rendered = "".join(traceback.format_exception(error))
        for text in (str(error), repr(error), rendered):
            self.assertNotIn(SENTINEL, text)
        self.assertNotIn("input_value", rendered)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)  # the raw ValidationError is not kept

    def test_missing_field_names_the_field_without_other_values(self):
        error = self.load({"HELIUS_API_KEY": SENTINEL, "INTERNAL_API_SECRET": SENTINEL,
                           "TELEGRAM_BOT_TOKEN": SENTINEL})
        self.assertIn("DATABASE_URL (missing)", str(error))
        self.assert_clean(error)

    def test_type_errors_name_field_and_type_only(self):
        error = self.load({"DATABASE_URL": SENTINEL_URL, "SECURITY_COLLECTOR_INTERVAL_SECONDS": SENTINEL,
                           "DEBUG": SENTINEL})
        self.assertIn("SECURITY_COLLECTOR_INTERVAL_SECONDS (int_parsing)", str(error))
        self.assertIn("DEBUG (bool_parsing)", str(error))
        self.assert_clean(error)

    def test_unparseable_json_value_is_reported_without_the_value(self):
        error = self.load({"DATABASE_URL": SENTINEL_URL, "ALLOWED_ORIGINS": f'["{SENTINEL}"'})
        self.assertIn("ALLOWED_ORIGINS", str(error))
        self.assert_clean(error)

    def test_logged_exception_does_not_contain_the_sentinel(self):
        error = self.load({"HELIUS_API_KEY": SENTINEL})
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("test_config_errors")
        logger.addHandler(handler)
        try:
            try:
                raise error
            except ConfigurationError:
                logger.exception("startup failed")
        finally:
            logger.removeHandler(handler)
        self.assertIn("DATABASE_URL (missing)", stream.getvalue())
        self.assertNotIn(SENTINEL, stream.getvalue())

    def test_production_checks_still_fail_startup(self):
        error = self.load({"DATABASE_URL": SENTINEL_URL, "ENVIRONMENT": "production", "DEBUG": "true",
                           "ALLOWED_ORIGINS": '["https://example.test"]', "INTERNAL_API_SECRET": "short"})
        self.assertIn("DEBUG must be false", str(error))
        self.assertIn("INTERNAL_API_SECRET must be at least 32 characters", str(error))
        self.assertNotIn(SENTINEL, str(error))

    def test_valid_settings_still_load(self):
        with patch.dict(os.environ, {"DATABASE_URL": SENTINEL_URL}, clear=True):
            self.assertEqual(get_settings().DATABASE_URL, SENTINEL_URL)

    def test_interpreter_startup_failure_output_is_clean(self):
        """A real process importing app.config (as the API and cron do) exits non-zero without the value."""
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(API_ROOT), "PYTHONDONTWRITEBYTECODE": "1",
               "HELIUS_API_KEY": SENTINEL, "SECURITY_COLLECTOR_INTERVAL_SECONDS": SENTINEL}
        result = subprocess.run([sys.executable, "-c", "import app.config"], cwd=self._tmp.name, env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ConfigurationError", result.stderr)
        self.assertIn("DATABASE_URL (missing)", result.stderr)
        self.assertNotIn(SENTINEL, result.stdout + result.stderr)
        self.assertNotIn("input_value", result.stderr)


if __name__ == "__main__":
    unittest.main()
