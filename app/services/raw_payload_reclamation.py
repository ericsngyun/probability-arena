"""RAW-PAYLOAD-RECLAMATION-001 — bounded historical raw-payload reclamation.

`RAW-PAYLOAD-STORAGE-001` stopped writing unread provider bodies **prospectively**
and deliberately touched no historical row. This replaces eligible historical
bodies with the SAME canonical envelope the live writer produces.

It is the only genuinely destructive step in the sequence: a reclaimed body is
gone, and the only recovery is a backup restore. Everything here is shaped by
that asymmetry — a registry instead of an arbitrary table interface, a
preservation floor tied to actual backup coverage, small batches with per-batch
verification, and a stop-on-anything posture rather than a retry loop.

What it does NOT do: delete a row, touch a normalized column, touch a timestamp,
run VACUUM, change a pragma, change a writer's transaction, or shrink the file.
Reclaimed pages go to the freelist and are reused; the SQLite file length is a
high-water mark. Compaction is SQLITE-COMPACT-COPY-001 and is not authorized.
"""

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import Base
from app.services.raw_payload_policy import (
    CAPTURE_MODE_NONE,
    GOVERNED_COLUMNS,
    MIN_SUPPRESSIBLE_BYTES,
    PINNED_FULL,
    SUPPRESSED_MARKER,
    provenance_envelope,
)

logger = logging.getLogger(__name__)

SUPPRESSED_LIKE = f'%"{SUPPRESSED_MARKER}"%'

# ---------------------------------------------------------------------------
# Bounds. Deliberately conservative for a first run: this competes with a 60 s
# watcher and a 5-minute MarketOps on a 4.4 GiB database in journal_mode=delete,
# where a long write lock turns recoverable waits into hard failures.
# ---------------------------------------------------------------------------
# 500, not 2000: each row is an individual UPDATE rewriting a page that
# holds a ~2 KiB payload, and the whole batch holds one write lock. The
# host has recorded 4 database_locked events EVER, so the bar for adding
# lock pressure is high and a first run should be boring.
MAX_ROWS_PER_BATCH = 500
MAX_BYTES_PER_BATCH = 8 * 1024 * 1024      # ~8 MiB of payload read per batch
MAX_BATCH_SECONDS = 10.0                    # a batch that runs long is stopped
MAX_TOTAL_SECONDS = 600.0                   # hard ceiling on a whole run
MAX_BATCHES = 500
LOCK_RETRY_ATTEMPTS = 3
LOCK_RETRY_SECONDS = 2.0

# A backup older than this is not adequate cover for an irreversible operation.
REQUIRED_BACKUP_MAX_AGE_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class ReclamationTarget:
    """One reviewed table.column. There is deliberately NO way to reclaim a
    table/column that is not in this registry — the CLI takes a target name,
    never a table name."""

    table: str
    column: str
    timestamp_column: str
    preservation_days: int
    source: str
    rationale: str

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"


# The registry. Every entry is a GOVERNED column from RAW-PAYLOAD-STORAGE-001
# with no production reader, minus the ones excluded below.
RECLAMATION_TARGETS: dict[str, ReclamationTarget] = {
    "market_snapshots.raw_payload": ReclamationTarget(
        table="market_snapshots", column="raw_payload",
        timestamp_column="captured_at", preservation_days=7,
        source="kalshi_rest",
        rationale=(
            "327.6 MiB, never pruned, no production reader. The body's stated "
            "purpose is scan debugging, and debugging value lives in the last "
            "few baseline cycles — 7 days is ~42 cycles."
        ),
    ),
    "crypto_token_discovery_events.raw_payload": ReclamationTarget(
        table="crypto_token_discovery_events", column="raw_payload",
        timestamp_column="created_at", preservation_days=14,
        source="dexscreener",
        rationale=(
            "224.9 MiB, never pruned. The lifecycle tape reads structured "
            "columns, not this body — but it does COPY the body into "
            "crypto_token_birth_events (a sink nothing reads), so 14 days "
            "covers the tape's full 24h survival-horizon set with wide margin "
            "plus the readiness measurement epochs."
        ),
    ),
    "opportunity_signals.raw_payload": ReclamationTarget(
        table="opportunity_signals", column="raw_payload",
        timestamp_column="created_at", preservation_days=7,
        source="kalshi_rest",
        rationale=(
            "63.7 MiB, kept forever (signal_days=0). MarketOps promotion only "
            "considers signals inside a 60-minute freshness window, so 7 days "
            "is two orders of magnitude beyond any live read."
        ),
    ),
    "market_detail_enrichments.raw_market_detail": ReclamationTarget(
        table="market_detail_enrichments", column="raw_market_detail",
        timestamp_column="created_at", preservation_days=7,
        source="kalshi_rest",
        rationale=(
            "24.4 MiB, never pruned. NOT NULL, which the envelope satisfies. "
            "Enrichment is re-derivable by re-fetching the market."
        ),
    ),
    "market_detail_enrichments.raw_series_detail": ReclamationTarget(
        table="market_detail_enrichments", column="raw_series_detail",
        timestamp_column="created_at", preservation_days=7,
        source="kalshi_rest",
        rationale=(
            "17.5 MiB, never pruned. Series metadata is static reference "
            "data re-fetchable from the same endpoint; the normalized "
            "columns already carry everything any consumer reads."
        ),
    ),
    "market_detail_enrichments.raw_event_detail": ReclamationTarget(
        table="market_detail_enrichments", column="raw_event_detail",
        timestamp_column="created_at", preservation_days=7,
        source="kalshi_rest",
        rationale=(
            "6.6 MiB, never pruned, and the smallest of the three. Event "
            "metadata is re-fetchable and is frequently NULL already, "
            "which reclamation preserves as a genuine absence."
        ),
    ),
}

