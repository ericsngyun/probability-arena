"""MarketOps Autopilot endpoints (OPS-006): read-only views over the
coordination-audit tables plus alert resolution. No endpoint here (or
anywhere) trades, sizes, orders, or moves money — the autopilot only
sequences existing read-only services."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import MarketOpsRun
from app.schemas import MarketOpsAlertOut, MarketOpsReport, MarketOpsRunOut
from app.services.marketops import MarketOpsAlertService, MarketOpsReportService

router = APIRouter(prefix="/marketops", tags=["marketops"])


@router.get("/runs", response_model=list[MarketOpsRunOut])
async def list_runs(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[MarketOpsRunOut]:
    """Recent autopilot cycles, newest first."""
    rows = db.execute(
        select(MarketOpsRun).order_by(MarketOpsRun.id.desc()).limit(limit)
    ).scalars().all()
    return [MarketOpsRunOut.model_validate(row) for row in rows]


@router.get("/report", response_model=MarketOpsReport)
async def marketops_report(db: Session = Depends(get_db)) -> MarketOpsReport:
    """Aggregate MarketOps report: last run, canary/forecaster/crypto
    snapshots, open alerts, and a recommended operator action."""
    return MarketOpsReportService().build(db)


@router.get("/alerts", response_model=list[MarketOpsAlertOut])
async def list_alerts(
    limit: int = Query(default=20, ge=1, le=200),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[MarketOpsAlertOut]:
    """Recent alerts, newest first (optionally filtered by open/resolved)."""
    if status is not None and status not in ("open", "resolved"):
        raise HTTPException(status_code=422, detail="status must be 'open' or 'resolved'")
    rows = MarketOpsAlertService().list_recent(db, limit=limit, status=status)
    return [MarketOpsAlertOut.model_validate(row) for row in rows]


@router.patch("/alerts/{alert_id}/resolve", response_model=MarketOpsAlertOut)
async def resolve_alert(alert_id: int, db: Session = Depends(get_db)) -> MarketOpsAlertOut:
    """Mark one alert resolved (idempotent)."""
    try:
        alert = MarketOpsAlertService().resolve(db, alert_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return MarketOpsAlertOut.model_validate(alert)


@router.get("/runs/{run_id}", response_model=MarketOpsRunOut)
async def get_run(run_id: int, db: Session = Depends(get_db)) -> MarketOpsRunOut:
    """One autopilot cycle with full config/summary."""
    run = db.get(MarketOpsRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"MarketOps run {run_id} not found")
    return MarketOpsRunOut.model_validate(run)
