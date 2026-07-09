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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import MarketPriceTick, MarketPriceTickBucket
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
class AggregationStats:
    """What one aggregation pass did. Counters only — never advice."""

    window_start: datetime | None = None
    window_end: datetime | None = None
    bucket_seconds: int = 300
    rows_read: int = 0
    rows_skipped_unusable: int = 0   # no midpoint AND no bid/ask/spread/liquidity
    buckets_written: int = 0
    buckets_inserted: int = 0
    buckets_updated: int = 0
    truncated: bool = False          # row cap stopped the pass early (hour boundary)
    covered_until: datetime | None = None  # aggregation is complete up to here
    dry_run: bool = False
    duration_ms: int = 0


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
    """Groups raw ticks into fixed buckets and upserts them idempotently."""

    def __init__(self, settings: Settings | None = None):
        s = settings or get_settings()
        self.default_bucket_seconds = s.tick_aggregation_bucket_seconds
        self.max_rows = s.tick_aggregation_max_rows

    def aggregate(
        self,
        session: Session,
        hours: int = 24,
        bucket_seconds: int | None = None,
        dry_run: bool = False,
        max_rows: int | None = None,
    ) -> AggregationStats:
        """One bounded aggregation pass over raw ticks observed in the last
        `hours`. Upserts buckets; NEVER deletes or modifies raw ticks. With
        dry_run=True everything is computed and counted but nothing is written."""
        started = _now()
        bucket_seconds = int(
            self.default_bucket_seconds if bucket_seconds is None else bucket_seconds
        )
        if bucket_seconds <= 0 or 3600 % bucket_seconds != 0:
            raise ValueError(
                f"bucket_seconds must be a positive divisor of 3600, got {bucket_seconds}"
            )
        row_cap = int(max_rows or self.max_rows)

        window_start = started - timedelta(hours=max(1, int(hours)))
        # hour-aligned sub-windows: bucket_seconds divides 3600, so no bucket
        # ever spans a sub-window and a row-cap stop lands on a bucket boundary.
        hour_floor = bucket_start_for(window_start, 3600)

        stats = AggregationStats(
            window_start=window_start, window_end=started,
            bucket_seconds=bucket_seconds, dry_run=dry_run,
        )

        cursor = hour_floor
        while cursor < started:
            sub_end = cursor + timedelta(hours=1)
            if stats.rows_read >= row_cap:
                stats.truncated = True
                logger.info(
                    "tick aggregation: row cap %d reached; covered until %s "
                    "(rerun to continue)", row_cap, cursor.isoformat(),
                )
                break
            rows = session.execute(
                select(MarketPriceTick)
                .where(
                    MarketPriceTick.observed_at >= cursor,
                    MarketPriceTick.observed_at < sub_end,
                )
                .order_by(MarketPriceTick.observed_at.asc(), MarketPriceTick.id.asc())
            ).scalars().all()
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

            for (ticker, start), acc in groups.items():
                inserted = self._upsert_bucket(
                    session, ticker, start, bucket_seconds, acc, dry_run
                )
                stats.buckets_written += 1
                if inserted:
                    stats.buckets_inserted += 1
                else:
                    stats.buckets_updated += 1

            stats.covered_until = min(sub_end, started)
            cursor = sub_end

        if not dry_run:
            session.commit()
        stats.duration_ms = int((_now() - started).total_seconds() * 1000)
        return stats

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


COVERAGE_HEALTHY_RATE = 0.95


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

        # hour-granularity coverage: of the distinct raw-tick hours in the last
        # 48h, how many have at least one bucket?
        cutoff = _now() - timedelta(hours=48)
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
        r.raw_hours_last_48h = len(raw_hours)
        r.covered_hours_last_48h = len(raw_hours & bucket_hours)
        if raw_hours:
            r.coverage_rate_last_48h = round(r.covered_hours_last_48h / len(raw_hours), 4)
            r.coverage_healthy = r.coverage_rate_last_48h >= COVERAGE_HEALTHY_RATE

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
