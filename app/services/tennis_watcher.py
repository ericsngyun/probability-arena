"""TENNIS-WATCHER-001 — read-only tennis market tick/book coverage.

TENNIS-LIVE-SOURCE-001 measured two independent tennis blockers: no live
score provider covers Kalshi's Challenger/ITF tier, AND tennis markets have
no price ticks because they are not in the realtime watcher universe. This
module closes the SECOND gap only: a manual, bounded, read-only tick capture
path so future tape recording and latency studies have market-side data.

Design:
- Discovery is DB-only (which active tennis markets exist; which have ticks).
- `scan_once` fetches fresh quotes for a bounded set of active tennis
  markets via the existing read-only Kalshi adapter and records plain
  `market_price_ticks` rows — the SAME table, shape, and retention window the
  realtime watcher uses. It detects NO signals, writes NO watcher_runs row,
  and touches nothing MarketOps/EDGE-AUTO reads for behavior. `--dry-run`
  persists nothing.
- The scheduled entry point no-ops unless `ENABLE_TENNIS_TICK_WATCHER=true`
  (default false); no timer artifact is installed by this milestone. Manual
  runs are always allowed.

MARKET OBSERVATION ONLY: ticks are quotes for research/latency measurement —
not signals, not forecasts, not EV, never advice. No paper trading,
recommendations, sizing, orders, wallets/keys, signing, swaps, execution, or
autonomy. No forecast/gate/promotion/MarketOps/EDGE-AUTO behavior changes.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.kalshi import KalshiRestAdapter
from app.config import Settings, get_settings
from app.models import Market, MarketPriceTick
from app.schemas import MarketData
from app.services.edge_followthrough import _aware, _mean, _rate
from app.services.tennis_live_source import (
    TENNIS_PREFIXES,
    classify_tennis_market,
)

logger = logging.getLogger(__name__)

TENNIS_WATCH_NOTE = (
    "Read-only tennis market tick capture. Quotes are recorded into the same "
    "market_price_ticks table (same retention) the realtime watcher uses — "
    "market OBSERVATION for research and latency measurement only. No signal "
    "detection, no forecasts, not EV, never advice; no sizing, orders, "
    "wallets, keys, swaps, signing, or execution."
)

# reported series buckets — full series tokens, most specific first
SERIES_BUCKETS = (
    "KXATPCHALLENGERMATCH",
    "KXITFWMATCH",
    "KXITFMATCH",
    "KXATP",
    "KXWTA",
    "KXITF",
)

SCAN_OK = "ok"
SCAN_DRY_RUN = "dry_run"
SCAN_SKIPPED_FLAG = "skipped_flag_disabled"
SCAN_NO_TARGETS = "no_targets"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def series_bucket(ticker: str) -> str:
    """Most-specific matching series bucket, or the raw series token."""
    upper = (ticker or "").upper()
    for bucket in SERIES_BUCKETS:
        if upper.startswith(bucket):
            return bucket
    return upper.split("-", 1)[0] or "unknown"


def _midpoint(m: MarketData) -> float | None:
    if m.yes_bid is None or m.yes_ask is None:
        return None
    return round((m.yes_bid + m.yes_ask) / 2 / 100, 4)


@dataclass
class TennisUniverse:
    """DB-only discovery of the tennis market universe and its tick coverage."""

    active: list[Market] = field(default_factory=list)
    covered_tickers: set = field(default_factory=set)

    @property
    def uncovered(self) -> list[Market]:
        return [m for m in self.active if m.ticker not in self.covered_tickers]


def discover_tennis_universe(
    session: Session, hours: int = 24
) -> TennisUniverse:
    """Active (recently-seen, not expired) tennis markets and which of them
    already have a tick in the window. Read-only."""
    now = _now()
    cutoff = now - timedelta(hours=hours)
    active: list[Market] = []
    for m in session.execute(select(Market)).scalars().all():
        if not (m.ticker or "").upper().startswith(TENNIS_PREFIXES):
            continue
        seen = _aware(m.last_seen_at)
        if seen is None or seen < cutoff:
            continue
        if (m.status or "").lower() not in ("active", "open", "unknown", ""):
            continue
        close = _aware(m.close_time) or _aware(m.expiration_time)
        if close is not None and close <= now:
            continue
        active.append(m)
    tickers = [m.ticker for m in active]
    covered = set()
    if tickers:
        covered = {
            t for (t,) in session.execute(
                select(MarketPriceTick.market_ticker)
                .where(
                    MarketPriceTick.market_ticker.in_(tickers),
                    MarketPriceTick.observed_at >= cutoff,
                )
                .distinct()
            ).all()
        }
    return TennisUniverse(active=active, covered_tickers=covered)


class TennisTickWatcher:
    """Manual, bounded, read-only tennis tick capture. Persists ONLY
    market_price_ticks rows (never signals, never watcher_runs)."""

    def __init__(
        self,
        adapter: KalshiRestAdapter | None = None,
        settings: Settings | None = None,
    ):
        self.settings = settings or get_settings()
        self.adapter = adapter or KalshiRestAdapter()

    def _targets(self, session: Session, limit: int, hours: int) -> list[str]:
        """Bounded target list: active tennis markets, match-winner first
        (the tape-relevant type), then set/prop, deterministic order."""
        universe = discover_tennis_universe(session, hours=hours)
        rank = {"match_winner": 0, "set_winner": 1, "prop": 2, "unknown": 3}
        ordered = sorted(
            universe.active,
            key=lambda m: (rank.get(classify_tennis_market(m.ticker), 3), m.ticker),
        )
        return [m.ticker for m in ordered[: max(limit, 0)]]

    async def scan_once(
        self,
        session: Session,
        limit: int | None = None,
        hours: int = 24,
        dry_run: bool = False,
        scheduled: bool = False,
    ) -> dict:
        """One bounded read-only quote pass over active tennis markets.
        dry_run reports what WOULD be recorded and persists nothing. The
        scheduled path no-ops unless ENABLE_TENNIS_TICK_WATCHER=true."""
        if scheduled and not self.settings.enable_tennis_tick_watcher:
            return {
                "status": SCAN_SKIPPED_FLAG,
                "note": (
                    "scheduled tennis tick scan skipped: "
                    "ENABLE_TENNIS_TICK_WATCHER=false (default). Manual runs "
                    "are always allowed."
                ),
                "targets": 0, "fetched": 0, "ticks_recorded": 0,
            }
        limit = limit if limit is not None else self.settings.tennis_tick_watch_limit
        targets = self._targets(session, limit, hours)
        if not targets:
            return {
                "status": SCAN_NO_TARGETS,
                "note": "no active tennis markets in the recency window",
                "targets": 0, "fetched": 0, "ticks_recorded": 0,
            }
        markets = await self.adapter.fetch_markets_by_tickers(targets)
        observed_at = _now()
        two_sided = sum(
            1 for m in markets if m.yes_bid is not None and m.yes_ask is not None
        )
        recorded = 0
        if not dry_run:
            for m in markets:
                session.add(MarketPriceTick(
                    market_ticker=m.ticker,
                    observed_at=observed_at,
                    yes_bid=m.yes_bid,
                    yes_ask=m.yes_ask,
                    midpoint=_midpoint(m),
                    spread=m.spread,
                    volume_24h=m.volume_24h,
                    liquidity_proxy=m.liquidity,
                    raw_payload=m.raw,
                    created_at=observed_at,
                ))
                recorded += 1
            session.commit()
        return {
            "status": SCAN_DRY_RUN if dry_run else SCAN_OK,
            "note": TENNIS_WATCH_NOTE,
            "targets": len(targets),
            "fetched": len(markets),
            "two_sided_quotes": two_sided,
            "ticks_recorded": recorded,
            "series_mix": self._mix(targets),
            "observed_at": observed_at.isoformat(),
        }

    @staticmethod
    def _mix(tickers: list[str]) -> dict:
        out: dict[str, int] = {}
        for t in tickers:
            b = series_bucket(t)
            out[b] = out.get(b, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def build_tennis_watch_report(session: Session, hours: int = 24) -> dict:
    """DB-only tennis tick-coverage report: who is covered, how fresh, how
    complete the quotes are. Read-only; no external call."""
    now = _now()
    universe = discover_tennis_universe(session, hours=hours)
    active = universe.active
    covered = [m for m in active if m.ticker in universe.covered_tickers]
    match_winner = [
        m for m in active if classify_tennis_market(m.ticker) == "match_winner"
    ]

    latest_age_s = None
    quote_stats: dict = {}
    if universe.covered_tickers:
        latest_at = session.execute(
            select(func.max(MarketPriceTick.observed_at)).where(
                MarketPriceTick.market_ticker.in_(list(universe.covered_tickers))
            )
        ).scalar()
        if latest_at is not None:
            latest_age_s = round((now - _aware(latest_at)).total_seconds(), 1)
        latest_rows = [
            session.execute(
                select(MarketPriceTick)
                .where(MarketPriceTick.market_ticker == t)
                .order_by(MarketPriceTick.observed_at.desc())
                .limit(1)
            ).scalars().first()
            for t in sorted(universe.covered_tickers)
        ]
        latest_rows = [r for r in latest_rows if r is not None]
        quote_stats = {
            "two_sided_share": _rate(
                sum(1 for r in latest_rows
                    if r.yes_bid is not None and r.yes_ask is not None),
                len(latest_rows),
            ),
            "mean_spread_cents": _mean([r.spread for r in latest_rows]),
            "mean_liquidity_proxy": _mean([r.liquidity_proxy for r in latest_rows]),
        }

    def series_mix(markets: list[Market]) -> dict:
        out: dict[str, int] = {}
        for m in markets:
            b = series_bucket(m.ticker)
            out[b] = out.get(b, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def type_mix(markets: list[Market]) -> dict:
        out: dict[str, int] = {}
        for m in markets:
            t = classify_tennis_market(m.ticker)
            out[t] = out.get(t, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    return {
        "note": TENNIS_WATCH_NOTE,
        "generated_at": now.isoformat(),
        "window_hours": hours,
        "flag_enable_tennis_tick_watcher": get_settings().enable_tennis_tick_watcher,
        "active_tennis_markets": len(active),
        "match_winner_markets": len(match_winner),
        "tick_covered": len(covered),
        "uncovered": len(active) - len(covered),
        "coverage_rate": _rate(len(covered), len(active)),
        "latest_tick_age_s": latest_age_s,
        "quote_stats": quote_stats,
        "series_mix_active": series_mix(active),
        "series_mix_covered": series_mix(covered),
        "market_type_mix": type_mix(active),
        "uncovered_examples": [m.ticker for m in universe.uncovered[:10]],
        "provider_state_relationship": (
            "score-side coverage remains provider_gap (TENNIS-LIVE-SOURCE-001: "
            "ESPN source_backed=0 on current Challenger-tier candidates) — tick "
            "coverage here is the market-side half only"
        ),
        "disclaimer": (
            "market observation only — no trading, no signals, no forecasts, "
            "not EV, never advice"
        ),
    }
