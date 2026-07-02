import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Market, MarketResolutionAssessment
from app.schemas import (
    CandidateOut,
    CandidatesResponse,
    MarketData,
    MarketDetailEnrichmentOut,
    RejectedMarketOut,
    ResolutionAssessmentOut,
    ScoreComponents,
)
from app.services import cache
from app.services.enrichment import (
    EnrichmentError,
    MarketDetailEnrichmentService,
    apply_latest_enrichment,
)
from app.services.resolution import get_judge, persist_assessment
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


def _latest_assessments(
    db: Session, tickers: list[str]
) -> dict[str, MarketResolutionAssessment]:
    """Most recent persisted resolution assessment per ticker."""
    if not tickers:
        return {}
    rows = db.execute(
        select(MarketResolutionAssessment)
        .where(MarketResolutionAssessment.market_ticker.in_(tickers))
        .order_by(
            MarketResolutionAssessment.created_at.desc(), MarketResolutionAssessment.id.desc()
        )
    ).scalars()
    latest: dict[str, MarketResolutionAssessment] = {}
    for row in rows:
        latest.setdefault(row.market_ticker, row)
    return latest


def _attach_resolutions(db: Session, response: CandidatesResponse) -> None:
    latest = _latest_assessments(db, [c.ticker for c in response.candidates])
    for candidate in response.candidates:
        row = latest.get(candidate.ticker)
        if row is not None:
            candidate.resolution = ResolutionAssessmentOut.model_validate(row)


@router.get("/candidates", response_model=CandidatesResponse)
async def get_candidates(
    limit: int | None = Query(default=None, ge=1, le=200),
    include_rejected: bool = Query(
        default=False,
        description="Include markets rejected by the eligibility gate, with rejection_reasons",
    ),
    include_resolution: bool = Query(
        default=False,
        description="Attach the latest persisted resolution assessment to each candidate",
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

    cache_key = (
        f"{cache.CANDIDATES_KEY}:{limit}"
        f":rejected={int(include_rejected)}:resolution={int(include_resolution)}"
    )
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
    if include_resolution:
        # Read-only DB lookup; never triggers new (potentially LLM) assessments
        _attach_resolutions(db, response)
    cache.set_cached(cache_key, response.model_dump_json(), settings.candidates_cache_ttl_seconds)
    return response


@router.post(
    "/{ticker}/resolution-assessment",
    response_model=ResolutionAssessmentOut,
    status_code=201,
)
async def create_resolution_assessment(
    ticker: str,
    db: Session = Depends(get_db),
) -> ResolutionAssessmentOut:
    """Assess one known market's resolution criteria ad hoc and persist the
    result (scanner_run_id null). Uses the configured judge — rule-based unless
    ENABLE_LLM_RESOLUTION=true. Run a scan first if the ticker is unknown."""
    market = db.execute(select(Market).where(Market.ticker == ticker)).scalar_one_or_none()
    if market is None:
        raise HTTPException(
            status_code=404,
            detail=f"Market {ticker!r} not found; run a scan first so its metadata is stored",
        )

    market_data = MarketData(
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        title=market.title or "",
        category=market.category,
        status=market.status,
        close_time=market.close_time,
        expiration_time=market.expiration_time,
        rules_primary=market.rules_primary,
    )
    market_data = apply_latest_enrichment(db, market_data)
    judge = get_judge()
    assessment = await judge.assess(market_data)
    row = persist_assessment(db, market.ticker, assessment, judge, scanner_run_id=None)
    return ResolutionAssessmentOut.model_validate(row)


@router.post(
    "/{ticker}/enrich-details",
    response_model=MarketDetailEnrichmentOut,
    status_code=201,
)
async def enrich_market_details(
    ticker: str,
    db: Session = Depends(get_db),
) -> MarketDetailEnrichmentOut:
    """Fetch detail/event/series metadata for one known market from Kalshi
    (read-only GETs), persist it with raw payloads, and return the normalized
    fields. Raw payloads stay DB-only."""
    market = db.execute(select(Market).where(Market.ticker == ticker)).scalar_one_or_none()
    if market is None:
        raise HTTPException(
            status_code=404,
            detail=f"Market {ticker!r} not found; run a scan first so its metadata is stored",
        )

    service = MarketDetailEnrichmentService()
    try:
        row = await service.enrich_ticker(db, market.ticker, scanner_run_id=None)
    except EnrichmentError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return MarketDetailEnrichmentOut.model_validate(row)
