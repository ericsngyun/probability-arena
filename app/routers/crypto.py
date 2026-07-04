"""Crypto Arena endpoints (CRYPTO-001): read-only views over the Solana
surveillance tables. Raw provider payloads never serialize here. No wallet,
swap, transaction, order, or execution endpoints exist — signals are
informational telemetry only."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    CryptoOpportunitySignal,
    CryptoPair,
    CryptoToken,
    CryptoTokenRiskAssessment,
)
from app.schemas import (
    CryptoPairOut,
    CryptoReport,
    CryptoRiskAssessmentOut,
    CryptoRiskReport,
    CryptoSignalOut,
    CryptoTokenOut,
)
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


# --- CRYPTO-002: risk endpoints (read-only risk intelligence; a risk level
# is an avoid/flag verdict for review, never a trade recommendation) ---


@router.get("/risk-assessments", response_model=list[CryptoRiskAssessmentOut])
async def list_risk_assessments(
    limit: int = Query(default=20, ge=1, le=200),
    risk_level: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[CryptoRiskAssessmentOut]:
    """Recent risk assessments, newest first (optionally filtered by
    composite level)."""
    query = (
        select(CryptoTokenRiskAssessment)
        .order_by(CryptoTokenRiskAssessment.id.desc())
        .limit(limit)
    )
    if risk_level:
        query = query.where(CryptoTokenRiskAssessment.composite_risk_level == risk_level)
    rows = db.execute(query).scalars().all()
    return [CryptoRiskAssessmentOut.model_validate(row) for row in rows]


@router.get("/risk-report", response_model=CryptoRiskReport)
async def crypto_risk_report(db: Session = Depends(get_db)) -> CryptoRiskReport:
    """Aggregate risk report: engine mode, level breakdown, worst tokens,
    common reasons, provider health, risk-signal counts."""
    from app.services.crypto_risk_engine import CryptoRiskReportService

    return CryptoRiskReportService().build(db)


@router.get("/tokens/{token_address}/risk", response_model=CryptoRiskAssessmentOut)
async def token_risk(token_address: str, db: Session = Depends(get_db)) -> CryptoRiskAssessmentOut:
    """Latest risk assessment for one token; 404 when never assessed."""
    row = db.execute(
        select(CryptoTokenRiskAssessment)
        .where(CryptoTokenRiskAssessment.token_address == token_address)
        .order_by(CryptoTokenRiskAssessment.id.desc())
    ).scalars().first()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"No risk assessment for {token_address!r}"
        )
    return CryptoRiskAssessmentOut.model_validate(row)
