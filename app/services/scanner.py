"""Scanner: fetch active markets, rank them, persist a scanner_run with
market rows and snapshots. Read-only against Kalshi; write-only to our DB."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.kalshi import KalshiRestAdapter
from app.models import Market, MarketSnapshot, ScannerRun
from app.schemas import MarketData, RankedMarket
from app.services.ranking import rank_markets

logger = logging.getLogger(__name__)


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
) -> ScannerRun:
    """Persist one completed scan. Commits on success; on failure rolls back
    and records an error run instead."""
    started_at = started_at or datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    run = ScannerRun(started_at=started_at, source=source, markets_fetched=len(ranked))
    session.add(run)
    try:
        session.flush()  # assign run.id
        for item in ranked:
            market = _upsert_market(session, item.market, now)
            session.flush()
            session.add(
                MarketSnapshot(
                    market_id=market.id,
                    scanner_run_id=run.id,
                    captured_at=now,
                    yes_bid=item.market.yes_bid,
                    yes_ask=item.market.yes_ask,
                    no_bid=item.market.no_bid,
                    no_ask=item.market.no_ask,
                    last_price=item.market.last_price,
                    volume=item.market.volume,
                    volume_24h=item.market.volume_24h,
                    open_interest=item.market.open_interest,
                    liquidity=item.market.liquidity,
                    score=item.score,
                    score_components=item.components.model_dump(),
                    raw_payload=item.market.raw,
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
        _record_failed_run(session, started_at, source, exc, markets_fetched=len(ranked))
        raise
    return run


async def run_scan(
    session: Session,
    adapter: KalshiRestAdapter | None = None,
    max_markets: int | None = None,
    source: str = "api",
) -> tuple[ScannerRun, list[RankedMarket]]:
    """Fetch -> rank -> persist. Fetch/rank failures are recorded as an error
    scanner_run before the exception propagates."""
    adapter = adapter or KalshiRestAdapter()
    started_at = datetime.now(timezone.utc)
    try:
        markets = await adapter.fetch_active_markets(max_markets=max_markets)
        ranked = rank_markets(markets)
    except Exception as exc:
        logger.exception("Scan fetch/rank failed")
        _record_failed_run(session, started_at, source, exc)
        raise
    run = persist_scan(session, ranked, source=source, started_at=started_at)
    return run, ranked
