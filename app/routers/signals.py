"""Opportunity signal endpoints: list, inspect, and review-status updates.
Signals are informational only — this is a review workflow, not an execution
queue. No EV, no sizing, no trade directives."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import OpportunitySignal
from app.schemas import OpportunitySignalOut, SignalStatusUpdate
from app.services.watcher import SIGNAL_STATUSES

router = APIRouter(prefix="/signals", tags=["signals"])


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
