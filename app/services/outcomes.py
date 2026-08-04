"""Outcome tracking: sync market settlement state from Kalshi (read-only
detail GETs) and persist one upserted row per ticker.

This service observes outcomes; it never places orders or touches trading
endpoints."""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.kalshi import KalshiRestAdapter, parse_market_outcome
from app.models import (
    Market,
    MarketForecastRecord,
    MarketOpsRun,
    MarketOutcomeRecord,
)

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

    def legacy_alphabetical_candidates(
        self, session: Session, limit: int
    ) -> list[str]:
        """The PRE-OUTCOME-SYNC-COVERAGE-001 selection, preserved verbatim.

        Kept, not deleted, for two reasons: it is what runs while the repair
        flag is off, so it must stay exactly as deployed; and the coverage
        report needs to describe the selection that is actually running rather
        than assume one.
        """
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
                select(Market.ticker).order_by(
                    Market.last_seen_at.desc(), Market.id.desc())
            ).all()
        ]
        tickers: list[str] = []
        for ticker in forecasted + recent:
            if ticker not in tickers:
                tickers.append(ticker)
            if len(tickers) >= limit:
                break
        return tickers

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
           stalest first;
        3. remaining forecasted markets with no row;
        4. recently seen non-forecasted markets, to preserve prior behavior.

        A TERMINAL row — settled with a yes/no side, or canceled/void — is never
        re-fetched. That is the whole freed budget, and it is what pays for the
        markets the prefix could never reach.

        **Starvation guard.** A failed fetch writes no row, so a ticker that
        cannot be fetched at all stays in the queue at the same position. With a
        strict oldest-first order, a permanently-unfetchable head — delisted
        markets Kalshi no longer serves — would monopolise the whole budget
        every cycle and never advance, which is the SAME defect in a different
        sort key. So the ordered queue is ROTATED by a monotonic cycle counter.

        Be exact about what that guarantees, because the first version of this
        docstring overclaimed and a review disproved it by running it:

        * **Total failure** (no fetch ever succeeds, so the pool is fixed):
          every candidate is reached within exactly ceil(n / limit) cycles.
        * **Partial success** (fetches land but leave markets non-terminal —
          the dominant real case): the offset indexes into a list whose length
          and membership change as rows appear and tickers move between
          priority tiers, so a single sweep can miss members. Coverage still
          completes, empirically within roughly 2-3 sweeps, once the pool
          composition settles. It is O(n / limit) cycles, not a hard bound.

        What holds in both cases, and is the property that matters: no member
        is permanently unreachable, and no head can monopolise the budget.
        """
        # Two columns, not the ORM row: `MarketOutcomeRecord.raw_payload` holds a
        # full Kalshi market detail, this runs every six minutes, and the repair
        # takes this table from ~1.8k rows to ~11k+.
        outcomes = {
            ticker: (status, side)
            for ticker, status, side in session.execute(
                select(MarketOutcomeRecord.market_ticker,
                       MarketOutcomeRecord.outcome_status,
                       MarketOutcomeRecord.winning_side)
            ).all()
        }
        now = datetime.now(timezone.utc)

        def is_terminal(row: tuple[str | None, str | None]) -> bool:
            status = (row[0] or "").strip().lower()
            side = (row[1] or "").strip().lower()
            if status == "settled" and side in ("yes", "no"):
                return True
            return status == "canceled" or side == "void"

        forecasted_tickers = {
            ticker for (ticker,) in session.execute(
                select(MarketForecastRecord.market_ticker).distinct()
            ).all()
        }
        # Scoped to forecasted tickers on purpose. The unscoped form loaded every
        # row of `markets` (100k+ on production) on every six-minute cycle to use
        # ~5% of it. The fallback below is the only path that needs the rest, and
        # it only runs when the forecasted set cannot fill the budget.
        close_by_ticker = {
            ticker: close
            for ticker, close in session.execute(
                select(Market.ticker, Market.close_time).where(
                    Market.ticker.in_(forecasted_tickers or {""})
                )
            ).all()
        }

        def close_age(ticker: str) -> float:
            close = close_by_ticker.get(ticker)
            if close is None:
                return -1.0
            close = close if close.tzinfo else close.replace(tzinfo=timezone.utc)
            return (now - close).total_seconds()

        forecasted = sorted(forecasted_tickers)

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
            # Bounded: `.all()` on an unlimited select materializes every row
            # before the loop can break. Fetch a few times the shortfall, which
            # is more than enough to fill it after terminal rows are skipped.
            for (ticker,) in session.execute(
                select(Market.ticker).order_by(
                    Market.last_seen_at.desc(), Market.id.desc()
                ).limit(max(limit * 4, 100))
            ).all():
                row = outcomes.get(ticker)
                if row is not None and is_terminal(row):
                    continue
                ordered.append(ticker)
                if len(ordered) >= limit * 2:
                    break

        # Rotate. `marketops_runs` is an already-persisted monotonic counter, so
        # this needs no new state and stays deterministic for tests.
        deduped: list[str] = []
        _seen_order: set[str] = set()
        for ticker in ordered:
            if ticker not in _seen_order:
                _seen_order.add(ticker)
                deduped.append(ticker)
        if deduped and len(deduped) > limit:
            # MAX(id), not COUNT(*). `retention_coverage` already recommends a
            # 30-day prune on `marketops_runs`; the day that lands, a count stops
            # being monotonic, the offset plateaus or walks backwards, and the
            # selection regresses to a fixed prefix — the very defect this
            # milestone exists to remove, re-introduced by an unrelated policy.
            cycles = session.execute(
                select(func.max(MarketOpsRun.id))
            ).scalar() or 0
            offset = (cycles * limit) % len(deduped)
            deduped = deduped[offset:] + deduped[:offset]
        ordered = deduped

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
        from app.config import get_settings

        if get_settings().enable_outcome_sync_coverage_repair:
            candidates = self.select_sync_candidates(session, limit)
        else:
            candidates = self.legacy_alphabetical_candidates(session, limit)

        synced: list[MarketOutcomeRecord] = []
        for ticker in candidates:
            try:
                synced.append(await self.sync_ticker(session, ticker))
            except OutcomeSyncError as exc:
                logger.warning("Skipping outcome sync for %s: %s", ticker, exc)
        return synced
