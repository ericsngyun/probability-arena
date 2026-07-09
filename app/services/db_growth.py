"""OPS-011 — database growth & retention observability (read-only).

Pure measurement over our own operational/audit tables: row counts, size
estimates, tick age buckets, per-domain tick distribution, and per-table
retention windows with dry-run pruning projections. This module changes no
alpha logic, no forecasts, no promotion/edge thresholds, and no trading
behavior — it only *reports* on storage. No EV, no advice, no execution.

SQLite per-table byte estimates use the `dbstat` virtual table when the
running SQLite is compiled with it; otherwise size-by-table is reported as
unavailable (row counts and the whole-file size always work)."""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import Base
from app.models import (
    CryptoPriceTick,
    CryptoTokenRiskAssessment,
    CryptoWatcherRun,
    EdgePrecheckSnapshot,
    MarketPriceTick,
    MarketPriceTickBucket,
    MemeAttentionSnapshot,
    MemeCatalystEvent,
)
from app.services.backup import backup_dir_stats
from app.services.research import DOMAIN_RULES
from app.services.retention import RetentionConfig

logger = logging.getLogger(__name__)

# Tick-age buckets (days). Ordered, non-overlapping; the last bucket is open.
TICK_AGE_BUCKETS = (1, 3, 7)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def domain_for_ticker(ticker: str) -> str:
    """Prefix-only domain classification (no title/category available for a
    bare tick row). Mirrors research.classify_domain's ticker-marker pass."""
    upper = (ticker or "").upper()
    for domain, markers, _keywords in DOMAIN_RULES:
        if any(upper.startswith(marker) for marker in markers):
            return domain
    return "general"


@dataclass
class TableStat:
    name: str
    rows: int
    est_mib: float | None = None  # None when dbstat is unavailable


@dataclass
class GrowthReport:
    database_url: str
    size_mib: float | None
    tables: list[TableStat]
    tick_total: int
    tick_by_age: dict[str, int]
    tick_by_domain: dict[str, int]
    tick_oldest: datetime | None
    tick_newest: datetime | None
    tick_last_hour: int
    tick_est_daily_mib: float | None
    edge_total: int
    edge_last_hour: int
    edge_last_24h: int
    crypto_tick_total: int
    crypto_tick_last_hour: int
    crypto_risk_total: int
    meme_attention_total: int
    meme_attention_last_hour: int
    meme_catalyst_total: int
    backups: tuple[int, float] | None
    retention: RetentionConfig
    thresholds: dict = field(default_factory=dict)
    # OPS-012 additions: which tickers dominate, what steady-state looks like
    # under the CURRENT retention window, aggregation state, and threshold status.
    tick_top_tickers: list = field(default_factory=list)  # [(ticker, rows), ...]
    tick_projected_steady_state_mib: float | None = None  # est_daily * retention_days
    tick_bucket_total: int = 0                             # aggregated buckets (OPS-012)
    above_warning: bool | None = None
    above_critical: bool | None = None

    @property
    def largest_tables(self) -> list[TableStat]:
        key = (lambda t: t.est_mib) if any(t.est_mib is not None for t in self.tables) else (
            lambda t: t.rows
        )
        return sorted(self.tables, key=lambda t: (key(t) or 0), reverse=True)


def _count(session: Session, model_or_table) -> int:
    return session.execute(select(func.count()).select_from(model_or_table)).scalar() or 0


def _count_since(session: Session, model, column, since: datetime) -> int:
    return session.execute(
        select(func.count()).select_from(model).where(column >= since)
    ).scalar() or 0


def _dbstat_sizes(session: Session) -> dict[str, float] | None:
    """Per-table byte size via the dbstat virtual table (MiB). None when the
    running SQLite lacks dbstat (it is an optional compile-time module)."""
    try:
        rows = session.execute(
            text("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")
        ).all()
    except Exception:
        logger.debug("dbstat unavailable", exc_info=True)
        return None
    return {name: (size or 0) / (1024 * 1024) for name, size in rows}


def _sqlite_file_mib(settings: Settings) -> float | None:
    try:
        url = make_url(settings.database_url)
        if url.get_backend_name() == "sqlite" and url.database and os.path.exists(url.database):
            return os.path.getsize(url.database) / (1024 * 1024)
    except Exception:  # pragma: no cover - defensive
        logger.debug("sqlite file size unavailable", exc_info=True)
    return None


