"""OPS-012 — tick aggregation (operational storage/durability only).

Rolls raw `market_price_ticks` — the dominant SQLite growth driver (~62% of the
file on EVO-X2) — into fixed-interval `market_price_tick_buckets` (OHLC
midpoint, open/close bid/ask, spread/liquidity ranges, tick counts) so history
can be kept long-term at a fraction of the storage. Aggregated buckets are
TELEMETRY SUMMARIES for storage and later measurement — never trading signals:
no EV, no trade recommendation, no sizing, no orders, no wallets/keys/signing/
swaps/execution, and no behavior change to the watcher/MarketOps/EDGE-AUTO
lanes that produce or consume raw ticks.

Guarantees:
- **Raw ticks are never deleted here.** Only the retention service — explicitly
  invoked, with its own UNCHANGED raw-tick window — prunes raw rows. OPS-012
  does not shorten raw retention; the report only STAGES that recommendation
  for a future milestone once coverage is proven healthy.
- **Idempotent.** Buckets are keyed (market_ticker, bucket_start,
  bucket_seconds); a rerun over the same window recomputes and overwrites the
  same buckets to identical values, never duplicates.
- **Bounded.** Raw rows read per invocation are capped; when the cap stops the
  pass early it stops on an hour boundary and reports exactly what was covered
  — truncation is reported, never silent.
- **Honest about gaps.** A tick with no midpoint contributes no OHLC; a bucket
  where no tick carried a midpoint has NULL OHLC — values are never fabricated.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, null, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import MarketPriceTick, MarketPriceTickBucket, TickAggregationRun
from app.services.db_growth import domain_for_ticker

logger = logging.getLogger(__name__)

AGGREGATION_NOTE = (
    "Read-only-posture storage plumbing: aggregated tick buckets are telemetry "
    "summaries of our own raw quote snapshots — not EV, not a signal, not a "
    "recommendation, not an instruction. No sizing, orders, wallets, keys, "
    "swaps, signing, or execution. Raw ticks are never deleted by aggregation."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def bucket_start_for(observed_at: datetime, bucket_seconds: int) -> datetime:
    """Deterministic bucket floor: epoch-aligned so bucket boundaries are stable
    across runs regardless of when aggregation happens."""
    ts = _aware(observed_at).timestamp()
    floored = int(ts // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(floored, tz=timezone.utc)


@dataclass
class SubwindowStat:
    """One committed sub-window (OPS-013). Counters only — never advice."""

    start: datetime
    end: datetime
    rows_read: int = 0
    buckets_written: int = 0
    duration_ms: int = 0
    commit_ms: int = 0
    commit_retries: int = 0
    status: str = "ok"  # ok | failed | oversized_skipped


@dataclass
class AggregationStats:
    """What one aggregation pass did. Counters only — never advice."""

    window_start: datetime | None = None
    window_end: datetime | None = None
    bucket_seconds: int = 300
    subwindow_hours: int = 1
    rows_read: int = 0
    rows_skipped_unusable: int = 0   # no midpoint AND no bid/ask/spread/liquidity
    buckets_written: int = 0
    buckets_inserted: int = 0
    buckets_updated: int = 0
    truncated: bool = False          # row cap stopped the pass early (hour boundary)
    covered_until: datetime | None = None  # aggregation is complete up to here
    dry_run: bool = False
    duration_ms: int = 0
    # OPS-013: per-sub-window commit accounting. A failed or oversized window is
    # recorded LOUDLY here (and on the audit run row), never silently skipped —
    # an idempotent rerun repairs it.
    subwindows: list = field(default_factory=list)          # [SubwindowStat, ...]
    failed_windows: list = field(default_factory=list)      # ISO starts, commit failed
    oversized_windows: list = field(default_factory=list)   # ISO starts, loud runaway skip
    max_commit_ms: int = 0
    run_id: int | None = None                               # tick_aggregation_runs audit row


@dataclass
class _Acc:
    """Accumulator for one (ticker, bucket_start) group."""

    first_at: datetime | None = None
    last_at: datetime | None = None
    tick_count: int = 0
    mids: list = field(default_factory=list)      # (observed_at-ordered) midpoints
    open_bid: int | None = None
    close_bid: int | None = None
    open_ask: int | None = None
    close_ask: int | None = None
    spreads: list = field(default_factory=list)
    liquidity: list = field(default_factory=list)


class TickAggregationService:
    """Groups raw ticks into fixed buckets and upserts them idempotently.

    OPS-013: commits after EVERY sub-window (default 1h), so the SQLite write
    lock is held for seconds per commit instead of one long transaction — the
    OPS-012 full-window pass held a ~49s commit and collided with a MarketOps
    cycle. A commit that still fails after bounded retries is rolled back,
    recorded LOUDLY (stats + audit row), and the pass continues; the idempotent
    rerun repairs the hole. No window is ever skipped silently."""

    def __init__(self, settings: Settings | None = None):
        s = settings or get_settings()
        self.default_bucket_seconds = s.tick_aggregation_bucket_seconds
        self.max_rows = s.tick_aggregation_max_rows
        self.default_subwindow_hours = s.tick_aggregation_subwindow_hours
        self.busy_retries = s.tick_aggregation_busy_retries
        self.busy_retry_seconds = s.tick_aggregation_busy_retry_seconds
        self.max_rows_per_subwindow = s.tick_aggregation_max_rows_per_subwindow

    def aggregate(
        self,
        session: Session,
        hours: int = 24,
        bucket_seconds: int | None = None,
        dry_run: bool = False,
        max_rows: int | None = None,
        subwindow_hours: int | None = None,
        scheduled: bool = False,
    ) -> AggregationStats:
        """One bounded aggregation pass over raw ticks observed in the last
        `hours`, committed per sub-window. Upserts buckets; NEVER deletes or
        modifies raw ticks. With dry_run=True everything is computed and
        counted but nothing is written (no commits, no audit row)."""
        started = _now()
        bucket_seconds = int(
            self.default_bucket_seconds if bucket_seconds is None else bucket_seconds
        )
        if bucket_seconds <= 0 or 3600 % bucket_seconds != 0:
            raise ValueError(
                f"bucket_seconds must be a positive divisor of 3600, got {bucket_seconds}"
            )
        subwindow_hours = int(
            self.default_subwindow_hours if subwindow_hours is None else subwindow_hours
        )
        if subwindow_hours < 1:
            raise ValueError(f"subwindow_hours must be >= 1, got {subwindow_hours}")
        row_cap = int(max_rows or self.max_rows)

        window_start = started - timedelta(hours=max(1, int(hours)))
        # hour-aligned sub-windows: bucket_seconds divides 3600, so no bucket
        # ever spans a sub-window and a row-cap stop lands on a bucket boundary.
        cursor = bucket_start_for(window_start, 3600)

        stats = AggregationStats(
            window_start=window_start, window_end=started,
            bucket_seconds=bucket_seconds, subwindow_hours=subwindow_hours,
            dry_run=dry_run,
        )

        run = None
        if not dry_run:
            run = TickAggregationRun(
                status="running", scheduled=scheduled, started_at=started,
                window_hours=int(hours), subwindow_hours=subwindow_hours,
                bucket_seconds=bucket_seconds, created_at=started,
            )
            # tiny audit-row commit; apply_fn re-adds after any retry rollback
            ok, _ = self._commit_unit(session, lambda: session.add(run))
            if not ok:
                raise RuntimeError(
                    "tick aggregation: could not commit audit row (database locked)"
                )
            stats.run_id = run.id

        while cursor < started:
            sub_end = cursor + timedelta(hours=subwindow_hours)
            if stats.rows_read >= row_cap:
                stats.truncated = True
                logger.info(
                    "tick aggregation: row cap %d reached; covered until %s "
                    "(rerun to continue)", row_cap, cursor.isoformat(),
                )
                break
            self._aggregate_subwindow(session, stats, cursor, sub_end, bucket_seconds, dry_run)
            stats.covered_until = min(sub_end, started)
            cursor = sub_end

        stats.duration_ms = int((_now() - started).total_seconds() * 1000)
        if run is not None:
            def finalize_run() -> None:
                # re-applied on every retry attempt: a rollback would discard
                # these attribute changes, so they must be part of the unit
                run.status = "ok" if not stats.failed_windows else "error"
                if stats.failed_windows:
                    run.error_type = "SubwindowCommitFailed"
                    run.error_message = f"failed windows: {stats.failed_windows}"[:500]
                run.finished_at = _now()
                run.duration_ms = stats.duration_ms
                run.rows_read = stats.rows_read
                run.buckets_written = stats.buckets_written
                run.buckets_inserted = stats.buckets_inserted
                run.buckets_updated = stats.buckets_updated
                # null() (SQL NULL), not None: assigning None to a JSON column
                # stores JSON 'null', which defeats SQL IS NULL predicates.
                run.failed_windows = stats.failed_windows or null()
                run.oversized_windows = stats.oversized_windows or null()
                run.truncated = stats.truncated

            ok, _ = self._commit_unit(session, finalize_run)
            if not ok:  # pragma: no cover - close-commit exhaustion is a hard stop
                raise RuntimeError(
                    "tick aggregation: could not finalize audit row (database locked)"
                )
        return stats

    def _aggregate_subwindow(
        self,
        session: Session,
        stats: AggregationStats,
        start: datetime,
        end: datetime,
        bucket_seconds: int,
        dry_run: bool,
    ) -> None:
        """Aggregate + COMMIT one bucket-aligned sub-window. A failed commit is
        rolled back, recorded loudly, and the pass continues (idempotent rerun
        repairs it). An oversized window (runaway guard) is skipped loudly."""
        sub_started = _now()
        sub = SubwindowStat(start=start, end=end)

        row_count = session.execute(
            select(func.count()).select_from(MarketPriceTick).where(
                MarketPriceTick.observed_at >= start,
                MarketPriceTick.observed_at < end,
            )
        ).scalar() or 0
        if row_count > self.max_rows_per_subwindow:
            sub.status = "oversized_skipped"
            stats.oversized_windows.append(start.isoformat())
            stats.subwindows.append(sub)
            logger.warning(
                "tick aggregation: sub-window %s has %d rows (> cap %d) — SKIPPED "
                "loudly; rerun with a larger --max-rows-per-subwindow or smaller "
                "--subwindow-hours", start.isoformat(), row_count, self.max_rows_per_subwindow,
            )
            return

        rows = session.execute(
            select(MarketPriceTick)
            .where(
                MarketPriceTick.observed_at >= start,
                MarketPriceTick.observed_at < end,
            )
            .order_by(MarketPriceTick.observed_at.asc(), MarketPriceTick.id.asc())
        ).scalars().all()
        sub.rows_read = len(rows)
        stats.rows_read += len(rows)

        groups: dict[tuple[str, datetime], _Acc] = {}
        for t in rows:
            usable = any(
                v is not None
                for v in (t.midpoint, t.yes_bid, t.yes_ask, t.spread)
            ) or bool(t.liquidity_proxy)
            if not usable:
                stats.rows_skipped_unusable += 1
                continue
            key = (t.market_ticker, bucket_start_for(t.observed_at, bucket_seconds))
            acc = groups.setdefault(key, _Acc())
            observed = _aware(t.observed_at)
            if acc.first_at is None or observed < acc.first_at:
                acc.first_at = observed
            if acc.last_at is None or observed >= acc.last_at:
                acc.last_at = observed
                acc.close_bid = t.yes_bid if t.yes_bid is not None else acc.close_bid
                acc.close_ask = t.yes_ask if t.yes_ask is not None else acc.close_ask
            acc.tick_count += 1
            if t.midpoint is not None:
                acc.mids.append(t.midpoint)
            if acc.open_bid is None and t.yes_bid is not None:
                acc.open_bid = t.yes_bid
            if acc.open_ask is None and t.yes_ask is not None:
                acc.open_ask = t.yes_ask
            if t.spread is not None:
                acc.spreads.append(t.spread)
            if t.liquidity_proxy is not None:
                acc.liquidity.append(t.liquidity_proxy)

        # The (upsert + commit) pair is ONE retryable unit: a rollback discards
        # the pending upserts, so a bare commit-retry would commit an empty
        # transaction and falsely report success while losing the window's data.
        # apply_fn therefore re-applies every upsert on each attempt.
        counts = {"inserted": 0, "updated": 0, "written": 0}

        def apply_upserts() -> None:
            counts["inserted"] = counts["updated"] = counts["written"] = 0
            for (ticker, bstart), acc in groups.items():
                inserted = self._upsert_bucket(
                    session, ticker, bstart, bucket_seconds, acc, dry_run
                )
                counts["written"] += 1
                if inserted:
                    counts["inserted"] += 1
                else:
                    counts["updated"] += 1

        if dry_run:
            apply_upserts()
            sub.buckets_written = counts["written"]
        elif groups:
            commit_started = _now()
            ok, retries = self._commit_unit(session, apply_upserts)
            sub.commit_ms = int((_now() - commit_started).total_seconds() * 1000)
            sub.commit_retries = retries
            sub.buckets_written = counts["written"]
            stats.max_commit_ms = max(stats.max_commit_ms, sub.commit_ms)
            if not ok:
                sub.status = "failed"
                stats.failed_windows.append(start.isoformat())
                logger.error(
                    "tick aggregation: commit FAILED for sub-window %s after %d "
                    "retries — rolled back, continuing (rerun repairs it; never silent)",
                    start.isoformat(), retries,
                )
                sub.duration_ms = int((_now() - sub_started).total_seconds() * 1000)
                stats.subwindows.append(sub)
                return

        stats.buckets_written += counts["written"]
        stats.buckets_inserted += counts["inserted"]
        stats.buckets_updated += counts["updated"]
        sub.duration_ms = int((_now() - sub_started).total_seconds() * 1000)
        stats.subwindows.append(sub)

    def _commit_unit(self, session: Session, apply_fn) -> tuple[bool, int]:
        """Apply work + commit as ONE retryable unit, bounded. Returns
        (succeeded, retries_used).

        `apply_fn` is re-invoked on every attempt because an OperationalError
        rollback discards pending state — retrying only the commit would commit
        an empty transaction and falsely report success. On final failure the
        session is rolled back so the pass continues with a clean transaction."""
        retries = 0
        while True:
            try:
                apply_fn()
                session.commit()
                return True, retries
            except OperationalError:
                session.rollback()
                if retries >= self.busy_retries:
                    return False, retries
                retries += 1
                logger.warning(
                    "tick aggregation: commit hit a locked database — retry %d/%d "
                    "in %.1fs", retries, self.busy_retries, self.busy_retry_seconds,
                )
                time.sleep(self.busy_retry_seconds)

    @staticmethod
    def _upsert_bucket(
        session: Session,
        ticker: str,
        start: datetime,
        bucket_seconds: int,
        acc: _Acc,
        dry_run: bool,
    ) -> bool:
        """Insert or update the bucket row for (ticker, start, seconds).
        Returns True on insert, False on update. Deterministic: the same raw
        rows always produce byte-identical bucket values."""
        values = dict(
            domain=domain_for_ticker(ticker),
            open_mid=acc.mids[0] if acc.mids else None,
            high_mid=max(acc.mids) if acc.mids else None,
            low_mid=min(acc.mids) if acc.mids else None,
            close_mid=acc.mids[-1] if acc.mids else None,
            open_bid=acc.open_bid,
            close_bid=acc.close_bid,
            open_ask=acc.open_ask,
            close_ask=acc.close_ask,
            spread_min=min(acc.spreads) if acc.spreads else None,
            spread_max=max(acc.spreads) if acc.spreads else None,
            spread_avg=round(sum(acc.spreads) / len(acc.spreads), 4) if acc.spreads else None,
            liquidity_min=min(acc.liquidity) if acc.liquidity else None,
            liquidity_max=max(acc.liquidity) if acc.liquidity else None,
            liquidity_avg=(
                round(sum(acc.liquidity) / len(acc.liquidity), 2) if acc.liquidity else None
            ),
            tick_count=acc.tick_count,
            first_seen_at=acc.first_at,
            last_seen_at=acc.last_at,
        )
        existing = session.execute(
            select(MarketPriceTickBucket).where(
                MarketPriceTickBucket.market_ticker == ticker,
                MarketPriceTickBucket.bucket_start == start,
                MarketPriceTickBucket.bucket_seconds == bucket_seconds,
            )
        ).scalars().first()
        if dry_run:
            return existing is None
        if existing is not None:
            for k, v in values.items():
                setattr(existing, k, v)
            return False
        session.add(MarketPriceTickBucket(
            market_ticker=ticker, bucket_start=start, bucket_seconds=bucket_seconds,
            created_at=_now(), **values,
        ))
        return True


# --- report -------------------------------------------------------------------


@dataclass
class TickAggregationReport:
    note: str
    bucket_total: int = 0
    bucket_oldest: datetime | None = None
    bucket_newest: datetime | None = None
    buckets_by_domain: dict = field(default_factory=dict)
    buckets_by_seconds: dict = field(default_factory=dict)
    raw_total: int = 0
    raw_oldest: datetime | None = None
    raw_newest: datetime | None = None
    compression_ratio: float | None = None   # raw ticks per bucket row
    # coverage of the recent raw window by buckets (hour granularity)
    raw_hours_last_48h: int = 0
    covered_hours_last_48h: int = 0
    coverage_rate_last_48h: float | None = None
    coverage_healthy: bool = False
    retention: dict = field(default_factory=dict)
    staged_recommendation: str = ""
    # OPS-013: raw-retention-reduction READINESS (evidence, never enactment)
    coverage_rate_last_72h: float | None = None
    recent_runs: list = field(default_factory=list)         # last N audit rows
    clean_scheduled_cycles: int = 0
    runs_with_errors_recent: int = 0
    raw_feed_fresh: bool = False                             # watcher still writing raw
    readiness: str = "not_ready"                             # not_ready | ready_to_stage
    readiness_reasons: list = field(default_factory=list)


COVERAGE_HEALTHY_RATE = 0.95
# OPS-013 readiness gates (evidence thresholds for STAGING — never enacting —
# a raw-retention reduction; the reduction itself is a separate, explicitly
# accepted milestone):
READINESS_COVERAGE_RATE = 0.98      # hour coverage over the last 72h
READINESS_CLEAN_CYCLES = 5          # clean SCHEDULED runs required
READINESS_RAW_FRESH_MINUTES = 15    # watcher must still be writing raw ticks


class TickAggregationReportService:
    """Read-only aggregation coverage view + the staged (NOT enacted) retention
    recommendation. Changes nothing."""

    def build(self, session: Session, settings: Settings | None = None) -> TickAggregationReport:
        s = settings or get_settings()
        r = TickAggregationReport(note=AGGREGATION_NOTE)

        r.bucket_total = session.execute(
            select(func.count()).select_from(MarketPriceTickBucket)
        ).scalar() or 0
        r.raw_total = session.execute(
            select(func.count()).select_from(MarketPriceTick)
        ).scalar() or 0
        if r.bucket_total:
            r.bucket_oldest, r.bucket_newest = (
                _aware(v) for v in session.execute(
                    select(
                        func.min(MarketPriceTickBucket.bucket_start),
                        func.max(MarketPriceTickBucket.bucket_start),
                    )
                ).one()
            )
            for domain, count in session.execute(
                select(MarketPriceTickBucket.domain, func.count())
                .group_by(MarketPriceTickBucket.domain)
            ).all():
                r.buckets_by_domain[domain or "general"] = count
            for seconds, count in session.execute(
                select(MarketPriceTickBucket.bucket_seconds, func.count())
                .group_by(MarketPriceTickBucket.bucket_seconds)
            ).all():
                r.buckets_by_seconds[str(seconds)] = count
            aggregated_ticks = session.execute(
                select(func.sum(MarketPriceTickBucket.tick_count))
            ).scalar() or 0
            r.compression_ratio = (
                round(aggregated_ticks / r.bucket_total, 2) if r.bucket_total else None
            )
        if r.raw_total:
            r.raw_oldest, r.raw_newest = (
                _aware(v) for v in session.execute(
                    select(
                        func.min(MarketPriceTick.observed_at),
                        func.max(MarketPriceTick.observed_at),
                    )
                ).one()
            )

        # hour-granularity coverage: of the distinct raw-tick hours in a recent
        # window, how many have at least one bucket?
        def hour_coverage(hours: int) -> tuple[int, int, float | None]:
            cutoff = _now() - timedelta(hours=hours)
            raw_hours = {
                bucket_start_for(_aware(v), 3600)
                for (v,) in session.execute(
                    select(MarketPriceTick.observed_at).where(
                        MarketPriceTick.observed_at >= cutoff
                    )
                ).all()
            }
            bucket_hours = {
                bucket_start_for(_aware(v), 3600)
                for (v,) in session.execute(
                    select(MarketPriceTickBucket.bucket_start).where(
                        MarketPriceTickBucket.bucket_start >= cutoff
                    )
                ).all()
            }
            covered = len(raw_hours & bucket_hours)
            rate = round(covered / len(raw_hours), 4) if raw_hours else None
            return len(raw_hours), covered, rate

        r.raw_hours_last_48h, r.covered_hours_last_48h, r.coverage_rate_last_48h = hour_coverage(48)
        if r.coverage_rate_last_48h is not None:
            r.coverage_healthy = r.coverage_rate_last_48h >= COVERAGE_HEALTHY_RATE
        _, _, r.coverage_rate_last_72h = hour_coverage(72)

        # OPS-013 readiness evidence from the audit spine
        runs = session.execute(
            select(TickAggregationRun)
            .where(TickAggregationRun.status != "running")
            .order_by(TickAggregationRun.id.desc())
            .limit(10)
        ).scalars().all()
        r.recent_runs = [
            {
                "id": x.id, "status": x.status, "scheduled": x.scheduled,
                "started_at": x.started_at.isoformat() if x.started_at else None,
                "rows_read": x.rows_read, "buckets_written": x.buckets_written,
                "failed_windows": x.failed_windows, "truncated": x.truncated,
            }
            for x in runs
        ]
        r.runs_with_errors_recent = sum(
            1 for x in runs if x.status != "ok" or x.failed_windows
        )
        # Count in Python, not with an IS NULL predicate: rows written before
        # the null() fix hold JSON 'null' (not SQL NULL) in failed_windows, and
        # both must count as clean. Reads map either form to falsy None/[].
        scheduled_ok = session.execute(
            select(TickAggregationRun.failed_windows).where(
                TickAggregationRun.scheduled.is_(True),
                TickAggregationRun.status == "ok",
            )
        ).all()
        r.clean_scheduled_cycles = sum(1 for (fw,) in scheduled_ok if not fw)
        if r.raw_newest is not None:
            age_min = (_now() - r.raw_newest).total_seconds() / 60
            r.raw_feed_fresh = age_min <= READINESS_RAW_FRESH_MINUTES

        # readiness verdict (evidence for a FUTURE milestone; enacts nothing)
        reasons: list[str] = []
        if r.coverage_rate_last_72h is None or r.coverage_rate_last_72h < READINESS_COVERAGE_RATE:
            reasons.append(
                f"coverage_72h={r.coverage_rate_last_72h} < {READINESS_COVERAGE_RATE}"
            )
        if r.clean_scheduled_cycles < READINESS_CLEAN_CYCLES:
            reasons.append(
                f"clean_scheduled_cycles={r.clean_scheduled_cycles} < {READINESS_CLEAN_CYCLES}"
            )
        if r.runs_with_errors_recent:
            reasons.append(f"recent_runs_with_errors={r.runs_with_errors_recent}")
        if not r.raw_feed_fresh:
            reasons.append("raw_feed_not_fresh (watcher must still write raw ticks)")
        r.readiness_reasons = reasons
        r.readiness = "ready_to_stage" if not reasons else "not_ready"

        r.retention = {
            "raw_tick_days (UNCHANGED by OPS-012)": s.tick_retention_days,
            "bucket_days": s.tick_bucket_retention_days,
        }
        if r.bucket_total == 0:
            r.staged_recommendation = (
                "No buckets yet — run aggregate-market-ticks first. Raw tick "
                "retention stays unchanged."
            )
        elif r.coverage_healthy:
            r.staged_recommendation = (
                f"Aggregation coverage is healthy ({r.coverage_rate_last_48h:.0%} of "
                f"raw-tick hours in the last 48h have buckets). A FUTURE OPS "
                f"milestone may reduce raw tick retention from "
                f"{s.tick_retention_days}d toward 24-48h while keeping buckets "
                f"{s.tick_bucket_retention_days}d — NOT enacted by OPS-012; raw "
                f"retention is unchanged until explicitly accepted."
            )
        else:
            r.staged_recommendation = (
                "Aggregation coverage is NOT yet healthy — keep raw tick "
                "retention unchanged and run aggregate-market-ticks regularly "
                "(manually) until coverage exceeds "
                f"{COVERAGE_HEALTHY_RATE:.0%} before staging any retention change."
            )
        return r
