"""Edge precheck endpoints (MVP-005A): read-only views over the gap
MEASUREMENT rows plus a flag-gated run trigger. Nothing here produces
advice: no dollar EV, no sides, no sizes, no orders, no execution —
paper_candidate_later is a review label with zero attached behavior."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import EdgePrecheckSnapshot
from app.schemas import EdgePrecheckReport, EdgePrecheckSnapshotOut
from app.services.edge_precheck import (
    ALL_STATUSES,
    EdgePrecheckReportService,
    EdgePrecheckService,
)

router = APIRouter(prefix="/edge-precheck", tags=["edge-precheck"])


@router.get("/snapshots", response_model=list[EdgePrecheckSnapshotOut])
async def list_snapshots(
    limit: int = Query(default=20, ge=1, le=200),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[EdgePrecheckSnapshotOut]:
    """Recent gap measurements, newest first (optionally by status)."""
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {ALL_STATUSES}")
    query = (
        select(EdgePrecheckSnapshot).order_by(EdgePrecheckSnapshot.id.desc()).limit(limit)
    )
    if status:
        query = query.where(EdgePrecheckSnapshot.status == status)
    rows = db.execute(query).scalars().all()
    return [EdgePrecheckSnapshotOut.model_validate(row) for row in rows]


@router.get("/report", response_model=EdgePrecheckReport)
async def edge_precheck_report(db: Session = Depends(get_db)) -> EdgePrecheckReport:
    """Aggregate measurement report (statuses, cohorts, gap stats)."""
    return EdgePrecheckReportService().build(db)


@router.post("/run", response_model=list[EdgePrecheckSnapshotOut])
async def run_edge_precheck(
    limit: int = Query(default=50, ge=1, le=200),
    force_readonly: bool = Query(
        default=False,
        description="Run despite ENABLE_EDGE_PRECHECK=false; still creates "
        "measurement rows only",
    ),
    db: Session = Depends(get_db),
) -> list[EdgePrecheckSnapshotOut]:
    """Create measurement snapshots for recent forecasts. Disabled unless
    ENABLE_EDGE_PRECHECK=true or force_readonly=true is passed explicitly.
    Only measurement rows are created either way."""
    if not get_settings().enable_edge_precheck and not force_readonly:
        raise HTTPException(
            status_code=409,
            detail="ENABLE_EDGE_PRECHECK=false; pass force_readonly=true for a "
            "one-off measurement pass (still read-only)",
        )
    snapshots = EdgePrecheckService().run_batch(db, limit=limit)
    return [EdgePrecheckSnapshotOut.model_validate(row) for row in snapshots]
