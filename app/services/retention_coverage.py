"""RETENTION-COVERAGE-001 — read-only retention coverage analysis.

Answers, for every significant table: how big is it, how fast is it growing,
what retains it today, who reads it, and what a bounded prune would actually be
eligible to remove.

This module DELETES NOTHING and has no `--confirm` path. It exists to make a
retention decision defensible, not to enact one. It makes zero provider calls,
writes no file, and creates no alert.

Two things it is careful to state honestly, because both are easy to get wrong:

1. **Logical pages are not filesystem bytes.** Deleting rows from a SQLite
   database in `journal_mode=delete` moves pages onto the freelist for reuse.
   The file itself is a high-water-mark ratchet and does not shrink. Reclaiming
   file bytes requires a separately-approved compaction (`VACUUM` / `VACUUM
   INTO`), which this milestone does not authorize.
2. **A window with nothing older than it removes nothing.** Every projection
   here is measured against real timestamps, never extrapolated past the
   history the database actually holds.
"""

import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import Base
from app.services.retention import PROTECTED_TABLES, RetentionConfig

logger = logging.getLogger(__name__)

# Retention classification (Gate B3).
RETAIN_INDEFINITELY = "retain_indefinitely"
RETAIN_LONG_WINDOW = "retain_long_window"
RETAIN_AGGREGATED_ONLY = "retain_aggregated_only"
SAFE_FOR_BOUNDED_PRUNING = "safe_for_bounded_pruning"
BLOCKED_PENDING_DEPENDENCY_REVIEW = "blocked_pending_dependency_review"

# Candidate timestamp columns, in preference order.
TIMESTAMP_COLUMNS = ("created_at", "captured_at", "observed_at", "started_at",
                     "recorded_at", "bucket_start")

# Growth-attribution windows. Reported ONLY where the table's own history
# actually covers them — a 30d number over 20d of data is a lie, not an estimate.
GROWTH_WINDOWS_DAYS = (1, 7, 14, 30)

# Minimum retention windows that are evidence-backed rather than chosen to force
# the database under a size gate. See docs/RETENTION_COVERAGE_2026_08.md §4.
PRESERVATION_FLOORS = {
    "market_price_ticks": (
        2,
        "Raw ticks are the only way to validate that OPS-012 bucket "
        "aggregation is correct. Below ~2 days there is no overlap left to "
        "re-derive a bucket from its own inputs.",
    ),
    "market_price_tick_buckets": (
        90,
        "The aggregated series is what survives raw-tick pruning; it is the "
        "long-horizon record and its 90d window is already the floor.",
    ),
    "market_forecasts": (
        None,
        "Unresolved forecasts must outlive their markets — a forecast deleted "
        "before its outcome arrives can never be scored.",
    ),
    "market_outcomes": (None, "Settlement truth; calibration is unreconstructable without it."),
    "forecast_scores": (None, "Current calibration evidence (ADR-004 gate)."),
    "crypto_horizon_cohorts": (None, "Frozen research cohorts — canary evidence."),
    "crypto_horizon_cohort_members": (None, "Frozen cohort membership."),
    "crypto_horizon_observations": (None, "Canary observation evidence."),
    "marketops_runs": (
        30,
        "The audit spine every deployment proof in docs/ cites; also the only "
        "record of hook behaviour per cycle.",
    ),
    "marketops_alerts": (
        None,
        "Operational incident history, including the DB-GROWTH-ALERT-IDENTITY-001 "
        "reconciliation record.",
    ),
    "crypto_token_birth_events": (None, "Lifecycle anchors — the tape cannot be replayed without them."),
    "crypto_token_survival_outcomes": (None, "Measured survival labels."),
}

# Readers/dependencies per table, traced from the code (Gate B3). A table with
# an unresolved reader is BLOCKED, not "probably fine".
DEPENDENCIES = {
    "market_price_ticks": ["db_growth report", "tick aggregation (OPS-012/013)",
                           "edge precheck market-snapshot freshness", "frontier_eval microstructure"],
    "market_price_tick_buckets": ["tick-aggregation-report", "db_growth report"],
    "market_snapshots": ["scanner/ranking", "eligibility assessment", "cross-venue matcher",
                         "edge precheck midpoint", "frontier_eval"],
    "opportunity_signals": ["signal workflow", "MarketOps promotion", "frontier_eval latency"],
    "crypto_token_discovery_events": ["crypto tape (birth anchors)", "provider health",
                                      "crypto coverage forensics"],
    "crypto_token_risk_assessments": ["risk engine", "provider budget accounting",
                                      "MEME-MAS agents", "crypto retrospect"],
    "crypto_price_ticks": ["crypto tape survival horizons", "horizon observations"],
    "market_forecasts": ["calibration scoring", "champion/challenger", "edge precheck"],
    "market_outcomes": ["calibration scoring", "outcome reconciliation"],
    "forecast_scores": ["calibration", "champion/challenger", "frontier_eval"],
    "marketops_runs": ["marketops-report", "crypto horizon orchestrator health gate",
                       "all deployment evidence"],
    "marketops_alerts": ["marketops-report", "marketops-alerts", "operator triage"],
}


