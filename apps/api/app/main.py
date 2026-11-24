import asyncio
from app.tasks.ingest import ingest_all_protocols
from app.tasks.anomaly_detection import anomaly_detector_loop
from app.tasks.security_collector import security_collector_loop
from app.tasks.defillama_refresh import defillama_refresh_loop
from app.config import settings
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from app.config import settings
from app.db import close_pool
from app.log_redaction import install_log_redaction
from app.routers import anomalies
from app.routers import history
from app.routers import intelligence
from app.routers import protocols
from app.routers import security
from app.routers import telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx logs every request URL at INFO; Helius and Telegram URLs embed credentials.
logging.getLogger("httpx").setLevel(logging.WARNING)
# Defence in depth: scrub credentials from every emitted record, tracebacks included.
install_log_redaction()
logger = logging.getLogger(__name__)


async def _ingestion_loop():
    await asyncio.sleep(settings.INGEST_INTERVAL_SECONDS)
    while True:
        try:
            count = await ingest_all_protocols()
            logger.info(f"Ingestion cycle complete: {count} snapshots written")
        except Exception:
            logger.exception("Ingestion cycle failed")
        await asyncio.sleep(settings.INGEST_INTERVAL_SECONDS)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 SolanaGuard API starting up")
    tasks = [
        asyncio.create_task(_ingestion_loop(), name="ingestion-loop"),
        asyncio.create_task(anomaly_detector_loop(), name="anomaly-detector-loop"),
    ]
    if settings.SECURITY_COLLECTOR_ENABLED:
        tasks.append(asyncio.create_task(security_collector_loop(), name="security-collector-loop"))
    if settings.DEFILLAMA_HISTORY_REFRESH_ENABLED:
        logger.info(
            "DefiLlama history refresh enabled: first cycle after %ss, then every %ss after each cycle finishes",
            settings.DEFILLAMA_HISTORY_REFRESH_STARTUP_DELAY_SECONDS,
            settings.DEFILLAMA_HISTORY_REFRESH_INTERVAL_SECONDS,
        )
        tasks.append(asyncio.create_task(defillama_refresh_loop(), name="defillama-history-refresh-loop"))
    else:
        logger.info("DefiLlama history refresh disabled (DEFILLAMA_HISTORY_REFRESH_ENABLED=false)")
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_pool()
        logger.info("👋 SolanaGuard API shutting down")


app = FastAPI(
    title="SolanaGuard API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS only controls which browser origins may read responses; it is not
# authentication. The web app calls this API from its server and only reads,
# so browsers get GET from the configured origins, without credentials.
# Write endpoints are protected by app/auth.py regardless of origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET"],
)

app.include_router(protocols.router, prefix="/api/v1/protocols", tags=["Protocols"])
app.include_router(security.router, prefix="/api/v1/protocols", tags=["Protocols"])
app.include_router(security.status_router, prefix="/api/v1/security", tags=["Security"])
app.include_router(intelligence.router, prefix="/api/v1/protocols", tags=["Intelligence"])
app.include_router(history.router, prefix="/api/v1/history", tags=["History"])
app.include_router(anomalies.router, prefix="/api/v1/anomalies", tags=["Anomalies"])
app.include_router(telegram.router, prefix="/api/v1/telegram", tags=["Telegram"])

@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy"}
