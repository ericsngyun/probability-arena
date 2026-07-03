"""Real-time opportunity watcher: read-only polling over the candidate
universe, recording price ticks and deterministic, informational-only
opportunity signals.

Signals record WHAT moved and WHY (with evidence) so a human or a later
research stage can review them. They carry no EV, no sizing, no trade
directives — signal_status ('new' -> reviewed/dismissed/promoted_to_research)
is a review workflow, not an execution queue.

Detectors (all deterministic, all comparing the previous tick to the new one):
- price_move_threshold        |Δ midpoint| >= threshold
- spread_tightened            spread crossed into the <= max_spread band
- newly_two_sided             market gained a two-sided quote
- liquidity_appeared          liquidity proxy crossed >= minimum
- price_crossed_latest_forecast  midpoint crossed the latest forecast p

Repeated (ticker, signal_type) alerts are deduped within a cooldown window.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.kalshi import KalshiRestAdapter
from app.config import Settings, get_settings
from app.models import (
    Market,
    MarketPriceTick,
    MarketSnapshot,
    OpportunitySignal,
    ScannerRun,
    WatcherRun,
)
from app.schemas import MarketData
from app.services.forecasting import latest_forecast_for

logger = logging.getLogger(__name__)

SIGNAL_PRICE_MOVE = "price_move_threshold"
SIGNAL_SPREAD_TIGHTENED = "spread_tightened"
SIGNAL_NEWLY_TWO_SIDED = "newly_two_sided"
SIGNAL_LIQUIDITY_APPEARED = "liquidity_appeared"
SIGNAL_PRICE_CROSSED_FORECAST = "price_crossed_latest_forecast"

# Signal statuses are owned by app.services.signal_workflow (ALL_STATUSES)


@dataclass
class WatcherConfig:
    market_limit: int = 100
    price_move_threshold: float = 0.07  # dollars
    max_spread_cents: int = 15
    min_liquidity_proxy: int = 100
    signal_cooldown_seconds: int = 900
    enable_retention: bool = False  # prune at most once/day from the loop

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "WatcherConfig":
        s = settings or get_settings()
        return cls(
            market_limit=s.watcher_market_limit,
            price_move_threshold=s.watcher_price_move_threshold,
            max_spread_cents=round(s.watcher_max_spread * 100),
            min_liquidity_proxy=s.watcher_min_liquidity_proxy,
            signal_cooldown_seconds=s.watcher_signal_cooldown_seconds,
            enable_retention=s.enable_watcher_retention,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _midpoint(market: MarketData) -> float | None:
    if market.yes_bid is None or market.yes_ask is None:
        return None
    return round((market.yes_bid + market.yes_ask) / 2 / 100, 4)


def latest_tick_for(session: Session, ticker: str) -> MarketPriceTick | None:
    return session.execute(
        select(MarketPriceTick)
        .where(MarketPriceTick.market_ticker == ticker)
        .order_by(MarketPriceTick.observed_at.desc(), MarketPriceTick.id.desc())
    ).scalars().first()


def _last_signal_at(session: Session, ticker: str, signal_type: str) -> datetime | None:
    row = session.execute(
        select(OpportunitySignal.created_at)
        .where(
            OpportunitySignal.market_ticker == ticker,
            OpportunitySignal.signal_type == signal_type,
        )
        .order_by(OpportunitySignal.created_at.desc(), OpportunitySignal.id.desc())
    ).scalars().first()
    return _aware(row)


class RealtimeWatcher:
    def __init__(self, adapter: KalshiRestAdapter | None = None, config: WatcherConfig | None = None):
        self.adapter = adapter or KalshiRestAdapter()
        self.config = config or WatcherConfig.from_settings()
        self._last_prune_at: datetime | None = None

    def _maybe_prune(self, session: Session) -> None:
        """When watcher retention is enabled, prune at most once per day per
        process — never on every 60s iteration. Prune failures are logged and
        never fail the watch pass."""
        if not self.config.enable_retention:
            return
        now = _now()
        if self._last_prune_at is not None and (now - self._last_prune_at) < timedelta(days=1):
            return
        try:
            from app.services.retention import RetentionService

            counts = RetentionService().prune(session)
            logger.info("Watcher retention pass: %s", counts)
        except Exception:
            logger.exception("Watcher retention pass failed (pass continues)")
        finally:
            self._last_prune_at = now

    def _universe_tickers(self, session: Session, limit: int) -> list[str]:
        """Eligible candidates of the latest successful scan (top by score);
        empty when no scan exists yet."""
        run = session.execute(
            select(ScannerRun).where(ScannerRun.status == "ok").order_by(ScannerRun.id.desc())
        ).scalars().first()
        if run is None:
            return []
        rows = session.execute(
            select(Market.ticker)
            .join(MarketSnapshot, MarketSnapshot.market_id == Market.id)
            .where(MarketSnapshot.scanner_run_id == run.id, MarketSnapshot.score > 0)
            .order_by(MarketSnapshot.score.desc())
            .limit(limit)
        ).all()
        return [ticker for (ticker,) in rows]

    def _detect_signals(
        self,
        session: Session,
        market: MarketData,
        previous: MarketPriceTick | None,
        tick: MarketPriceTick,
        observed_at: datetime,
    ) -> list[OpportunitySignal]:
        """Deterministic detectors; all require a previous observation."""
        if previous is None:
            return []
        cfg = self.config
        signals: list[OpportunitySignal] = []
        old_mid = previous.midpoint
        new_mid = tick.midpoint

        def make(signal_type: str, reason: str, evidence: dict, **fields) -> None:
            signals.append(
                OpportunitySignal(
                    market_ticker=market.ticker,
                    signal_type=signal_type,
                    signal_status="new",
                    observed_at=observed_at,
                    old_midpoint=old_mid,
                    new_midpoint=new_mid,
                    spread=tick.spread,
                    liquidity_proxy=tick.liquidity_proxy,
                    reason=reason,
                    evidence=evidence,
                    raw_payload=market.raw,
                    created_at=observed_at,
                    **fields,
                )
            )

        if old_mid is not None and new_mid is not None:
            change = round(new_mid - old_mid, 4)
            if abs(change) >= cfg.price_move_threshold:
                make(
                    SIGNAL_PRICE_MOVE,
                    f"Midpoint moved {change:+.2f} (|Δ| >= {cfg.price_move_threshold:.2f}) "
                    f"from {old_mid:.2f} to {new_mid:.2f}",
                    {
                        "old_midpoint": old_mid,
                        "new_midpoint": new_mid,
                        "threshold": cfg.price_move_threshold,
                    },
                    price_change=change,
                )

        if (
            previous.spread is not None
            and tick.spread is not None
            and previous.spread > cfg.max_spread_cents >= tick.spread
        ):
            make(
                SIGNAL_SPREAD_TIGHTENED,
                f"Spread tightened from {previous.spread}c to {tick.spread}c "
                f"(entered <= {cfg.max_spread_cents}c band)",
                {
                    "old_spread_cents": previous.spread,
                    "new_spread_cents": tick.spread,
                    "max_spread_cents": cfg.max_spread_cents,
                },
            )

        if previous.midpoint is None and new_mid is not None:
            make(
                SIGNAL_NEWLY_TWO_SIDED,
                f"Market gained a two-sided quote (yes {tick.yes_bid}c / {tick.yes_ask}c)",
                {"yes_bid": tick.yes_bid, "yes_ask": tick.yes_ask},
            )

        if (
            previous.liquidity_proxy < cfg.min_liquidity_proxy
            and tick.liquidity_proxy >= cfg.min_liquidity_proxy
        ):
            make(
                SIGNAL_LIQUIDITY_APPEARED,
                f"Liquidity proxy rose from {previous.liquidity_proxy}c to "
                f"{tick.liquidity_proxy}c (>= {cfg.min_liquidity_proxy}c)",
                {
                    "old_liquidity_proxy": previous.liquidity_proxy,
                    "new_liquidity_proxy": tick.liquidity_proxy,
                    "min_liquidity_proxy": cfg.min_liquidity_proxy,
                },
            )

        if old_mid is not None and new_mid is not None:
            forecast = latest_forecast_for(session, market.ticker)
            if forecast is not None:
                p = forecast.estimated_probability
                if (old_mid - p) * (new_mid - p) < 0:
                    make(
                        SIGNAL_PRICE_CROSSED_FORECAST,
                        f"Midpoint crossed the latest forecast probability {p:.2f} "
                        f"(moved {old_mid:.2f} -> {new_mid:.2f})",
                        {
                            "old_midpoint": old_mid,
                            "new_midpoint": new_mid,
                            "forecast_probability": p,
                            "forecast_id": forecast.id,
                        },
                        price_change=round(new_mid - old_mid, 4),
                        latest_forecast_id=forecast.id,
                        latest_forecast_probability=p,
                    )

        return signals

    def _passes_cooldown(
        self, session: Session, signal: OpportunitySignal, now: datetime
    ) -> bool:
        last = _last_signal_at(session, signal.market_ticker, signal.signal_type)
        if last is None:
            return True
        return (now - last) >= timedelta(seconds=self.config.signal_cooldown_seconds)

    async def watch_once(self, session: Session, limit: int | None = None) -> WatcherRun:
        """One polling pass: fetch fresh quotes for the candidate universe,
        record ticks, detect + persist deduped signals. Errors are recorded
        on the watcher_runs row and re-raised (loop callers catch and go on)."""
        limit = limit or self.config.market_limit
        started_at = _now()
        run = WatcherRun(status="running", started_at=started_at, created_at=started_at)
        session.add(run)
        session.commit()

        try:
            tickers = self._universe_tickers(session, limit)
            if tickers:
                markets = await self.adapter.fetch_markets_by_tickers(tickers)
            else:
                markets = await self.adapter.fetch_active_markets(max_markets=limit)

            observed_at = _now()
            signals_created = 0
            for market in markets:
                previous = latest_tick_for(session, market.ticker)
                tick = MarketPriceTick(
                    market_ticker=market.ticker,
                    observed_at=observed_at,
                    yes_bid=market.yes_bid,
                    yes_ask=market.yes_ask,
                    midpoint=_midpoint(market),
                    spread=market.spread,
                    volume_24h=market.volume_24h,
                    liquidity_proxy=market.liquidity,
                    raw_payload=market.raw,
                    created_at=observed_at,
                )
                session.add(tick)
                for signal in self._detect_signals(session, market, previous, tick, observed_at):
                    if self._passes_cooldown(session, signal, observed_at):
                        session.add(signal)
                        signals_created += 1
                    else:
                        logger.debug(
                            "Cooldown: suppressing %s for %s",
                            signal.signal_type,
                            signal.market_ticker,
                        )
            run.markets_checked = len(markets)
            run.ticks_recorded = len(markets)
            run.signals_created = signals_created
            run.status = "ok"
            run.finished_at = _now()
            run.duration_ms = max(0, int((run.finished_at - started_at).total_seconds() * 1000))
            session.commit()
            self._maybe_prune(session)
            return run
        except Exception as exc:
            session.rollback()
            logger.exception("Watcher pass failed")
            run.status = "error"
            run.error_type = type(exc).__name__
            run.error_message = str(exc)[:2000]
            run.finished_at = _now()
            run.duration_ms = max(0, int((run.finished_at - started_at).total_seconds() * 1000))
            session.commit()
            raise