@dataclass
class TableCoverage:
    table: str
    rows: int
    data_bytes: int | None
    index_bytes: int | None
    total_bytes: int | None
    percent_of_database: float | None
    timestamp_column: str | None
    oldest: str | None
    newest: str | None
    history_days: float | None
    retention_days: int | None          # None => retained indefinitely
    protected: bool
    classification: str
    eligible_now: int | None            # rows past the CURRENT window
    eligible_now_bytes: int | None
    rows_added: dict = field(default_factory=dict)   # window label -> count (measured only)
    bytes_per_row: float | None = None
    readers: list = field(default_factory=list)
    preservation_floor_days: int | None = None
    preservation_floor_reason: str | None = None


@dataclass
class RetentionCoverageReport:
    generated_at: str
    database_bytes: int | None
    page_count: int | None
    page_size: int | None
    freelist_pages: int | None
    freelist_bytes: int | None
    freelist_percent: float | None
    warning_mb: float
    critical_mb: float
    over_critical_by_mb: float | None
    dbstat_available: bool
    tables: list
    external_calls: int = 0
    persisted: bool = False
    deletes_performed: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sqlite_path(settings: Settings) -> str | None:
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    return url.database


def _page_stats(session: Session) -> tuple[int | None, int | None, int | None]:
    try:
        pc = session.execute(text("PRAGMA page_count")).scalar()
        ps = session.execute(text("PRAGMA page_size")).scalar()
        fl = session.execute(text("PRAGMA freelist_count")).scalar()
        return pc, ps, fl
    except Exception:  # pragma: no cover - non-SQLite
        logger.debug("page stats unavailable", exc_info=True)
        return None, None, None


def _dbstat(session: Session) -> dict | None:
    """Per-object page bytes. Returns None when the running SQLite lacks the
    optional dbstat module.

    NOTE: this walks every page of every table. It is fine on a disposable or
    modest database, but on a large production file under `journal_mode=delete`
    a long read lock can block the concurrent writer's COMMIT. Callers that
    need it on a big production database should run it against a decompressed
    BACKUP snapshot instead — which is exactly how the measurements in
    docs/RETENTION_COVERAGE_2026_08.md were taken.
    """
    try:
        rows = session.execute(
            text("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")
        ).all()
    except Exception:
        logger.debug("dbstat unavailable", exc_info=True)
        return None
    return {name: (size or 0) for name, size in rows}


def _index_owner(session: Session) -> dict:
    try:
        rows = session.execute(
            text("SELECT name, tbl_name FROM sqlite_master WHERE type='index'")
        ).all()
    except Exception:  # pragma: no cover - non-SQLite
        return {}
    return {name: owner for name, owner in rows}


def _classify(table: str, retention_days: int | None, protected: bool) -> str:
    if table in PRESERVATION_FLOORS and PRESERVATION_FLOORS[table][0] is None:
        return RETAIN_INDEFINITELY
    if table == "market_price_ticks":
        return RETAIN_AGGREGATED_ONLY
    if protected and retention_days is None:
        # Protected AND unbounded: growing forever with a documented reader.
        # That is precisely the case that needs a human decision, not a default.
        return BLOCKED_PENDING_DEPENDENCY_REVIEW
    if retention_days is None:
        return BLOCKED_PENDING_DEPENDENCY_REVIEW
    if retention_days >= 30:
        return RETAIN_LONG_WINDOW
    return SAFE_FOR_BOUNDED_PRUNING


def _retention_days_for(table: str, cfg: RetentionConfig) -> int | None:
    mapping = {
        "market_price_ticks": cfg.tick_days,
        "market_price_tick_buckets": cfg.tick_bucket_days,
        "watcher_runs": cfg.watcher_run_days,
        "crypto_price_ticks": cfg.crypto_days,
        "crypto_watcher_runs": cfg.crypto_days,
        "pipeline_runs": cfg.pipeline_run_days,
        "pipeline_stage_runs": cfg.pipeline_run_days,
        "meme_scout_runs": cfg.meme_days,
        "meme_attention_snapshots": cfg.meme_days,
        "meme_catalyst_events": cfg.meme_days,
        "polymarket_scout_runs": cfg.polymarket_days,
        "polymarket_markets": cfg.polymarket_days,
        "polymarket_orderbook_snapshots": cfg.polymarket_days,
        "tick_aggregation_runs": cfg.pipeline_run_days,
    }
    if table == "opportunity_signals":
        # 0 means "keep forever" in RetentionConfig.
        return cfg.signal_days or None
    return mapping.get(table)


