"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 Gates 2-7 — chained records, manifests,
segment lifecycle, single-writer ownership, crash consistency.

These are one design, not five, because they all have to agree on what a
*committed record* is. The archive's failures came from disagreement: the
digest was taken over one representation and the bytes written were another
(Gate 1); nothing pinned an expected record count, so deleting a whole record —
or the whole file — verified as intact; and every producer held its own file
descriptor, so concurrent appends interleaved gzip members and destroyed the
file.

The shape:

    N producers ─→ bounded queue ─→ ONE ArchiveWriter ─→ ONE open segment
                                                             │
                                          records chained by previous_digest
                                                             │
                                      CLOSING → reconcile → manifest published
                                                             │
                                                          CLOSED

**The manifest is the commit record.** A segment is canonical evidence only
once its manifest exists, and the manifest is published last, by atomic rename.
A crash at any earlier point leaves a segment that is recoverable or
uncommitted — never falsely `CLOSED`. That ordering is the whole crash-safety
argument, so it is enforced in one place rather than trusted to callers.

Terminology is deliberate: this is **tamper-evident**, not tamper-proof.
Anyone who can write the event file can also rewrite the chain and the
manifest. What the chain buys is that a *partial* edit — the realistic case:
deletion, insertion, reorder, a flipped byte, a copied manifest — cannot be
made consistent without recomputing everything downstream of it.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from app.realtime.canonical import (
    CANONICAL_SCHEMA_VERSION,
    CanonicalError,
    canonical_bytes,
    canonical_datetime,
    digest_hex,
    parse_canonical,
    parse_canonical_datetime,
)

RECORD_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
ARCHIVE_SCHEMA_VERSION = 1
WRITER_VERSION = "kalshi-archive-writer/1"

EVENTS_FILENAME = "events.jsonl.gz"
MANIFEST_FILENAME = "manifest.json"
MANIFEST_TEMP_SUFFIX = ".tmp"

_SEGMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ENVIRONMENTS = ("demo", "production")

# Digest-bearing record fields. `record_digest` is excluded because it is the
# output; everything else is bound. Listing them explicitly rather than
# digesting "whatever is in the dict" means a field added later cannot silently
# fall outside the digest.
RECORD_FIELDS = (
    "schema_version",
    "canonical_schema_version",
    "environment",
    "segment_id",
    "connection_generation",
    "subscription_id",
    "subscription_generation",
    "receive_ordinal",
    "message_type",
    "market_ticker",
    "seq",
    "received_at_utc",
    "received_monotonic_ns",
    "raw_event",
    "normalized_event",
    "previous_record_digest",
)

REQUIRED_RECORD_FIELDS = RECORD_FIELDS + ("record_digest",)


def write_all(fd: int, payload: bytes) -> int:
    """Write every byte or raise. The one primitive for commitment bytes.

    `os.write` may legally write fewer bytes than asked — partial-ENOSPC, NFS
    and EINTR all do it. Discarding the return value is how a TRUNCATED manifest
    got fsynced, renamed and committed while `close()` reported CLOSED over
    unreadable evidence. Zero progress is terminal rather than a reason to spin:
    a writer that reports no progress will not make progress by being asked
    again.
    """
    total = len(payload)
    written = 0
    while written < total:
        try:
            n = os.write(fd, payload[written:])
        except InterruptedError:
            continue                      # EINTR: retry, it is not a failure
        if n <= 0:
            raise OSError(
                f"write reported no progress after {written} of {total} bytes")
        written += n
    return written


class SegmentError(RuntimeError):
    """A segment operation that would compromise the evidence."""


class RecordSchemaError(SegmentError):
    """A record that cannot be trusted to mean what it says."""


class SegmentState(str, Enum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    INVALID = "INVALID"


class RejectReason(str, Enum):
    """Why an event was not written. Every one is counted; none is silent."""

    QUEUE_FULL = "queue_full"
    ENQUEUE_TIMEOUT = "enqueue_timeout"
    SERIALIZATION_FAILURE = "serialization_failure"
    WRITER_FAILED = "writer_failed"
    SEGMENT_NOT_OPEN = "segment_not_open"
    SHUTDOWN_IN_PROGRESS = "shutdown_in_progress"
    SEGMENT_INVALID = "segment_invalid"


# --- Gate 2: the record envelope --------------------------------------------------


def genesis_digest(*, segment_id: str, environment: str) -> str:
    """The chain's anchor, derived from the segment's identity.

    A constant sentinel would let record #1 of one segment be spliced into
    another segment's head and still chain. Deriving it binds the first record
    to the segment it was written in.
    """
    return "genesis:" + digest_hex({
        "schema_version": RECORD_SCHEMA_VERSION,
        "segment_id": segment_id,
        "environment": environment,
    })


def build_record(*, envelope_fields: dict, segment_id: str, environment: str,
                 previous_record_digest: str, receive_ordinal: int) -> dict:
    """Assemble one canonical, chained record.

    Only declared fields enter the digest. Nothing derived from runtime state —
    file path, pid, buffer position — is included: those differ between the
    write and any later verification, so binding them would make a faithful
    archive fail its own check.
    """
    record = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "environment": environment,
        "segment_id": segment_id,
        "connection_generation": envelope_fields.get("connection_generation"),
        "subscription_id": envelope_fields.get("subscription_id"),
        "subscription_generation": envelope_fields.get("subscription_generation"),
        "receive_ordinal": receive_ordinal,
        "message_type": envelope_fields.get("message_type"),
        "market_ticker": envelope_fields.get("market_ticker"),
        "seq": envelope_fields.get("seq"),
        "received_at_utc": envelope_fields.get("received_at_utc"),
        "received_monotonic_ns": envelope_fields.get("received_monotonic_ns"),
        "raw_event": envelope_fields.get("raw_event"),
        "normalized_event": envelope_fields.get("normalized_event"),
        "previous_record_digest": previous_record_digest,
    }
    record["record_digest"] = digest_hex(
        {k: record[k] for k in RECORD_FIELDS})
    return record


def verify_record_self_digest(record: dict) -> bool:
    recorded = record.get("record_digest")
    if not isinstance(recorded, str) or len(recorded) != 64:
        return False
    return recorded == digest_hex({k: record.get(k) for k in RECORD_FIELDS})


