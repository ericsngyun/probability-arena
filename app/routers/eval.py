"""Frontier evaluation endpoint (EVAL-001): the full-desk measurement
report. Evaluation only — no EV, no trades, no positions; readiness labels
never authorize live capital."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import FrontierEvalReport
from app.services.frontier_eval import FrontierEvalService

router = APIRouter(prefix="/eval", tags=["eval"])


@router.get("/frontier-report", response_model=FrontierEvalReport)
async def frontier_report(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    domain: list[str] | None = Query(default=None),
    include_crypto: bool = Query(default=True),
    include_safety: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> FrontierEvalReport:
    """Signal/forecast/edge/microstructure/crypto/latency quality + safety
    audit + conservative readiness scorecard over the window."""
    return FrontierEvalService().build(
        db,
        hours=hours,
        domains=domain,
        include_crypto=include_crypto,
        include_safety=include_safety,
    )