# Governed columns deliberately NOT reclaimable, and why. Kept as a constant so
# the exclusion is auditable rather than an absence someone has to notice.
EXCLUDED_FROM_RECLAMATION = {
    "market_price_ticks.raw_payload": (
        "SELF-EXPIRING. 877 MiB across 448k rows, but the table already carries "
        "a 2-day retention window — every one of those bodies is deleted by "
        "retention within 48h of prospective suppression, for free. Rewriting "
        "448,000 rows on a live 4.4 GiB database to beat retention to work it "
        "will do anyway is all risk and no durable benefit."
    ),
}


@dataclass
class TargetPlan:
    target: str
    table: str
    column: str
    preservation_days: int
    cutoff: str
    backup_cutoff: str
    effective_cutoff: str
    eligible_rows: int
    eligible_bytes: int
    estimated_replacement_bytes: int
    estimated_reclaimed_bytes: int
    preserved_recent_rows: int
    already_suppressed_rows: int
    below_min_size_rows: int
    null_rows: int
    total_rows: int
    rationale: str


@dataclass
class ReclamationReport:
    generated_at: str
    targets: list = field(default_factory=list)
    excluded: dict = field(default_factory=dict)
    pinned: list = field(default_factory=list)
    backup_ok: bool = False
    backup_detail: str = ""
    backup_created_at: str | None = None
    external_calls: int = 0
    persisted: bool = False
    rows_changed: int = 0
    prints_payload_contents: bool = False
    physical_file_shrinks: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _table_for(target: ReclamationTarget):
    table = Base.metadata.tables.get(target.table)
    if table is None or target.column not in table.c:
        raise LookupError(f"unknown reclamation target: {target.qualified}")
    return table


def resolve_target(name: str) -> ReclamationTarget:
    """Registry lookup. The ONLY way to name a table/column — there is no
    arbitrary-table interface, because a typo'd table name on an irreversible
    operation is not a recoverable mistake."""
    target = RECLAMATION_TARGETS.get(name)
    if target is None:
        raise LookupError(
            f"{name!r} is not a reviewed reclamation target. Valid targets: "
            f"{sorted(RECLAMATION_TARGETS)}"
        )
    if name in PINNED_FULL:  # pragma: no cover - registry is asserted disjoint
        raise LookupError(f"{name!r} is pinned to full capture and is never reclaimable")
    if name not in GOVERNED_COLUMNS:  # pragma: no cover - same
        raise LookupError(f"{name!r} is not governed by the capture policy")
    return target


def backup_precondition(settings: Settings | None = None) -> tuple[bool, str, str | None]:
    """(ok, detail, backup_created_at).

    Reclamation is irreversible, so the backup is the entire recovery story. It
    must be recent, verified, manifest-backed and structurally sound — which is
    exactly what SQLITE-BACKUP-FRESHNESS-ALERT-001's evaluator already proves,
    so this reuses it rather than inventing a second notion of "good backup".
    """
    from app.services.backup_freshness import evaluate_backup_freshness

    result = evaluate_backup_freshness(settings)
    if not result.healthy:
        return False, f"backup not healthy: {result.reason}", result.newest_verified_at
    if result.age_seconds is None or result.age_seconds > REQUIRED_BACKUP_MAX_AGE_SECONDS:
        return (
            False,
            f"backup is {result.age_seconds}s old, older than the "
            f"{REQUIRED_BACKUP_MAX_AGE_SECONDS}s reclamation requirement",
            result.newest_verified_at,
        )
    return (
        True,
        f"verified backup {result.newest_backup_filename} "
        f"({result.age_seconds:.0f}s old, alembic "
        f"{result.manifest_alembic_revision})",
        result.newest_verified_at,
    )