def parse_record(raw: dict) -> dict:
    """Fail closed on anything that would let a record mean something else."""
    if not isinstance(raw, dict):
        raise RecordSchemaError(f"record is {type(raw).__name__}, not an object")
    version = raw.get("schema_version")
    if version != RECORD_SCHEMA_VERSION:
        # An unknown FUTURE version is refused, not tolerated. A newer writer
        # may bind fields this reader cannot see, so "it parsed" would not mean
        # "it verified".
        raise RecordSchemaError(
            f"record schema_version {version!r} is not {RECORD_SCHEMA_VERSION}; "
            "a version this reader does not implement may bind fields it "
            "cannot check, so it is refused rather than partially trusted")
    if raw.get("canonical_schema_version") != CANONICAL_SCHEMA_VERSION:
        raise RecordSchemaError(
            f"canonical_schema_version {raw.get('canonical_schema_version')!r} "
            f"is not {CANONICAL_SCHEMA_VERSION}; digests are only comparable "
            "within one encoding version")
    missing = [f for f in REQUIRED_RECORD_FIELDS if f not in raw]
    if missing:
        raise RecordSchemaError(f"record is missing {missing}")
    # The envelope is CLOSED. record_digest covers a declared field list, so an
    # unknown top-level key rides entirely outside the digest — data inside a
    # record that "passes its own digest" but was never committed to. Venue
    # extensibility belongs inside raw_event/normalized_event, which stay
    # opaque and ARE digest-bound as whole values.
    unknown = sorted(set(raw) - set(REQUIRED_RECORD_FIELDS))
    if unknown:
        raise RecordSchemaError(
            f"record carries undeclared top-level field(s) {unknown}; the v1 "
            "envelope is closed, and an undeclared key is not covered by "
            "record_digest. Extending the envelope requires a new "
            "schema_version, not permissive parsing")
    if raw.get("environment") not in _ENVIRONMENTS:
        raise RecordSchemaError(f"unknown environment {raw.get('environment')!r}")
    if not isinstance(raw.get("receive_ordinal"), int):
        raise RecordSchemaError("receive_ordinal must be an integer")
    for name in ("record_digest", "previous_record_digest"):
        value = raw.get(name)
        if not isinstance(value, str):
            raise RecordSchemaError(f"{name} must be a string")
    try:
        parse_canonical_datetime(raw["received_at_utc"])
    except CanonicalError as exc:
        raise RecordSchemaError(f"received_at_utc: {exc}") from None
    if not verify_record_self_digest(raw):
        raise RecordSchemaError(
            f"record {raw.get('receive_ordinal')} fails its own digest")
    return raw


# --- Gate 3: the ordered chain ----------------------------------------------------


def fold_stream_digest(previous: str, record_digest: str) -> str:
    """Running digest over the ORDER of records, not just their contents.

    Folding position in is what makes a reorder detectable: two records that
    swap places produce the same set of self-digests but a different fold.
    """
    return hashlib.sha256(
        (previous + ":" + record_digest).encode("utf-8")).hexdigest()


@dataclass
class ChainVerdict:
    ok: bool
    record_count: int = 0
    first_record_digest: str | None = None
    last_record_digest: str | None = None
    ordered_stream_digest: str | None = None
    broken_at: int | None = None
    reason: str | None = None


def verify_chain(records, *, segment_id: str, environment: str) -> ChainVerdict:
    """Walk the chain. Self-digests alone are not enough — order is bound too."""
    expected_prev = genesis_digest(segment_id=segment_id, environment=environment)
    stream = expected_prev
    first = last = None
    for index, raw in enumerate(records):
        try:
            record = parse_record(raw)
        except RecordSchemaError as exc:
            return ChainVerdict(False, index, first, last, stream, index, str(exc))
        if record["segment_id"] != segment_id:
            return ChainVerdict(False, index, first, last, stream, index,
                                f"record belongs to segment "
                                f"{record['segment_id']!r}, not {segment_id!r}")
        if record["environment"] != environment:
            return ChainVerdict(False, index, first, last, stream, index,
                                f"record environment {record['environment']!r} "
                                f"does not match {environment!r}")
        if record["previous_record_digest"] != expected_prev:
            return ChainVerdict(
                False, index, first, last, stream, index,
                "chain break: previous_record_digest does not match the "
                "preceding record (deletion, insertion, reorder or a copied "
                "record all present this way)")
        if record["receive_ordinal"] != index:
            return ChainVerdict(False, index, first, last, stream, index,
                                f"receive_ordinal {record['receive_ordinal']} "
                                f"is not the position {index}")
        digest = record["record_digest"]
        first = first or digest
        last = digest
        stream = fold_stream_digest(stream, digest)
        expected_prev = digest
    return ChainVerdict(True, len(list(records)) if not hasattr(records, "__len__")
                        else len(records), first, last, stream)


# --- Gate 4: the manifest ---------------------------------------------------------


MANIFEST_FIELDS = (
    "manifest_schema_version",
    "archive_schema_version",
    "canonical_schema_version",
    "environment",
    "segment_id",
    "partition_identity",
    "writer_version",
    "opened_at",
    "closed_at",
    "record_count",
    "first_record_digest",
    "last_record_digest",
    "ordered_stream_digest",
    "event_file_size_bytes",
    "event_file_sha256",
    "subscription_metadata_digest",
    "previous_segment_digest",
    "close_status",
)


def build_manifest(*, environment, segment_id, partition_identity, opened_at,
                   closed_at, record_count, first_record_digest,
                   last_record_digest, ordered_stream_digest,
                   event_file_size_bytes, event_file_sha256,
                   subscription_metadata, previous_segment_digest=None,
                   close_status="clean") -> dict:
    body = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "environment": environment,
        "segment_id": segment_id,
        "partition_identity": partition_identity,
        "writer_version": WRITER_VERSION,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "record_count": record_count,
        "first_record_digest": first_record_digest,
        "last_record_digest": last_record_digest,
        "ordered_stream_digest": ordered_stream_digest,
        "event_file_size_bytes": event_file_size_bytes,
        "event_file_sha256": event_file_sha256,
        "subscription_metadata_digest": digest_hex(subscription_metadata or {}),
        "previous_segment_digest": previous_segment_digest,
        "close_status": close_status,
    }
    body["subscription_metadata"] = subscription_metadata or {}
    body["manifest_digest"] = digest_hex({k: body[k] for k in MANIFEST_FIELDS})
    return body


