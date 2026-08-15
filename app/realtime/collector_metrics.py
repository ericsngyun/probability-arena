"""KALSHI-LIVE-TAPE-COLLECTOR-001 CP4 — the measurement lane.

**The measurement is the deliverable; the tape is the by-product** (§7). The
milestone exists to replace one number with another: `segment.py:200-212` sizes
three shipped rotation constants against a "~500 events/s ASSUMED peak" while
the only real Kalshi rate this repository has ever measured is "4 records over
~2 minutes, DEMO". §8.4's arithmetic puts the closer's ceiling at roughly 6,900
events/s against a measured burst-drain of ~7,000/s — **under 2x margin** — so
the constants will be retuned from whatever this file records. That makes the
credibility of these numbers the whole point, and it is why almost every design
choice below is about not lying rather than about measuring more.

WHAT THIS MODULE IS NOT ALLOWED TO DO
-------------------------------------

**1. It must not distort what it measures (§7.1).** `archive.append()` is
synchronous, caller-threaded and lock-serialised, so anything this module does
per frame is paid for in archive throughput. The per-frame path here is
therefore integer arithmetic and pre-allocated list indexing only: no `os.write`,
no `json.dumps`, no string formatting, no unbounded list growth, no allocation
proportional to event count, and no lock. Every file write happens on the
flusher thread, once per interval — four orders of magnitude off the hot path at
a thousand events/s. CP5 is an explicit overhead gate that can FAIL this design;
`NULL_METRICS` exists so CP5 can run the identical fixture with instrumentation
off without the collector growing an `if metrics is not None` branch per frame.

**2. It must never raise into the collector loop (§8.6).** Every public method
is total. The `on_*` hot-path methods swallow and count; `start()`/`stop()`
swallow and count; a flusher thread that dies records how it died and leaves the
collector running. A measurement lane that can kill a session is a measurement
lane that destroys the evidence it exists to produce.

**3. No ticker, ever (§7.2).** Not "no ticker is written" — *no field can carry
one*. The record has exactly four string-valued fields, and each is pinned to a
format regex (a uuid4 hex, a two-member environment enum, and two ISO-8601
timestamps). The subscription reaches this module as `markets_subscribed`, an
`int`, and the constructor refuses a list, a tuple or a string in that slot. The
guarantee is structural, not a blocklist: there is no field for a ticker to ride
in, so no future caller can put one there by accident.

**4. The validator is closed (§7.2, precedent `app/telemetry/schema.py:162`).**
An unknown field is a refusal, not a passthrough. That is the high-cardinality
guard, and it is the same reasoning the shared telemetry envelope uses: "an
event carrying ANY other key is rejected".

TELEMETRY REUSE — THE DECISION AND ITS REASON
---------------------------------------------

**Reused: `TelemetrySink`'s write TECHNIQUE. Not reused: its envelope, its
validator, or its file.** §5.4 established that `sqlite-writes.jsonl` is the
right mechanism and the wrong home, for four reasons that all still hold: the
field set is closed and SQLite-shaped with nowhere to put a rate, a size or a
latency percentile; the model is one event per PASS and a collector session is
hours long; `emit_writer_pass` derives `writer_class` from how the pass was
launched and can never emit `continuous_daemon`, so a long-running collector
would be mislabelled; and the file has no rotation until 001E while
`read_events` slurps it whole (420 MB peak heap on 60 MB).

Growing `ALLOWED_FIELDS` by twelve rate/size/latency fields would contradict the
shared schema's own stated rule (`schema.py:22-24`): "this envelope is the safety
boundary for five writers and must not grow a label describing one lane's
bookkeeping." So this module copies `TelemetrySink._write_line` verbatim in
technique — `O_APPEND|O_WRONLY|O_CREAT|O_NOFOLLOW`, one unbuffered `os.write`,
a short write counted as a drop and never resumed, 0700/0600 modes re-enforced —
into its own file with its own equally closed schema. Bucket labels deliberately
follow the shared `_HISTOGRAM_KEY_RE` convention (`schema.py:267`) so one parser
reads both files; `test_kalshi_live_tape_cp4_001.py` pins the two patterns as
identical strings so a drift in either fails loudly.

**This module does NOT add a name to `WRITER_NAMES`.** Stream B of §7.2 — the
one `emit_writer_pass` session record — needs `kalshi_live_tape` in that closed
enum, which §6.6 flags as an architectural change and §11 Q5 puts to Eric. That
is a session-level concern belonging to the orchestrator, not to the interval
lane, and it is left undone here deliberately rather than done silently.

THE INTERFACE THE ORCHESTRATOR IS EXPECTED TO CALL (CP3 wires this; CP4 does not)
--------------------------------------------------------------------------------

Every method below is safe to call unconditionally, is a no-op on
`NULL_METRICS`, and cannot raise. Nothing in this module reaches into the
collector, the transport or the archive — the orchestrator supplies the three
`bind_*` sources and the two per-frame facts, and this file knows nothing else
about any of them.

    metrics = CollectorMetrics(environment=cfg.environment,
                               markets_subscribed=len(cfg.market_tickers))
    metrics.bind_reader_lag(transport.queue_depth)
    metrics.bind_transport_counters(transport.counters.snapshot)
    metrics.bind_archive_state(lambda: {
        "rotation_failures": len(archive.rotation_failures),
        "closer_outstanding": archive._closer.outstanding()})
    flusher = MetricsFlusher(metrics)      # start() in the session's `try`,
    flusher.start()                        # stop() in its `finally`

    # per frame, step 5 of §6.3 — after `archive.append()` has returned
    metrics.on_frame(received_mono_ns, wire_bytes)
    metrics.on_append(end_ns - start_ns, rotated=path != previous_path)

`received_mono_ns` is the `time.monotonic_ns()` the loop already took at step 1,
reused rather than re-read. `wire_bytes` is the delta of
`transport.counters.bytes_received` across the yield, which is that frame's
payload length. `rotated` is `True` when the `Path` returned by
`archive.append()` differs from the previous one — the only rotation signal
available without reading a private archive attribute, and it is observed on the
producer thread where the rotation cost is actually paid.

The remaining calls are per-event-class, not per-frame:
`on_append_rejected(elapsed_ns)` for a typed `ArchiveError`,
`on_frame_malformed()` (only when no transport source is bound — the transport
is otherwise the single authority), `on_sequence_fault("gap"|"regression"|
"duplicate")`, `on_disconnect()`, `on_reconnect(generation)`, and
`on_segment_closed(elapsed_ns)` from an `_on_rotation_closed` hook **on the
closer thread**.

SCHEMA DELTAS FROM §7.3, AND WHY EACH EXISTS
--------------------------------------------

§7.3's field list is explicitly abbreviated. Six fields are added, none
invented — each one is required by a row of §7.4 or §8.4 that §7.3's summary
omits:

- `append_us_rotation_histogram` — §8.4 point 2 requires TWO append histograms,
  because rotation cost lands on exactly one append and "the session maximum is
  unattributable and will be misread as ordinary append latency" otherwise.
- `events_per_second`, `events_per_second_truncated` — §7.4's p95/p99 burst row
  requires the 1 s ring; a ring that never leaves the process measures nothing.
  The list carries at most `MAX_PER_SECOND_SLOTS` completed seconds and says so
  when it had to drop older ones.
- `segments_closed`, `segment_close_ms_histogram`, `segment_close_ms_max` —
  §7.4's "archive close latency" row, which §7.3 has no field for. Timed on the
  CLOSER thread, so measuring close does not put close back on the producer.

KNOWN IMPRECISIONS, STATED RATHER THAN HIDDEN
---------------------------------------------

- **Counters are cumulative and the flusher reports deltas.** This is what makes
  the conservation property hold without a lock on the hot path: a snapshot
  taken mid-burst attributes the in-flight events to the NEXT interval instead of
  losing them, so the per-interval `events_received` always sums to the session
  total. A per-interval counter reset would have made a lost event look like an
  idle interval.
- **Producer-owned maxima (`append_us_max`, `segment_close_ms_max`) are zeroed
  by the flusher.** If the zeroing lands between the producer's compare and its
  store, one sample is attributed to the adjacent interval. The sample is never
  fabricated and never dropped; only its interval can be off by one.
- **`reader_stall_ms_max` is a SESSION WATERMARK, not an interval maximum.** It
  is read from `TransportCounters`, which this milestone may not modify, and a
  cumulative maximum cannot be differenced. It rises in the interval that
  produced a new worst stall and is flat otherwise; it is never a claim that the
  interval's worst stall was that value.
- **`reader_lag_frames_max` is SAMPLED, not per-frame.** Reading it costs an
  undocumented attribute chain (CP0 12.3) that §7.1 does not permit on the hot
  path, so the flusher thread samples it every `sample_interval_s`. When the
  chain breaks after a `websockets` upgrade the field is `null` — **UNAVAILABLE,
  never 0**, per CP0 12.3, because a fabricated zero reads as "no backlog".
- **There is no `transport_dropped` field.** CP0 12.4 deleted it: the installed
  library has no drop path and no drop counter, so the number has no source and
  needs none. Loss enters only across a disconnect (§8.3) or upstream at the
  venue, and sequence integrity is the detector for the latter.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

from app.realtime.kalshi import ENVIRONMENTS
from app.telemetry.sink import DIR_MODE, FILE_MODE, telemetry_dir

SCHEMA_VERSION = 1

# §7.2: a separate file in the same telemetry directory as the shared sink.
INTERVAL_FILENAME = "kalshi-live-tape.jsonl"

# Larger than the shared sink's 4096 because an interval record legitimately
# carries two histograms and a per-second series, and unlike the sink's envelope
# every one of those is a bounded integer structure. Still a hard cap: a line
# that cannot be made to fit is dropped and counted, never split.
MAX_LINE_BYTES = 8192

DEFAULT_FLUSH_INTERVAL_S = 10.0
DEFAULT_SAMPLE_INTERVAL_S = 0.1
# §7.4: 600 slots of 1 s. Sixty flush intervals of slack before the ring could
# wrap, which is the margin that lets a stalled flusher be visible rather than
# silently lossy.
DEFAULT_RING_SLOTS = 600
# Completed seconds carried in one record. Twelve times the flush interval, so
# an ordinary record carries ten and a late one still carries its whole span.
MAX_PER_SECOND_SLOTS = 120

# --- bucket edges -----------------------------------------------------------------
#
# Chosen against the numbers this milestone already has rather than round ones:
# the measured synchronous append is ~3,440 events/s (~290 us), so the resolution
# is dense from 50 us to 2 ms where ordinary appends live, and coarse above it
# where only a rotation or a GIL steal from the closer thread can put a sample.
APPEND_US_EDGES = (50, 100, 200, 500, 1_000, 2_000, 5_000, 10_000, 50_000,
                   250_000, 1_000_000)
# Wire bytes per frame. A `ticker` frame is a few hundred bytes; a deep
# `orderbook_snapshot` is the tail this exists to find, and the top bucket sits
# at 1 MiB because that is the library's own default `max_size` (CP0 12.1).
EVENT_BYTES_EDGES = (64, 128, 256, 512, 1_024, 2_048, 4_096, 8_192, 16_384,
                     65_536, 262_144, 1_048_576)
# Segment close, in ms. The committed benchmark says ~145 ms per 1,000 records
# and 2,497 ms at 13,000 idle / 14,800 ms at 20,000 contended, so the edges
# bracket exactly the range where "the closer keeps up" stops being true.
CLOSE_MS_EDGES = (10, 50, 100, 250, 500, 1_000, 2_500, 5_000, 15_000, 60_000)


def _labels_for(edges: tuple[int, ...]) -> tuple[str, ...]:
    """`<50`, `50-100`, ..., `>=1000000` — the shared histogram convention."""
    labels = [f"<{edges[0]}"]
    labels += [f"{lo}-{hi}" for lo, hi in zip(edges, edges[1:])]
    labels.append(f">={edges[-1]}")
    return tuple(labels)


APPEND_US_LABELS = _labels_for(APPEND_US_EDGES)
EVENT_BYTES_LABELS = _labels_for(EVENT_BYTES_EDGES)
CLOSE_MS_LABELS = _labels_for(CLOSE_MS_EDGES)

# Deliberately IDENTICAL in pattern to `app.telemetry.schema._HISTOGRAM_KEY_RE`
# and deliberately NOT imported from it: importing a private name would make this
# safety boundary depend on another module's unexported detail, and forking it
# silently would let the two files drift into needing two parsers. The test suite
# asserts the two pattern STRINGS are equal, so drift fails loudly instead.
HISTOGRAM_KEY_RE = re.compile(r"^(?:<\d{1,7}|\d{1,7}-\d{1,7}|>=\d{1,7})$")
MAX_HISTOGRAM_BUCKETS = 16

SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ISO_Z_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")

SEQUENCE_FAULTS = frozenset({"gap", "regression", "duplicate"})


class MetricsValidationError(ValueError):
    """An interval record failed the closed schema.

    Never propagates past the writer boundary — the writer counts and drops,
    exactly as the shared sink does. It is a `ValueError` so a caller that
    already handles malformed data handles this too.
    """


# --- the closed schema ------------------------------------------------------------

_STRING_FIELDS = {
    "session_id": SESSION_ID_RE,
    "interval_started_at": ISO_Z_RE,
    "interval_ended_at": ISO_Z_RE,
}

_COUNT_FIELDS = (
    "interval_index", "interval_wall_ms",
    "events_received", "events_archived", "events_rejected", "frames_malformed",
    "event_bytes_total", "append_us_max", "append_calls",
    "rotations_started", "rotation_failures", "closer_outstanding_max",
    "segments_closed", "segment_close_ms_max",
    "reader_stall_ms_max", "disconnects", "reconnects",
    "subscription_generation", "sequence_gaps", "sequence_regressions",
    "sequence_duplicates", "markets_subscribed", "metric_flush_drops",
)

_HISTOGRAM_FIELDS = (
    "event_bytes_histogram", "append_us_histogram",
    "append_us_rotation_histogram", "segment_close_ms_histogram",
)

# `reader_lag_frames_max` is the ONE nullable count: CP0 12.3 requires
# UNAVAILABLE and 0 to be distinguishable.
_NULLABLE_COUNT_FIELDS = ("reader_lag_frames_max",)

_BOOL_FIELDS = ("events_per_second_truncated",)

_LIST_FIELDS = ("events_per_second",)

ALLOWED_FIELDS = frozenset(
    {"schema_version", "session_id", "environment"}
    | set(_STRING_FIELDS) | set(_COUNT_FIELDS) | set(_HISTOGRAM_FIELDS)
    | set(_NULLABLE_COUNT_FIELDS) | set(_BOOL_FIELDS) | set(_LIST_FIELDS)
)

# Everything is required. Unlike the shared envelope, which carries optional
# fields for five different writers, this record has exactly one producer, so an
# absent field means the producer is broken and a reader averaging over records
# with holes in them would silently weight them wrong.
REQUIRED_FIELDS = ALLOWED_FIELDS


def validate_interval_record(record: object) -> dict:
    """Validate one interval record. Returns it, or raises.

    Closed on every axis a ticker or a payload could enter by: the field set is
    closed, every string field is pinned to a format regex, every count is a
    non-negative `int` with `bool` excluded, and the histograms accept bucket
    LABELS only.
    """
    if not isinstance(record, dict):
        raise MetricsValidationError("record must be a dict")

    unknown = set(record) - ALLOWED_FIELDS
    if unknown:
        raise MetricsValidationError(
            f"unknown/high-cardinality fields rejected: {sorted(unknown)}")
    missing = REQUIRED_FIELDS - set(record)
    if missing:
        raise MetricsValidationError(f"missing required fields: {sorted(missing)}")

    if record["schema_version"] != SCHEMA_VERSION:
        raise MetricsValidationError("unsupported schema_version")
    if record["environment"] not in ENVIRONMENTS:
        raise MetricsValidationError("environment outside the closed enum")

    for field, pattern in _STRING_FIELDS.items():
        value = record[field]
        if not isinstance(value, str) or not pattern.match(value):
            raise MetricsValidationError(
                f"{field} does not match its pinned format")

    for field in _COUNT_FIELDS:
        value = record[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MetricsValidationError(f"impossible count: {field}={value!r}")

    for field in _NULLABLE_COUNT_FIELDS:
        value = record[field]
        if value is None:
            continue  # UNAVAILABLE, and deliberately not 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MetricsValidationError(f"impossible count: {field}={value!r}")

    for field in _BOOL_FIELDS:
        if not isinstance(record[field], bool):
            raise MetricsValidationError(f"{field} must be a bool")

    for field in _LIST_FIELDS:
        value = record[field]
        if not isinstance(value, list) or len(value) > MAX_PER_SECOND_SLOTS:
            raise MetricsValidationError(
                f"{field} must be a list of at most {MAX_PER_SECOND_SLOTS} counts")
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise MetricsValidationError(
                    f"impossible count in {field}: {item!r}")

    for field in _HISTOGRAM_FIELDS:
        value = record[field]
        if not isinstance(value, dict) or len(value) > MAX_HISTOGRAM_BUCKETS:
            raise MetricsValidationError(f"{field} must be a bounded bucket map")
        for label, count in value.items():
            if not isinstance(label, str) or not HISTOGRAM_KEY_RE.match(label):
                raise MetricsValidationError(
                    f"{field} key is not a bucket label: {label!r}")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise MetricsValidationError(
                    f"impossible count: {field}[{label}]={count!r}")

    # Impossible timing, in the one form this record can express it.
    if record["interval_ended_at"] < record["interval_started_at"]:
        raise MetricsValidationError(
            "impossible timing: interval_ended_at < interval_started_at")
    return record


# --- the interval-record writer ----------------------------------------------------


def interval_path(directory: Path | str | None = None) -> Path:
    """The stream-A file. Same directory as the shared sink, different file."""
    base = Path(directory) if directory is not None else telemetry_dir()
    return base / INTERVAL_FILENAME


class IntervalWriter:
    """One validated record per unbuffered `os.write`. Never raises.

    A verbatim reuse of `TelemetrySink._write_line`'s technique and of its
    failure contract: `O_NOFOLLOW` so a planted symlink is a refused write
    rather than an append into whatever it points at, 0700/0600 modes
    re-enforced on a pre-existing directory, and a SHORT WRITE COUNTED AS A DROP
    AND NEVER RESUMED — resuming would let a second appender interleave between
    the two syscalls and produce a line that parses as neither record.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        # The sink's lock, for the sink's reason: `stop()` may flush from the
        # caller's thread while a flusher thread that outlived its join is still
        # running. `O_APPEND` keeps the LINES from interleaving; the lock keeps
        # the COUNTERS from doing so.
        self._lock = threading.Lock()
        self.written = 0
        self.dropped = 0
        self.rejected = 0
        self.last_error: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _serialize(self, record: dict) -> bytes:
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        data = (line + "\n").encode("utf-8")
        if len(data) <= MAX_LINE_BYTES:
            return data
        # The per-second series is the only field whose length is not fixed, so
        # it is the only one shed — and shedding it sets the field that says so,
        # rather than leaving a reader to infer a quiet interval.
        slim = dict(record)
        slim["events_per_second"] = []
        slim["events_per_second_truncated"] = True
        validate_interval_record(slim)
        line = json.dumps(slim, sort_keys=True, separators=(",", ":"))
        data = (line + "\n").encode("utf-8")
        if len(data) <= MAX_LINE_BYTES:
            return data
        raise MetricsValidationError("record exceeds line cap after truncation")

    def _write_line(self, data: bytes) -> bool:
        self._path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        os.chmod(self._path.parent, DIR_MODE)
        fd = os.open(
            self._path,
            os.O_APPEND | os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
            FILE_MODE,
        )
        try:
            written = os.write(fd, data)
        finally:
            os.close(fd)
        return written == len(data)

    def write(self, record: dict) -> bool:
        """True only when the whole line landed. Every failure is counted here
        and nothing escapes: this is called from the flusher thread, and an
        exception crossing it would kill the measurement for the rest of the
        session."""
        try:
            validate_interval_record(record)
        except Exception as exc:
            self.rejected += 1
            self.last_error = type(exc).__name__
            return False
        try:
            data = self._serialize(record)
            with self._lock:
                if self._write_line(data):
                    self.written += 1
                    return True
            self.dropped += 1
            self.last_error = "short_write"
            return False
        except Exception as exc:
            # Unwritable directory, ENOSPC, EPERM, a symlink refused by
            # O_NOFOLLOW, an encoding failure — all the same answer: count it,
            # name only the exception CLASS (a message can carry a path), move on.
            self.dropped += 1
            self.last_error = type(exc).__name__
            return False


