"""Crypto Arena endpoints (CRYPTO-001): read-only views over the Solana
surveillance tables. Raw provider payloads never serialize here. No wallet,
swap, transaction, order, or execution endpoints exist — signals are
informational telemetry only."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import CryptoOpportunitySignal, CryptoPair, CryptoToken
from app.schemas import CryptoPairOut, CryptoReport, CryptoSignalOut, CryptoTokenOut
from app.services.crypto_scout import CryptoReportService

router = APIRouter(prefix="/crypto", tags=["crypto"])


@router.get("/signals", response_model=list[CryptoSignalOut])
async def list_crypto_signals(
    limit: int = Query(default=20, ge=1, le=200),
    signal_type: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[CryptoSignalOut]:
    """Recent crypto signals, newest first (optionally filtered by type)."""
    query = (
        select(CryptoOpportunitySignal)
        .order_by(CryptoOpportunitySignal.id.desc())
        .limit(limit)
    )
    if signal_type:
        query = query.where(CryptoOpportunitySignal.signal_type == signal_type)
    rows = db.execute(query).scalars().all()
    return [CryptoSignalOut.model_validate(row) for row in rows]


@router.get("/tokens", response_model=list[CryptoTokenOut])
async def list_crypto_tokens(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[CryptoTokenOut]:
    """Recently-seen tokens, most recent first."""
    rows = db.execute(
        select(CryptoToken)
        .order_by(CryptoToken.last_seen_at.desc(), CryptoToken.id.desc())
        .limit(limit)
    ).scalars().all()
    return [CryptoTokenOut.model_validate(row) for row in rows]


@router.get("/pairs", response_model=list[CryptoPairOut])
async def list_crypto_pairs(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[CryptoPairOut]:
    """Recently-seen pairs, most recent first."""
    rows = db.execute(
        select(CryptoPair)
        .order_by(CryptoPair.last_seen_at.desc(), CryptoPair.id.desc())
        .limit(limit)
    ).scalars().all()
    return [CryptoPairOut.model_validate(row) for row in rows]


@router.get("/report", response_model=CryptoReport)
async def crypto_report(db: Session = Depends(get_db)) -> CryptoReport:
    """Aggregate crypto surveillance report: totals, signals by type/status,
    risk levels, recent activity, and provider errors."""
    return CryptoReportService().build(db)