def verify_manifest_self_digest(manifest: dict) -> bool:
    recorded = manifest.get("manifest_digest")
    if not isinstance(recorded, str):
        return False
    try:
        return recorded == digest_hex({k: manifest.get(k) for k in MANIFEST_FIELDS})
    except CanonicalError:
        return False


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_segment_id(segment_id: str) -> str:
    """A segment id becomes a directory name; it must not be able to escape one."""
    if not isinstance(segment_id, str) or not _SEGMENT_ID_RE.match(segment_id):
        raise SegmentError(
            f"segment_id {segment_id!r} is not a safe path component "
            f"({_SEGMENT_ID_RE.pattern})")
    return segment_id


# --- Gate 5/6/7: the writer -------------------------------------------------------


@dataclass
class WriterAccounting:
    """Every submitted event lands in exactly one of these. They must reconcile."""

    generated: int = 0
    written: int = 0
    rejected: int = 0
    rejections: dict = field(default_factory=dict)

    def reject(self, reason: RejectReason) -> None:
        self.rejected += 1
        self.rejections[reason.value] = self.rejections.get(reason.value, 0) + 1

    def reconciles(self) -> bool:
        return self.generated == self.written + self.rejected

    def to_dict(self) -> dict:
        return {"generated": self.generated, "written": self.written,
                "rejected": self.rejected, "rejections": dict(self.rejections),
                "reconciles": self.reconciles()}