def _effective_cutoff(
    target: ReclamationTarget, now: datetime, backup_created_at: str | None
) -> tuple[datetime, datetime, datetime]:
    """(age_cutoff, backup_cutoff, effective_cutoff).

    Two independent floors, and the EARLIER of the two wins:

    * the target's own preservation window, which is about research/debug value;
    * the latest verified backup's instant, which is about recoverability. A row
      written after the newest backup is not in any backup, so reclaiming it has
      no recovery path at all. That floor is not negotiable per-table.
    """
    age_cutoff = now - timedelta(days=target.preservation_days)
    backup_cutoff = age_cutoff
    if backup_created_at:
        try:
            parsed = datetime.fromisoformat(backup_created_at)
            backup_cutoff = parsed if parsed.tzinfo else parsed.replace(
                tzinfo=timezone.utc)
        except ValueError:  # pragma: no cover - defensive
            backup_cutoff = age_cutoff
    return age_cutoff, backup_cutoff, min(age_cutoff, backup_cutoff)


def _eligible_where(table, target: ReclamationTarget, cutoff: datetime):
    col = table.c[target.column]
    ts = table.c[target.timestamp_column]
    return (
        col.is_not(None),
        # SQLAlchemy JSON stores Python None as the JSON literal `null`,
        # not SQL NULL, so IS NOT NULL alone does not exclude it.
        func.cast(col, String) != "null",
        col.not_like(SUPPRESSED_LIKE),
        func.length(col) > MIN_SUPPRESSIBLE_BYTES,
        ts < cutoff,
    )


def plan_target(
    session: Session,
    target: ReclamationTarget,
    *,
    now: datetime | None = None,
    backup_created_at: str | None = None,
) -> TargetPlan:
    """Read-only projection for one target. Writes nothing."""
    now = now or _now()
    table = _table_for(target)
    col = table.c[target.column]
    ts = table.c[target.timestamp_column]
    age_cutoff, backup_cutoff, cutoff = _effective_cutoff(
        target, now, backup_created_at)

    with session.no_autoflush:
        total = session.execute(
            select(func.count()).select_from(table)
        ).scalar() or 0
        eligible, eligible_bytes = session.execute(
            select(func.count(), func.coalesce(func.sum(func.length(col)), 0))
            .select_from(table).where(*_eligible_where(table, target, cutoff))
        ).one()
        already = session.execute(
            select(func.count()).select_from(table).where(col.like(SUPPRESSED_LIKE))
        ).scalar() or 0
        # SQLAlchemy JSON stores Python None as the JSON literal `null`, so
        # `IS NULL` alone finds none of them. Both forms count as "the provider
        # gave us nothing", and neither is reclaimable.
        is_absent = or_(col.is_(None), func.cast(col, String) == "null")
        nulls = session.execute(
            select(func.count()).select_from(table).where(is_absent)
        ).scalar() or 0
        preserved = session.execute(
            select(func.count()).select_from(table).where(
                ~is_absent, col.not_like(SUPPRESSED_LIKE), ts >= cutoff)
        ).scalar() or 0
        # Disjoint from `nulls` on purpose: a JSON `null` is 4 bytes and would
        # otherwise be double-counted as "too small to suppress".
        small = session.execute(
            select(func.count()).select_from(table).where(
                ~is_absent, col.not_like(SUPPRESSED_LIKE),
                func.length(col) <= MIN_SUPPRESSIBLE_BYTES)
        ).scalar() or 0

    # The envelope is a fixed shape; ~118 B as SQLAlchemy stores it.
    replacement = eligible * 118
    return TargetPlan(
        target=target.qualified, table=target.table, column=target.column,
        preservation_days=target.preservation_days,
        cutoff=age_cutoff.isoformat(), backup_cutoff=backup_cutoff.isoformat(),
        effective_cutoff=cutoff.isoformat(),
        eligible_rows=eligible, eligible_bytes=eligible_bytes,
        estimated_replacement_bytes=replacement,
        estimated_reclaimed_bytes=max(eligible_bytes - replacement, 0),
        preserved_recent_rows=preserved, already_suppressed_rows=already,
        below_min_size_rows=small, null_rows=nulls, total_rows=total,
        rationale=target.rationale,
    )