def build_growth_report(session: Session, settings: Settings | None = None) -> GrowthReport:
    """Assemble the DB growth/retention snapshot. Read-only."""
    settings = settings or get_settings()
    now = _now()
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(hours=24)

    dbstat = _dbstat_sizes(session)
    tables: list[TableStat] = []
    for name in sorted(Base.metadata.tables):
        tbl = Base.metadata.tables[name]
        tables.append(
            TableStat(
                name=name,
                rows=_count(session, tbl),
                est_mib=(round(dbstat.get(name, 0.0), 3) if dbstat is not None else None),
            )
        )

    # market_price_ticks age buckets + domain distribution
    tick_total = _count(session, MarketPriceTick)
    tick_by_age: dict[str, int] = {}
    prev = 0
    for edge in TICK_AGE_BUCKETS:
        lo = now - timedelta(days=edge)
        hi = now - timedelta(days=prev)
        label = f"<{edge}d" if prev == 0 else f"{prev}-{edge}d"
        tick_by_age[label] = session.execute(
            select(func.count()).select_from(MarketPriceTick).where(
                MarketPriceTick.created_at >= lo, MarketPriceTick.created_at < hi
            )
        ).scalar() or 0
        prev = edge
    tick_by_age[f">{TICK_AGE_BUCKETS[-1]}d"] = session.execute(
        select(func.count()).select_from(MarketPriceTick).where(
            MarketPriceTick.created_at < now - timedelta(days=TICK_AGE_BUCKETS[-1])
        )
    ).scalar() or 0

    tick_by_domain: dict[str, int] = {}
    for (ticker, cnt) in session.execute(
        select(MarketPriceTick.market_ticker, func.count()).group_by(
            MarketPriceTick.market_ticker
        )
    ).all():
        dom = domain_for_ticker(ticker)
        tick_by_domain[dom] = tick_by_domain.get(dom, 0) + cnt

    oldest, newest = session.execute(
        select(func.min(MarketPriceTick.created_at), func.max(MarketPriceTick.created_at))
    ).one()
    tick_last_hour = _count_since(session, MarketPriceTick, MarketPriceTick.created_at, hour_ago)

    # Estimate raw-tick daily MiB from dbstat bytes-per-row * ticks/day
    tick_est_daily_mib = None
    if dbstat is not None and tick_total > 0:
        tick_mib = dbstat.get("market_price_ticks", 0.0)
        per_row_mib = tick_mib / tick_total
        tick_est_daily_mib = round(per_row_mib * tick_last_hour * 24, 2)

    # OPS-012: heaviest tickers, steady-state projection, threshold status
    retention_cfg = RetentionConfig.from_settings(settings)
    tick_top_tickers = [
        (ticker, cnt)
        for ticker, cnt in session.execute(
            select(MarketPriceTick.market_ticker, func.count())
            .group_by(MarketPriceTick.market_ticker)
            .order_by(func.count().desc())
            .limit(10)
        ).all()
    ]
    tick_projected_steady_state_mib = (
        round(tick_est_daily_mib * retention_cfg.tick_days, 2)
        if tick_est_daily_mib is not None
        else None
    )
    tick_bucket_total = _count(session, MarketPriceTickBucket)
    size_mib = _sqlite_file_mib(settings)
    above_warning = (
        size_mib > settings.db_growth_warning_mb if size_mib is not None else None
    )
    above_critical = (
        size_mib > settings.db_growth_critical_mb if size_mib is not None else None
    )

    return GrowthReport(
        database_url=make_url(settings.database_url).render_as_string(hide_password=True),
        size_mib=size_mib,
        tables=tables,
        tick_total=tick_total,
        tick_by_age=tick_by_age,
        tick_by_domain=dict(sorted(tick_by_domain.items(), key=lambda kv: kv[1], reverse=True)),
        tick_oldest=oldest,
        tick_newest=newest,
        tick_last_hour=tick_last_hour,
        tick_est_daily_mib=tick_est_daily_mib,
        tick_top_tickers=tick_top_tickers,
        tick_projected_steady_state_mib=tick_projected_steady_state_mib,
        tick_bucket_total=tick_bucket_total,
        above_warning=above_warning,
        above_critical=above_critical,
        edge_total=_count(session, EdgePrecheckSnapshot),
        edge_last_hour=_count_since(
            session, EdgePrecheckSnapshot, EdgePrecheckSnapshot.created_at, hour_ago
        ),
        edge_last_24h=_count_since(
            session, EdgePrecheckSnapshot, EdgePrecheckSnapshot.created_at, day_ago
        ),
        crypto_tick_total=_count(session, CryptoPriceTick),
        crypto_tick_last_hour=_count_since(
            session, CryptoPriceTick, CryptoPriceTick.created_at, hour_ago
        ),
        crypto_risk_total=_count(session, CryptoTokenRiskAssessment),
        meme_attention_total=_count(session, MemeAttentionSnapshot),
        meme_attention_last_hour=_count_since(
            session, MemeAttentionSnapshot, MemeAttentionSnapshot.created_at, hour_ago
        ),
        meme_catalyst_total=_count(session, MemeCatalystEvent),
        backups=backup_dir_stats(settings),
        retention=RetentionConfig.from_settings(settings),
        thresholds={
            "db_growth_warning_mb": settings.db_growth_warning_mb,
            "db_growth_critical_mb": settings.db_growth_critical_mb,
            "db_growth_warning_daily_mb": settings.db_growth_warning_daily_mb,
            "db_growth_window_hours": settings.db_growth_window_hours,
            "signal_flood_warning_per_hour": settings.marketops_signal_flood_warning_per_hour,
            "signal_flood_critical_per_hour": settings.marketops_signal_flood_critical_per_hour,
        },
    )
