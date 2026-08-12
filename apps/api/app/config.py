from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import List


class Settings(BaseSettings):
    DEBUG: bool = True
    ALLOWED_ORIGINS: List[str] = ["http://localhost:3000"]

    DATABASE_URL: str
    HELIUS_API_KEY: str = ""
    HELIUS_RPC_URL: str = "https://mainnet.helius-rpc.com/?api-key="
    INGEST_INTERVAL_SECONDS: int = 300  # 5 min MVP cadence — tune later

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()