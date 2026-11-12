from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache
from typing import List


class Settings(BaseSettings):
    DEBUG: bool = True
    ALLOWED_ORIGINS: List[str] = ["http://localhost:3000"]

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
    ALERT_FROM_EMAIL: str = "SolanaGuard Alerts <onboarding@resend.dev>"
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""
    
    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
