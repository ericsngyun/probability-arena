import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.schemas import CandidateOut, CandidatesResponse, RejectedMarketOut, ScoreComponents
from app.services import cache
from app.services.scanner import ScanResult, run_scan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/markets", tags=["markets"])


def _build_response(result: ScanResult, limit: int, include_rejected: bool) -> CandidatesResponse:
    candidates = []
    for item in result.ranked[:limit]:
        assessment = result.assessment_for(item.market.ticker)
        candidates.append(
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
                is_eligible=True,
                warnings=assessment.warnings if assessment else [],
            )
        )

    rejected = []
    if include_rejected:
        rejected = [
            RejectedMarketOut(
                ticker=market.ticker,
                title=market.title,
                status=market.status,
                rejection_reasons=assessment.rejection_reasons,
                warnings=assessment.warnings,
                yes_bid=market.yes_bid,
                yes_ask=market.yes_ask,
                spread=assessment.spread,
                liquidity=market.liquidity,
                volume_24h=market.volume_24h,
                expiration_days=assessment.expiration_days,
                market_type_flags=assessment.market_type_flags,
            )
            for market, assessment in result.rejected
        ]

    return CandidatesResponse(
        scanner_run_id=result.run.id,
        as_of=datetime.now(timezone.utc),
        cached=False,
        markets_assessed=len(result.assessed),
        eligible_count=len(result.ranked),
        rejected_count=len(result.rejected),
        candidates=candidates,
        rejected=rejected,
    )


@router.get("/candidates", response_model=CandidatesResponse)
async def get_candidates(
    limit: int | None = Query(default=None, ge=1, le=200),
    include_rejected: bool = Query(
        default=False,
        description="Include markets rejected by the eligibility gate, with rejection_reasons",
    ),
    db: Session = Depends(get_db),
) -> CandidatesResponse:
    """Rank currently active Kalshi markets and return the top eligible candidates.

    Markets failing the hygiene gate (no/one-sided quotes, wide spread, low
    liquidity/volume, bad expiration window) are excluded by default; pass
    include_rejected=true to see them with their rejection reasons.
    Read-only market intelligence; never places orders.
    """
    settings = get_settings()
    limit = limit or settings.candidates_default_limit

    cache_key = f"{cache.CANDIDATES_KEY}:{limit}:rejected={int(include_rejected)}"
    cached = cache.get_cached(cache_key)
    if cached:
        response = CandidatesResponse.model_validate_json(cached)
        response.cached = True
        return response

    try:
        result = await run_scan(db, max_markets=settings.scanner_max_markets)
    except httpx.HTTPError as exc:
        logger.exception("Kalshi fetch failed")
        raise HTTPException(status_code=502, detail=f"Kalshi API unavailable: {exc}") from exc

    response = _build_response(result, limit, include_rejected)
    cache.set_cached(cache_key, response.model_dump_json(), settings.candidates_cache_ttl_seconds)
    return response
