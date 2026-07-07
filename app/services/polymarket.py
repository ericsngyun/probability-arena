"""Polymarket market-data observer lane (POLY-001): a bounded, READ-ONLY scan
that fetches the public Polymarket market catalog + CLOB order books, persists
market/orderbook/domain-inventory snapshots, and builds windowed reports.

This is a second prediction-market VENUE for observation only — it exists so we
can watch Polymarket microstructure alongside Kalshi. It computes NO EV, does NO
arbitrage, recommends NO trades, sizes NO positions, places/cancels NO orders,
and touches NO wallets/keys/signing/swaps/execution. Prices and order books are
informational quotes for human review, never advice or a trade trigger. A
future semantic link to Kalshi markets (POLY-002) is a documented placeholder
only — no comparable_market_candidate / unresolved_semantic_match /
cross_venue_observation rows exist yet, and no arb/EV labels are produced.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.polymarket import SOURCE_NAME, PolymarketAdapter, PolymarketMarketData
from app.config import Settings, get_settings
from app.models import (
    PolymarketDomainInventorySnapshot,
    PolymarketMarket,
    PolymarketOrderbookSnapshot,
    PolymarketScoutRun,
)

logger = logging.getLogger(__name__)

UNCATEGORIZED = "uncategorized"

CROSS_VENUE_NOTE = (
    "Cross-venue semantic linking to Kalshi is a documented POLY-002 placeholder "
    "only: no comparable_market_candidate / unresolved_semantic_match / "
    "cross_venue_observation rows are produced, and NO EV, arbitrage, or "
    "trade-candidate labels exist or are implied."
)

READONLY_NOTE = (
    "Read-only Polymarket market-data observation. Prices/order books are "
    "informational quotes for human review — not EV, not a recommendation, not "
    "an instruction. No sizing, orders, wallets, keys, swaps, signing, or "
    "execution."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pct(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return round(ordered[idx], 6)


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


@dataclass
class PolymarketConfig:
    enabled: bool = False
    market_limit: int = 50
    orderbook_limit: int = 20
    provider_version: str = "v1"

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "PolymarketConfig":
        s = settings or get_settings()
        return cls(
            enabled=s.enable_polymarket_scout,
            market_limit=s.polymarket_market_limit,
            orderbook_limit=s.polymarket_orderbook_limit,
            provider_version=s.polymarket_provider_version,
        )


class PolymarketScoutService:
    """Orchestrates one bounded read-only scan: fetch markets → persist market
    rows → fetch a bounded number of order books → persist orderbook rows →
    roll up per-domain inventory → record the audit run."""

    def __init__(
        self,
        adapter: PolymarketAdapter | None = None,
        config: PolymarketConfig | None = None,
    ):
        self.config = config or PolymarketConfig.from_settings()
        self.adapter = adapter or PolymarketAdapter()

    async def scan_once(self, session: Session, limit: int | None = None) -> PolymarketScoutRun:
        """One read-only pass. Returns the PolymarketScoutRun (status ok|error).
        A provider outage is NOT an error — the adapter degrades to an empty
        result and the pass records `ok` with zero markets."""
        started = _now()
        run = PolymarketScoutRun(
            status="running",
            started_at=started,
            provider=SOURCE_NAME,
            provider_version=self.config.provider_version,
            created_at=started,
        )
        session.add(run)
        session.flush()

        try:
            market_cap = limit if limit is not None else self.config.market_limit
            markets = await self.adapter.fetch_markets(limit=market_cap)
            markets = markets[:market_cap]

            observed = _now()
            for m in markets:
                session.add(self._market_row(run.id, m, observed))

            ob_fetched, ob_errors = await self._fetch_orderbooks(session, run.id, markets, observed)
            domains = self._persist_domain_inventory(session, run.id, markets, observed)

            run.status = "ok"
            run.finished_at = _now()
            run.duration_ms = int((run.finished_at - started).total_seconds() * 1000)
            run.markets_seen = len(markets)
            run.markets_persisted = len(markets)
            run.orderbooks_fetched = ob_fetched
            run.orderbook_errors = ob_errors
            run.domains_seen = domains
            session.commit()
            return run
        except Exception as exc:  # unexpected (e.g. DB) — record + re-raise
            logger.exception("polymarket scan_once failed: %s", exc)
            run.status = "error"
            run.error_type = type(exc).__name__
            run.error_message = str(exc)[:500]
            run.finished_at = _now()
            try:
                session.commit()
            except Exception:  # pragma: no cover - defensive
                session.rollback()
            raise

    @staticmethod
    def _market_row(run_id, m: PolymarketMarketData, observed: datetime) -> PolymarketMarket:
        return PolymarketMarket(
            run_id=run_id,
            market_id=m.market_id,
            condition_id=m.condition_id,
            question=m.question,
            slug=m.slug,
            category=(m.category or UNCATEGORIZED)[:64],
            description=m.description,
            active=m.active,
            closed=m.closed,
            archived=m.archived,
            restricted=m.restricted,
            enable_order_book=m.enable_order_book,
            accepting_orders=m.accepting_orders,
            outcomes=m.outcomes or None,
            outcome_prices=m.outcome_prices or None,
            clob_token_ids=m.clob_token_ids or None,
            num_outcomes=len(m.outcomes),
            best_bid=m.best_bid,
            best_ask=m.best_ask,
            last_trade_price=m.last_trade_price,
            spread=m.spread,
            two_sided=m.two_sided,
            liquidity_usd=m.liquidity_usd,
            volume_24h_usd=m.volume_24h_usd,
            volume_total_usd=m.volume_total_usd,
            start_date=m.start_date,
            end_date=m.end_date,
            observed_at=observed,
            created_at=observed,
        )

    async def _fetch_orderbooks(
        self, session: Session, run_id, markets: list[PolymarketMarketData], observed: datetime
    ) -> tuple[int, int]:
        """Fetch up to orderbook_limit token books (most-liquid markets first,
        which is the adapter's sort order) and persist a snapshot per token.
        Reads the books only — never places an order. Returns (fetched, errors)."""
        budget = self.config.orderbook_limit
        targets: list[tuple[PolymarketMarketData, str, str | None]] = []
        for m in markets:
            if not (m.enable_order_book and m.clob_token_ids):
                continue
            for idx, token_id in enumerate(m.clob_token_ids):
                outcome = m.outcomes[idx] if idx < len(m.outcomes) else None
                targets.append((m, token_id, outcome))
        targets = targets[:budget]

        fetched = 0
        errors = 0
        for m, token_id, outcome in targets:
            book = await self.adapter.fetch_orderbook(token_id)
            if book is None:
                errors += 1
                continue
            session.add(
                PolymarketOrderbookSnapshot(
                    run_id=run_id,
                    market_id=m.market_id,
                    token_id=token_id,
                    outcome=outcome,
                    best_bid=book.best_bid,
                    best_ask=book.best_ask,
                    mid=book.mid,
                    spread=book.spread,
                    bid_depth=book.bid_depth,
                    ask_depth=book.ask_depth,
                    total_depth=book.total_depth,
                    num_bids=book.num_bids,
                    num_asks=book.num_asks,
                    liquidity_proxy=book.liquidity_proxy,
                    tick_size=book.tick_size,
                    observed_at=observed,
                    created_at=observed,
                )
            )
            fetched += 1
        return fetched, errors

    def _persist_domain_inventory(
        self, session: Session, run_id, markets: list[PolymarketMarketData], observed: datetime
    ) -> int:
        by_domain: dict[str, list[PolymarketMarketData]] = {}
        for m in markets:
            by_domain.setdefault((m.category or UNCATEGORIZED)[:64], []).append(m)

        for domain, ms in by_domain.items():
            count = len(ms)
            two_sided = sum(1 for m in ms if m.two_sided)
            spreads = [m.spread for m in ms if m.spread is not None]
            session.add(
                PolymarketDomainInventorySnapshot(
                    run_id=run_id,
                    domain=domain,
                    market_count=count,
                    active_count=sum(1 for m in ms if m.active),
                    two_sided_count=two_sided,
                    orderbook_enabled_count=sum(1 for m in ms if m.enable_order_book),
                    two_sided_rate=round(two_sided / count, 4) if count else None,
                    total_liquidity_usd=round(sum(m.liquidity_usd or 0 for m in ms), 2),
                    total_volume_24h_usd=round(sum(m.volume_24h_usd or 0 for m in ms), 2),
                    avg_spread=_avg(spreads),
                    observed_at=observed,
                    created_at=observed,
                )
            )
        return len(by_domain)


class PolymarketScoutRunner:
    """One bounded read-only scan cycle. Wraps PolymarketScoutService and
    guarantees it never raises out of `run_cycle` (a scheduled lane must not
    crash-loop)."""

    def __init__(
        self,
        scout: PolymarketScoutService | None = None,
        config: PolymarketConfig | None = None,
    ):
        self.config = config or PolymarketConfig.from_settings()
        self.scout = scout or PolymarketScoutService(config=self.config)

    async def run_cycle(self, session: Session, limit: int | None = None) -> PolymarketScoutRun | None:
        """Run one bounded pass. Returns the PolymarketScoutRun (status ok|
        error), or None only if even the audit row could not be recorded.
        Never raises."""
        try:
            return await self.scout.scan_once(session, limit=limit)
        except Exception as exc:
            logger.exception("polymarket scheduled cycle failed: %s", exc)
            try:
                return session.execute(
                    select(PolymarketScoutRun).order_by(PolymarketScoutRun.id.desc())
                ).scalars().first()
            except Exception:  # pragma: no cover - defensive
                return None


# --- windowed reports -------------------------------------------------------


@dataclass
class PolymarketReport:
    note: str
    cross_venue_note: str
    window_hours: int
    last_run: dict | None
    runs_in_window: int
    error_runs_in_window: int
    markets_seen: int  # distinct market_id in window
    active_markets: int
    categories: int
    two_sided_markets: int
    two_sided_rate: float | None
    orderbook_enabled_markets: int
    orderbook_snapshots_in_window: int
    spread_p50: float | None
    spread_p90: float | None
    avg_book_total_depth: float | None
    avg_book_liquidity_proxy: float | None
    provider_errors_in_window: int
    newest_markets: list[dict] = field(default_factory=list)
    top_volume_markets: list[dict] = field(default_factory=list)
    top_liquidity_markets: list[dict] = field(default_factory=list)
    row_counts: dict = field(default_factory=dict)


class PolymarketReportService:
    def build(self, session: Session, hours: int = 24, top: int = 10) -> PolymarketReport:
        now = _now()
        start = now - timedelta(hours=hours)

        runs = session.execute(
            select(PolymarketScoutRun).where(PolymarketScoutRun.started_at >= start)
        ).scalars().all()
        last = session.execute(
            select(PolymarketScoutRun).order_by(PolymarketScoutRun.id.desc())
        ).scalars().first()

        markets = session.execute(
            select(PolymarketMarket).where(PolymarketMarket.observed_at >= start)
        ).scalars().all()
        books = session.execute(
            select(PolymarketOrderbookSnapshot).where(
                PolymarketOrderbookSnapshot.observed_at >= start
            )
        ).scalars().all()

        # latest snapshot per market in window
        latest: dict[str, PolymarketMarket] = {}
        for m in markets:
            cur = latest.get(m.market_id)
            if cur is None or m.id > cur.id:
                latest[m.market_id] = m
        rows = list(latest.values())

        two_sided = sum(1 for m in rows if m.two_sided)
        categories = {(m.category or UNCATEGORIZED) for m in rows}
        market_spreads = [m.spread for m in rows if m.spread is not None]

        def market_dict(m: PolymarketMarket) -> dict:
            return {
                "market_id": m.market_id,
                "question": (m.question or "")[:60],
                "category": m.category,
                "best_bid": m.best_bid,
                "best_ask": m.best_ask,
                "spread": m.spread,
                "liquidity_usd": m.liquidity_usd,
                "volume_24h_usd": m.volume_24h_usd,
                "two_sided": m.two_sided,
            }

        newest = sorted(
            (m for m in rows if m.start_date is not None),
            key=lambda x: x.start_date,
            reverse=True,
        )[:top]
        top_volume = sorted(rows, key=lambda x: -(x.volume_24h_usd or 0))[:top]
        top_liquidity = sorted(rows, key=lambda x: -(x.liquidity_usd or 0))[:top]

        book_depths = [b.total_depth for b in books if b.total_depth is not None]
        book_liq = [b.liquidity_proxy for b in books if b.liquidity_proxy is not None]

        return PolymarketReport(
            note=READONLY_NOTE,
            cross_venue_note=CROSS_VENUE_NOTE,
            window_hours=hours,
            last_run=(
                {
                    "id": last.id,
                    "status": last.status,
                    "started_at": last.started_at.isoformat() if last.started_at else None,
                    "duration_ms": last.duration_ms,
                    "markets_seen": last.markets_seen,
                    "orderbooks_fetched": last.orderbooks_fetched,
                    "orderbook_errors": last.orderbook_errors,
                    "domains_seen": last.domains_seen,
                    "error_type": last.error_type,
                }
                if last
                else None
            ),
            runs_in_window=len(runs),
            error_runs_in_window=sum(1 for r in runs if r.status == "error"),
            markets_seen=len(latest),
            active_markets=sum(1 for m in rows if m.active),
            categories=len(categories),
            two_sided_markets=two_sided,
            two_sided_rate=round(two_sided / len(rows), 4) if rows else None,
            orderbook_enabled_markets=sum(1 for m in rows if m.enable_order_book),
            orderbook_snapshots_in_window=len(books),
            spread_p50=_pct(market_spreads, 50),
            spread_p90=_pct(market_spreads, 90),
            avg_book_total_depth=_avg(book_depths),
            avg_book_liquidity_proxy=_avg(book_liq),
            provider_errors_in_window=sum(r.orderbook_errors for r in runs),
            newest_markets=[market_dict(m) for m in newest],
            top_volume_markets=[market_dict(m) for m in top_volume],
            top_liquidity_markets=[market_dict(m) for m in top_liquidity],
            row_counts={
                "polymarket_scout_runs": session.execute(
                    select(func.count()).select_from(PolymarketScoutRun)
                ).scalar()
                or 0,
                "polymarket_markets": session.execute(
                    select(func.count()).select_from(PolymarketMarket)
                ).scalar()
                or 0,
                "polymarket_orderbook_snapshots": session.execute(
                    select(func.count()).select_from(PolymarketOrderbookSnapshot)
                ).scalar()
                or 0,
                "polymarket_domain_inventory_snapshots": session.execute(
                    select(func.count()).select_from(PolymarketDomainInventorySnapshot)
                ).scalar()
                or 0,
            },
        )


@dataclass
class PolymarketDomainReport:
    note: str
    cross_venue_note: str
    last_run_id: int | None
    total_domains: int
    domains: list[dict] = field(default_factory=list)


class PolymarketDomainReportService:
    """Point-in-time per-domain inventory from the most recent completed run
    (read-only coverage view)."""

    def build(self, session: Session, top: int = 30) -> PolymarketDomainReport:
        last = session.execute(
            select(PolymarketScoutRun)
            .where(PolymarketScoutRun.status == "ok")
            .order_by(PolymarketScoutRun.id.desc())
        ).scalars().first()

        domains: list[dict] = []
        if last is not None:
            snaps = session.execute(
                select(PolymarketDomainInventorySnapshot)
                .where(PolymarketDomainInventorySnapshot.run_id == last.id)
                .order_by(PolymarketDomainInventorySnapshot.market_count.desc())
            ).scalars().all()
            for s in snaps[:top]:
                domains.append(
                    {
                        "domain": s.domain,
                        "market_count": s.market_count,
                        "active_count": s.active_count,
                        "two_sided_count": s.two_sided_count,
                        "two_sided_rate": s.two_sided_rate,
                        "orderbook_enabled_count": s.orderbook_enabled_count,
                        "total_liquidity_usd": s.total_liquidity_usd,
                        "total_volume_24h_usd": s.total_volume_24h_usd,
                        "avg_spread": s.avg_spread,
                    }
                )

        return PolymarketDomainReport(
            note=READONLY_NOTE,
            cross_venue_note=CROSS_VENUE_NOTE,
            last_run_id=last.id if last else None,
            total_domains=len(domains),
            domains=domains,
        )
