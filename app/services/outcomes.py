"""Outcome tracking: sync market settlement state from Kalshi (read-only
detail GETs) and persist one upserted row per ticker.

This service observes outcomes; it never places orders or touches trading
endpoints."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.kalshi import KalshiRestAdapter, parse_market_outcome
from app.models import Market, MarketForecastRecord, MarketOutcomeRecord

logger = logging.getLogger(__name__)


class OutcomeSyncError(RuntimeError):
    """Raised when the market detail payload cannot be fetched at all."""


def latest_outcome_for(session: Session, ticker: str) -> MarketOutcomeRecord | None:
    return session.execute(
        select(MarketOutcomeRecord).where(MarketOutcomeRecord.market_ticker == ticker)
    ).scalar_one_or_none()


class OutcomeService:
    def __init__(self, adapter: KalshiRestAdapter | None = None):
        self.adapter = adapter or KalshiRestAdapter()

    async def sync_ticker(self, session: Session, ticker: str) -> MarketOutcomeRecord:
        """Fetch the market detail and upsert the ticker's outcome row."""
        detail = await self.adapter.get_market_detail(ticker)
        if detail is None:
            raise OutcomeSyncError(f"Kalshi returned no market detail for {ticker!r}")
        outcome = parse_market_outcome(detail)

        row = latest_outcome_for(session, ticker)
        if row is None:
            row = MarketOutcomeRecord(market_ticker=ticker, created_at=datetime.now(timezone.utc))
            session.add(row)
        row.outcome_status = outcome.outcome_status
        row.resolved_probability = outcome.resolved_probability
        row.winning_side = outcome.winning_side
        row.settlement_price = outcome.settlement_price
        row.close_time = outcome.close_time
        row.settled_time = outcome.settled_time
        row.source = outcome.source
        row.raw_payload = outcome.raw_payload
        session.commit()
        return row

    async def sync_known_markets(
        self, session: Session, limit: int = 100
    ) -> list[MarketOutcomeRecord]:
        """Sync outcomes for known tickers, prioritizing markets that have
        forecasts (those are what calibration needs), then recently seen
        markets, up to `limit`. Individual fetch failures are skipped."""
        forecasted = [
            ticker
            for (ticker,) in session.execute(
                select(MarketForecastRecord.market_ticker)
                .distinct()
                .order_by(MarketForecastRecord.market_ticker)
            ).all()
        ]
        recent = [
            ticker
            for (ticker,) in session.execute(
                select(Market.ticker).order_by(Market.last_seen_at.desc(), Market.id.desc())
            ).all()
        ]
        tickers: list[str] = []
        for ticker in forecasted + recent:
            if ticker not in tickers:
                tickers.append(ticker)
            if len(tickers) >= limit:
                break

        synced: list[MarketOutcomeRecord] = []
        for ticker in tickers:
            try:
                synced.append(await self.sync_ticker(session, ticker))
            except OutcomeSyncError as exc:
                logger.warning("Skipping outcome sync for %s: %s", ticker, exc)
        return synced
