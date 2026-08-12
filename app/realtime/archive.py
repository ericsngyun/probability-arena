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
import queue
import re
import statistics
import threading
import time
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.realtime.book import EventEnvelope
from app.realtime.segment import (
    DEFAULT_MAX_SEGMENT_AGE_S,
    DEFAULT_MAX_SEGMENT_BYTES,
    DEFAULT_MAX_SEGMENT_RECORDS,
    EVENTS_FILENAME,
    MANIFEST_FILENAME,
    SegmentError,
    SegmentState,
    SegmentWriter,
    read_segment_records,
    verify_archive,
    verify_chain,
)
from app.realtime.archive_head import (
    ArchiveNotInitializedError,
    initialize_archive,
    load_authoritative_head,
    read_generation,
    read_genesis,
)
from app.realtime.canonical import (
    CANONICAL_SCHEMA_VERSION,
    parse_canonical_datetime,
    canonical_bytes,
    digest_hex,
    parse_canonical,
)
from app.realtime.fixedpoint import loads_exact
from app.realtime import evidence_fs

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


_CLOSER_STOP = object()


def _swallow(fn):
    """Run `fn()` and return the exception it raised, or None. Used where a
    failure must be RECORDED against one segment rather than propagated and
    allowed to wedge every other partition."""
    try:
        fn()
    except BaseException as exc:            # noqa: BLE001 - returned, not lost
        return exc
    return None


class _RotationCloser:
    """KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- ONE dedicated thread that
    runs `SegmentWriter.close()` for RETIRING segments, so a rotation never
    runs on the producer's thread.

    THE STALL THIS REMOVES. `close()` is not a tail operation: it flushes and
    fsyncs the event file, reads every record back for reconciliation, runs a
    full independent `verify_segment` (a third complete read), publishes the
    manifest, and -- with `commit_to_head` True, which is `SegmentWriter`'s
    default and which `_writer_for` has never overridden -- fsyncs a
    generation record and the current-head pointer under the archive lock.
    Measured on the committed benchmark (`tests/benchmarks/
    segment_close_cost.py`) at the old 20,000-record bound with
    `commit_to_head=True`: ~2.3-2.9 s of CPU and 3.4-4.7 s of wall on an idle
    machine, and 14.8 s of wall under contention. `_writer_for` ran that
    inline, on whichever thread called `append()` -- for the Kalshi collector,
    the websocket reader. A 3-15 s stall there is a missed ping, a
    server-side disconnect, and a hole in the tape: the archive's own commit
    step destroying the ingestion it exists to record.

    WHY THIS IS SMALL AND SAFE. The successor writer is ALREADY constructed
    and installed under `_writers_lock` before the predecessor is handed
    here, so nothing about admission depends on when this finishes. The
    retiring writer stays in `_retiring` (so its segment id is never reissued)
    until this thread has closed it and the sink has popped it. Exactly one
    thread runs closes, so two rotations of two partitions still serialise
    against each other rather than contending on the archive head lock.
    """

    def __init__(self, sink, *, name="kalshi-segment-closer"):
        self._sink = sink                  # callable(writer, exc_or_None)
        self._name = name
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._cv = threading.Condition()
        self._outstanding = 0
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._start_lock = threading.Lock()

    @property
    def outstanding(self) -> int:
        with self._cv:
            return self._outstanding

    def submit(self, writer) -> bool:
        """Queue one retiring writer for closing. False if the closer has
        already been shut down, in which case NOTHING was queued and the
        caller must close inline -- silently accepting work onto a stopped
        thread would leave `outstanding` permanently non-zero and hang the
        next `drain()`.

        KNOWN-OPEN RACE, DOCUMENTED RATHER THAN FIXED
        (KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 ROUND 3; two reviewers
        reconstructed it independently, and it was recorded nowhere in the
        code -- unlike every other accepted risk in this module.)

        The `_stopped` check and the `_outstanding` increment are TWO
        separate critical sections under TWO different locks. Between them a
        concurrent `shutdown()` can observe `outstanding == 0`, complete
        `drain()`, set `_stopped` and stop the thread -- after which this
        method returns True having put a writer onto a queue nobody will ever
        read again, and `_outstanding` is permanently stuck at 1. The
        consequence is NOT lost durability: `EventArchive.close()` drains
        first and then closes whatever is still in `_retiring` INLINE, which
        catches exactly this writer (verified 4/4 trials: `acked ==
        records_read`, `verdict VALID`, zero uncommitted directories). What
        it does cost is a false negative on the way out -- any later
        `wait_for_rotations()` blocks its full timeout and returns False, and
        the `<closer>` rotation-failure line is appended, for a rotation that
        actually succeeded.

        THE FIX, when it is taken: fold the `_stopped` check and the
        `_outstanding` increment into ONE `_start_lock` critical section, so
        no `shutdown()` can interleave between them. It is deliberately NOT
        applied here -- no test can be made to FAIL on revert without an
        injection point (a seam between the two blocks) that the fixed
        version never executes, so the "fix" would ship with a pin that
        proves nothing. See the round-2 pass that declined it for the same
        reason.
        """
        with self._start_lock:
            if self._stopped:
                return False
        # <-- the window: `_stopped` was False a moment ago, and nothing holds
        #     it False until the increment below lands. See the docstring.
        with self._cv:
            self._outstanding += 1
        with self._start_lock:
            if self._thread is None:
                # Daemon: a wedged close must never keep the interpreter
                # alive. `EventArchive.close()` drains explicitly, which is
                # the path that actually needs the work to finish.
                self._thread = threading.Thread(
                    target=self._run, name=self._name, daemon=True)
                self._thread.start()
        self._q.put(writer)
        return True

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is _CLOSER_STOP:
                return
            exc = None
            try:
                item.close()
            except BaseException as e:      # noqa: BLE001 - reported via sink
                exc = e
            try:
                self._sink(item, exc)
            except BaseException:           # noqa: BLE001 - never wedge
                pass
            finally:
                with self._cv:
                    self._outstanding -= 1
                    self._cv.notify_all()

    def drain(self, timeout_s: float | None = None) -> bool:
        """Block until every submitted close has finished. False on timeout."""
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._cv:
            while self._outstanding:
                if deadline is None:
                    self._cv.wait(timeout=0.25)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(timeout=remaining)
        return True

    def shutdown(self, timeout_s: float | None = None) -> bool:
        drained = self.drain(timeout_s)
        with self._start_lock:
            self._stopped = True
            thread = self._thread
            if thread is not None:
                self._q.put(_CLOSER_STOP)
        if thread is not None:
            thread.join(timeout=5.0)
        return drained


