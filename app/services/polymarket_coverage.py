"""POLY-COVERAGE-001: read-only Polymarket COVERAGE reporting.

Answers one question for a human: *does the Polymarket sample we have persisted
contain markets that are structurally capable of being compared against the
Kalshi markets we have persisted* — and where does that supply not exist?

It is a SUPPLY/COVERAGE census, not a matcher and not a signal:

* It reports per-domain and per-market-type COUNTS on both venues.
* It reports whether a domain has the structural prerequisites for comparability
  (both venues present, a yes-scale outcome type, a resolution time on each side)
  and, when not, WHY the supply is missing.
* It never pairs two specific markets, never scores a pair, never measures a
  price difference, and never ranks anything for action.

Hard boundary (docs/SAFETY_BOUNDARIES.md): coverage counts only. No EV, no
arbitrage/arb label, no trade candidate, no recommendation, no side, no size, no
dollar/profit figure, no order, no wallet/private key, no signing, no swap, no
execution. "Comparable supply" means *a comparison could be attempted here*,
never *this is an opportunity*. Derived on demand from already-persisted rows;
no external call, no provider-budget impact, nothing persisted.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Market, PolymarketMarket, PolymarketOrderbookSnapshot
from app.services.cross_venue import coarse_domain, normalize_outcome, outcome_is_yes_scale

logger = logging.getLogger(__name__)

COVERAGE_NOTE = (
    "Read-only Polymarket COVERAGE census (POLY-COVERAGE-001). Counts the "
    "observation supply on each venue per domain/market type. `comparable_supply` "
    "means a comparison could be ATTEMPTED in that domain — it is NOT arbitrage, "
    "NOT EV, NOT a trade candidate, NOT a recommendation, NOT a side/size, and "
    "NOT an action. No orders, wallets, keys, swaps, signing, or execution."
)

# Why a domain cannot currently yield a comparable observation (supply diagnosis).
REASON_NO_POLYMARKET = "no_polymarket_markets"
REASON_NO_KALSHI = "no_kalshi_markets"
REASON_NO_POLY_RESOLUTION = "no_polymarket_resolution_time"
REASON_NO_KALSHI_RESOLUTION = "no_kalshi_resolution_time"
REASON_NO_YES_SCALE_OUTCOME = "no_yes_scale_outcome_type_on_either_venue"


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
class DomainCoverage:
    domain: str
    polymarket_markets: int = 0
    polymarket_active: int = 0
    polymarket_two_sided: int = 0
    polymarket_with_resolution: int = 0
    polymarket_yes_scale: int = 0
    polymarket_orderbook_enabled: int = 0
    polymarket_orderbook_snapshots: int = 0
    polymarket_avg_spread: float | None = None
    polymarket_avg_depth: float | None = None
    polymarket_liquidity_usd: float = 0.0
    kalshi_markets: int = 0
    kalshi_with_resolution: int = 0
    kalshi_yes_scale: int = 0
    comparable_supply: bool = False
    missing_reasons: list[str] = field(default_factory=list)

    @property
    def two_sided_rate(self) -> float | None:
        if not self.polymarket_markets:
            return None
        return round(self.polymarket_two_sided / self.polymarket_markets, 4)

    @property
    def orderbook_coverage_rate(self) -> float | None:
        """Share of order-book-enabled Polymarket markets we actually snapshotted."""
        if not self.polymarket_orderbook_enabled:
            return None
        return round(
            min(self.polymarket_orderbook_snapshots, self.polymarket_orderbook_enabled)
            / self.polymarket_orderbook_enabled,
            4,
        )

    def as_dict(self) -> dict:
        return {
            "domain": self.domain,
            "polymarket_markets": self.polymarket_markets,
            "polymarket_active": self.polymarket_active,
            "polymarket_two_sided": self.polymarket_two_sided,
            "two_sided_rate": self.two_sided_rate,
            "polymarket_with_resolution": self.polymarket_with_resolution,
            "polymarket_yes_scale": self.polymarket_yes_scale,
            "polymarket_orderbook_enabled": self.polymarket_orderbook_enabled,
            "polymarket_orderbook_snapshots": self.polymarket_orderbook_snapshots,
            "orderbook_coverage_rate": self.orderbook_coverage_rate,
            "polymarket_avg_spread": self.polymarket_avg_spread,
            "polymarket_avg_depth": self.polymarket_avg_depth,
            "polymarket_liquidity_usd": round(self.polymarket_liquidity_usd, 2),
            "kalshi_markets": self.kalshi_markets,
            "kalshi_with_resolution": self.kalshi_with_resolution,
            "kalshi_yes_scale": self.kalshi_yes_scale,
            "comparable_supply": self.comparable_supply,
            "missing_reasons": self.missing_reasons,
        }


@dataclass
class PolymarketCoverageReport:
    note: str
    polymarket_markets: int
    polymarket_active: int
    kalshi_markets: int
    kalshi_truncated: bool  # True when kalshi_limit capped the census (never a silent cap)
    categories: int
    orderbook_enabled: int
    orderbook_snapshots: int
    two_sided_rate: float | None
    spread_p50: float | None
    spread_p90: float | None
    avg_book_depth: float | None
    domains: list[dict] = field(default_factory=list)
    overlap_domains: list[str] = field(default_factory=list)
    comparable_supply_domains: list[str] = field(default_factory=list)
    no_comparable_supply_domains: list[dict] = field(default_factory=list)
    polymarket_market_types: dict = field(default_factory=dict)
    kalshi_market_types: dict = field(default_factory=dict)
    top_categories: list[dict] = field(default_factory=list)


class PolymarketCoverageReportService:
    """Read-only coverage census over the latest persisted snapshot of each
    Polymarket market plus the persisted Kalshi active markets."""

    def _latest_polymarket(self, session: Session) -> list[PolymarketMarket]:
        rows = session.execute(
            select(PolymarketMarket).order_by(PolymarketMarket.id.desc())
        ).scalars().all()
        latest: dict[str, PolymarketMarket] = {}
        for m in rows:
            latest.setdefault(m.market_id, m)
        return list(latest.values())

    def _kalshi_active(self, session: Session, limit: int) -> list[Market]:
        markets = session.execute(
            select(Market).where(Market.status == "active").limit(limit)
        ).scalars().all()
        if not markets:
            markets = session.execute(
                select(Market).order_by(Market.last_seen_at.desc()).limit(limit)
            ).scalars().all()
        return markets

    def build(self, session: Session, top: int = 30, kalshi_limit: int = 4000) -> PolymarketCoverageReport:
        polys = self._latest_polymarket(session)
        kalshi = self._kalshi_active(session, kalshi_limit)

        # order-book snapshot coverage, counted per distinct market_id
        booked_market_ids = {
            row[0]
            for row in session.execute(
                select(PolymarketOrderbookSnapshot.market_id).distinct()
            ).all()
            if row[0] is not None
        }

        coverage: dict[str, DomainCoverage] = {}

        def bucket(domain: str) -> DomainCoverage:
            return coverage.setdefault(domain, DomainCoverage(domain=domain))

        poly_types: dict[str, int] = {}
        kalshi_types: dict[str, int] = {}
        categories: dict[str, int] = {}
        spreads_by_domain: dict[str, list[float]] = {}
        depths_by_domain: dict[str, list[float]] = {}

        for m in polys:
            domain = coarse_domain(m.question, m.category)
            outcome = normalize_outcome(m.question)
            c = bucket(domain)
            c.polymarket_markets += 1
            if m.active:
                c.polymarket_active += 1
            if m.two_sided:
                c.polymarket_two_sided += 1
            if m.end_date is not None:
                c.polymarket_with_resolution += 1
            if outcome_is_yes_scale(outcome):
                c.polymarket_yes_scale += 1
            if m.enable_order_book:
                c.polymarket_orderbook_enabled += 1
            if m.market_id in booked_market_ids:
                c.polymarket_orderbook_snapshots += 1
            c.polymarket_liquidity_usd += m.liquidity_usd or 0.0
            if m.spread is not None:
                spreads_by_domain.setdefault(domain, []).append(m.spread)

            poly_types[outcome] = poly_types.get(outcome, 0) + 1
            categories[m.category or "uncategorized"] = categories.get(m.category or "uncategorized", 0) + 1

        for mk in kalshi:
            domain = coarse_domain(mk.title, mk.category, mk.ticker)
            outcome = normalize_outcome(mk.title)
            c = bucket(domain)
            c.kalshi_markets += 1
            if (mk.close_time or mk.expiration_time) is not None:
                c.kalshi_with_resolution += 1
            if outcome_is_yes_scale(outcome):
                c.kalshi_yes_scale += 1
            kalshi_types[outcome] = kalshi_types.get(outcome, 0) + 1

        # book depth per domain (join snapshots back to their market's domain)
        poly_by_id = {m.market_id: m for m in polys}
        books = session.execute(select(PolymarketOrderbookSnapshot)).scalars().all()
        for b in books:
            m = poly_by_id.get(b.market_id)
            if m is None or b.total_depth is None:
                continue
            depths_by_domain.setdefault(coarse_domain(m.question, m.category), []).append(b.total_depth)

        for domain, c in coverage.items():
            c.polymarket_avg_spread = _avg(spreads_by_domain.get(domain, []))
            c.polymarket_avg_depth = _avg(depths_by_domain.get(domain, []))
            c.comparable_supply, c.missing_reasons = self._diagnose(c)

        ordered = sorted(
            coverage.values(),
            key=lambda c: (-(c.polymarket_markets + c.kalshi_markets), c.domain),
        )

        all_spreads = [m.spread for m in polys if m.spread is not None]
        all_depths = [b.total_depth for b in books if b.total_depth is not None]
        two_sided = sum(1 for m in polys if m.two_sided)

        return PolymarketCoverageReport(
            note=COVERAGE_NOTE,
            polymarket_markets=len(polys),
            polymarket_active=sum(1 for m in polys if m.active),
            kalshi_markets=len(kalshi),
            kalshi_truncated=len(kalshi) >= kalshi_limit,
            categories=len(categories),
            orderbook_enabled=sum(1 for m in polys if m.enable_order_book),
            orderbook_snapshots=len(books),
            two_sided_rate=round(two_sided / len(polys), 4) if polys else None,
            spread_p50=_pct(all_spreads, 50),
            spread_p90=_pct(all_spreads, 90),
            avg_book_depth=_avg(all_depths),
            domains=[c.as_dict() for c in ordered[:top]],
            overlap_domains=sorted(
                c.domain for c in ordered if c.polymarket_markets and c.kalshi_markets
            ),
            comparable_supply_domains=sorted(c.domain for c in ordered if c.comparable_supply),
            no_comparable_supply_domains=[
                {"domain": c.domain, "reasons": c.missing_reasons}
                for c in ordered
                if not c.comparable_supply
            ],
            polymarket_market_types=dict(sorted(poly_types.items(), key=lambda kv: -kv[1])),
            kalshi_market_types=dict(sorted(kalshi_types.items(), key=lambda kv: -kv[1])),
            top_categories=[
                {"category": k, "markets": v}
                for k, v in sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
            ],
        )

    @staticmethod
    def _diagnose(c: DomainCoverage) -> tuple[bool, list[str]]:
        """Structural prerequisites for a comparison to even be ATTEMPTABLE in
        this domain. Says nothing about whether any specific pair matches, and
        nothing whatsoever about opportunity."""
        reasons: list[str] = []
        if not c.polymarket_markets:
            reasons.append(REASON_NO_POLYMARKET)
        if not c.kalshi_markets:
            reasons.append(REASON_NO_KALSHI)
        if c.polymarket_markets and not c.polymarket_with_resolution:
            reasons.append(REASON_NO_POLY_RESOLUTION)
        if c.kalshi_markets and not c.kalshi_with_resolution:
            reasons.append(REASON_NO_KALSHI_RESOLUTION)
        if c.polymarket_markets and c.kalshi_markets and not (
            c.polymarket_yes_scale and c.kalshi_yes_scale
        ):
            reasons.append(REASON_NO_YES_SCALE_OUTCOME)
        return (not reasons), reasons