def build_report(
    session: Session,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    targets: list[str] | None = None,
) -> ReclamationReport:
    """Full read-only dry-run report. Writes nothing, calls no provider, and
    never reads a payload's contents — only its length."""
    settings = settings or get_settings()
    now = now or _now()
    ok, detail, backup_at = backup_precondition(settings)
    names = targets or sorted(RECLAMATION_TARGETS)
    plans = []
    for name in names:
        try:
            plans.append(asdict(plan_target(
                session, resolve_target(name), now=now,
                backup_created_at=backup_at)))
        except Exception as exc:
            # Per-target isolation: a schema this database does not carry (an
            # older snapshot, a partial restore) must not take the whole report
            # down. Report the gap; never guess a count.
            logger.warning("reclamation plan failed for %s: %s", name, exc)
            plans.append({
                "target": name, "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "eligible_rows": None, "eligible_bytes": None,
                "estimated_reclaimed_bytes": 0,
            })
    return ReclamationReport(
        generated_at=now.isoformat(), targets=plans,
        excluded=dict(EXCLUDED_FROM_RECLAMATION), pinned=sorted(PINNED_FULL),
        backup_ok=ok, backup_detail=detail, backup_created_at=backup_at,
    )


@dataclass
class BatchResult:
    batch: int
    rows_selected: int
    rows_changed: int
    bytes_before: int
    bytes_after: int
    duration_seconds: float
    verified: bool
    stopped: str | None = None


@dataclass
class RunResult:
    target: str
    started_at: str
    finished_at: str
    batches_attempted: int = 0
    batches_committed: int = 0
    rows_changed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    lock_retries: int = 0
    stop_reason: str = "completed"
    total_seconds: float = 0.0
    batches: list = field(default_factory=list)
    verified: bool = True
    persisted: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


