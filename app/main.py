"""Probability Arena — MVP-001: Kalshi read-only market intelligence.

Safety: this service reads public market data and stores snapshots. It has no
trading, order placement, or account-mutation code paths.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import run_migrations
from app.routers.calibration import router as calibration_router
from app.routers.crypto import router as crypto_router
from app.routers.edge_precheck import router as edge_precheck_router
from app.routers.eval import router as eval_router
from app.routers.marketops import router as marketops_router
from app.routers.markets import router as markets_router
from app.routers.pipeline import router as pipeline_router
from app.routers.signals import router as signals_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Migrations only.

    The Kalshi WebSocket snapshot service was removed in
    KALSHI-OBSERVER-PREAUTH-HARDENING-001. It carried the repository's second
    RSA-PSS signing implementation — one that accepted an arbitrary URL and
    performed no credential-file confinement — and it had no consumer: no
    systemd unit starts this API, `orderbook_snapshots` held zero rows, and
    nothing read that table. Two signing implementations with different
    security contracts is one more than the boundary permits.
    """
    run_migrations()
    yield


app = FastAPI(
    title="Probability Arena",
    description="Read-only Kalshi market intelligence. No trading capability.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(markets_router)
app.include_router(calibration_router)
app.include_router(pipeline_router)
app.include_router(signals_router)
app.include_router(crypto_router)
app.include_router(marketops_router)
app.include_router(edge_precheck_router)
app.include_router(eval_router)


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"status": "ok",
            "observer_credential_configured":
                get_settings().observer_credential_configured}
