"""Probability Arena — MVP-001: Kalshi read-only market intelligence.

Safety: this service reads public market data and stores snapshots. It has no
trading, order placement, or account-mutation code paths.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import init_db
from app.routers.markets import router as markets_router
from app.services.ws_snapshots import WsSnapshotService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ws_service = WsSnapshotService(get_settings())
    ws_service.start()
    app.state.ws_service = ws_service
    yield
    await ws_service.stop()


app = FastAPI(
    title="Probability Arena",
    description="Read-only Kalshi market intelligence. No trading capability.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(markets_router)


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"status": "ok", "ws_enabled": get_settings().ws_enabled}
