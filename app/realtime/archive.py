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
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.realtime.book import EventEnvelope
from app.realtime.fixedpoint import loads_exact

# Becomes a directory component, so it must not be able to escape one.
_VENUE_RE = re.compile(r"^[a-z0-9_-]+$")


def _digest_matches(rec: dict) -> bool:
    recorded = rec.get("record_digest")
    if not recorded:
        return False
    recomputed = hashlib.sha256(
        _canon({k: v for k, v in rec.items() if k != "record_digest"})
        .encode("utf-8")).hexdigest()
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
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False, default=str)


class EventArchive:
    """Append-only gzip-JSONL, partitioned `env/venue/date/hour`.

    JSONL rather than Parquet at this stage on purpose: 001A's job is to prove
    the events are correct and replayable. Parquet's columnar benefits arrive
    with the research archive in a later phase, and choosing a storage format
    before the schema has stabilised would be optimising the wrong thing.

    A truncated tail is tolerated on read and reported — an interrupted write
    loses at most the final record, never the file. Read-side digest
    verification and the environment check are mandatory rather than opt-in:
    an integrity control that runs only when someone remembers to call
    `verify()` is not protecting the path that actually reads the evidence.
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

    def partition(self, when: datetime) -> Path:
        if when.tzinfo is None or when.utcoffset() is None:
            raise ArchiveError(
                "collector_receive_time must be timezone-aware; astimezone() "
                "reads a naive datetime as LOCAL time, so the same events "
                "would land in different hour partitions on different hosts")
        when = when.astimezone(timezone.utc)
        return (self.root / f"env={self.environment}" / f"venue={self.venue}"
                / f"date={when:%Y-%m-%d}" / f"hour={when:%H}")

    def _path_for(self, when: datetime) -> Path:
        return self.partition(when) / "events.jsonl.gz"

    def append(self, envelope: EventEnvelope) -> Path:
        if envelope.environment != self.environment:
            raise ArchiveError(
                f"refusing to write a {envelope.environment!r} event into the "
                f"{self.environment!r} archive: demo events must never become "
                "production evidence")
        when = datetime.fromisoformat(envelope.collector_receive_time)
        path = self._path_for(when)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = envelope.to_dict()
        record["record_digest"] = hashlib.sha256(
            _canon(record).encode("utf-8")).hexdigest()
        with gzip.open(path, "at", encoding="utf-8") as fh:
            fh.write(_canon(record) + "\n")
        self.written += 1
        return path

    def _read_lines(self, path) -> tuple[list, int]:
        """Decode one partition member by member, keeping whatever survived.

        `gzip.open(...).read()` buffers the whole file and then raises on a
        truncated trailer, and the exception discards everything already
        decoded — so an interrupted write lost the entire hour, not the final
        record, while the docstring above promised the opposite and `verify()`
        called the empty result intact.

        `append` writes one gzip member per record, so walking members with
        `decompressobj().unused_data` gives exactly the promised behaviour: the
        torn member is dropped and every complete member before it is kept.
        """
        lines, truncated, buf = [], 0, ""
        try:
            data = path.read_bytes()
        except OSError:
            return [], 1
        while data:
            dec = zlib.decompressobj(31)        # 31 = gzip wrapper
            try:
                chunk = dec.decompress(data) + dec.flush()
            except (zlib.error, EOFError):
                truncated += 1
                break
            if not dec.eof:                      # member ended mid-stream
                truncated += 1
                break
            try:
                buf += chunk.decode("utf-8")
            except UnicodeDecodeError:
                truncated += 1
                break
            data = dec.unused_data
        *complete, buf = buf.split("\n")
        lines.extend(complete)
        if buf.strip():
            truncated += 1      # a final line with no terminator is a torn write
        return lines, truncated

    def read_all(self) -> list:
        """Every archived record in deterministic order, tail-tolerant.

        Digests are verified here rather than only in `verify()`. A digest that
        is checked only when someone remembers to ask is not an integrity
        control — `replay()` never called it, so a tampered record rebuilt a
        book with no complaint.
        """
        out, truncated, tampered, foreign = [], 0, [], 0
        for path in sorted(self.root.rglob("events.jsonl.gz")):
            # Compare the env= PATH COMPONENT, not a substring of the whole
            # string: an archive rooted at .../env=demo would otherwise match
            # its own root and read production records written beneath it.
            try:
                parts = path.relative_to(self.root).parts
            except ValueError:
                continue
            if f"env={self.environment}" not in parts:
                continue
            lines, t = self._read_lines(path)
            truncated += t
            for line in lines:
                if not line.strip():
                    continue
                try:
                    rec = loads_exact(line)
                except (json.JSONDecodeError, ValueError):
                    truncated += 1  # malformed record: drop it, keep the file
                    continue
                # The record's own label must agree with the partition it was
                # found in. A file copy, an rsync or a restore can put a demo
                # record under env=production, and replaying demo events as
                # production evidence is a fabricated observation.
                if rec.get("environment") != self.environment:
                    foreign += 1
                    continue
                if not _digest_matches(rec):
                    tampered.append(rec.get("seq"))
                    continue
                out.append(rec)
        out.sort(key=_read_order)
        self.truncated_records = truncated
        self.tampered_records = tampered
        self.foreign_environment_records = foreign
        return out

    def verify(self) -> dict:
        """Report what `read_all` had to reject.

        `intact` now accounts for truncation and foreign records. It previously
        reported `intact: True` over a totally destroyed archive, because
        `read_all` had silently returned zero records and `verify` only checked
        the digests of what came back.
        """
        records = self.read_all()
        bad = list(getattr(self, "tampered_records", []))
        truncated = getattr(self, "truncated_records", 0)
        foreign = getattr(self, "foreign_environment_records", 0)
        return {"records": len(records), "mismatched": bad,
                "intact": not bad and not truncated and not foreign,
                "truncated_records": truncated,
                "foreign_environment_records": foreign}


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
        age = r.get("data_age_ms")
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

    Pure: no network, no credential, no database, no clock dependence. The same
    records must always produce the same per-market checksums, which is the
    acceptance test for the whole data path.
    """
    from app.realtime.book import BookIntegrityError, OrderBook
    from app.realtime.fixedpoint import FixedPointError

    books: dict[str, OrderBook] = {}
    faults, applied, rejected = [], 0, 0
    for r in records:
        ticker = r.get("market_ticker")
        if not ticker:
            continue
        etype = r.get("event_type")
        msg = (r.get("raw") or {}).get("msg") or {}
        book = books.get(ticker)
        if book is None:
            book = books[ticker] = OrderBook(ticker, grid=grid)
        try:
            if etype == "orderbook_snapshot":
                book.apply_snapshot(msg, sid=r.get("sid"), seq=r.get("seq"))
                applied += 1
            elif etype == "orderbook_delta":
                book.apply_delta(msg, seq=r.get("seq"), sid=r.get("sid"))
                applied += 1
            else:
                continue
        # Deliberately broad: a KeyError or a TypeError from venue field drift
        # previously escaped and aborted the whole run, so one malformed record
        # made an entire archive unreplayable — the opposite of the
        # tail-tolerance this module promises.
        except Exception as exc:
            rejected += 1
            faults.append({"market_ticker": ticker, "seq": r.get("seq"),
                           "error": f"{type(exc).__name__}: {exc}"})
    return {
        "markets": len(books), "events_applied": applied,
        "events_rejected": rejected, "faults": faults,
        # None for a halted book. A consumer comparing checksums without also
        # reading `publishable` would otherwise accept a torn book.
        "checksums": {t: (b.checksum() if b.publishable else None)
                      for t, b in sorted(books.items())},
        "publishable": {t: b.publishable for t, b in sorted(books.items())},
        "stats": {t: dict(b.stats) for t, b in sorted(books.items())},
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