def _canon(obj) -> str:
    """Deprecated shim. Everything now digests over `canonical_bytes`.

    Kept only so an older archive can still be *read* for diagnosis; it must
    never be used to compute a digest that will later be re-verified, which is
    precisely how the two-serializer defect arose.
    """
    return canonical_bytes(obj).decode("utf-8")


def _undecodable_tail_records(events_path, decoded: int) -> int:
    """1 when bytes remain past the decodable prefix, else 0.

    A torn or malformed tail costs the incomplete terminal record; anything
    before it is recovered. This reports that it happened rather than leaving
    the loss invisible.

    Routed through `evidence_fs.bounded_read`/`open_bounded_gzip`, not a raw
    `Path.read_bytes()` + `gzip.open(events_path)`: this call sits three
    lines below `read_segment_records` (its sibling call in the same salvage
    read), which correctly refuses a device node or FIFO via `is_file()`
    before opening it -- this one had no such guard, so `events.jsonl.gz`
    symlinked to `/dev/zero` (reachable only via the diagnostic salvage read,
    which deliberately does not enforce containment) read forever, allocating
    without bound. `bounded_read` proves the target resolves to a regular
    file and is under a fixed size bound BEFORE any byte is read, so neither
    a hang nor an unbounded allocation is reachable from here regardless of
    what the salvage path is pointed at.
    """
    raw, why = evidence_fs.bounded_read(events_path)
    if why is not None or not raw:
        return 0
    try:
        with evidence_fs.open_bounded_gzip(events_path) as fh:
            fh.read()
    except Exception:
        return 1
    return 0


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

    def __init__(self, root: str | Path, *, environment: str, venue: str = "kalshi",
                 expected_archive_id: str | None = None,
                 max_segment_records: int | None = DEFAULT_MAX_SEGMENT_RECORDS,
                 max_segment_age_s: float | None = DEFAULT_MAX_SEGMENT_AGE_S,
                 max_segment_bytes: int | None = DEFAULT_MAX_SEGMENT_BYTES):
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
        # Rotation policy, passed through to every writer. A3 (synchronous
        # canonical archive): these default to `segment.py`'s measured,
        # bounded constants -- not `None` -- because shipping synchronous
        # archival with an unbounded segment is exactly what this milestone
        # forbids: an hour-long segment at a few thousand events/s would be
        # millions of records and take minutes to close. A caller can still
        # pass `None` explicitly to opt out, the same way it always could.
        # What is NOT acceptable is committing nothing until process
        # shutdown — a collector running for a day held every hour open, and
        # a SIGKILL lost all of them at once.
        self.max_segment_records = max_segment_records
        self.max_segment_age_s = max_segment_age_s
        self.max_segment_bytes = max_segment_bytes
        self.expected_archive_id = expected_archive_id
        self.rotations = 0
        self.rotation_failures: list = []
        # A8: rotations close OFF the producer thread. See `_RotationCloser`.
        self._closer = _RotationCloser(self._on_rotation_closed)
        # How long `close()` waits for in-flight rotations before giving up
        # and closing whatever is left inline. Generous by design: `close()`
        # is the commit point, and an unclosed segment is not evidence.
        self.rotation_close_timeout_s: float | None = 300.0
        self.missing_committed_segments: list = []
        self.diagnostic_order_unauthenticated = False
        self._retiring: dict = {}
        # A partition's CURRENT segment id, which diverges from the partition
        # id as soon as it rotates.
        self._live_segment_id: dict = {}
        self._rotations_for: dict = {}
        # The archive must already exist. A collector consumes an initialized
        # archive and can never bring one into existence: "the head is missing,
        # therefore this is a new archive" is the inference that let a rebuilt
        # history certify its own deletions.
        self.genesis = read_genesis(self.root, self.environment,
                                    expected_archive_id=expected_archive_id)
        self.archive_id = self.genesis["archive_id"]

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
        """One LIVE writer per partition, rotated without ever exposing a closer.

        Wall-clock hour is partition METADATA, not segment identity. Several
        segments may live inside one hour, and the partition's current segment
        id is resolved against disk rather than remembered only in RAM — a
        collector that restarted inside an hour it had already committed used to
        walk straight back into the committed segment and raise on every event
        for the rest of that hour, which negated rotation entirely.
        """
        if self._closed:
            raise ArchiveError("archive is closed; no further events accepted")
        base = self._segment_id(when)
        writer = self._writers.get(self._live_segment_id.get(base, base))
        if writer is not None and writer.state is SegmentState.OPEN \
                and not writer.rotation_due:
            return writer

        retiring = None
        with self._writers_lock:
            if self._closed:
                raise ArchiveError("archive is closed; no further events accepted")
            seg_id = self._live_segment_id.get(base, base)
            writer = self._writers.get(seg_id)
            if writer is not None and writer.state is SegmentState.OPEN \
                    and not writer.rotation_due:
                return writer
            if writer is not None:
                # Hand nobody a CLOSING writer. Returning one meant every other
                # producer was told `shutdown_in_progress` for the duration of
                # the close: 2,023 of 2,400 events rejected under 8 producers.
                # The successor is installed FIRST and the predecessor is closed
                # outside the lock, so admissions switch atomically and the
                # drain never blocks the hot path.
                retiring = writer
                self._writers.pop(seg_id, None)
                # Still holds its flock and has not published a manifest yet, so
                # the successor must not be handed the same id.
                self._retiring[seg_id] = writer
            # Construct FIRST, then publish the id. Assigning
            # `_live_segment_id` before the constructor meant a writer.lock held
            # by ANOTHER process left the partition pointing at an id with no
            # writer, and `_next_segment_id` re-derived that same blocked id
            # forever — every event lost for the rest of a rolling restart.
            writer = last_error = None
            for candidate in self._candidate_segment_ids(base):
                try:
                    writer = SegmentWriter(
                        self.root, environment=self.environment,
                        segment_id=candidate,
                        partition_identity=str(
                            self.partition(when).relative_to(self.root)),
                        subscription_metadata={"venue": self.venue},
                        expected_archive_id=self.expected_archive_id,
                        max_records=self.max_segment_records,
                        max_age_s=self.max_segment_age_s,
                        max_bytes=self.max_segment_bytes)
                except SegmentError as exc:
                    # A foreign flock means the id is in use, not that the
                    # partition is dead. Advance to the next one.
                    last_error = exc
                    continue
                except OSError as exc:
                    self.rotation_failures.append((candidate, repr(exc)))
                    raise ArchiveError(
                        f"could not open segment {candidate!r}: {exc!r}") from exc
                seg_id = candidate
                self._live_segment_id[base] = seg_id
                self._writers[seg_id] = writer
                break
            if writer is None:
                self.rotation_failures.append((base, repr(last_error)))
                raise ArchiveError(
                    f"every candidate segment id for partition {base!r} is "
                    f"held by a live writer: {last_error!r}")
        if retiring is not None:
            # A8: HANDED OFF, not run here. This used to be `retiring.close()`
            # inline -- outside the lock, but still on the PRODUCER's thread,
            # which for the Kalshi collector is the websocket reader. See
            # `_RotationCloser` for the measured stall that produced (~3-5 s
            # at the old bound on an idle machine, 14.8 s under contention)
            # and why handing it to one dedicated thread is a small change:
            # the successor above is already constructed and installed, so
            # nothing waiting on this call was ever waiting for anything a
            # producer needs.
            if not self._closer.submit(retiring):
                # The closer is already shut down (an append racing close()).
                # Do it here rather than lose the commit.
                self._on_rotation_closed(
                    retiring,
                    _swallow(retiring.close))
        return writer

    def _on_rotation_closed(self, writer, exc) -> None:
        """Called on the closer thread when one retiring segment finishes."""
        if exc is None:
            with self._writers_lock:
                self.rotations += 1
        else:
            # One partition's failed rotation must not wedge the archive:
            # the successor is already installed and admitting.
            self.rotation_failures.append((writer.segment_id, repr(exc)))
        with self._writers_lock:
            self._retiring.pop(writer.segment_id, None)

    def wait_for_rotations(self, timeout_s: float | None = 60.0) -> bool:
        """Block until every rotation handed to the closer has committed.

        Public because rotation is now ASYNCHRONOUS: an operator (or a test)
        that needs to observe `rotations`/`rotation_failures`, or to read the
        archive head, has to be able to say "after the rotations that have
        already been triggered". Returns False on timeout rather than raising
        -- the caller decides what a slow commit means.
        """
        return self._closer.drain(timeout_s)

    def _check_partition_writable(self, env_dir: Path, base: str) -> None:
        """Refuse a PERMANENTLY invalid partition location once, up front.

        KALSHI-ARCHIVE-CORE-REMEDIATION-002 A1.4: a symlinked `env=<name>/`
        makes `SegmentWriter.__init__`'s containment check raise
        `SegmentError` for EVERY candidate id, identically -- and
        `_writer_for`'s retry loop cannot tell "this id collided with a live
        writer" (transient; try the next one) apart from "this location can
        never be written to" (permanent; no candidate will ever succeed), so
        it retried up to 10,000 times, at a measured cost of 2-20+ seconds,
        to reach a verdict that was knowable on the FIRST attempt. Checking
        containment on `env_dir` itself, once, before any candidate is even
        constructed, makes the permanently-invalid case fail immediately
        with a typed `ArchiveError` and leaves the transient-collision retry
        loop in `_candidate_segment_ids` for the case it actually exists for.
        """
        present, why = evidence_fs.presence(env_dir)
        if why is not None:
            raise ArchiveError(
                f"cannot determine whether partition {base!r} is writable: "
                f"{why}")
        if not present:
            return
        reason = evidence_fs.containment_reason(self.root, env_dir)
        if reason is not None:
            raise ArchiveError(
                f"partition {base!r} cannot be written: {reason}")

    def _candidate_segment_ids(self, base: str):
        """Every id this partition may use, in order, skipping committed ones.

        Per-candidate manifest presence is checked with `evidence_fs.presence`
        rather than `Path.exists()`: `Path.exists()` swallows every OSError
        EXCEPT `EACCES`, so an unreadable segment directory (the manifest
        file itself unreadable, or the directory that contains it) let a raw
        `PermissionError` escape from here, uncaught and untyped, straight out
        of `append()`. `presence` is total, so an unexaminable candidate now
        raises the same typed `ArchiveError` every other failure in this
        generator raises, instead of a bare builtin exception.
        """
        env_dir = self.root / f"env={self.environment}"
        self._check_partition_writable(env_dir, base)
        for n in range(0, 10_000):
            candidate = base if n == 0 else f"{base}.r{n:04d}"
            directory = env_dir / f"segment={candidate}"
            present, why = evidence_fs.presence(directory / MANIFEST_FILENAME)
            if why is not None:
                raise ArchiveError(
                    f"cannot determine whether segment {candidate!r} is "
                    f"already committed: {why}")
            if present:
                continue
            if candidate in self._writers or candidate in self._retiring:
                continue
            yield candidate

    def _next_segment_id(self, base: str) -> str:
        """The first id in this partition that is not already committed evidence.

        Resolved against DISK, so it survives a process restart. A committed
        segment is immutable and may never be reopened, and an in-memory
        rotation counter always restarted at `base`, which rotation guarantees
        is committed.
        """
        env_dir = self.root / f"env={self.environment}"
        self._check_partition_writable(env_dir, base)
        for n in range(0, 10_000):
            candidate = base if n == 0 else f"{base}.r{n:04d}"
            directory = env_dir / f"segment={candidate}"
            present, why = evidence_fs.presence(directory / MANIFEST_FILENAME)
            if why is not None:
                raise ArchiveError(
                    f"cannot determine whether segment {candidate!r} is "
                    f"already committed: {why}")
            if present:
                continue
            if candidate in self._writers or candidate in self._retiring:
                continue
            return candidate
        raise ArchiveError(
            f"partition {base!r} has exhausted 10000 segment ids in one run")

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
            # `raw` is dropped from the normalized copy: `to_dict()` carries it,
            # so every record stored the venue payload TWICE — 41% of the two
            # payload fields was a byte-identical duplicate that no reader ever
            # touched. RAW-PAYLOAD-STORAGE-001 already settled this question in
            # this repository; shipping a new evidence store that reintroduces
            # it was not a decision left open.
            "normalized_event": {k: v for k, v in raw.items() if k != "raw"},
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
        # Every writer gets its own attempt. This loop used to be unguarded, so
        # one transient error on one hour discarded the commit of every other
        # hour: fully written, healthy evidence in three other partitions was
        # left OPEN with its lock still held and no manifest, and a second
        # close() re-raised the FIRST writer's error forever. One failure must
        # not decide the fate of unrelated evidence.
        # Set `_closed` UNDER the lock and BEFORE snapshotting. It used to be
        # set after the loop, and `_writer_for` read it outside the lock, so an
        # append racing close() created a brand-new writer that close() never
        # saw: the producer was told the event was written, close() reported
        # success, the archive verified VALID, and the event was gone.
        with self._writers_lock:
            self._closed = True
        # A8: `_closed` is set FIRST, then in-flight rotations are drained,
        # and only THEN is `pending` snapshotted. Order matters in both
        # directions. Setting `_closed` first means `_writer_for` refuses
        # from here on, so nothing new can be created or handed to the
        # closer; draining before the snapshot means a segment the closer is
        # already committing is finished by the closer (and popped from
        # `_retiring`) rather than being closed a second time, concurrently,
        # from here. If the drain times out, whatever is still in `_retiring`
        # is closed inline below -- `SegmentWriter.close()` is idempotent
        # under its own `_close_lock`, so the worst case is a redundant
        # `read_manifest()`, never a double publish.
        if not self._closer.shutdown(self.rotation_close_timeout_s):
            self.rotation_failures.append(
                ("<closer>", f"rotation closer did not drain within "
                             f"{self.rotation_close_timeout_s}s; closing the "
                             f"remainder inline"))
        with self._writers_lock:
            pending = list(self._writers.items()) + list(self._retiring.items())
        manifests, failures = {}, {}
        for seg_id, writer in pending:
            try:
                manifests[seg_id] = writer.close()
            except Exception as exc:            # noqa: BLE001 - aggregated below
                failures[seg_id] = exc
        self.close_failures = failures
        if failures:
            raise ArchiveError(
                f"{len(failures)} of {len(failures) + len(manifests)} segment(s) "
                f"failed to close: "
                f"{ {k: repr(v) for k, v in failures.items()} }; the remaining "
                f"{len(manifests)} were committed")
        return manifests

    # -- evidence location (for operators and corruption tests) ----------------
    def segment_paths(self) -> list:
        """Where this environment's evidence physically lives.

        Public so that operators and filesystem-corruption tests can find the
        canonical files without hard-coding a layout. The `env=/venue=/date=/
        hour=` tree is retired; callers that need a path should ask.
        """
        return [{"segment_id": d.name.split("segment=", 1)[-1],
                 "directory": d,
                 "events_path": d / EVENTS_FILENAME,
                 "manifest_path": d / MANIFEST_FILENAME}
                for d in self._segment_dirs()]

    # -- read ------------------------------------------------------------------
    def _segment_dirs(self) -> list:
        """Every segment directory found on disk. Diagnostic order ONLY --
        see `_committed_segment_dirs` for the authenticated one.

        `evidence_fs.presence`/`safe_enumerate`, not `Path.exists()`/
        `Path.glob()`: `.exists()` propagates `EACCES` and `.glob()`
        swallows it, so this diagnostic listing failed two different ways
        depending on which guard a caller remembered. Total here means the
        same thing it does everywhere else in this module: an unreadable
        environment directory reports no segments rather than raising or
        silently pretending the archive is empty with no signal either way.
        """
        env_root = self.root / f"env={self.environment}"
        present, why = evidence_fs.presence(env_root)
        if why is not None or not present:
            return []
        children, err = evidence_fs.safe_enumerate(env_root, "segment=*")
        if err is not None:
            return []
        return sorted(p for p in children if p.is_dir())

    def read_verified(self) -> list:
        """Canonical evidence. Verifies the archive first and fails closed.

        `read_all` used to serve a cleanly re-gzipped truncation with a clean
        bill of health, and it is the API that feeds replay. Verification that
        runs only when a caller remembers to ask is not protecting the path
        that actually reads the evidence.
        """
        report = verify_archive(self.root, environment=self.environment,
                                expected_archive_id=self.expected_archive_id)
        if report["verdict"] != "VALID":
            raise ArchiveError(
                "refusing to return canonical evidence from an archive that "
                f"does not verify: {report.get('reasons')}")
        return self._read_records()

    def read_unverified_diagnostic(self) -> list:
        """Salvage read for diagnosis ONLY.

        Never canonical, never replayable, never complete. It exists so an
        operator can look at a broken archive, and it is named so that no
        caller can reach for it by accident while meaning the other thing.
        """
        return self._read_records(strict=False)

    def _committed_segment_dirs(self, *, strict: bool = True) -> list:
        """Segment directories in the order the archive head COMMITTED them.

        `strict=True` (the canonical path) walks the generation chain and
        REFUSES if the history cannot be resolved. A silent downgrade to
        directory order is the unsafe default in a tamper-evidence store, and
        it is what previously served manifest-less forged segments as evidence.

        `strict=False` is for `read_unverified_diagnostic` ONLY. Removing the
        fallback outright broke the salvage reader for exactly the archives it
        exists to inspect — a deleted pointer or generation record made it
        raise. Salvage falls back to directory order and sets
        `diagnostic_order_unauthenticated`, so a caller can never mistake it
        for committed order.
        """
        env_root = self.root / f"env={self.environment}"
        try:
            head = load_authoritative_head(
                self.root, self.environment,
                expected_archive_id=self.expected_archive_id)
            ordered, missing = [], []
            for gen in range(1, head.generation_record["generation"] + 1):
                rec = read_generation(self.root, self.environment, gen)
                seg_id = rec.get("committed_segment_id")
                d = env_root / f"segment={seg_id}"
                if d.is_dir() and not d.is_symlink():
                    ordered.append(d)
                else:
                    # Counted, never silently skipped: "nothing was lost" must
                    # not be asserted by omission.
                    missing.append(seg_id)
            self.missing_committed_segments = missing
            self.diagnostic_order_unauthenticated = False
            return ordered
        except Exception as exc:              # noqa: BLE001 - see strict rules
            if strict:
                raise ArchiveError(
                    f"refusing to read: the committed history could not be "
                    f"resolved ({type(exc).__name__}: {exc})") from exc
            self.diagnostic_order_unauthenticated = True
            self.missing_committed_segments = []
            return self._segment_dirs()

    def read_all(self) -> list:
        """Deprecated alias for `read_verified`. Fails closed like it does."""
        return self.read_verified()

    def _read_records(self, *, strict: bool = True) -> list:
        """Every readable record, in chain order, across this environment.

        Records whose chain or environment does not verify are dropped and
        counted rather than returned — a reader that hands back unverified
        evidence is the thing this milestone exists to remove.
        """
        out, truncated, foreign, tampered = [], 0, 0, []
        # Read in COMMITTED order, not in directory-name order. Verification
        # walked `head["segments"]` while reading walked `sorted(glob(...))`,
        # so a segment committed out of lexical order — reachable whenever the
        # first event for a later partition arrives first — verified in one
        # order and was replayed in another. Two traversal rules merely
        # intended to agree is the pattern this milestone exists to remove.
        for directory in self._committed_segment_dirs(strict=strict):
            events_path = directory / EVENTS_FILENAME
            records = read_segment_records(events_path)
            seg_id = directory.name.split("segment=", 1)[-1]
            # Records the reader could not decode at all (a torn or malformed
            # tail) versus records it decoded but cannot trust (a chain break).
            # They are different failures and are counted separately.
            truncated += getattr(read_segment_records, "last_unreadable", 0)
            truncated += _undecodable_tail_records(events_path, len(records))
            verdict = verify_chain(records, segment_id=seg_id,
                                   environment=self.environment)
            if not verdict.ok:
                tampered.append(verdict.broken_at)
                records = records[:verdict.broken_at or 0]
            for rec in records:
                if rec.get("environment") != self.environment:
                    foreign += 1
                    continue
                # Reassemble the envelope from the ONE stored copy of the venue
                # payload. `normalized_event` no longer carries `raw` — storing
                # it in both fields duplicated the highest-volume artifact in
                # every record for no reader — so the authoritative `raw_event`
                # is grafted back on here, at read time, where it costs nothing.
                envelope = rec.get("normalized_event")
                if isinstance(envelope, dict):
                    out.append({**envelope, "raw": rec.get("raw_event")})
                else:
                    out.append(envelope or rec)
        self.truncated_records = truncated
        self.tampered_records = tampered
        self.foreign_environment_records = foreign
        return out

    def verify(self, *, expected_head: tuple | None = None,
               minimum_generation: int | None = None) -> dict:
        """Delegate to the one canonical verifier. Fail-closed.

        The legacy shape is preserved for existing callers, with the
        authoritative segment verdicts alongside it — and now with everything
        the module knows. This wrapper rebuilt its dict field by field, so when
        crash residue was demoted from a gating `reason` to a non-gating
        `warning`, the operator-facing path lost the ONLY signal that it
        existed: `intact: True` with 20,717 bytes of recoverable evidence on
        disk and nothing naming it. Demoting was right; deleting the signal was
        not.

        It also took no parameters, so `expected_head` — the one control the
        accepted threat model depends on — was unreachable from the object a
        collector actually holds.
        """
        report = verify_archive(
            self.root, environment=self.environment,
            expected_archive_id=self.expected_archive_id,
            expected_head=expected_head,
            minimum_generation=minimum_generation)
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
            # Forwarded rather than re-derived, so nothing the verifier knows
            # can be lost on the way to the caller again.
            "head_state": report.get("head_state"),
            "head_generation": report.get("head_generation"),
            "reasons": report.get("reasons", []),
            "warnings": report.get("warnings", []),
            "abandoned_segments": report.get("abandoned_segments", []),
            "uncommitted_segments": report.get("uncommitted_segments", []),
            "orphaned_committed_segments": report.get(
                "orphaned_committed_segments", []),
            # From the REPORT. Reading `self` meant `[]` denoted "nobody
            # called a read method yet", not "nothing is missing" — the field
            # added against loss-by-omission asserting exactly that, and
            # reporting a phantom on a healthy archive after a prior read.
            "missing_committed_segments": report.get(
                "missing_committed_segments", []),
            "records_expected": report.get("records_expected"),
            "archive_id": report.get("archive_id"),
            "generations_present": report.get("generations_present", []),
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
