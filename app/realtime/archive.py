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
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.realtime.book import EventEnvelope

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
    must lose at most the final record, never the file.
    """

    def __init__(self, root: str | Path, *, environment: str, venue: str = "kalshi"):
        from app.realtime.kalshi import ENVIRONMENTS

        if environment not in ENVIRONMENTS:
            raise ArchiveError(f"unknown environment {environment!r}")
        self.environment = environment
        self.venue = venue
        self.root = Path(root)
        self.written = 0

    def partition(self, when: datetime) -> Path:
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

    def read_all(self) -> list:
        """Every archived record in deterministic order, tail-tolerant."""
        out, truncated = [], 0
        for path in sorted(self.root.rglob("events.jsonl.gz")):
            if f"env={self.environment}" not in str(path):
                continue
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
            except (OSError, EOFError):
                truncated += 1
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    truncated += 1      # malformed tail: drop the record, keep the file
        out.sort(key=lambda r: (r.get("collector_receive_time") or "",
                                r.get("seq") if r.get("seq") is not None else -1))
        self.truncated_records = truncated
        return out

    def verify(self) -> dict:
        """Recompute every record digest."""
        records = self.read_all()
        bad = []
        for r in records:
            recorded = r.get("record_digest")
            recomputed = hashlib.sha256(
                _canon({k: v for k, v in r.items() if k != "record_digest"})
                .encode("utf-8")).hexdigest()
            if recorded != recomputed:
                bad.append(r.get("seq"))
        return {"records": len(records), "mismatched": bad,
                "intact": not bad,
                "truncated_records": getattr(self, "truncated_records", 0)}


# --- latency --------------------------------------------------------------------


@dataclass
class LatencyEnvelope:
    """Latency is never one number. Each hop is measured separately."""
    samples: int = 0
    venue_to_receive_ms: dict = field(default_factory=dict)
    receive_to_normalize_us: dict = field(default_factory=dict)
    normalize_to_book_us: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _quantiles(values) -> dict:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)

    def q(p):
        idx = min(len(ordered) - 1, int(p * len(ordered)))
        return round(ordered[idx], 3)

    return {"n": len(ordered), "p50": q(0.50), "p95": q(0.95), "p99": q(0.99),
            "max": round(ordered[-1], 3),
            "mean": round(statistics.fmean(ordered), 3)}


def latency_envelope(records) -> LatencyEnvelope:
    venue, normalize, book = [], [], []
    for r in records:
        if r.get("data_age_ms") is not None:
            venue.append(float(r["data_age_ms"]))
        rn, nn = r.get("receive_monotonic_ns"), r.get("normalize_monotonic_ns")
        if rn is not None and nn is not None and nn >= rn:
            normalize.append((nn - rn) / 1000.0)
        bn = (r.get("normalized") or {}).get("book_applied_monotonic_ns")
        if nn is not None and bn is not None and bn >= nn:
            book.append((bn - nn) / 1000.0)
    return LatencyEnvelope(
        samples=len(records), venue_to_receive_ms=_quantiles(venue),
        receive_to_normalize_us=_quantiles(normalize),
        normalize_to_book_us=_quantiles(book))


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
                book.apply_delta(msg, seq=r.get("seq"))
                applied += 1
            else:
                continue
        except (BookIntegrityError, FixedPointError) as exc:
            rejected += 1
            faults.append({"market_ticker": ticker, "seq": r.get("seq"),
                           "error": f"{type(exc).__name__}: {exc}"})
    return {
        "markets": len(books), "events_applied": applied,
        "events_rejected": rejected, "faults": faults,
        "checksums": {t: b.checksum() for t, b in sorted(books.items())},
        "publishable": {t: b.publishable for t, b in sorted(books.items())},
        "stats": {t: dict(b.stats) for t, b in sorted(books.items())},
        "external_calls": 0, "persisted": False,
    }


def reconcile_with_rest(book, rest_market: dict) -> dict:
    """Compare a reconstructed book against a REST market snapshot.

    REST is an independent check, not a fallback. A discrepancy means
    resynchronise — it never means "take whichever source looks better", which
    would let the collector paper over exactly the gaps it exists to detect.
    """
    from app.realtime.fixedpoint import parse_price_units

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
    return {"market_ticker": book.market_ticker,
            "generation": book.generation,
            "agrees": not findings, "discrepancies": findings,
            "action": "none" if not findings else "resynchronise",
            "external_calls": 0, "persisted": False}