class HealthCheck:
    """Between-batch health gate. Anything unexpected stops the run — there is
    no retry-until-it-works path, because the operation is irreversible."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def check(self, session: Session) -> str | None:
        """Returns a stop reason, or None when healthy."""
        try:
            ok, detail, _ = backup_precondition(self.settings)
            if not ok:
                return f"backup_precondition_failed: {detail}"
            from app.models import MarketOpsRun, WatcherRun

            latest = session.execute(
                select(MarketOpsRun).order_by(MarketOpsRun.id.desc()).limit(1)
            ).scalars().first()
            if latest is not None and latest.status == "error":
                return f"marketops_run_error: run {latest.id}"
            watcher = session.execute(
                select(WatcherRun).order_by(WatcherRun.id.desc()).limit(1)
            ).scalars().first()
            if watcher is not None and watcher.status == "error":
                return f"watcher_run_error: run {watcher.id}"
        except Exception as exc:
            return f"health_check_failed: {type(exc).__name__}"
        return None


def _is_locked(exc: BaseException) -> bool:
    return "database is locked" in str(exc).lower() or "database is busy" in str(exc).lower()


def reclaim_target(
    session: Session,
    target: ReclamationTarget,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    max_rows: int | None = None,
    max_batches: int = MAX_BATCHES,
    rows_per_batch: int = MAX_ROWS_PER_BATCH,
    bytes_per_batch: int = MAX_BYTES_PER_BATCH,
    max_batch_seconds: float = MAX_BATCH_SECONDS,
    max_total_seconds: float = MAX_TOTAL_SECONDS,
    health: HealthCheck | None = None,
    require_backup: bool = True,
) -> RunResult:
    """Replace eligible historical bodies with the canonical envelope, in
    bounded batches, verifying each one. Idempotent and resumable: a reclaimed
    row no longer matches the eligibility predicate, so re-running finds zero.
    """
    settings = settings or get_settings()
    now = now or _now()
    started = _now()
    result = RunResult(target=target.qualified, started_at=started.isoformat(),
                       finished_at=started.isoformat())

    ok, detail, backup_at = backup_precondition(settings)
    if require_backup and not ok:
        result.stop_reason = f"backup_precondition_failed: {detail}"
        result.persisted = False
        result.finished_at = _now().isoformat()
        return result

    table = _table_for(target)
    col = table.c[target.column]
    pk = list(table.primary_key.columns)[0]
    _age, _bk, cutoff = _effective_cutoff(target, now, backup_at)
    health = health or HealthCheck(settings)
    run_started = time.monotonic()
    run_deadline = run_started + max_total_seconds
    # High-water mark. Without it every batch re-scans from the first row,
    # skipping everything already reclaimed — O(n^2) over a 150k-row run, and
    # each scan re-reads payload pages. Rows are processed in ascending primary
    # key order, so carrying the last key forward is exact: anything below it is
    # either done or was deliberately skipped, and a skipped row is simply
    # picked up by the next RUN (the predicate still matches it).
    last_pk = None

    while result.batches_attempted < max_batches:
        if time.monotonic() > run_deadline:
            result.stop_reason = "max_total_seconds"
            break
        if max_rows is not None and result.rows_changed >= max_rows:
            result.stop_reason = "max_rows"
            break

        stop = health.check(session)
        if stop:
            result.stop_reason = stop
            break

        remaining = None if max_rows is None else max_rows - result.rows_changed
        limit = rows_per_batch if remaining is None else min(rows_per_batch, remaining)

        # Select the batch. Reading length() not content — the body itself is
        # only ever read inside the batch loop to build its own envelope.
        conditions = list(_eligible_where(table, target, cutoff))
        if last_pk is not None:
            conditions.append(pk > last_pk)
        with session.no_autoflush:
            rows = session.execute(
                select(pk, col, func.length(col))
                .where(*conditions).order_by(pk.asc()).limit(limit)
            ).all()
        if not rows:
            result.stop_reason = "completed"
            break

        # Byte budget: stop adding rows once the batch would exceed it.
        selected, batch_bytes = [], 0
        for row_pk, payload, length in rows:
            if selected and batch_bytes + (length or 0) > bytes_per_batch:
                break
            selected.append((row_pk, payload, length or 0))
            batch_bytes += length or 0
        if not selected:  # pragma: no cover - a single row over budget
            selected = [(rows[0][0], rows[0][1], rows[0][2] or 0)]
            batch_bytes = rows[0][2] or 0

        result.batches_attempted += 1
        batch_started = time.monotonic()
        changed = 0
        try:
            for row_pk, payload, _length in selected:
                envelope = provenance_envelope(
                    payload, source=target.source, mode=CAPTURE_MODE_NONE)
                # Re-check inside the UPDATE: the row must STILL hold a full
                # body. If a writer changed it since selection, skip it rather
                # than overwrite whatever is there now.
                outcome = session.execute(
                    table.update()
                    .where(pk == row_pk, col.not_like(SUPPRESSED_LIKE),
                           col.is_not(None))
                    .values({target.column: envelope})
                )
                changed += outcome.rowcount or 0
            session.commit()
        except Exception as exc:
            session.rollback()
            if _is_locked(exc):
                result.lock_retries += 1
                if result.lock_retries > LOCK_RETRY_ATTEMPTS:
                    result.stop_reason = "lock_retry_exhausted"
                    break
                time.sleep(LOCK_RETRY_SECONDS)
                continue
            result.stop_reason = f"batch_failed: {type(exc).__name__}"
            break

        elapsed = time.monotonic() - batch_started

        # Verify: every selected key must now be suppressed.
        keys = [row_pk for row_pk, _p, _l in selected]
        with session.no_autoflush:
            still_full = session.execute(
                select(func.count()).select_from(table).where(
                    pk.in_(keys), col.not_like(SUPPRESSED_LIKE), col.is_not(None))
            ).scalar() or 0
            after_bytes = session.execute(
                select(func.coalesce(func.sum(func.length(col)), 0))
                .select_from(table).where(pk.in_(keys))
            ).scalar() or 0
        verified = still_full == 0
        result.batches.append(asdict(BatchResult(
            batch=result.batches_attempted, rows_selected=len(selected),
            rows_changed=changed, bytes_before=batch_bytes,
            bytes_after=after_bytes, duration_seconds=round(elapsed, 3),
            verified=verified,
        )))
        if not verified:
            result.verified = False
            result.stop_reason = "verification_failed"
            break

        last_pk = selected[-1][0]
        result.batches_committed += 1
        result.rows_changed += changed
        result.bytes_before += batch_bytes
        result.bytes_after += after_bytes

        if elapsed > max_batch_seconds:
            result.stop_reason = "max_batch_seconds"
            break
    else:
        result.stop_reason = "max_batches"

    result.total_seconds = round(time.monotonic() - run_started, 3)
    result.finished_at = _now().isoformat()
    return result