def iter_interval_records(path: Path | str):
    """Stream validated records. Deliberately a generator.

    §7.5 requires the report lane to stream this file. `sink.read_events` slurps
    (420 MB peak heap on a 60 MB file) and is documented as tests-only for
    exactly that reason; providing the streaming reader here means CP9 never has
    a reason to write a slurping one. Malformed and schema-violating lines are
    skipped, not raised — a session's measurement must survive one bad line.
    """
    path = Path(path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                validate_interval_record(record)
            except Exception:
                continue
            yield record


# --- the hot-path recorder ---------------------------------------------------------


def _utcnow_iso() -> str:
    return (datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


class CollectorMetrics:
    """Per-frame recording. Integer work only; never raises; no I/O.

    Threading model, stated because it is the correctness argument:

    - **One producer.** §6.3 mandates one producer thread/task per subscription,
      and every `on_frame`/`on_append` counter has that single writer. No lock is
      taken, because §7.1 forbids one on this path and because a single writer
      plus a reader needs none under CPython's GIL: a `list` element read yields
      either the old value or the new one, never a torn one.
    - **The closer thread** owns `on_segment_closed` and only the three
      close-latency slots, so it is a second single-writer, not a second writer
      of the same slot.
    - **The flusher thread** only reads, except for the two producer-owned
      maxima it zeroes (documented in the module docstring).
    - Because every counter is CUMULATIVE and the flusher reports DELTAS, a
      snapshot taken mid-burst cannot lose an event; it can only postpone it to
      the next interval.
    """

    # Lets a caller tell the real lane from `NULL_METRICS` without an
    # `isinstance` check against a class it would then have to import.
    enabled = True

    def __init__(self, *, environment: str, markets_subscribed: int,
                 session_id: str | None = None,
                 ring_slots: int = DEFAULT_RING_SLOTS) -> None:
        if environment not in ENVIRONMENTS:
            raise MetricsValidationError(
                f"unknown environment {environment!r}; expected one of "
                f"{list(ENVIRONMENTS)}")
        # THE NO-TICKER CONTROL, and the only place it needs to exist. The
        # subscription enters this module as a COUNT. A list or a string here
        # would be the one plausible route by which a market identifier reached
        # the telemetry directory, so both are refused at construction rather
        # than coerced with `len()` — coercion would accept a ticker string and
        # silently record its character count.
        if (isinstance(markets_subscribed, bool)
                or not isinstance(markets_subscribed, int)
                or markets_subscribed < 0):
            raise MetricsValidationError(
                "markets_subscribed must be a non-negative int — a COUNT. The "
                "metrics lane never accepts market identifiers; §7.2 keeps "
                "high-cardinality market identity out of the telemetry "
                "directory structurally, not by filtering.")
        if session_id is None:
            session_id = uuid.uuid4().hex
        if not SESSION_ID_RE.match(session_id):
            raise MetricsValidationError(
                "session_id must be a uuid4 hex string")

        self.session_id = session_id
        self.environment = environment
        self.markets_subscribed = markets_subscribed

        # --- cumulative producer counters ---------------------------------------
        self.events_received = 0
        self.events_archived = 0
        self.events_rejected = 0
        self.frames_malformed = 0
        self.event_bytes_total = 0
        self.append_calls = 0
        self.rotations_started = 0
        self.disconnects = 0
        self.reconnects = 0
        self.sequence_gaps = 0
        self.sequence_regressions = 0
        self.sequence_duplicates = 0
        self.subscription_generation = 0
        # Zeroed by the flusher each interval; see the module docstring's note
        # about the one-sample attribution race.
        self.append_us_max = 0

        # --- closer-thread counters ---------------------------------------------
        self.segments_closed = 0
        self.segment_close_ms_max = 0

        # --- pre-allocated histograms -------------------------------------------
        self._append_us = [0] * (len(APPEND_US_EDGES) + 1)
        self._append_us_rotation = [0] * (len(APPEND_US_EDGES) + 1)
        self._event_bytes = [0] * (len(EVENT_BYTES_EDGES) + 1)
        self._close_ms = [0] * (len(CLOSE_MS_EDGES) + 1)

        # --- the 1 s ring --------------------------------------------------------
        # Two parallel fixed lists: the count, and the absolute monotonic second
        # that count belongs to. The stamp is what makes a stale slot
        # distinguishable from a genuinely idle second after a wrap.
        if ring_slots < 2:
            raise MetricsValidationError("ring_slots must be at least 2")
        self.ring_slots = ring_slots
        self._ring = [0] * ring_slots
        self._ring_sec = [-1] * ring_slots

        # --- honesty fields -------------------------------------------------------
        # A metrics call that threw is a metrics call whose data is gone. It is
        # counted so a reader can see that the measurement itself misbehaved.
        self.observe_errors = 0
        self.metric_flush_drops = 0

        # --- flusher-thread sources (bound by the orchestrator) -------------------
        self._reader_lag_source = None
        self._transport_counters_source = None
        self._archive_state_source = None
        self.source_failures = 0

    # -- binding: read BY THE FLUSHER THREAD, never on the hot path ---------------

    def bind_reader_lag(self, source) -> None:
        """`() -> int | None`. Recommended: `transport.queue_depth`.

        Sampled off the hot path because CP0 12.3's attribute chain is exactly
        the kind of work §7.1 forbids per frame. `None` from the source means
        UNAVAILABLE and is carried through as `null`, never as 0."""
        self._reader_lag_source = source

    def bind_transport_counters(self, source) -> None:
        """`() -> dict`. Recommended: `transport.counters.snapshot`.

        Read for `frames_malformed` and `reader_stall_ms_max`, both of which the
        transport already counts on its own read path — recomputing them here
        would be a second measurement of the same thing that could disagree."""
        self._transport_counters_source = source

    def bind_archive_state(self, source) -> None:
        """`() -> dict` with keys `rotation_failures` and `closer_outstanding`.

        §7.3 defines `closer_outstanding_max` as the peak of
        `EventArchive._closer.outstanding()`. That reach into a private
        attribute stays in the orchestrator's lambda rather than here: this
        module must not know the archive's internals, and a `websockets`-style
        upgrade of the archive should break one line in one file."""
        self._archive_state_source = source

    # -- hot path ------------------------------------------------------------------

    def on_frame(self, received_mono_ns: int, wire_bytes: int = 0) -> None:
        """One frame off the wire. THE hot-path call.

        `received_mono_ns` is the collector's own `time.monotonic_ns()` from
        step 1 of §6.3 — reused rather than re-read, so this call adds no clock
        syscall to the per-frame budget. `wire_bytes` is the delta of
        `transport.counters.bytes_received` across the yield, which is that
        frame's payload length; a malformed frame skipped between two yields is
        attributed to the following frame's size sample, which is a known and
        bounded imprecision in the size DISTRIBUTION only — `event_bytes_total`
        stays exact.

        Counts every frame the collector received, malformed included, so it is
        the denominator §7.4 asks for: received = archived + rejected +
        malformed + (whatever a dry run never appended).
        """
        try:
            self.events_received += 1
            if wire_bytes > 0:
                self.event_bytes_total += wire_bytes
                self._event_bytes[bisect_right(EVENT_BYTES_EDGES, wire_bytes)] += 1
            sec = received_mono_ns // 1_000_000_000
            idx = sec % self.ring_slots
            if self._ring_sec[idx] == sec:
                self._ring[idx] += 1
            else:
                self._ring_sec[idx] = sec
                self._ring[idx] = 1
        except Exception:
            self.observe_errors += 1

    def on_append(self, elapsed_ns: int, rotated: bool = False) -> None:
        """One completed `archive.append()`.

        `rotated` splits the sample into the second histogram. §8.4 point 2: the
        rotation lands on ONE append (`_writer_for` does `presence()` stats, an
        flock and a file open inline), and merging it into the ordinary
        distribution makes the session maximum unattributable. The orchestrator
        knows a rotation happened because `append()` returns the segment `Path`
        and it differs from the previous one.
        """
        try:
            us = elapsed_ns // 1_000
            self.append_calls += 1
            if rotated:
                self.rotations_started += 1
                self._append_us_rotation[bisect_right(APPEND_US_EDGES, us)] += 1
            else:
                self._append_us[bisect_right(APPEND_US_EDGES, us)] += 1
            self.events_archived += 1
            if us > self.append_us_max:
                self.append_us_max = us
        except Exception:
            self.observe_errors += 1

    def on_append_rejected(self, elapsed_ns: int = 0) -> None:
        """A typed archive rejection (`ArchiveError` from a `RejectReason`).

        Counted apart from `frames_malformed` forever: §7.4 is explicit that a
        single merged "dropped" number would hide which layer failed. The append
        still cost time, so it still lands in the latency histogram."""
        try:
            self.events_rejected += 1
            us = elapsed_ns // 1_000
            self.append_calls += 1
            self._append_us[bisect_right(APPEND_US_EDGES, us)] += 1
            if us > self.append_us_max:
                self.append_us_max = us
        except Exception:
            self.observe_errors += 1

    def on_frame_malformed(self) -> None:
        """Not JSON, or not a dict. Ignored when a transport source is bound —
        the transport is then the single authority and two counters of one thing
        would eventually disagree."""
        try:
            self.frames_malformed += 1
        except Exception:  # pragma: no cover - an int increment cannot fail
            self.observe_errors += 1

    # -- warm path: per reconnect / per fault, not per frame ------------------------

    def on_disconnect(self) -> None:
        try:
            self.disconnects += 1
        except Exception:  # pragma: no cover
            self.observe_errors += 1

    def on_reconnect(self, subscription_generation: int | None = None) -> None:
        try:
            self.reconnects += 1
            if isinstance(subscription_generation, int):
                self.subscription_generation = subscription_generation
        except Exception:  # pragma: no cover
            self.observe_errors += 1

    def on_sequence_fault(self, kind: str) -> None:
        """`gap` | `regression` | `duplicate`. An unknown kind is counted as an
        observe error rather than silently ignored — a fault the collector could
        not classify is itself information."""
        try:
            if kind == "gap":
                self.sequence_gaps += 1
            elif kind == "regression":
                self.sequence_regressions += 1
            elif kind == "duplicate":
                self.sequence_duplicates += 1
            else:
                self.observe_errors += 1
        except Exception:  # pragma: no cover
            self.observe_errors += 1

    # -- closer thread ---------------------------------------------------------------

    def on_segment_closed(self, elapsed_ns: int) -> None:
        """Called ON THE CLOSER THREAD, from an `_on_rotation_closed` hook.

        §7.4: close is already off the producer thread and measuring it must not
        put it back on. Timing it here means the producer pays nothing for the
        close-latency distribution."""
        try:
            ms = elapsed_ns // 1_000_000
            self.segments_closed += 1
            self._close_ms[bisect_right(CLOSE_MS_EDGES, ms)] += 1
            if ms > self.segment_close_ms_max:
                self.segment_close_ms_max = ms
        except Exception:
            self.observe_errors += 1

    # -- flusher-thread reads ---------------------------------------------------------

    def histogram_snapshot(self) -> dict:
        """Cumulative bucket counts, copied. Lists, not dicts, so the flusher
        can difference them positionally and only pay for labels once, on the
        record it is about to write."""
        return {
            "append_us": list(self._append_us),
            "append_us_rotation": list(self._append_us_rotation),
            "event_bytes": list(self._event_bytes),
            "close_ms": list(self._close_ms),
        }

    def per_second_counts(self, first_sec: int, last_sec: int) -> list[int]:
        """Counts for the whole seconds in `[first_sec, last_sec)`.

        A slot whose stamp does not match the second it would represent is a
        genuinely idle second (or one the ring has wrapped past) and reads 0.
        The caller bounds the span, so this allocates a list of at most
        `MAX_PER_SECOND_SLOTS` ints."""
        counts = []
        for sec in range(first_sec, last_sec):
            idx = sec % self.ring_slots
            counts.append(self._ring[idx] if self._ring_sec[idx] == sec else 0)
        return counts

    def take_append_us_max(self) -> int:
        """Read-and-zero. The flusher owns the reset; see the module docstring's
        note on the one-sample attribution race."""
        value = self.append_us_max
        self.append_us_max = 0
        return value

    def take_segment_close_ms_max(self) -> int:
        value = self.segment_close_ms_max
        self.segment_close_ms_max = 0
        return value

    def read_source(self, source):
        """Call a bound source defensively. `None` on any failure.

        CP0 12.3's chain is undocumented and an `AttributeError` after a library
        upgrade must never reach the loop — and must never be laundered into a
        `0` either, which is why the failure returns `None` and is counted."""
        if source is None:
            return None
        try:
            return source()
        except Exception:
            self.source_failures += 1
            return None


class NullCollectorMetrics:
    """The disabled lane. Every method is a no-op with the same signature.

    Exists for CP5, which must run the SAME fixture load with instrumentation on
    and off. Without it the comparison would require an `if metrics is not None`
    branch in the per-frame path, which would mean the "off" run and the "on" run
    no longer execute the same code — the overhead gate would then be measuring
    its own scaffolding.
    """

    session_id = "0" * 32
    enabled = False

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def _noop(self, *_args, **_kwargs) -> None:
        return None

    on_frame = on_append = on_append_rejected = on_frame_malformed = _noop
    on_disconnect = on_reconnect = on_sequence_fault = _noop
    on_segment_closed = _noop
    bind_reader_lag = bind_transport_counters = bind_archive_state = _noop


NULL_METRICS = NullCollectorMetrics()


# --- the flusher ------------------------------------------------------------------


class MetricsFlusher:
    """One thread, two cadences, and no way to break the collector.

    Every `flush_interval_s` it turns the cumulative counters into one interval
    record and appends it. Every `sample_interval_s` — much faster — it samples
    the two quantities that are maxima rather than counts (`reader_lag_frames`
    and `closer_outstanding`), because a 10 s sample of a peak is not a peak.
    Both duties live on one thread so the measurement costs the host one thread,
    not two.

    The failure contract is the sink's, promoted to the thread level: a failing
    write is counted in `metric_flush_drops` and the next interval is still
    attempted; a source that raises is counted and its field goes `null`; and if
    the thread itself dies, `thread_error` names the exception CLASS, the
    collector keeps running, and `stop()` still writes a final record from the
    caller's thread. A measurement lane that can end a session would destroy
    more evidence than it produces.
    """

    def __init__(self, metrics: CollectorMetrics, *,
                 path: Path | str | None = None,
                 directory: Path | str | None = None,
                 flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
                 sample_interval_s: float = DEFAULT_SAMPLE_INTERVAL_S,
                 writer: IntervalWriter | None = None,
                 clock_ns=time.monotonic_ns) -> None:
        self._metrics = metrics
        if writer is not None:
            self._writer = writer
        else:
            self._writer = IntervalWriter(
                Path(path) if path is not None else interval_path(directory))
        self._flush_interval_s = float(flush_interval_s)
        self._sample_interval_s = min(float(sample_interval_s),
                                      float(flush_interval_s))
        # ONE clock, in nanoseconds, and injectable. The interval's elapsed
        # time and the 1 s ring's second index must come from the SAME clock the
        # producer stamps frames with, or the per-second series and the counters
        # would describe two slightly different windows.
        self._clock_ns = clock_ns

        self._stop = threading.Event()
        self._flush_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._interval_index = 0
        self._prev_counters = self._counter_snapshot()
        self._prev_histograms = metrics.histogram_snapshot()
        self._interval_start_mono = self._clock_ns()
        self._interval_start_wall = _utcnow_iso()
        # The first second not yet reported in `events_per_second`.
        self._ring_cursor_sec = self._interval_start_mono // 1_000_000_000
        self._lag_max: int | None = None
        self._lag_seen = False
        self._closer_outstanding_max = 0
        self.thread_error: str | None = None
        self.intervals_written = 0

    # -- lifecycle ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._writer.path

    @property
    def writer(self) -> IntervalWriter:
        return self._writer

    @property
    def alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def start(self) -> bool:
        """Start the thread. Never raises — a host that cannot start a thread
        must still be able to collect a tape."""
        try:
            if self._thread is not None:
                return False
            self._interval_start_mono = self._clock_ns()
            self._interval_start_wall = _utcnow_iso()
            self._ring_cursor_sec = (
                self._interval_start_mono // 1_000_000_000)
            thread = threading.Thread(
                target=self._run, name="kalshi-tape-metrics", daemon=True)
            thread.start()
            self._thread = thread
            return True
        except Exception as exc:
            self.thread_error = type(exc).__name__
            self._metrics.metric_flush_drops += 1
            return False

    def stop(self, timeout_s: float = 5.0) -> bool:
        """Stop, join, and write ONE final record on the CALLER'S thread.

        The final flush is not the thread's job on purpose: if the thread died
        an hour ago, the session's last interval is the one that explains why,
        and it must still be written."""
        try:
            self._stop.set()
            thread = self._thread
            if thread is not None:
                thread.join(timeout_s)
            self._thread = None
            self._flush(final=True)
            return True
        except Exception as exc:
            self.thread_error = self.thread_error or type(exc).__name__
            self._metrics.metric_flush_drops += 1
            return False

    def __enter__(self) -> "MetricsFlusher":
        self.start()
        return self

    def __exit__(self, *_exc) -> bool:
        self.stop()
        return False

    # -- the thread -------------------------------------------------------------------

    def _run(self) -> None:
        try:
            flush_ns = int(self._flush_interval_s * 1_000_000_000)
            next_flush = self._clock_ns() + flush_ns
            while not self._stop.wait(self._sample_interval_s):
                self._sample()
                if self._clock_ns() >= next_flush:
                    self._flush()
                    next_flush = self._clock_ns() + flush_ns
        except Exception as exc:  # pragma: no cover - defence in depth
            # The thread is dying. It cannot take the collector with it, and it
            # must not vanish silently: the class name is the whole diagnosis a
            # measurement file is allowed to carry.
            self.thread_error = type(exc).__name__
            try:
                self._metrics.metric_flush_drops += 1
            except Exception:
                pass

    def _sample(self) -> None:
        """Peak-tracking for the two quantities a 10 s read would miss."""
        try:
            metrics = self._metrics
            lag = metrics.read_source(metrics._reader_lag_source)
            if isinstance(lag, int) and not isinstance(lag, bool) and lag >= 0:
                self._lag_seen = True
                if self._lag_max is None or lag > self._lag_max:
                    self._lag_max = lag
            state = metrics.read_source(metrics._archive_state_source)
            if isinstance(state, dict):
                outstanding = state.get("closer_outstanding")
                if (isinstance(outstanding, int)
                        and not isinstance(outstanding, bool)
                        and outstanding > self._closer_outstanding_max):
                    self._closer_outstanding_max = outstanding
        except Exception:
            self._metrics.source_failures += 1

    # -- record assembly ----------------------------------------------------------------

    def _counter_snapshot(self) -> dict:
        m = self._metrics
        return {
            "events_received": m.events_received,
            "events_archived": m.events_archived,
            "events_rejected": m.events_rejected,
            "frames_malformed": m.frames_malformed,
            "event_bytes_total": m.event_bytes_total,
            "append_calls": m.append_calls,
            "rotations_started": m.rotations_started,
            "disconnects": m.disconnects,
            "reconnects": m.reconnects,
            "sequence_gaps": m.sequence_gaps,
            "sequence_regressions": m.sequence_regressions,
            "sequence_duplicates": m.sequence_duplicates,
            "segments_closed": m.segments_closed,
        }

    @staticmethod
    def _delta_histogram(current: list, previous: list,
                         labels: tuple[str, ...]) -> dict:
        """Bucket labels are attached HERE, once per interval — never per event.
        Empty buckets are omitted so the record stays small and a reader never
        has to distinguish "zero" from "absent" (both mean no sample)."""
        out = {}
        for index, label in enumerate(labels):
            count = current[index] - previous[index]
            if count > 0:
                out[label] = count
        return out

    def _build_record(self, *, final: bool) -> dict:
        metrics = self._metrics
        now_ns = self._clock_ns()
        counters = self._counter_snapshot()
        histograms = metrics.histogram_snapshot()

        # The per-second window covers only COMPLETED seconds, so the in-flight
        # one is never half-read and then skipped. The final flush includes it,
        # because by then the producer has stopped and the second IS complete.
        now_sec = now_ns // 1_000_000_000
        last_sec = now_sec + 1 if final else now_sec
        first_sec = self._ring_cursor_sec
        truncated = False
        if last_sec - first_sec > MAX_PER_SECOND_SLOTS:
            first_sec = last_sec - MAX_PER_SECOND_SLOTS
            truncated = True
        per_second = (metrics.per_second_counts(first_sec, last_sec)
                      if last_sec > first_sec else [])
        self._ring_cursor_sec = max(last_sec, self._ring_cursor_sec)

        transport = metrics.read_source(metrics._transport_counters_source)
        malformed_total = counters["frames_malformed"]
        stall_ms_max = 0
        if isinstance(transport, dict):
            value = transport.get("frames_malformed")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                malformed_total = value
            value = transport.get("reader_stall_ms_max")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                stall_ms_max = value

        # Whichever authority supplied `frames_malformed` this interval must also
        # be the baseline for the NEXT one. Differencing a transport total
        # against a producer counter would re-report the whole transport total
        # in every record.
        counters["frames_malformed"] = malformed_total

        archive_state = metrics.read_source(metrics._archive_state_source)
        rotation_failures = 0
        if isinstance(archive_state, dict):
            value = archive_state.get("rotation_failures")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                rotation_failures = value

        previous = self._prev_counters
        record = {
            "schema_version": SCHEMA_VERSION,
            "session_id": metrics.session_id,
            "environment": metrics.environment,
            "interval_index": self._interval_index,
            "interval_started_at": self._interval_start_wall,
            "interval_ended_at": _utcnow_iso(),
            "interval_wall_ms": max(
                0, (now_ns - self._interval_start_mono) // 1_000_000),

            "events_received": counters["events_received"] - previous["events_received"],
            "events_archived": counters["events_archived"] - previous["events_archived"],
            "events_rejected": counters["events_rejected"] - previous["events_rejected"],
            "frames_malformed": max(0, malformed_total - previous["frames_malformed"]),

            "event_bytes_total": counters["event_bytes_total"] - previous["event_bytes_total"],
            "event_bytes_histogram": self._delta_histogram(
                histograms["event_bytes"], self._prev_histograms["event_bytes"],
                EVENT_BYTES_LABELS),
            "append_us_histogram": self._delta_histogram(
                histograms["append_us"], self._prev_histograms["append_us"],
                APPEND_US_LABELS),
            "append_us_rotation_histogram": self._delta_histogram(
                histograms["append_us_rotation"],
                self._prev_histograms["append_us_rotation"], APPEND_US_LABELS),
            "append_us_max": metrics.take_append_us_max(),
            "append_calls": counters["append_calls"] - previous["append_calls"],

            "rotations_started": counters["rotations_started"] - previous["rotations_started"],
            "rotation_failures": rotation_failures,
            "closer_outstanding_max": self._closer_outstanding_max,
            "segments_closed": counters["segments_closed"] - previous["segments_closed"],
            "segment_close_ms_histogram": self._delta_histogram(
                histograms["close_ms"], self._prev_histograms["close_ms"],
                CLOSE_MS_LABELS),
            "segment_close_ms_max": metrics.take_segment_close_ms_max(),

            # UNAVAILABLE (null) rather than 0 when the CP0 12.3 chain broke or
            # when no source is bound at all.
            "reader_lag_frames_max": self._lag_max if self._lag_seen else None,
            "reader_stall_ms_max": stall_ms_max,
            "disconnects": counters["disconnects"] - previous["disconnects"],
            "reconnects": counters["reconnects"] - previous["reconnects"],
            "subscription_generation": metrics.subscription_generation,
            "sequence_gaps": counters["sequence_gaps"] - previous["sequence_gaps"],
            "sequence_regressions": (counters["sequence_regressions"]
                                     - previous["sequence_regressions"]),
            "sequence_duplicates": (counters["sequence_duplicates"]
                                    - previous["sequence_duplicates"]),

            "events_per_second": per_second,
            "events_per_second_truncated": truncated,
            "markets_subscribed": metrics.markets_subscribed,
            # Cumulative on purpose: a record that reports its OWN drop count
            # cannot exist, so the only useful form is "how many have been lost
            # in this session so far".
            "metric_flush_drops": metrics.metric_flush_drops,
        }

        self._prev_counters = counters
        self._prev_histograms = histograms
        self._interval_start_mono = now_ns
        self._interval_start_wall = record["interval_ended_at"]
        self._interval_index += 1
        self._lag_max = None
        self._lag_seen = False
        self._closer_outstanding_max = 0
        return record

    def _flush(self, final: bool = False) -> bool:
        """Build and append one record. Total: every path returns a bool.

        Serialised against itself, because `stop()`'s final flush can race a
        flusher thread that outlived its join, and two interleaved builds would
        double-count one interval's delta into both records."""
        with self._flush_lock:
            return self._flush_locked(final)

    def _flush_locked(self, final: bool) -> bool:
        try:
            record = self._build_record(final=final)
        except Exception as exc:
            self.thread_error = self.thread_error or type(exc).__name__
            try:
                self._metrics.metric_flush_drops += 1
            except Exception:  # pragma: no cover
                pass
            return False
        if self._writer.write(record):
            self.intervals_written += 1
            return True
        self._metrics.metric_flush_drops += 1
        return False

    def flush_now(self, final: bool = False) -> bool:
        """Deterministic flush for tests and for a session's end. Same path the
        thread takes, so a test proves the thread's behaviour and not a
        parallel implementation of it."""
        return self._flush(final=final)
