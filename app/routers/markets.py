import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.schemas import CandidateOut, CandidatesResponse, ScoreComponents
from app.services import cache
from app.services.scanner import run_scan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/markets", tags=["markets"])


@router.get("/candidates", response_model=CandidatesResponse)
async def get_candidates(
    limit: int | None = Query(default=None, ge=1, le=200),
    db: Session = Depends(get_db),
) -> CandidatesResponse:
    """Rank currently active Kalshi markets and return the top candidates.

    Runs a scan (fetch -> rank -> persist) unless a recent result is cached.
    Read-only market intelligence; never places orders.
    """
    settings = get_settings()
    limit = limit or settings.candidates_default_limit

    cache_key = f"{cache.CANDIDATES_KEY}:{limit}"
    cached = cache.get_cached(cache_key)
    if cached:
        response = CandidatesResponse.model_validate_json(cached)
        response.cached = True
        return response

    try:
        run, ranked = await run_scan(db, max_markets=settings.scanner_max_markets)
    except httpx.HTTPError as exc:
        logger.exception("Kalshi fetch failed")
        raise HTTPException(status_code=502, detail=f"Kalshi API unavailable: {exc}") from exc

    candidates = [
        CandidateOut(
            ticker=item.market.ticker,
            title=item.market.title,
            status=item.market.status,
            yes_bid=item.market.yes_bid,
            yes_ask=item.market.yes_ask,
            spread=item.market.spread,
            volume_24h=item.market.volume_24h,
            open_interest=item.market.open_interest,
            liquidity=item.market.liquidity,
            close_time=item.market.close_time,
            score=item.score,
            components=ScoreComponents.model_validate(item.components),
        )
        for item in ranked[:limit]
    ]
    response = CandidatesResponse(
        scanner_run_id=run.id,
        as_of=datetime.now(timezone.utc),
        cached=False,
        candidates=candidates,
    )
    cache.set_cached(cache_key, response.model_dump_json(), settings.candidates_cache_ttl_seconds)
    return response
