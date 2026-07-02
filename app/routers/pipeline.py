"""Read-only pipeline audit endpoints: list runs and inspect stage records."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import PipelineRun
from app.schemas import PipelineRunDetailOut, PipelineRunOut

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.get("/runs", response_model=list[PipelineRunOut])
async def list_pipeline_runs(
    limit: int = Query(default=20, ge=1, le=200),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[PipelineRunOut]:
    """Recent pipeline runs, newest first (stage details via the id endpoint)."""
    query = select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)
    if status:
        query = query.where(PipelineRun.status == status)
    rows = db.execute(query).scalars().all()
    return [PipelineRunOut.model_validate(row) for row in rows]


@router.get("/runs/{run_id}", response_model=PipelineRunDetailOut)
async def get_pipeline_run(run_id: int, db: Session = Depends(get_db)) -> PipelineRunDetailOut:
    """One pipeline run with its per-stage audit records."""
    row = db.get(PipelineRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Pipeline run {run_id} not found")
    return PipelineRunDetailOut.model_validate(row)
