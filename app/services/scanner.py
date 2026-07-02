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


def persist_scan(session: Session, ranked: list[RankedMarket]) -> ScannerRun:
    """Persist one completed scan. Commits on success, rolls back and records
    the failure on error."""
    now = datetime.now(timezone.utc)
    run = ScannerRun(started_at=now, markets_fetched=len(ranked))
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
                )
            )
        run.markets_ranked = len(ranked)
        run.status = "ok"
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.exception("Failed to persist scan")
        failed = ScannerRun(
            started_at=now,
            finished_at=datetime.now(timezone.utc),
            status="error",
            markets_fetched=len(ranked),
            error=str(exc)[:2000],
        )
        session.add(failed)
        session.commit()
        raise
    return run


async def run_scan(
    session: Session,
    adapter: KalshiRestAdapter | None = None,
    max_markets: int | None = None,
) -> tuple[ScannerRun, list[RankedMarket]]:
    adapter = adapter or KalshiRestAdapter()
    markets = await adapter.fetch_active_markets(max_markets=max_markets)
    ranked = rank_markets(markets)
    run = persist_scan(session, ranked)
    return run, ranked
