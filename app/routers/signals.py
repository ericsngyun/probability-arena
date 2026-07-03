"""Opportunity signal endpoints: list, inspect, and review-status updates.
Signals are informational only — this is a review workflow, not an execution
queue. No EV, no sizing, no trade directives."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import OpportunitySignal
from app.schemas import OpportunitySignalOut, SignalReport, SignalStatusUpdate
from app.services.signal_workflow import (
    ALL_STATUSES,
    PromotionNotAllowedError,
    SignalNotFoundError,
    SignalProcessingService,
    SignalPromotionService,
    build_signal_report,
)

SIGNAL_STATUSES = ALL_STATUSES

router = APIRouter(prefix="/signals", tags=["signals"])


# NOTE: literal paths (/recent, /report, /process-promoted) are declared
# before /{signal_id} so they don't get captured by the int path param.


@router.get("/recent", response_model=list[OpportunitySignalOut])
async def recent_signals(
    limit: int = Query(default=20, ge=1, le=200),
    signal_status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[OpportunitySignalOut]:
    """Recent signals, newest first (optionally filtered by status)."""
    if signal_status is not None and signal_status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail=f"signal_status must be one of {ALL_STATUSES}")
    rows = SignalPromotionService().list_recent(db, limit=limit, signal_status=signal_status)
    return [OpportunitySignalOut.model_validate(row) for row in rows]


@router.get("/report", response_model=SignalReport)
async def signal_report(db: Session = Depends(get_db)) -> SignalReport:
    """Aggregate signal-workflow view: counts by status/type, backlog,
    errors, and recently refreshed forecasts. Informational only."""
    return build_signal_report(db)


@router.post("/process-promoted", response_model=list[OpportunitySignalOut])
async def process_promoted_signals(
    limit: int = Query(default=5, ge=1, le=50),
    db: Session = Depends(get_db),
) -> list[OpportunitySignalOut]:
    """Refresh enrichment/assessment/research/forecast for promoted signals
    (oldest promotion first; previously-failed signals are skipped). Uses
    template services unless the ENABLE_* env flags are true."""
    processed = await SignalProcessingService().process_promoted(db, limit=limit)
    return [OpportunitySignalOut.model_validate(row) for row in processed]


@router.post("/{signal_id}/promote", response_model=OpportunitySignalOut)
async def promote_signal(signal_id: int, db: Session = Depends(get_db)) -> OpportunitySignalOut:
    """Promote one 'new' signal to promoted_to_research. Idempotent for
    already-promoted signals; 409 for dismissed/reviewed/processed ones."""
    try:
        row = SignalPromotionService().promote(db, signal_id)
    except SignalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PromotionNotAllowedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return OpportunitySignalOut.model_validate(row)


@router.get("", response_model=list[OpportunitySignalOut])
async def list_signals(
    signal_status: str | None = Query(default=None),
    signal_type: str | None = Query(default=None),
    market_ticker: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[OpportunitySignalOut]:
    """Recent signals, newest first, with optional filters."""
    if signal_status is not None and signal_status not in SIGNAL_STATUSES:
        raise HTTPException(status_code=422, detail=f"signal_status must be one of {SIGNAL_STATUSES}")
    query = select(OpportunitySignal).order_by(OpportunitySignal.id.desc()).limit(limit)
    if signal_status:
        query = query.where(OpportunitySignal.signal_status == signal_status)
    if signal_type:
        query = query.where(OpportunitySignal.signal_type == signal_type)
    if market_ticker:
        query = query.where(OpportunitySignal.market_ticker == market_ticker)
    rows = db.execute(query).scalars().all()
    return [OpportunitySignalOut.model_validate(row) for row in rows]


@router.get("/{signal_id}", response_model=OpportunitySignalOut)
async def get_signal(signal_id: int, db: Session = Depends(get_db)) -> OpportunitySignalOut:
    row = db.get(OpportunitySignal, signal_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Signal {signal_id} not found")
    return OpportunitySignalOut.model_validate(row)


@router.patch("/{signal_id}/status", response_model=OpportunitySignalOut)
async def update_signal_status(
    signal_id: int,
    update: SignalStatusUpdate,
    db: Session = Depends(get_db),
) -> OpportunitySignalOut:
    """Move a signal through the review workflow
    (new -> reviewed/dismissed/promoted_to_research)."""
    row = db.get(OpportunitySignal, signal_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Signal {signal_id} not found")
    row.signal_status = update.signal_status
    db.commit()
    return OpportunitySignalOut.model_validate(row)