class SegmentWriter:
    """The single owner of one segment's file descriptor.

    Producers never touch the file. They hand events to a bounded queue and one
    writer thread drains it, so there is exactly one appender and interleaved
    gzip members are structurally impossible rather than merely unobserved.

    Durability is explicit: records are flushed on a cadence, `fsync` happens at
    close, and the manifest is written to a temp file, fsynced, atomically
    renamed, and the directory fsynced after. `close()` is not the durability
    contract — rename-after-fsync is.
    """

    def __init__(self, root, *, environment: str, segment_id: str,
                 partition_identity: str, subscription_metadata: dict | None = None,
                 queue_maxsize: int = 10_000, enqueue_timeout_s: float = 1.0,
                 flush_every: int = 256, previous_segment_digest: str | None = None,
                 archive_identity: str = "kalshi-realtime",
                 commit_to_head: bool = True):
        if environment not in _ENVIRONMENTS:
            raise SegmentError(f"unknown environment {environment!r}")
        self.environment = environment
        self.segment_id = safe_segment_id(segment_id)
        self.partition_identity = partition_identity
        self.subscription_metadata = subscription_metadata or {}
        self.archive_identity = archive_identity
        self.commit_to_head = commit_to_head
        # The predecessor is taken from the AUTHORITATIVE head, not from the
        # caller: a segment that names its own predecessor is asserting its
        # place in history rather than being told it.
        if previous_segment_digest is None and commit_to_head:
            _head = read_head(Path(root).resolve(), environment)
            previous_segment_digest = (
                _head.get("terminal_segment_digest") if _head else None)
        self.previous_segment_digest = previous_segment_digest
        self.root = Path(root).resolve()
        self.dir = (self.root / f"env={environment}" / f"segment={self.segment_id}")
        self.events_path = self.dir / EVENTS_FILENAME
        self.manifest_path = self.dir / MANIFEST_FILENAME

        self.state = SegmentState.OPEN
        self.accounting = WriterAccounting()
        self.opened_at = canonical_datetime(datetime.now(timezone.utc))
        self.closed_at: str | None = None

        self._queue: queue.Queue = queue.Queue(maxsize=queue_maxsize)
        self._enqueue_timeout = enqueue_timeout_s
        self._flush_every = flush_every
        self._lock = threading.Lock()
        self._prev_digest = genesis_digest(segment_id=self.segment_id,
                                           environment=environment)
        self._ordinal = 0
        self._stream_digest = self._prev_digest
        self._first_digest: str | None = None
        self._last_digest: str | None = None
        self._writer_error: BaseException | None = None
        self._shutdown = threading.Event()
        # close() is reachable from several threads (a shutdown handler and an
        # application path, say). Without this the second caller finalises an
        # already-finalised file and the segment is destroyed by its own
        # shutdown.
        self._close_lock = threading.Lock()
        self.queue_high_water = 0
        # Injection seams. Production leaves both None; tests use them to slow
        # the writer or fail a specific durability stage without weakening the
        # real fsync path.
        self.pre_write_hook = None
        self.durability_hooks: dict = {}

        self.dir.mkdir(parents=True, exist_ok=True)
        # A CLOSED segment is immutable evidence. Reopening one for append
        # would add records its published manifest does not describe, turning a
        # valid segment into an invalid one by ordinary use.
        if self.manifest_path.exists():
            raise SegmentError(
                f"segment {self.segment_id!r} is already committed (its "
                "manifest exists); a closed segment is immutable evidence and "
                "cannot be reopened for writing")
        # EXCLUSIVE OWNERSHIP. One segment, one writer, enforced by the
        # filesystem rather than by convention. Six processes each opening
        # their own descriptor on one segment is precisely how the original
        # archive destroyed 719 of 720 records — and a second owner must fail
        # LOUDLY here rather than interleave gzip members and be discovered
        # later by a reader that can no longer tell what was lost.
        self._lock_path = self.dir / "writer.lock"
        try:
            self._lock_fd = os.open(
                self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(self._lock_fd, f"{os.getpid()}\n".encode())
            os.fsync(self._lock_fd)
        except FileExistsError:
            raise SegmentError(
                f"segment {self.segment_id!r} already has a writer "
                f"({self._lock_path} exists). A segment has exactly one owner; "
                "concurrent appenders interleave gzip members and destroy the "
                "file. Producers share one writer through its queue.") from None
        # O_NOFOLLOW for the same reason as the manifest temp: a symlinked
        # events file would append gzip members into an arbitrary victim file.
        if os.path.islink(self.events_path):
            self._release_lock()
            raise SegmentError(
                f"{self.events_path} is a symlink; refusing to write evidence "
                "through it")
        _ev_fd = os.open(self.events_path,
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600)
        self._fh = gzip.open(os.fdopen(_ev_fd, "ab"), "ab")
        self._since_flush = 0
        self._thread = threading.Thread(target=self._run, name="archive-writer",
                                        daemon=True)
        self._thread.start()

    # -- health ----------------------------------------------------------------
    @property
    def healthy(self) -> bool:
        return self.state is SegmentState.OPEN and self._writer_error is None

    @property
    def accepting(self) -> bool:
        """Evidence that cannot be durably recorded must not be accepted."""
        return self.healthy and not self._shutdown.is_set()

    # -- producer side ---------------------------------------------------------
    def submit(self, envelope_fields: dict) -> RejectReason | None:
        """Called by producers. Never touches the file.

        Returns `None` on acceptance or a typed reason on rejection. There is
        no path that drops an event without returning a reason.
        """
        with self._lock:
            self.accounting.generated += 1
        if self._writer_error is not None:
            with self._lock:
                self.accounting.reject(RejectReason.WRITER_FAILED)
            return RejectReason.WRITER_FAILED
        if self._shutdown.is_set():
            with self._lock:
                self.accounting.reject(RejectReason.SHUTDOWN_IN_PROGRESS)
            return RejectReason.SHUTDOWN_IN_PROGRESS
        if self.state is SegmentState.INVALID:
            with self._lock:
                self.accounting.reject(RejectReason.SEGMENT_INVALID)
            return RejectReason.SEGMENT_INVALID
        if self.state is not SegmentState.OPEN:
            with self._lock:
                self.accounting.reject(RejectReason.SEGMENT_NOT_OPEN)
            return RejectReason.SEGMENT_NOT_OPEN
        try:
            self._queue.put(envelope_fields, timeout=self._enqueue_timeout)
            depth = self._queue.qsize()
            if depth > self.queue_high_water:
                self.queue_high_water = depth
        except queue.Full:
            with self._lock:
                self.accounting.reject(RejectReason.ENQUEUE_TIMEOUT)
            return RejectReason.ENQUEUE_TIMEOUT
        return None

    # -- writer side -----------------------------------------------------------
    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._shutdown.is_set() and self._queue.empty():
                    return
                continue
            try:
                self._write_one(item)
            except BaseException as exc:            # noqa: BLE001 - recorded
                self._writer_error = exc
                self.state = SegmentState.INVALID
                with self._lock:
                    self.accounting.reject(RejectReason.WRITER_FAILED)
                # Stop. Continuing appends more records after a half-written
                # one and amplifies the corruption.
                return
            finally:
                self._queue.task_done()

    def _write_one(self, envelope_fields: dict) -> None:
        if self.pre_write_hook is not None:
            self.pre_write_hook(self)
        try:
            record = build_record(
                envelope_fields=envelope_fields, segment_id=self.segment_id,
                environment=self.environment,
                previous_record_digest=self._prev_digest,
                receive_ordinal=self._ordinal)
            line = canonical_bytes(record)
        except CanonicalError:
            with self._lock:
                self.accounting.reject(RejectReason.SERIALIZATION_FAILURE)
            return
        self._fh.write(line + b"\n")
        self._since_flush += 1
        if self._since_flush >= self._flush_every:
            self._fh.flush()
            self._since_flush = 0
        digest = record["record_digest"]
        self._first_digest = self._first_digest or digest
        self._last_digest = digest
        self._stream_digest = fold_stream_digest(self._stream_digest, digest)
        self._prev_digest = digest
        self._ordinal += 1
        with self._lock:
            self.accounting.written += 1

    # -- lifecycle -------------------------------------------------------------
    def close(self) -> dict:
        """CLOSING → reconcile → durability → manifest publish → CLOSED.

        The manifest is written LAST and published by atomic rename, so a crash
        anywhere before that leaves a segment with no manifest — recoverable and
        uncommitted, never falsely CLOSED.
        """
        with self._close_lock:
            if self.state is SegmentState.CLOSED:
                return self.read_manifest()
            if self.state is SegmentState.INVALID:
                raise SegmentError(
                    f"segment is INVALID and cannot be closed: "
                    f"{self._writer_error!r}")
            return self._close_locked()

    def _close_locked(self) -> dict:
        try:
            return self._close_stages()
        except BaseException:
            # Ownership must not leak on ANY failure path, or one mid-stream
            # write error locks the partition out for every future process.
            self._release_lock()
            raise

    def _close_stages(self) -> dict:
        self.state = SegmentState.CLOSING
        self._shutdown.set()
        self._thread.join(timeout=30)
        if self._thread.is_alive():
            self.state = SegmentState.INVALID
            raise SegmentError("writer did not drain within the shutdown timeout")
        if self._writer_error is not None:
            self.state = SegmentState.INVALID
            raise SegmentError(f"writer failed: {self._writer_error!r}")

        # Event-file durability first. Each stage is separately injectable so a
        # failure can be attributed to the exact stage rather than collapsed
        # into one generic error.
        try:
            self._stage("flush")
            self._fh.flush()
            self._stage("fsync")
            self._fh.close()
            fd = os.open(self.events_path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException as exc:
            self.state = SegmentState.INVALID
            self._writer_error = exc
            self._release_lock()
            raise SegmentError(f"event-file durability failed: {exc!r}") from exc

        # Reconcile what we believe we wrote against what is on disk.
        size = self.events_path.stat().st_size
        file_hash = file_sha256(self.events_path)
        on_disk = read_segment_records(self.events_path)
        if len(on_disk) != self.accounting.written:
            self.state = SegmentState.INVALID
            raise SegmentError(
                f"reconciliation failed: wrote {self.accounting.written} "
                f"records, {len(on_disk)} readable on disk")
        verdict = verify_chain(on_disk, segment_id=self.segment_id,
                               environment=self.environment)
        if not verdict.ok:
            self.state = SegmentState.INVALID
            raise SegmentError(f"chain reconciliation failed: {verdict.reason}")

        self.closed_at = canonical_datetime(datetime.now(timezone.utc))
        manifest = build_manifest(
            environment=self.environment, segment_id=self.segment_id,
            partition_identity=self.partition_identity,
            opened_at=self.opened_at, closed_at=self.closed_at,
            record_count=verdict.record_count,
            first_record_digest=verdict.first_record_digest,
            last_record_digest=verdict.last_record_digest,
            ordered_stream_digest=verdict.ordered_stream_digest,
            event_file_size_bytes=size, event_file_sha256=file_hash,
            subscription_metadata=self.subscription_metadata,
            previous_segment_digest=self.previous_segment_digest)
        try:
            publish_manifest(self.dir, manifest, stage=self._stage)
        except BaseException as exc:
            # A manifest that did not publish means the segment is NOT closed.
            # The distinction the docstring makes — rename succeeded but
            # directory fsync failed — is preserved by the stage name, because
            # the two have materially different recovery stories.
            self.state = SegmentState.INVALID
            self._writer_error = exc
            self._release_lock()
            raise SegmentError(f"manifest publication failed: {exc!r}") from exc
        # The segment is now independently verifiable. Only then does it enter
        # the archive's committed history — a segment that is not itself
        # evidence must never be recorded as part of the archive.
        if self.commit_to_head:
            independent = verify_segment(self.dir, environment=self.environment)
            if not independent.valid:
                self.state = SegmentState.INVALID
                self._release_lock()
                raise SegmentError(
                    "segment does not verify after publication; refusing to "
                    f"commit it to the archive head: {independent.reasons}")
            try:
                commit_segment_to_head(
                    self.root, self.environment,
                    archive_identity=self.archive_identity,
                    manifest=manifest, stage=self._stage)
            except BaseException as exc:
                # The segment IS committed; the history is not. That is an
                # ORPHANED_COMMITTED_SEGMENT, which verify_archive reports
                # explicitly rather than absorbing.
                self.state = SegmentState.INVALID
                self._release_lock()
                raise SegmentError(
                    f"archive head update failed after segment commit "
                    f"(ORPHANED_COMMITTED_SEGMENT): {exc!r}") from exc
        self.state = SegmentState.CLOSED
        self._release_lock()
        return manifest

    failed_stage: str | None = None

    def _stage(self, name: str) -> None:
        hook = self.durability_hooks.get(name)
        if hook is not None:
            self.failed_stage = name
            try:
                hook()
            except BaseException:
                raise
            else:
                self.failed_stage = None

    def _release_lock(self) -> None:
        fd = getattr(self, "_lock_fd", None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            self._lock_fd = None
        try:
            self._lock_path.unlink()
        except (OSError, AttributeError):
            pass

    def read_manifest(self) -> dict:
        return parse_canonical(self.manifest_path.read_bytes())


def publish_manifest(directory: Path, manifest: dict, *, stage=None) -> Path:
    """Temp write → fsync → atomic rename → directory fsync.

    Rename is the commit. Everything before it is invisible to a reader, so an
    interrupted publication cannot produce a half-written manifest that looks
    authoritative.
    """
    directory = Path(directory)
    tmp = directory / (MANIFEST_FILENAME + MANIFEST_TEMP_SUFFIX)
    final = directory / MANIFEST_FILENAME
    def _s(name):
        if stage is not None:
            stage(name)

    payload = canonical_bytes(manifest)
    _s("manifest_temp_create")
    # O_EXCL|O_NOFOLLOW: a pre-planted symlink at the temp path would otherwise
    # let O_TRUNC destroy an arbitrary writable file and then promote that
    # symlink to `manifest.json`, putting the commit record outside the archive
    # entirely. `auth.py` already opens credentials this way; this adopts it.
    try:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o600)
    try:
        _s("manifest_write")
        write_all(fd, payload)
        _s("manifest_fsync")
        os.fsync(fd)
    finally:
        os.close(fd)
    # Read the temp back and verify it before promoting it to the commit
    # record. The manifest is a few hundred bytes; not checking it is how the
    # whole temp->fsync->rename ceremony gets defeated one level up.
    check = parse_canonical(Path(tmp).read_bytes())
    if not verify_manifest_self_digest(check):
        raise SegmentError(
            "the staged manifest does not verify against its own digest; "
            "refusing to publish it as a commit record")
    _s("manifest_rename")
    if os.path.islink(final):
        raise SegmentError(
            f"{final} is a symlink; refusing to publish a commit record "
            "through it")
    os.replace(tmp, final)
    # After this point the manifest is VISIBLE. A directory-fsync failure here
    # means the rename may not survive a power loss, but a reader today sees a
    # committed segment — a materially different situation from failing before
    # the rename, and reported as such rather than as one generic error.
    _s("directory_fsync")
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return final


def read_segment_records(events_path: Path) -> list:
    """Read complete records, dropping only an incomplete terminal one.

    The writer holds ONE continuous gzip stream for the whole segment, so a
    torn tail truncates that stream rather than a per-record member. Feeding
    the bytes in incrementally and keeping whatever decompressed BEFORE the
    error is what preserves the complete prefix — decompressing in one call
    loses everything, because the exception discards the return value.
    """
    import zlib

    events_path = Path(events_path)
    if not events_path.exists():
        return []
    data = events_path.read_bytes()
    text = ""
    while data:
        dec = zlib.decompressobj(31)
        out = []
        try:
            for i in range(0, len(data), 65536):
                out.append(dec.decompress(data[i:i + 65536]))
            out.append(dec.flush())
        except (zlib.error, EOFError):
            pass                     # keep the prefix decoded before the fault
        try:
            text += b"".join(out).decode("utf-8")
        except UnicodeDecodeError:
            text += b"".join(out).decode("utf-8", errors="ignore")
        if not dec.eof:
            break                    # stream ended mid-member: nothing follows
        data = dec.unused_data
    records = []
    lines = [ln for ln in text.split("\n") if ln.strip()]
    for line in lines:
        try:
            records.append(parse_canonical(line))
        except Exception:
            break                    # an unparseable line ends the readable prefix
    # How many decodable lines the reader had to abandon. Reported rather than
    # silently dropped: a torn tail and a clean file must not look alike.
    read_segment_records.last_unreadable = len(lines) - len(records)
    return records


read_segment_records.last_unreadable = 0


# --- verification -----------------------------------------------------------------


@dataclass
class SegmentVerdict:
    """Fail-closed. `valid` is only True when every check agreed."""

    segment_id: str
    state: SegmentState
    valid: bool
    reasons: list = field(default_factory=list)
    records_expected: int | None = None
    records_read: int = 0
    chain_valid: bool = False
    first_digest_match: bool = False
    last_digest_match: bool = False
    stream_digest_match: bool = False
    file_digest_match: bool = False
    file_size_match: bool = False
    manifest_valid: bool = False
    environment_valid: bool = False
    subscription_metadata_match: bool = False

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["state"] = self.state.value
        return d


# --- PART 1/2: the archive head — an authoritative, committed inventory -----------

ARCHIVE_HEAD_FILENAME = "archive-head.json"
HEAD_SCHEMA_VERSION = 1

HEAD_FIELDS = (
    "schema_version", "canonical_schema_version", "environment",
    "archive_identity", "generation", "segment_count", "segments",
    "first_segment_digest", "terminal_segment_digest",
    "archive_segments_digest", "previous_head_digest", "updated_at",
)


class ArchiveHeadError(SegmentError):
    """The archive's committed history does not describe what is on disk."""


def segment_commitment(manifest: dict) -> str:
    """A segment's identity as the archive commits to it.

    The manifest's own digest: it already binds record_count, first/last record
    digests, the stream digest and the file hash, so committing to it commits
    to all of them transitively.
    """
    return manifest["manifest_digest"]


def fold_segments_digest(previous: str, commitment: str) -> str:
    """Position-bound fold, so reordering segments changes the result."""
    return hashlib.sha256(
        (previous + "|" + commitment).encode("utf-8")).hexdigest()


def _head_genesis(environment: str, archive_identity: str) -> str:
    return "head-genesis:" + digest_hex(
        {"environment": environment, "archive_identity": archive_identity})


def build_head(*, environment: str, archive_identity: str,
               previous_head: dict | None, segments: list) -> dict:
    """Build the next head from the PREVIOUS authoritative head plus a segment.

    Never from whatever segments happen to be on disk. Regenerating the head by
    discovery would certify exactly the deletion and grafting it exists to
    detect — the verifier would simply agree with whatever survived.
    """
    fold = _head_genesis(environment, archive_identity)
    for entry in segments:
        fold = fold_segments_digest(fold, entry["manifest_digest"])
    head = {
        "schema_version": HEAD_SCHEMA_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "environment": environment,
        "archive_identity": archive_identity,
        "generation": (previous_head["generation"] + 1) if previous_head else 1,
        "segment_count": len(segments),
        "segments": segments,
        "first_segment_digest": segments[0]["manifest_digest"] if segments else None,
        "terminal_segment_digest": segments[-1]["manifest_digest"] if segments else None,
        "archive_segments_digest": fold,
        "previous_head_digest": (previous_head or {}).get("head_digest"),
        "updated_at": canonical_datetime(datetime.now(timezone.utc)),
    }
    head["head_digest"] = digest_hex({k: head[k] for k in HEAD_FIELDS})
    return head


def verify_head_self_digest(head: dict) -> bool:
    recorded = head.get("head_digest")
    if not isinstance(recorded, str):
        return False
    try:
        return recorded == digest_hex({k: head.get(k) for k in HEAD_FIELDS})
    except (CanonicalError, KeyError):
        return False


def head_path(root, environment: str) -> Path:
    return Path(root) / f"env={environment}" / ARCHIVE_HEAD_FILENAME


def read_head(root, environment: str) -> dict | None:
    path = head_path(root, environment)
    if not path.exists():
        return None
    return parse_canonical(path.read_bytes())


def publish_head(root, environment: str, head: dict, *, stage=None) -> Path:
    """Stage -> write_all -> fsync -> VERIFY STAGED BYTES -> rename -> dir fsync.

    Reading the staged artifact back is not a substitute for correct write
    handling — `write_all` already guarantees completeness — it is a second,
    independent statement that what is about to become the commit record is the
    thing we meant to commit.
    """
    def _s(name):
        if stage is not None:
            stage(name)

    directory = head_path(root, environment).parent
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / ARCHIVE_HEAD_FILENAME
    tmp = directory / (ARCHIVE_HEAD_FILENAME + MANIFEST_TEMP_SUFFIX)
    payload = canonical_bytes(head)

    _s("head_temp_create")
    try:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o600)
    try:
        _s("head_write")
        write_all(fd, payload)
        _s("head_fsync")
        os.fsync(fd)
    finally:
        os.close(fd)

    _s("head_verify")
    staged = parse_canonical(Path(tmp).read_bytes())
    if not verify_head_self_digest(staged):
        raise ArchiveHeadError(
            "the staged archive head does not verify against its own digest; "
            "refusing to publish it as the history commit record")
    if staged.get("generation") != head["generation"]:
        raise ArchiveHeadError("staged head generation does not match")

    _s("head_rename")
    if os.path.islink(final):
        raise ArchiveHeadError(
            f"{final} is a symlink; refusing to publish the head through it")
    os.replace(tmp, final)
    _s("head_directory_fsync")
    dir_fd = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return final


def commit_segment_to_head(root, environment: str, *, archive_identity: str,
                           manifest: dict, stage=None) -> dict:
    """Append one committed segment to the archive's authoritative history."""
    previous = read_head(root, environment)
    if previous is not None and not verify_head_self_digest(previous):
        raise ArchiveHeadError(
            "the existing archive head fails its own digest; refusing to "
            "extend a history that is already untrustworthy")
    segments = list(previous["segments"]) if previous else []
    entry = {
        "segment_id": manifest["segment_id"],
        "manifest_digest": segment_commitment(manifest),
        "partition_identity": manifest["partition_identity"],
        "record_count": manifest["record_count"],
    }
    if any(e["segment_id"] == entry["segment_id"] for e in segments):
        raise ArchiveHeadError(
            f"segment {entry['segment_id']!r} is already in the archive head")
    segments.append(entry)
    head = build_head(environment=environment, archive_identity=archive_identity,
                      previous_head=previous, segments=segments)
    publish_head(root, environment, head, stage=stage)
    return head


def _partition_for_segment(segment_id: str, environment: str) -> str | None:
    """`kalshi.2026-08-08T12` -> `env=demo/venue=kalshi/date=2026-08-08/hour=12`.

    Mirrors exactly what `EventArchive.partition()` produces relative to the
    archive root — including the `env=` component, whose omission in an earlier
    revision made every production-shaped segment fail this check.

    The segment id is bound by the directory name AND by every record's genesis,
    so deriving the partition from it means a relabelled partition_identity
    contradicts the records rather than merely the manifest. Segment ids that do
    not carry a venue.dateThour shape return None and are simply not constrained
    this way — a test-shaped id is not evidence of tampering.
    """
    try:
        venue, stamp = segment_id.split(".", 1)
        date, hour = stamp.split("T", 1)
    except ValueError:
        return None
    if not (venue and date and hour) or "-" not in date:
        return None
    return f"env={environment}/venue={venue}/date={date}/hour={hour}"


def verify_segment(directory, *, environment: str,
                   allow_open: bool = False) -> SegmentVerdict:
    """Verify one segment against its manifest.

    A missing manifest is INVALID rather than "probably still open", and a
    missing event file with a manifest is INVALID too. Neither is ever
    repaired: regenerating a manifest from the surviving records would certify
    exactly the deletion it is supposed to detect.
    """
    directory = Path(directory)
    seg_id = directory.name.split("segment=", 1)[-1]
    events_path = directory / EVENTS_FILENAME
    manifest_path = directory / MANIFEST_FILENAME
    reasons: list = []

    has_events = events_path.exists()
    if not manifest_path.exists():
        if allow_open and has_events:
            return SegmentVerdict(
                seg_id, SegmentState.OPEN, False,
                ["segment has no manifest and is therefore not committed"],
                None, len(read_segment_records(events_path)))
        return SegmentVerdict(
            seg_id, SegmentState.INVALID, False,
            ["no manifest: a segment without its commit record is not evidence, "
             "and reconstructing one would certify the loss"],
            None, len(read_segment_records(events_path)) if has_events else 0)

    try:
        manifest = parse_canonical(manifest_path.read_bytes())
    except Exception as exc:
        return SegmentVerdict(seg_id, SegmentState.INVALID, False,
                              [f"manifest is unreadable ({type(exc).__name__})"])

    v = SegmentVerdict(seg_id, SegmentState.INVALID, False, reasons)
    v.manifest_valid = verify_manifest_self_digest(manifest)
    if not v.manifest_valid:
        reasons.append("manifest fails its own digest (edited or truncated)")
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        reasons.append("unsupported manifest schema version")
    v.environment_valid = manifest.get("environment") == environment
    if not v.environment_valid:
        reasons.append(f"manifest environment {manifest.get('environment')!r} "
                       f"!= {environment!r}")
    if manifest.get("segment_id") != seg_id:
        reasons.append(
            f"manifest segment_id {manifest.get('segment_id')!r} != {seg_id!r} "
            "(a manifest copied from another segment presents this way)")

    if not has_events:
        reasons.append("manifest exists but the event file is missing")
        v.records_expected = manifest.get("record_count")
        return v

    records = read_segment_records(events_path)
    v.records_read = len(records)
    v.records_expected = manifest.get("record_count")

    verdict = verify_chain(records, segment_id=manifest.get("segment_id", seg_id),
                           environment=manifest.get("environment", environment))
    v.chain_valid = verdict.ok
    if not verdict.ok:
        reasons.append(f"chain invalid at record {verdict.broken_at}: {verdict.reason}")
    if v.records_expected != v.records_read:
        reasons.append(
            f"record count {v.records_read} != manifest {v.records_expected} "
            "(a deleted complete record presents this way)")

    v.first_digest_match = verdict.first_record_digest == manifest.get("first_record_digest")
    v.last_digest_match = verdict.last_record_digest == manifest.get("last_record_digest")
    v.stream_digest_match = verdict.ordered_stream_digest == manifest.get("ordered_stream_digest")
    for ok, name in ((v.first_digest_match, "first_record_digest"),
                     (v.last_digest_match, "last_record_digest"),
                     (v.stream_digest_match, "ordered_stream_digest")):
        if not ok:
            reasons.append(f"{name} does not match the manifest")

    size = events_path.stat().st_size
    v.file_size_match = size == manifest.get("event_file_size_bytes")
    v.file_digest_match = file_sha256(events_path) == manifest.get("event_file_sha256")
    if not v.file_size_match:
        reasons.append(f"event file size {size} != manifest "
                       f"{manifest.get('event_file_size_bytes')}")
    if not v.file_digest_match:
        reasons.append("event file sha256 does not match the manifest")

    v.subscription_metadata_match = (
        digest_hex(manifest.get("subscription_metadata") or {})
        == manifest.get("subscription_metadata_digest"))
    if not v.subscription_metadata_match:
        reasons.append("subscription metadata does not match its digest")

    # --- PROVENANCE (B2) ---------------------------------------------------
    # "The manifest says X and its digest agrees with X" is self-consistency,
    # not verification: an attacker who edits a field and recomputes the digest
    # satisfies it trivially. Every field below is checked against a source the
    # manifest does not control.
    #
    # partition_identity: bound to the segment id, which is itself bound by the
    # directory name AND by every record's genesis. Relabelling the partition
    # therefore contradicts the records.
    expected_partition = _partition_for_segment(
        manifest.get("segment_id") or "", manifest.get("environment") or environment)
    declared_partition = manifest.get("partition_identity")
    if expected_partition and declared_partition != expected_partition:
        reasons.append(
            f"partition_identity {declared_partition!r} is not the partition "
            f"derived from segment_id ({expected_partition!r})")

    # opened_at / closed_at: bracket the actual record times. Operational
    # fields, so the constraint is an envelope, not equality — the writer opens
    # before the first record arrives and closes after the last.
    times = [r.get("received_at_utc") for r in records
             if isinstance(r.get("received_at_utc"), str)]
    opened, closed = manifest.get("opened_at"), manifest.get("closed_at")
    if isinstance(opened, str) and isinstance(closed, str):
        if opened > closed:
            reasons.append("opened_at is after closed_at")
        # `closed_at` IS record-bound: the writer cannot close before the last
        # record it wrote arrived.
        if times and closed < max(times):
            reasons.append(
                "closed_at is before the last record's receive time")
        # `opened_at` is NOT record-bound, and asserting that it was is a
        # mistake this check originally made. Writers are created LAZILY on the
        # first append, so the first record is genuinely received before the
        # segment opens. It is an operational field: constrained against
        # closed_at, and otherwise operator/writer-declared.
    else:
        reasons.append("opened_at/closed_at are not canonical timestamps")

    if manifest.get("writer_version") != WRITER_VERSION:
        reasons.append(
            f"writer_version {manifest.get('writer_version')!r} is not a "
            f"version this verifier knows ({WRITER_VERSION!r})")
    if manifest.get("close_status") not in ("clean",):
        reasons.append(f"close_status {manifest.get('close_status')!r} is not "
                       "a recognised value")
    # subscription_metadata.venue must agree with the segment id's venue prefix.
    meta_venue = (manifest.get("subscription_metadata") or {}).get("venue")
    seg_venue = (manifest.get("segment_id") or "").split(".", 1)[0]
    if meta_venue is not None and seg_venue and meta_venue != seg_venue:
        reasons.append(
            f"subscription_metadata venue {meta_venue!r} contradicts the "
            f"segment id's venue ({seg_venue!r})")
    # previous_segment_digest is NOT verifiable from the segment alone — only
    # the archive head knows the real predecessor. verify_archive checks it.

    # A legitimately empty segment is valid ONLY when its manifest declares it.
    if v.records_read == 0 and v.records_expected != 0:
        reasons.append("archive is empty but the manifest expects records")

    v.valid = not reasons
    v.state = SegmentState.CLOSED if v.valid else SegmentState.INVALID
    return v


def verify_archive(root, *, environment: str) -> dict:
    """Verify the archive against its COMMITTED history, not against discovery.

    The previous version verified whatever segment directories happened to
    survive, which is why a whole segment could be deleted — or a valid foreign
    segment grafted in — while every survivor stayed individually valid and the
    archive still reported VALID. The head is the authoritative statement of
    which segments must exist, in what order, with what terminal commitment.
    """
    root = Path(root)
    env_root = root / f"env={environment}"
    reasons: list = []
    discovered = {}
    if env_root.exists():
        for d in sorted(env_root.glob("segment=*")):
            # Do not follow a symlinked segment directory: it would place
            # evidence outside the archive root while still verifying.
            if d.is_symlink() or not d.is_dir():
                reasons.append(f"{d.name} is not a real directory")
                continue
            discovered[d.name.split("segment=", 1)[-1]] = d

    head = read_head(root, environment)
    if head is None:
        return {"environment": environment, "verdict": "INVALID",
                "reasons": ["no archive head: the archive has no committed "
                            "history, so nothing states which segments are "
                            "supposed to exist"],
                "segments": len(discovered), "closed_segments": 0,
                "open_segments": 0, "invalid_segments": 0,
                "orphaned_committed_segments": sorted(discovered),
                "records_expected": 0, "records_read": 0,
                "segment_verdicts": []}
    if not verify_head_self_digest(head):
        reasons.append("archive head fails its own digest")
    if head.get("environment") != environment:
        reasons.append(f"head environment {head.get('environment')!r} != "
                       f"{environment!r}")

    expected = head.get("segments") or []
    results, previous_commitment = [], None
    fold = _head_genesis(head.get("environment"), head.get("archive_identity"))

    for index, entry in enumerate(expected):
        seg_id = entry.get("segment_id")
        directory = discovered.pop(seg_id, None)
        if directory is None:
            reasons.append(
                f"segment {seg_id!r} is committed in the head at position "
                f"{index} but is MISSING from the archive")
            continue
        verdict = verify_segment(directory, environment=environment,
                                 allow_open=False)
        results.append(verdict)
        if not verdict.valid:
            reasons.append(f"segment {seg_id!r} does not verify")
            continue
        manifest = parse_canonical(
            (directory / MANIFEST_FILENAME).read_bytes())
        commitment = segment_commitment(manifest)
        if commitment != entry.get("manifest_digest"):
            reasons.append(
                f"segment {seg_id!r} does not match the commitment the head "
                "records for it (substituted or rebuilt)")
        # PART 2: the predecessor link must match the AUTHORITATIVE previous
        # segment, not merely whatever the manifest claims.
        declared_prev = manifest.get("previous_segment_digest")
        if declared_prev != previous_commitment:
            reasons.append(
                f"segment {seg_id!r} names predecessor {declared_prev!r}, but "
                f"the head's previous segment commits to {previous_commitment!r}")
        previous_commitment = commitment
        fold = fold_segments_digest(fold, commitment)

    if discovered:
        # Present on disk, absent from the committed history. Never silently
        # incorporated — that is how a grafted segment becomes evidence.
        reasons.append(
            f"ORPHANED_COMMITTED_SEGMENT: {sorted(discovered)} exist on disk "
            "but are not in the archive head")

    if head.get("segment_count") != len(expected):
        reasons.append("head segment_count does not match its own segment list")
    if fold != head.get("archive_segments_digest"):
        reasons.append(
            "archive_segments_digest does not match the ordered segments "
            "(deletion, insertion or reorder present this way)")
    if expected:
        if head.get("first_segment_digest") != expected[0].get("manifest_digest"):
            reasons.append("head first_segment_digest does not match position 0")
        if head.get("terminal_segment_digest") != expected[-1].get("manifest_digest"):
            reasons.append("head terminal_segment_digest does not match the last "
                           "committed segment")

    closed = [r for r in results if r.state is SegmentState.CLOSED]
    invalid = [r for r in results if r.state is SegmentState.INVALID]
    return {
        "environment": environment,
        "head_generation": head.get("generation"),
        "head_digest": head.get("head_digest"),
        "segments": len(expected),
        "closed_segments": len(closed),
        "open_segments": 0,
        "invalid_segments": len(invalid),
        "orphaned_committed_segments": sorted(discovered),
        "records_expected": sum(e.get("record_count") or 0 for e in expected),
        "records_read": sum(r.records_read for r in results),
        "segment_verdicts": [r.to_dict() for r in results],
        "reasons": reasons,
        "verdict": "VALID" if (expected and not reasons and not invalid)
                   else "INVALID",
    }
