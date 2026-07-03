"""Read-only calibration endpoints: forecast scores and aggregate summaries.
No EV, no sizing, no trade metrics."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ForecastScoreRecord, MarketForecastRecord
from app.schemas import CalibrationSummary, ForecastScoreOut, ForecasterComparisonSummary
from app.services.calibration import CalibrationService
from app.services.champion_challenger import (
    DEFAULT_BASELINE,
    DEFAULT_CHALLENGER,
    ChampionChallengerService,
)

router = APIRouter(tags=["calibration"])


@router.get("/forecasts/scores", response_model=list[ForecastScoreOut])
async def list_forecast_scores(
    score_status: str | None = Query(default=None, pattern="^(scored|pending_outcome|unscorable)$"),
    market_ticker: str | None = Query(default=None),
    forecaster_name: str | None = Query(default=None),
    evidence_depth: str | None = Query(
        default=None, pattern="^(template_only|source_backed|mixed)$"
    ),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[ForecastScoreOut]:
    """Recent forecast scores, newest first, with optional filters."""
    query = (
        select(ForecastScoreRecord)
        .join(MarketForecastRecord, ForecastScoreRecord.forecast_id == MarketForecastRecord.id)
        .order_by(ForecastScoreRecord.id.desc())
        .limit(limit)
    )
    if score_status:
        query = query.where(ForecastScoreRecord.score_status == score_status)
    if market_ticker:
        query = query.where(ForecastScoreRecord.market_ticker == market_ticker)
    if forecaster_name:
        query = query.where(MarketForecastRecord.forecaster_name == forecaster_name)
    if evidence_depth:
        query = query.where(MarketForecastRecord.evidence_depth == evidence_depth)
    rows = db.execute(query).scalars().all()
    return [ForecastScoreOut.model_validate(row) for row in rows]


@router.get("/calibration/summary", response_model=CalibrationSummary)
async def calibration_summary(db: Session = Depends(get_db)) -> CalibrationSummary:
    """Aggregate Brier / log-loss / absolute-error over the latest score per
    forecast, grouped by evidence depth, risk, forecaster, domain, and tag.
    For head-to-head forecaster comparison see /calibration/champion-challenger."""
    return CalibrationService().summary(db)


@router.get("/calibration/champion-challenger", response_model=ForecasterComparisonSummary)
async def champion_challenger(
    baseline_forecaster: str = Query(default=DEFAULT_BASELINE),
    challenger_forecaster: str = Query(default=DEFAULT_CHALLENGER),
    domain: str | None = Query(default=None),
    market_type: str | None = Query(default=None),
    signal_type: str | None = Query(default=None),
    min_created_at: datetime | None = Query(default=None),
    max_created_at: datetime | None = Query(default=None),
    paired_only: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> ForecasterComparisonSummary:
    """Champion/challenger comparison over resolved forecasts. Read-only
    measurement (no EV, no trade semantics): delta < 0 favors the challenger,
    paired beats unpaired, and the warning field gates interpretation on
    sample size."""
    return ChampionChallengerService().compare(
        db,
        baseline=baseline_forecaster,
        challenger=challenger_forecaster,
        domain=domain,
        market_type=market_type,
        signal_type=signal_type,
        min_created_at=min_created_at,
        max_created_at=max_created_at,
        paired_only=paired_only,
    )
