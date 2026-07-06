"""Domain-expansion scout (MEME-NEWS-001, Part C): a read-only market-domain
inventory over the probability markets we have already scanned. It groups
markets by domain / series prefix and reports coverage + a candidate
`canary_priority` so a human can decide which domain (weather, tennis,
basketball, golf, esports, ...) is worth a future forecasting canary.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): inventory + prioritization
INTELLIGENCE only. It adds NO forecaster, changes NO MarketOps promotion / edge
/ forecast logic, and is never advice. No EV, no trade, no sizing, no orders,
no wallets/keys/swaps/signing/execution. Reads persisted Market / MarketSnapshot
/ resolution rows only — it issues no new Kalshi calls.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    DomainMarketInventorySnapshot,
    DomainScoutRun,
    Market,
    MarketResolutionAssessment,
    MarketSnapshot,
)
from app.services.research import DOMAIN_GENERAL, DOMAIN_RULES

logger = logging.getLogger(__name__)

# Domains that already have an evidence-aware forecaster canary.
FORECASTER_DOMAINS = ("sports_baseball", "sports_soccer")

ACTIVE_STATUSES = ("active", "open")

# Series prefixes that currently classify as "general" but are real candidate
# expansion domains — used for REPORTING/priority only (does not change
# research.classify_domain or any promotion logic).
CANDIDATE_DOMAIN_HINTS = {
    "KXNBA": "basketball", "KXWNBA": "basketball", "KXNCAAB": "basketball",
    "KXPGA": "golf", "KXGOLF": "golf", "KXLIV": "golf", "KXMASTERS": "golf",
    "KXLOL": "esports", "KXCS": "esports", "KXCSGO": "esports",
    "KXDOTA": "esports", "KXVAL": "esports", "KXESPORT": "esports",
    "KXNFL": "football", "KXNCAAF": "football",
    "KXNHL": "hockey",
}

# Public data-source availability notes per candidate domain (read-only hints).
DATA_SOURCE_NOTES = {
    "sports_baseball": "MLB Stats API (live canary)",
    "sports_soccer": "ESPN API (live canary)",
    "sports_tennis": "ESPN/ATP/WTA public feeds",
    "weather": "NOAA/NWS public API",
    "basketball": "ESPN/NBA public feeds",
    "golf": "ESPN/PGA public feeds",
    "esports": "HLTV/Liquipedia feeds — availability TBD",
    "football": "ESPN/NFL public feeds",
    "hockey": "ESPN/NHL public feeds",
    "crypto": "DexScreener (in-scope crypto lane)",
    "macro": "FRED/BLS public data",
    "politics": "official results feeds",
    "general": "varies; classify further before a canary",
}

# canary_priority weights (sum 1.0)
W_SUPPLY = 0.25
W_TWO_SIDED = 0.25
W_LIQUIDITY = 0.15
W_CLARITY = 0.15
W_FORECASTER_GAP = 0.10
W_DATA_SOURCE = 0.10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _classify(ticker: str, title: str | None, category: str | None, rules: str | None) -> str:
    """Domain for a persisted market (prefix first, then keywords), mirroring
    research.classify_domain but operating on ORM fields."""
    upper = (ticker or "").upper()
    for domain, markers, _kw in DOMAIN_RULES:
        if any(upper.startswith(m) for m in markers):
            return domain
    text = " ".join(x for x in (title, category, rules) if x).lower()
    for domain, _m, keywords in DOMAIN_RULES:
        if any(k in text for k in keywords):
            return domain
    return DOMAIN_GENERAL


def _label_for(ticker: str, domain: str) -> str:
    """Report label: refine an otherwise-'general' market into a candidate
    expansion domain by series prefix (basketball/golf/esports/...)."""
    if domain != DOMAIN_GENERAL:
        return domain
    prefix = (ticker or "").upper().split("-", 1)[0]
    return CANDIDATE_DOMAIN_HINTS.get(prefix, DOMAIN_GENERAL)


def _series_prefix(ticker: str) -> str:
    return (ticker or "").upper().split("-", 1)[0]


@dataclass
class _Agg:
    domain: str
    market_count: int = 0
    active_count: int = 0
    two_sided_count: int = 0
    volume_cents: int = 0
    liquidity_cents: int = 0
    clarity_values: list[float] = field(default_factory=list)
    series: dict[str, int] = field(default_factory=dict)


@dataclass
class DomainScoutReport:
    note: str
    run_id: int | None
    markets_scanned: int
    domains: list[dict]


class DomainScoutService:
    """Builds the read-only domain inventory; optionally persists an audit run."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _latest_snapshots(self, session: Session) -> dict[int, MarketSnapshot]:
        latest_ids = session.execute(
            select(func.max(MarketSnapshot.id)).group_by(MarketSnapshot.market_id)
        ).scalars().all()
        if not latest_ids:
            return {}
        rows = session.execute(
            select(MarketSnapshot).where(MarketSnapshot.id.in_(latest_ids))
        ).scalars().all()
        return {s.market_id: s for s in rows}

    def _latest_clarity(self, session: Session) -> dict[str, float]:
        rows = session.execute(
            select(
                MarketResolutionAssessment.market_ticker,
                MarketResolutionAssessment.clarity_score,
                MarketResolutionAssessment.id,
            ).order_by(MarketResolutionAssessment.id.desc())
        ).all()
        out: dict[str, float] = {}
        for ticker, clarity, _id in rows:
            if ticker not in out and clarity is not None:
                out[ticker] = clarity
        return out

    def build(self, session: Session, persist: bool = True) -> DomainScoutReport:
        now = _now()
        run = DomainScoutRun(status="running", started_at=now, created_at=now)
        if persist:
            session.add(run)
            session.flush()

        markets = session.execute(select(Market)).scalars().all()
        snaps = self._latest_snapshots(session)
        clarity = self._latest_clarity(session)

        aggs: dict[str, _Agg] = {}
        for m in markets:
            domain = _classify(m.ticker, m.title, m.category, m.rules_primary)
            label = _label_for(m.ticker, domain)
            agg = aggs.setdefault(label, _Agg(domain=label))
            agg.market_count += 1
            if (m.status or "").lower() in ACTIVE_STATUSES:
                agg.active_count += 1
            prefix = _series_prefix(m.ticker)
            agg.series[prefix] = agg.series.get(prefix, 0) + 1
            snap = snaps.get(m.id)
            if snap is not None:
                agg.volume_cents += snap.volume_24h or 0
                agg.liquidity_cents += snap.liquidity or 0
                if (snap.yes_bid or 0) > 0 and (snap.yes_ask or 0) > 0:
                    agg.two_sided_count += 1
            c = clarity.get(m.ticker)
            if c is not None:
                agg.clarity_values.append(c)

        # run-relative normalizers (avoid magic absolute scales)
        max_active = max((a.active_count for a in aggs.values()), default=0) or 1
        max_liq = max((a.liquidity_cents for a in aggs.values()), default=0) or 1

        domains: list[dict] = []
        for label, a in aggs.items():
            two_sided_rate = round(a.two_sided_count / a.market_count, 4) if a.market_count else None
            clarity_proxy = (
                round(sum(a.clarity_values) / len(a.clarity_values), 4)
                if a.clarity_values else None
            )
            has_forecaster = label in FORECASTER_DOMAINS
            note = DATA_SOURCE_NOTES.get(label, "varies; classify further before a canary")
            data_source_available = 0.0 if note.startswith("varies") or "TBD" in note else 1.0

            supply_score = min(a.active_count / max_active, 1.0)
            liq_score = min(a.liquidity_cents / max_liq, 1.0)
            forecaster_gap = 0.0 if has_forecaster else 1.0
            components = {
                "supply": round(supply_score, 4),
                "two_sided_rate": two_sided_rate or 0.0,
                "liquidity": round(liq_score, 4),
                "resolution_clarity": clarity_proxy or 0.0,
                "forecaster_gap": forecaster_gap,
                "data_source_available": data_source_available,
            }
            priority = round(
                W_SUPPLY * supply_score
                + W_TWO_SIDED * (two_sided_rate or 0.0)
                + W_LIQUIDITY * liq_score
                + W_CLARITY * (clarity_proxy or 0.0)
                + W_FORECASTER_GAP * forecaster_gap
                + W_DATA_SOURCE * data_source_available,
                4,
            )
            top_series = sorted(a.series.items(), key=lambda i: -i[1])
            row = {
                "domain": label,
                "series_prefix": top_series[0][0] if top_series else None,
                "series": dict(top_series[:6]),
                "market_count": a.market_count,
                "active_count": a.active_count,
                "two_sided_count": a.two_sided_count,
                "two_sided_rate": two_sided_rate,
                "volume_proxy_cents": a.volume_cents,
                "liquidity_proxy_cents": a.liquidity_cents,
                "resolution_clarity_proxy": clarity_proxy,
                "has_evidence_forecaster": has_forecaster,
                "data_source_notes": note,
                "canary_priority": priority,
                "priority_components": components,
            }
            domains.append(row)

            if persist:
                session.add(
                    DomainMarketInventorySnapshot(
                        run_id=run.id,
                        domain=label,
                        series_prefix=row["series_prefix"],
                        market_count=a.market_count,
                        active_count=a.active_count,
                        two_sided_count=a.two_sided_count,
                        two_sided_rate=two_sided_rate,
                        volume_proxy_cents=a.volume_cents,
                        liquidity_proxy_cents=a.liquidity_cents,
                        resolution_clarity_proxy=clarity_proxy,
                        has_evidence_forecaster=has_forecaster,
                        data_source_notes=note[:256],
                        canary_priority=priority,
                        priority_components={**components, "series": dict(top_series[:6])},
                        observed_at=now,
                        created_at=now,
                    )
                )

        domains.sort(key=lambda d: -(d["canary_priority"] or 0))

        if persist:
            run.status = "ok"
            run.markets_scanned = len(markets)
            run.domains_seen = len(aggs)
            run.series_seen = len({p for a in aggs.values() for p in a.series})
            run.finished_at = _now()
            run.duration_ms = int((run.finished_at - now).total_seconds() * 1000)
            session.commit()

        return DomainScoutReport(
            note=(
                "Read-only domain inventory + candidate canary priority. Adds no "
                "forecaster, changes no promotion/forecast logic, and is never advice. "
                "has_evidence_forecaster marks domains already covered; canary_priority "
                "ranks expansion candidates only."
            ),
            run_id=run.id if persist else None,
            markets_scanned=len(markets),
            domains=domains,
        )
