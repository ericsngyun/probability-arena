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

    def select_sync_candidates(self, session: Session, limit: int) -> list[str]:
        """Tickers worth spending a provider call on, most urgent first.

        OUTCOME-SYNC-COVERAGE-001 replaced an alphabetical prefix here. The old
        selection sorted every distinct forecasted ticker by name and kept the
        first `limit`, which meant the markets ranked past the cap were not
        *delayed* — they were unreachable on every cycle, forever, and no amount
        of waiting or scheduling would have reached them. Meanwhile the markets
        inside the prefix were re-fetched every six minutes including the ones
        that had already settled, whose outcome can never change again.

        The call budget is unchanged. It is only spent on markets whose outcome
        can still move:

        1. matured forecasted markets with no outcome row (oldest close first);
        2. forecasted markets whose row is non-terminal (open/closed/unknown),
           staleset first;
        3. remaining forecasted markets with no row;
        4. recently seen non-forecasted markets, to preserve prior behavior.

        A TERMINAL row — settled with a yes/no side, or canceled/void — is never
        re-fetched. That is the whole freed budget, and it is what pays for the
        markets the prefix could never reach.
        """
        outcomes = {
            row.market_ticker: row
            for row in session.execute(select(MarketOutcomeRecord)).scalars()
        }
        now = datetime.now(timezone.utc)

        def is_terminal(row: MarketOutcomeRecord) -> bool:
            status = (row.outcome_status or "").strip().lower()
            side = (row.winning_side or "").strip().lower()
            if status == "settled" and side in ("yes", "no"):
                return True
            return status == "canceled" or side == "void"

        close_by_ticker = {
            ticker: close
            for ticker, close in session.execute(
                select(Market.ticker, Market.close_time)
            ).all()
        }

        def close_age(ticker: str) -> float:
            close = close_by_ticker.get(ticker)
            if close is None:
                return -1.0
            close = close if close.tzinfo else close.replace(tzinfo=timezone.utc)
            return (now - close).total_seconds()

        forecasted = [
            ticker
            for (ticker,) in session.execute(
                select(MarketForecastRecord.market_ticker).distinct()
            ).all()
        ]

        missing_matured, missing_open, non_terminal = [], [], []
        for ticker in forecasted:
            row = outcomes.get(ticker)
            if row is not None and is_terminal(row):
                continue  # final; a further fetch cannot change it
            if row is None:
                (missing_matured if close_age(ticker) > 0 else missing_open).append(ticker)
            else:
                non_terminal.append(ticker)

        # Oldest close first: those have been waiting longest and are likeliest
        # to have settled already.
        missing_matured.sort(key=close_age, reverse=True)
        non_terminal.sort(key=close_age, reverse=True)

        ordered = missing_matured + non_terminal + missing_open
        if len(ordered) < limit:
            for (ticker,) in session.execute(
                select(Market.ticker).order_by(
                    Market.last_seen_at.desc(), Market.id.desc()
                )
            ).all():
                row = outcomes.get(ticker)
                if row is not None and is_terminal(row):
                    continue
                ordered.append(ticker)
                if len(ordered) >= limit * 2:
                    break

        seen: set[str] = set()
        tickers: list[str] = []
        for ticker in ordered:
            if ticker in seen:
                continue
            seen.add(ticker)
            tickers.append(ticker)
            if len(tickers) >= limit:
                break
        return tickers

    async def sync_known_markets(
        self, session: Session, limit: int = 100
    ) -> list[MarketOutcomeRecord]:
        """Sync outcomes for the tickers that most need one, up to `limit`.

        Individual fetch failures are skipped so one bad ticker cannot starve
        the rest of the batch.
        """
        synced: list[MarketOutcomeRecord] = []
        for ticker in self.select_sync_candidates(session, limit):
            try:
                synced.append(await self.sync_ticker(session, ticker))
            except OutcomeSyncError as exc:
                logger.warning("Skipping outcome sync for %s: %s", ticker, exc)
        return synced
