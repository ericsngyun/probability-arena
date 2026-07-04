"""Scanner: fetch active markets, rank them, persist a scanner_run with
market rows and snapshots. Read-only against Kalshi; write-only to our DB.

SCANNER-002/OPS-010: the generic first-N fetch is supplemented by targeted
series fetches (game winner / totals / spreads) so measurable game-level
markets can't be crowded out by props. Targeted markets flow through the same
eligibility gate and ranking as everything else — coverage only, never a
bypass, and never any EV/advice/trading capability."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.kalshi import KalshiRestAdapter
from app.config import Settings, get_settings
from app.models import Market, MarketEligibilityAssessment, MarketSnapshot, ScannerRun
from app.schemas import MarketData, RankedMarket
from app.services.eligibility import EligibilityAssessment, EligibilityThresholds, assess_market
from app.services.ranking import rank_markets

logger = logging.getLogger(__name__)

AssessedMarket = tuple[MarketData, EligibilityAssessment]


@dataclass
class TargetedScanStats:
    """Audit counts for the targeted series supplement (SCANNER-002)."""

    series: list[str] = field(default_factory=list)
    generic_fetched: int = 0
    targeted_fetched: int = 0
    targeted_added: int = 0  # after dedupe against the generic page
    by_series: dict[str, int] = field(default_factory=dict)
    failed_series: dict[str, str] = field(default_factory=dict)  # series -> error type

    def as_summary(self) -> dict:
        return {
            "generic_fetched": self.generic_fetched,
            "targeted_fetched": self.targeted_fetched,
            "targeted_added": self.targeted_added,
            "targeted_by_series": dict(self.by_series),
            "targeted_failed_series": dict(self.failed_series),
        }


@dataclass
class ScanResult:
    run: ScannerRun
    ranked: list[RankedMarket]  # eligible markets only, sorted by score
    rejected: list[AssessedMarket] = field(default_factory=list)
    assessed: list[AssessedMarket] = field(default_factory=list)
    targeted: TargetedScanStats | None = None  # present when targeted scans ran

    def assessment_for(self, ticker: str) -> EligibilityAssessment | None:
        for market, assessment in self.assessed:
            if market.ticker == ticker:
                return assessment
        return None


def parse_targeted_series(raw: str) -> list[str]:
    """Parse the comma-separated TARGETED_MARKET_SERIES setting."""
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


async def fetch_targeted_markets(
    adapter: KalshiRestAdapter,
    generic: list[MarketData],
    settings: Settings | None = None,
) -> tuple[list[MarketData], TargetedScanStats]:
    """Fetch configured supported series and merge them after the generic
    page. Per-series failures are recorded and skipped — a partial targeted
    fetch never fails the scan. Read-only GETs only."""
    settings = settings or get_settings()
    stats = TargetedScanStats(
        series=parse_targeted_series(settings.targeted_market_series),
        generic_fetched=len(generic),
    )
    seen = {market.ticker for market in generic}
    added: list[MarketData] = []
    for series in stats.series:
        try:
            markets = await adapter.fetch_markets_by_series(
                series,
                max_markets=settings.targeted_market_scan_limit_per_series,
                active_only=settings.targeted_market_scan_active_only,
            )
        except Exception as exc:
            stats.failed_series[series] = type(exc).__name__
            logger.warning("Targeted scan for series %s failed: %s", series, exc)
            continue
        stats.by_series[series] = len(markets)
        stats.targeted_fetched += len(markets)
        for market in markets:
            if settings.targeted_market_scan_dedup and market.ticker in seen:
                continue
            seen.add(market.ticker)
            added.append(market)
    stats.targeted_added = len(added)
    logger.info(
        "Targeted scan: generic=%d targeted_fetched=%d added=%d by_series=%s failed=%s",
        stats.generic_fetched,
        stats.targeted_fetched,
        stats.targeted_added,
        stats.by_series,
        stats.failed_series or "none",
    )
    return added, stats


def _duration_ms(started_at: datetime, finished_at: datetime) -> int:
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _upsert_market(session: Session, data: MarketData, now: datetime) -> Market:
    market = session.execute(
        select(Market).where(Market.ticker == data.ticker)
    ).scalar_one_or_none()
    if market is None:
        market = Market(ticker=data.ticker, first_seen_at=now)
        session.add(market)
    market.event_ticker = data.event_ticker
    market.title = data.title
    market.category = data.category
    market.status = data.status
    market.close_time = data.close_time
    market.expiration_time = data.expiration_time
    market.rules_primary = data.rules_primary
    market.last_seen_at = now
    return market


def _record_failed_run(
    session: Session,
    started_at: datetime,
    source: str,
    exc: Exception,
    markets_fetched: int = 0,
) -> ScannerRun:
    finished_at = datetime.now(timezone.utc)
    failed = ScannerRun(
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=_duration_ms(started_at, finished_at),
        status="error",
        source=source,
        markets_fetched=markets_fetched,
        error_type=type(exc).__name__,
        error_message=str(exc)[:2000],
    )
    session.add(failed)
    session.commit()
    return failed


def persist_scan(
    session: Session,
    ranked: list[RankedMarket],
    source: str = "api",
    started_at: datetime | None = None,
    assessed: list[AssessedMarket] | None = None,
) -> ScannerRun:
    """Persist one completed scan. Commits on success; on failure rolls back
    and records an error run instead.

    With `assessed`, every fetched market gets a snapshot (ineligible ones at
    score 0.0) and an eligibility assessment row linked to the run. Without it
    (legacy path), only the ranked markets are persisted.
    """
    started_at = started_at or datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    if assessed is None:
        items: list[tuple[MarketData, EligibilityAssessment | None, RankedMarket | None]] = [
            (item.market, None, item) for item in ranked
        ]
    else:
        ranked_by_ticker = {item.market.ticker: item for item in ranked}
        items = [
            (market, assessment, ranked_by_ticker.get(market.ticker))
            for market, assessment in assessed
        ]

    run = ScannerRun(started_at=started_at, source=source, markets_fetched=len(items))
    session.add(run)
    try:
        session.flush()  # assign run.id
        for market_data, assessment, ranked_item in items:
            market = _upsert_market(session, market_data, now)
            session.flush()
            session.add(
                MarketSnapshot(
                    market_id=market.id,
                    scanner_run_id=run.id,
                    captured_at=now,
                    yes_bid=market_data.yes_bid,
                    yes_ask=market_data.yes_ask,
                    no_bid=market_data.no_bid,
                    no_ask=market_data.no_ask,
                    last_price=market_data.last_price,
                    volume=market_data.volume,
                    volume_24h=market_data.volume_24h,
                    open_interest=market_data.open_interest,
                    liquidity=market_data.liquidity,
                    # Ineligible markets are hard-zeroed, never weighted-scored
                    score=ranked_item.score if ranked_item else 0.0,
                    score_components=ranked_item.components.model_dump() if ranked_item else None,
                    raw_payload=market_data.raw,
                )
            )
            if assessment is not None:
                session.add(
                    MarketEligibilityAssessment(
                        market_ticker=market_data.ticker,
                        scanner_run_id=run.id,
                        is_eligible=assessment.is_eligible,
                        rejection_reasons=assessment.rejection_reasons,
                        warnings=assessment.warnings,
                        has_two_sided_quote=assessment.has_two_sided_quote,
                        yes_bid=market_data.yes_bid,
                        yes_ask=market_data.yes_ask,
                        spread=assessment.spread,
                        liquidity=market_data.liquidity,
                        volume_24h=market_data.volume_24h,
                        expiration_days=assessment.expiration_days,
                        market_type_flags=assessment.market_type_flags,
                        created_at=now,
                    )
                )
        run.markets_ranked = len(ranked)
        run.status = "ok"
        run.finished_at = datetime.now(timezone.utc)
        run.duration_ms = _duration_ms(started_at, run.finished_at)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.exception("Failed to persist scan")
        _record_failed_run(session, started_at, source, exc, markets_fetched=len(items))
        raise
    return run


async def run_scan(
    session: Session,
    adapter: KalshiRestAdapter | None = None,
    max_markets: int | None = None,
    source: str = "api",
    thresholds: EligibilityThresholds | None = None,
    settings: Settings | None = None,
) -> ScanResult:
    """Fetch -> assess eligibility -> rank eligible -> persist everything.
    Fetch/assess/rank failures are recorded as an error scanner_run before
    the exception propagates."""
    adapter = adapter or KalshiRestAdapter()
    settings = settings or get_settings()
    started_at = datetime.now(timezone.utc)
    targeted_stats: TargetedScanStats | None = None
    try:
        markets = await adapter.fetch_active_markets(max_markets=max_markets)
        if settings.enable_targeted_market_scans:
            targeted_markets, targeted_stats = await fetch_targeted_markets(
                adapter, markets, settings=settings
            )
            markets = markets + targeted_markets
        thresholds = thresholds or EligibilityThresholds.from_settings()
        now = datetime.now(timezone.utc)
        assessed = [(m, assess_market(m, thresholds, now=now)) for m in markets]
        eligible = [m for m, assessment in assessed if assessment.is_eligible]
        ranked = rank_markets(eligible, now=now)
    except Exception as exc:
        logger.exception("Scan fetch/rank failed")
        _record_failed_run(session, started_at, source, exc)
        raise
    run = persist_scan(session, ranked, source=source, started_at=started_at, assessed=assessed)
    return ScanResult(
        run=run,
        ranked=ranked,
        rejected=[(m, a) for m, a in assessed if not a.is_eligible],
        assessed=assessed,
        targeted=targeted_stats,
    )
