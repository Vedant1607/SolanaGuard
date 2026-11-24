from pydantic_settings import BaseSettings, SettingsError
from pydantic import Field, ValidationError
from functools import lru_cache
from typing import List, Literal
from urllib.parse import urlsplit

# Shortest INTERNAL_API_SECRET accepted in production (e.g. `openssl rand -hex 32` gives 64).
MIN_INTERNAL_SECRET_LENGTH = 32
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}


class Settings(BaseSettings):
    # "production" turns on the strict checks in ``production_config_problems``. The
    # default is "development" so that tooling and tests work without a full
    # deployment config; the defaults below are still locked down (no debug,
    # localhost-only CORS, internal endpoints fail closed without a secret).
    ENVIRONMENT: Literal["development", "production"] = "development"
    DEBUG: bool = False
    # Browser origins allowed by CORS. The Next.js app calls this API from its
    # server, so production normally needs only the web app's own origin(s).
    # CORS is not authentication: write endpoints are protected separately.
    ALLOWED_ORIGINS: List[str] = ["http://localhost:3000"]
    # Shared secret for internal write endpoints (header X-Internal-Api-Secret,
    # see app/auth.py). Empty => those endpoints answer 503 (fail closed).
    INTERNAL_API_SECRET: str = ""

    DATABASE_URL: str
    HELIUS_API_KEY: str = ""
    HELIUS_RPC_URL: str = "https://mainnet.helius-rpc.com/?api-key="
    # Upper bound on getSignaturesForAddress pages (1,000 signatures each) per
    # protocol per ingestion cycle. If the 24-hour boundary is not reached within
    # this many pages, the count is reported as unavailable rather than partial.
    HELIUS_MAX_SIGNATURE_PAGES: int = Field(default=10, gt=0)
    INGEST_INTERVAL_SECONDS: int = 300  # 5 min MVP cadence — tune later
    ANOMALY_DETECTOR_INTERVAL_SECONDS: int = Field(  # 15 min detector cadence
        default=900, gt=0
    )
    # JSON-RPC endpoint for on-chain security reads. Empty => the public
    # mainnet endpoint in DEBUG only; non-debug deployments must set a
    # reliable provider here or the security collector stays idle.
    SOLANA_RPC_URL: str = ""
    SECURITY_COLLECTOR_ENABLED: bool = True
    SECURITY_COLLECTOR_INTERVAL_SECONDS: int = Field(default=900, gt=0)  # 15 min
    RESEND_API_KEY: str = ""
    # Recurring refresh of DefiLlama daily history (app/tasks/defillama_refresh.py).
    # Local development only: off unless explicitly enabled, and every cycle
    # verifies the connected cluster against the expected identity below
    # (no default for the system identifier, so it fails closed until set).
    DEFILLAMA_HISTORY_REFRESH_ENABLED: bool = False
    DEFILLAMA_HISTORY_REFRESH_INTERVAL_SECONDS: int = Field(default=21_600, gt=0)  # 6 h after a cycle finishes
    DEFILLAMA_HISTORY_REFRESH_STARTUP_DELAY_SECONDS: int = Field(default=60, ge=0)
    DEFILLAMA_HISTORY_REFRESH_EXPECTED_DATABASE: str = "solanaguard"
    DEFILLAMA_HISTORY_REFRESH_EXPECTED_SYSTEM_IDENTIFIER: str = ""
    ALERT_FROM_EMAIL: str = "SolanaGuard Alerts <onboarding@resend.dev>"
    TELEGRAM_BOT_TOKEN: str = ""
    # Required for /api/v1/telegram/webhook; empty => every webhook call is rejected.
    TELEGRAM_WEBHOOK_SECRET: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = True


class ConfigurationError(RuntimeError):
    """Invalid deployment configuration. Messages name settings, never their secret values."""


def production_config_problems(s: Settings) -> list[str]:
    """Checks applied when ENVIRONMENT=production; empty when the configuration is acceptable."""
    if s.ENVIRONMENT != "production":
        return []
    problems = []
    if s.DEBUG:
        problems.append("DEBUG must be false")
    if "ALLOWED_ORIGINS" not in s.model_fields_set or not s.ALLOWED_ORIGINS:
        problems.append("ALLOWED_ORIGINS must be set explicitly")
    for origin in s.ALLOWED_ORIGINS:
        parts = urlsplit(origin)
        if origin == "*" or parts.scheme != "https" or not parts.hostname or parts.hostname in LOCAL_HOSTS \
                or parts.path or parts.query or parts.fragment:
            problems.append(f"ALLOWED_ORIGINS entry {origin!r} is not an https origin")  # origins are public
    if s.INTERNAL_API_SECRET and len(s.INTERNAL_API_SECRET) < MIN_INTERNAL_SECRET_LENGTH:
        problems.append(f"INTERNAL_API_SECRET must be at least {MIN_INTERNAL_SECRET_LENGTH} characters")
    if s.DEFILLAMA_HISTORY_REFRESH_ENABLED:
        problems.append("DEFILLAMA_HISTORY_REFRESH_ENABLED must be false (the hosted refresh runs as a cron job)")
    return problems


def validate_settings(s: Settings) -> Settings:
    problems = production_config_problems(s)
    if problems:
        raise ConfigurationError("Invalid production configuration: " + "; ".join(problems))
    return s


def describe_settings_error(error: Exception) -> str:
    """Field names and error types only, never input values.

    Pydantic's ``str(ValidationError)`` embeds ``input_value=...`` (the raw
    environment, including secrets and connection strings), and SettingsError
    chains the parser exception that saw the value. Neither is ever formatted.
    """
    if isinstance(error, ValidationError):
        details = sorted({
            f"{'.'.join(str(part) for part in item['loc']) or '<settings>'} ({item['type']})"
            for item in error.errors(include_url=False, include_context=False, include_input=False)
        })
        return "Invalid settings: " + "; ".join(details)
    # pydantic-settings' own message names only the field and the source.
    return f"Invalid settings: {error}"


@lru_cache
def get_settings() -> Settings:
    try:
        loaded = Settings()
    except (ValidationError, SettingsError) as error:
        message = describe_settings_error(error)
    else:
        return validate_settings(loaded)
    # Raised outside the except block so the original error (and its input
    # values) is neither chained as __context__ nor printed in the traceback.
    raise ConfigurationError(message)


settings = get_settings()