def build_retention_coverage(
    session: Session,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    include_dbstat: bool = True,
    min_rows: int = 1,
) -> RetentionCoverageReport:
    """Assemble the read-only coverage report. Deletes nothing, writes nothing."""
    settings = settings or get_settings()
    cfg = RetentionConfig.from_settings(settings)
    now = now or _now()

    pc, ps, fl = _page_stats(session)
    db_bytes = None
    path = _sqlite_path(settings)
    if path and os.path.exists(path):
        db_bytes = os.path.getsize(path)
    elif pc and ps:
        db_bytes = pc * ps

    sizes = _dbstat(session) if include_dbstat else None
    owners = _index_owner(session) if sizes else {}
    index_bytes: dict = {}
    if sizes:
        for name, byte_count in sizes.items():
            owner = owners.get(name)
            if owner is None and name.startswith("sqlite_autoindex_"):
                owner = name[len("sqlite_autoindex_"):].rsplit("_", 1)[0]
            if owner:
                index_bytes[owner] = index_bytes.get(owner, 0) + byte_count

    tables: list[TableCoverage] = []
    for name in sorted(Base.metadata.tables):
        table = Base.metadata.tables[name]
        try:
            rows = session.execute(select(func.count()).select_from(table)).scalar() or 0
        except Exception:  # pragma: no cover - table absent on an older schema
            continue
        if rows < min_rows:
            continue

        columns = {c.name for c in table.columns}
        tcol = next((c for c in TIMESTAMP_COLUMNS if c in columns), None)
        oldest = newest = None
        history_days = None
        added: dict = {}
        eligible = eligible_bytes = None

        if tcol is not None:
            col = table.c[tcol]
            oldest_dt, newest_dt = session.execute(
                select(func.min(col), func.max(col))
            ).one()
            oldest = str(oldest_dt) if oldest_dt else None
            newest = str(newest_dt) if newest_dt else None
            if oldest_dt is not None and newest_dt is not None:
                try:
                    history_days = round(
                        (newest_dt - oldest_dt).total_seconds() / 86400.0, 2
                    )
                except TypeError:  # pragma: no cover - mixed tz round-trip
                    history_days = None

            for days in GROWTH_WINDOWS_DAYS:
                # Only report a window the table's own history actually covers.
                if history_days is not None and days > history_days + 1:
                    continue
                cutoff = now - timedelta(days=days)
                added[f"{days}d"] = session.execute(
                    select(func.count()).select_from(table).where(col >= cutoff)
                ).scalar() or 0

        data_b = sizes.get(name) if sizes else None
        index_b = index_bytes.get(name) if sizes else None
        total_b = (data_b or 0) + (index_b or 0) if sizes else None
        per_row = (total_b / rows) if (total_b and rows) else None

        retention_days = _retention_days_for(name, cfg)
        protected = name in PROTECTED_TABLES
        if retention_days is not None and tcol is not None:
            cutoff = now - timedelta(days=retention_days)
            eligible = session.execute(
                select(func.count()).select_from(table).where(table.c[tcol] < cutoff)
            ).scalar() or 0
            eligible_bytes = int(eligible * per_row) if per_row else None

        floor_days, floor_reason = PRESERVATION_FLOORS.get(name, (None, None))
        tables.append(TableCoverage(
            table=name,
            rows=rows,
            data_bytes=data_b,
            index_bytes=index_b,
            total_bytes=total_b,
            percent_of_database=(
                round(100.0 * total_b / db_bytes, 2)
                if (total_b and db_bytes) else None
            ),
            timestamp_column=tcol,
            oldest=oldest,
            newest=newest,
            history_days=history_days,
            retention_days=retention_days,
            protected=protected,
            classification=_classify(name, retention_days, protected),
            eligible_now=eligible,
            eligible_now_bytes=eligible_bytes,
            rows_added=added,
            bytes_per_row=round(per_row, 1) if per_row else None,
            readers=DEPENDENCIES.get(name, []),
            preservation_floor_days=floor_days,
            preservation_floor_reason=floor_reason,
        ))

    # Biggest first. With --no-dbstat there are no byte counts, so fall back to
    # row count rather than silently presenting an alphabetical list as if it
    # were a ranking.
    if sizes:
        tables.sort(key=lambda t: -(t.total_bytes or 0))
    else:
        tables.sort(key=lambda t: -t.rows)
    size_mb = (db_bytes / (1024 * 1024)) if db_bytes else None
    return RetentionCoverageReport(
        generated_at=now.isoformat(),
        database_bytes=db_bytes,
        page_count=pc,
        page_size=ps,
        freelist_pages=fl,
        freelist_bytes=(fl * ps) if (fl is not None and ps) else None,
        freelist_percent=(round(100.0 * fl / pc, 2) if (fl is not None and pc) else None),
        warning_mb=settings.db_growth_warning_mb,
        critical_mb=settings.db_growth_critical_mb,
        over_critical_by_mb=(
            round(size_mb - settings.db_growth_critical_mb, 1)
            if size_mb is not None else None
        ),
        dbstat_available=sizes is not None,
        tables=[asdict(t) for t in tables],
    )
