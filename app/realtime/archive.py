"""KALSHI-REALTIME-OBSERVATION-001A — append-only archive, replay, latency.

The archive is the evidence. Everything downstream — book reconstruction,
reconciliation, eventually shadow fills — must be reproducible from it alone,
which is why replay is a first-class operation rather than a debugging aid.

Two hard separations:

* **demo and production archives never share a directory.** A demo event
  replayed as production evidence would be a fabricated observation, so the
  environment is part of the path and the writer refuses to mix them.
* **nothing here touches SQLite.** High-frequency events do not go near a 4.24
  GiB research database with five lifetime lock events.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
import statistics
import threading
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.realtime.book import EventEnvelope
from app.realtime.segment import (
    EVENTS_FILENAME,
    MANIFEST_FILENAME,
    SegmentWriter,
    read_segment_records,
    verify_archive,
    verify_chain,
)
from app.realtime.canonical import (
    CANONICAL_SCHEMA_VERSION,
    parse_canonical_datetime,
    canonical_bytes,
    digest_hex,
    parse_canonical,
)
from app.realtime.fixedpoint import loads_exact

# Becomes a directory component, so it must not be able to escape one.
_VENUE_RE = re.compile(r"^[a-z0-9_-]+$")


def _digest_matches(rec: dict) -> bool:
    recorded = rec.get("record_digest")
    if not recorded:
        return False
    recomputed = digest_hex({k: v for k, v in rec.items()
                             if k != "record_digest"})
    return recorded == recomputed


def _read_order(rec: dict):
    """Order by INSTANT, not by the text of the timestamp.

    Sorting ISO strings puts `2026-08-06T13:00:00-04:00` before
    `2026-08-06T12:00:00+00:00` even though it is five hours later, which
    reorders the stream and destroys the book on a purely cosmetic timezone
    difference. `seq` is coerced because a venue that sends it as a string
    otherwise raises TypeError mid-sort and takes the whole read down.
    """
    raw = rec.get("collector_receive_time") or ""
    try:
        when = datetime.fromisoformat(raw).astimezone(timezone.utc)
    except (TypeError, ValueError):
        when = datetime.min.replace(tzinfo=timezone.utc)
    seq = rec.get("seq")
    try:
        seq_key = int(seq)
    except (TypeError, ValueError):
        seq_key = -1
    return (when, seq_key)

ARCHIVE_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"


class ArchiveError(RuntimeError):
    """A write or read that would compromise the evidence."""


def _canon(obj) -> str:
    """Deprecated shim. Everything now digests over `canonical_bytes`.

    Kept only so an older archive can still be *read* for diagnosis; it must
    never be used to compute a digest that will later be re-verified, which is
    precisely how the two-serializer defect arose.
    """
    return canonical_bytes(obj).decode("utf-8")


class EventArchive:
    """Compatibility façade over the hardened segment core.

    This class used to be an independent persistence implementation — its own
    canonicaliser, its own digest, its own gzip append, its own tail recovery,
    its own `verify`. That is exactly the split that let a deleted record and a
    deleted FILE both verify as intact: two implementations that were merely
    intended to agree, and no authoritative count to contradict.

    It now owns no persistence logic at all. Every write goes through one
    `SegmentWriter` per partition, every record is chained, and every closed
    segment is committed by an atomically published manifest. What survives
    here is the API its callers already use, and the `env/venue/date/hour`
    partition layout, mapped onto a segment id.

    One behavioural change callers must know about: **a segment is only
    canonical evidence once it is closed.** `close()` publishes the manifests.
    `verify()` and `read_all()` do *not* close for you — finalising a segment
    on the way into verification would compute the manifest from whatever the
    file currently says, which would certify a deletion instead of detecting it.
    """

    def __init__(self, root: str | Path, *, environment: str, venue: str = "kalshi"):
        from app.realtime.kalshi import ENVIRONMENTS

        if environment not in ENVIRONMENTS:
            raise ArchiveError(f"unknown environment {environment!r}")
        # `venue` becomes a path component. Unvalidated, a value like
        # "../../env=production/venue=kalshi" wrote demo records into the
        # production tree.
        if not _VENUE_RE.match(venue):
            raise ArchiveError(
                f"venue {venue!r} is not a safe path component; expected "
                f"{_VENUE_RE.pattern}")
        self.environment = environment
        self.venue = venue
        self.root = Path(root)
        self.written = 0
        self._writers: dict = {}
        self._closed = False
        # Lazy writer creation is itself concurrent: without this, two threads
        # both observe "no writer yet" and both construct one, and the second
        # hits the segment's exclusive lock. The lock below is what makes
        # "one writer per segment" true for the *creation* step too.
        self._writers_lock = threading.Lock()

    # -- partition identity ----------------------------------------------------
    def partition(self, when: datetime) -> Path:
        if when.tzinfo is None or when.utcoffset() is None:
            raise ArchiveError(
                "collector_receive_time must be timezone-aware; astimezone() "
                "reads a naive datetime as LOCAL time, so the same events "
                "would land in different hour partitions on different hosts")
        when = when.astimezone(timezone.utc)
        return (self.root / f"env={self.environment}" / f"venue={self.venue}"
                / f"date={when:%Y-%m-%d}" / f"hour={when:%H}")

    def _segment_id(self, when: datetime) -> str:
        when = when.astimezone(timezone.utc)
        return f"{self.venue}.{when:%Y-%m-%d}T{when:%H}"

    def _writer_for(self, when: datetime):
        """One writer per partition, created lazily and owned exclusively."""
        if self._closed:
            raise ArchiveError("archive is closed; no further events accepted")
        seg_id = self._segment_id(when)
        writer = self._writers.get(seg_id)
        if writer is not None:
            return writer
        with self._writers_lock:
            writer = self._writers.get(seg_id)
            if writer is not None:
                return writer
            writer = self._writers[seg_id] = SegmentWriter(
                self.root, environment=self.environment, segment_id=seg_id,
                partition_identity=str(self.partition(when).relative_to(self.root)),
                subscription_metadata={"venue": self.venue})
        return writer

    # -- write -----------------------------------------------------------------
    def append(self, envelope: EventEnvelope) -> Path:
        """Submit one envelope. The façade never touches a file descriptor."""
        if envelope.environment != self.environment:
            raise ArchiveError(
                f"refusing to write a {envelope.environment!r} event into the "
                f"{self.environment!r} archive: demo events must never become "
                "production evidence")
        when = parse_canonical_datetime(envelope.collector_receive_time)
        writer = self._writer_for(when)
        raw = envelope.to_dict()
        reason = writer.submit({
            "connection_generation": raw.get("connection_id"),
            "subscription_id": raw.get("sid"),
            "subscription_generation": raw.get("subscription_generation"),
            "message_type": raw.get("event_type"),
            "market_ticker": raw.get("market_ticker"),
            "seq": raw.get("seq"),
            "received_at_utc": raw.get("collector_receive_time"),
            "received_monotonic_ns": raw.get("receive_monotonic_ns"),
            "raw_event": raw.get("raw"),
            "normalized_event": raw,
        })
        if reason is not None:
            raise ArchiveError(f"event rejected by the writer: {reason.value}")
        self.written += 1
        return writer.events_path

    def close(self) -> dict:
        """Close every open segment, publishing its manifest.

        This is the commit point. Until it runs there is no authoritative
        record count, and an unclosed segment is explicitly not evidence.
        """
        manifests = {}
        for seg_id, writer in list(self._writers.items()):
            manifests[seg_id] = writer.close()
        self._closed = True
        return manifests

    # -- read ------------------------------------------------------------------
    def _segment_dirs(self) -> list:
        env_root = self.root / f"env={self.environment}"
        if not env_root.exists():
            return []
        return sorted(p for p in env_root.glob("segment=*") if p.is_dir())

    def read_all(self) -> list:
        """Every readable record, in chain order, across this environment.

        Records whose chain or environment does not verify are dropped and
        counted rather than returned — a reader that hands back unverified
        evidence is the thing this milestone exists to remove.
        """
        out, truncated, foreign, tampered = [], 0, 0, []
        for directory in self._segment_dirs():
            records = read_segment_records(directory / EVENTS_FILENAME)
            seg_id = directory.name.split("segment=", 1)[-1]
            verdict = verify_chain(records, segment_id=seg_id,
                                   environment=self.environment)
            if not verdict.ok:
                tampered.append(verdict.broken_at)
                records = records[:verdict.broken_at or 0]
            for rec in records:
                if rec.get("environment") != self.environment:
                    foreign += 1
                    continue
                out.append(rec.get("normalized_event") or rec)
        self.truncated_records = truncated
        self.tampered_records = tampered
        self.foreign_environment_records = foreign
        return out

    def verify(self) -> dict:
        """Delegate to the one canonical verifier. Fail-closed.

        The legacy shape is preserved for existing callers, with the
        authoritative segment verdicts alongside it.
        """
        report = verify_archive(self.root, environment=self.environment)
        records_read = report["records_read"]
        intact = report["verdict"] == "VALID"
        return {
            "records": records_read,
            "mismatched": [v["segment_id"] for v in report["segment_verdicts"]
                           if not v["valid"]],
            "intact": intact,
            "truncated_records": max(
                0, report["records_expected"] - records_read),
            "foreign_environment_records": sum(
                0 if v["environment_valid"] else 1
                for v in report["segment_verdicts"]),
            "verdict": report["verdict"],
            "segments": report["segments"],
            "closed_segments": report["closed_segments"],
            "open_segments": report["open_segments"],
            "invalid_segments": report["invalid_segments"],
            "segment_verdicts": report["segment_verdicts"],
        }


# --- latency --------------------------------------------------------------------


@dataclass
class LatencyEnvelope:
    """Latency is never one number. Each hop is measured separately.

    `venue_to_receive_offset_contaminated_ms` is named at length on purpose. It
    subtracts the venue's clock from ours, so it equals
    `true_transit + (our_offset - their_offset)` — on an unsynchronised host the
    offset term dominates and the value can be negative. Sitting unmarked next
    to two genuine monotonic durations, it read as a measured hop. The clock
    offset is not characterised, so this is evidence, not a latency.
    """
    records_scanned: int = 0
    venue_to_receive_offset_contaminated_ms: dict = field(default_factory=dict)
    receive_to_normalize_us: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# A percentile needs enough samples for the rank to mean anything. Below this,
# p99 is just the maximum wearing a percentile's name.
MIN_SAMPLES_FOR = {"p50": 3, "p95": 20, "p99": 100}


def _quantiles(values, *, negative: int = 0) -> dict:
    """Nearest-rank percentiles. Same keys whether or not there is data.

    `int(p * n)` is a floor, which returns the (floor(p*n)+1)-th order
    statistic — one rank too high whenever p*n is an integer, and saturating at
    the last index for small n. It made p99 identical to max for every n <= 100
    and overstated the tail by ~27% on a 100-sample exponential draw, which is
    precisely the statistic this milestone exists to measure.

    The empty dict carried five keys and the populated one carried six, so a
    consumer reading `mean` raised KeyError exactly when there was no data.
    """
    n = len(values)
    if not n:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None,
                "mean": None, "negative": negative}
    ordered = sorted(values)

    def q(name, p):
        if n < MIN_SAMPLES_FOR[name]:
            return None
        return round(ordered[max(0, math.ceil(p * n) - 1)], 3)

    return {"n": n, "p50": q("p50", 0.50), "p95": q("p95", 0.95),
            "p99": q("p99", 0.99), "max": round(ordered[-1], 3),
            "mean": round(statistics.fmean(ordered), 3), "negative": negative}


def latency_envelope(records) -> LatencyEnvelope:
    """Per-hop distributions plus what the distributions do not cover.

    Negative samples are counted, never dropped. Discarding them silently made
    a broken clock or a field-ordering bug look like a clean, slightly smaller
    sample — and on the venue hop, negatives are the offset evidence.

    The `normalize_to_book_us` hop was removed. Nothing ever populated
    `book_applied_monotonic_ns`, so it reported `n: 0` forever while the CLI
    and the milestone doc advertised three decomposed hops. A permanently empty
    hop reads as "measured, and fast", which is worse than an absent one.
    """
    venue, normalize = [], []
    venue_negative = normalize_negative = 0
    with_venue_time = 0
    for r in records:
        age = r.get("data_age_us")
        if age is not None:
            age = float(age)
            with_venue_time += 1
            if age < 0:
                venue_negative += 1
            venue.append(age)
        rn, nn = r.get("receive_monotonic_ns"), r.get("normalize_monotonic_ns")
        if rn is not None and nn is not None:
            if nn < rn:
                # Impossible inside one process: the record is corrupt or the
                # two stamps came from different processes.
                normalize_negative += 1
            else:
                normalize.append((nn - rn) / 1000.0)
    return LatencyEnvelope(
        records_scanned=len(records),
        venue_to_receive_offset_contaminated_ms=_quantiles(
            venue, negative=venue_negative),
        receive_to_normalize_us=_quantiles(normalize,
                                           negative=normalize_negative),
        coverage={
            "records_with_venue_time": with_venue_time,
            "records_without_venue_time": len(records) - with_venue_time,
            # Reconnect gaps are NOT measured. Every percentile above is
            # conditioned on "we were connected", which biases the tail
            # optimistically in exactly the regime that matters. There is no
            # collector yet to measure disconnects; 001B must add it before any
            # percentile here is quoted as a latency figure.
            "observation_gaps_measured": False,
            "host_clock_offset_characterised": False,
        })


# --- replay ---------------------------------------------------------------------


def replay(records, *, grid=None) -> dict:
    """Deterministically rebuild books from archived events.

    Replay is grouped by **sid**, not by market, because that is where ordering
    lives: `seq` counts messages across a whole subscription. Replaying each
    market against its own sequence view put a hole at every sibling message,
    so a two-market archive produced two permanently halted books and reported
    it as a venue fault.

    Pure: no network, no credential, no database, no clock dependence. The same
    records must always produce the same per-market checksums, which is the
    acceptance test for the whole data path.
    """
    from app.realtime.book import SubscriptionRouter, SubscriptionState

    routers: dict[object, SubscriptionRouter] = {}
    faults, applied, rejected = [], 0, 0
    for r in records:
        etype = r.get("event_type")
        if etype not in ("orderbook_snapshot", "orderbook_delta"):
            continue
        sid = r.get("sid")
        router = routers.get(sid)
        if router is None:
            # Tickers are not constrained on replay: the archive is the record
            # of what the subscription actually carried, and re-deriving the
            # subscribe list from it would just assert the file against itself.
            router = routers[sid] = SubscriptionRouter(
                SubscriptionState(sid if sid is not None else 0), grid=grid)
        try:
            out = router.dispatch(r)
            if out.get("action") in ("snapshot", "delta"):
                applied += 1
        except Exception as exc:
            rejected += 1
            faults.append({"market_ticker": r.get("market_ticker"),
                           "sid": sid, "seq": r.get("seq"),
                           "error": f"{type(exc).__name__}: {exc}"})

    books = {t: b for router in routers.values()
             for t, b in router.books.items()}
    publishable = {}
    for router in routers.values():
        publishable.update(router.publishable_books())
    return {
        "markets": len(books), "subscriptions": len(routers),
        "events_applied": applied, "events_rejected": rejected, "faults": faults,
        # None for a halted book. A consumer comparing checksums without also
        # reading `publishable` would otherwise accept a torn book.
        "checksums": {t: (books[t].checksum() if publishable.get(t) else None)
                      for t in sorted(books)},
        "publishable": {t: publishable.get(t, False) for t in sorted(books)},
        "stats": {t: dict(books[t].stats) for t in sorted(books)},
        "subscription_stats": {
            str(sid): dict(r.subscription.stats) for sid, r in sorted(
                routers.items(), key=lambda kv: str(kv[0]))},
        "external_calls": 0, "persisted": False,
    }


def reconcile_with_rest(book, rest_market: dict) -> dict:
    """Compare a reconstructed book against a REST market snapshot.

    REST is an independent check, not a fallback. A discrepancy means
    resynchronise — it never means "take whichever source looks better", which
    would let the collector paper over exactly the gaps it exists to detect.

    Identity is checked first. Without it this function would happily reconcile
    one market's book against another market's payload and return a confident
    verdict either way, which is worse than no reconciliation.
    """
    from app.realtime.fixedpoint import parse_price_units

    rest_ticker = rest_market.get("ticker") or rest_market.get("market_ticker")
    if rest_ticker != book.market_ticker:
        return {"market_ticker": book.market_ticker,
                "agrees": False, "discrepancies": [],
                "classification": "identity_mismatch",
                "action": "abort",
                "detail": (f"REST payload is for {rest_ticker!r}, book is "
                           f"{book.market_ticker!r}"),
                "external_calls": 0, "persisted": False}

    if not book.publishable:
        # The function whose job is "should we resynchronise" must not raise on
        # the one input where the answer is definitely yes.
        return {"market_ticker": book.market_ticker,
                "generation": book.generation,
                "agrees": False, "discrepancies": [],
                "classification": "sequence_gap",
                "action": "resynchronise",
                "detail": book.integrity_reason,
                "external_calls": 0, "persisted": False}

    findings = []
    top = book.top_of_book()
    for label, rest_key, ours in (
        ("best_yes_bid", "yes_bid_dollars", top["best_yes_bid_units"]),
        ("best_yes_ask", "yes_ask_dollars", top["best_yes_ask_units"]),
    ):
        raw = rest_market.get(rest_key)
        if raw in (None, ""):
            continue
        theirs = parse_price_units(raw, field=rest_key)
        if ours != theirs:
            findings.append({
                "field": label, "reconstructed_units": ours,
                "rest_units": theirs, "delta_units": (
                    None if ours is None else ours - theirs)})

    status = rest_market.get("status")
    if status is not None and status not in ("active", "open"):
        findings.append({"field": "status", "rest_status": status,
                         "detail": "REST reports the market is not open"})

    return {"market_ticker": book.market_ticker,
            "generation": book.generation,
            "rest_status": status,
            "agrees": not findings, "discrepancies": findings,
            # Gate 11 taxonomy. `unknown` is the honest default: this function
            # can see a difference but not its cause, and guessing
            # `timing_difference` because that is the benign one is how a
            # normalisation defect gets filed as noise.
            "classification": "agreement" if not findings else "unknown",
            "action": "none" if not findings else "resynchronise",
            "external_calls": 0, "persisted": False}
