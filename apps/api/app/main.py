import asyncio
from app.tasks.ingest import ingest_all_protocols
from app.config import settings
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from app.config import settings
from app.db import close_pool
from app.routers import protocols

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
    task = asyncio.create_task(_ingestion_loop())
    yield
    task.cancel()
    await close_pool()
    logger.info("👋 SolanaGuard API shutting down")


app = FastAPI(
    title="SolanaGuard API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(protocols.router, prefix="/api/v1/protocols", tags=["Protocols"])


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy"}