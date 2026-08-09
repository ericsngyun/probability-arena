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
import contextlib
import fcntl
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


def assert_contained(root: Path, target: Path) -> Path:
    """Every component between root and target must be a real directory.

    `Path(root).resolve()` resolves the ROOT, not its children, and
    `mkdir(parents=True, exist_ok=True)` happily traverses a symlinked
    `env=<name>` component. A planted symlink there put every record, every
    manifest and the authoritative head outside the configured root while
    verification still reported VALID — the archive root stopped bounding the
    evidence.

    The target is matched against the root BOTH as given and as resolved. A
    caller that passes an unresolved root (`/tmp/...` on macOS, or the kind of
    symlinked `BACKUP_DIR` an operator actually configures) previously got a
    bare `ValueError` out of `relative_to` — raised from the orphan repair
    path, which is the one route out of a crash between manifest publish and
    head commit.

    This is a path-based check, not `openat` on a pinned dirfd: a component
    swapped between here and the subsequent open is NOT prevented. Verification
    catches such a swap afterwards, so the property is tamper-EVIDENT, not
    race-free, and it is stated that way deliberately.
    """
    root_raw = Path(root)
    root_res = root_raw.resolve()
    target = Path(target)
    # Three spellings, because callers legitimately mix them: the root as
    # configured, the root resolved, and a target reached through a symlinked
    # ancestor of the root itself (macOS `/tmp` and `/var` are symlinks, and a
    # symlinked BACKUP_DIR is an ordinary operator choice). Resolving the
    # target does not weaken the check — containment is decided here, and the
    # component walk below still runs against the real path.
    for candidate, base in ((target, root_raw), (target, root_res),
                            (target.resolve() if target.exists() else target,
                             root_res)):
        try:
            parts = candidate.relative_to(base).parts
            break
        except ValueError:
            continue
    else:
        raise SegmentError(f"{target} is outside the archive root {root_raw}")
    current = root_res
    last = len(parts) - 1
    for i, part in enumerate(parts):
        current = current / part
        if current.is_symlink():
            raise SegmentError(
                f"{current} is a symlink; no path component between the "
                "archive root and its segments may be a link, or the root "
                "stops bounding the evidence")
        if i != last and current.exists() and not current.is_dir():
            raise SegmentError(f"{current} is not a directory")
    return target


def containment_reason(root, target) -> str | None:
    """`assert_contained` as a verification reason rather than an exception.

    The verify side had NO containment check at all. `assert_contained` existed
    and was called three times, every one of them on the write side, so a
    symlink planted at `events.jsonl.gz`, `manifest.json` or `archive-head.log`
    AFTER the writer finished put the evidence outside the root while
    `verify_archive` still returned VALID with no reasons. Content digests
    still caught edits to the external target, but the root had stopped
    bounding the bytes — which is the property backup, retention and permission
    controls are scoped to.
    """
    try:
        assert_contained(root, target)
    except SegmentError as exc:
        return str(exc)
    return None


class SegmentError(RuntimeError):
    """A segment operation that would compromise the evidence."""


class RecordSchemaError(SegmentError):
    """A record that cannot be trusted to mean what it says."""


class DurabilityNotProvenError(SegmentError):
    """The rename landed; the directory fsync that would prove it did not.

    A distinct type because the recovery story is materially different and the
    operator has to be able to tell. Before the rename, the artifact does not
    exist and there is nothing to reconcile. After the rename but before the
    directory fsync, a reader TODAY sees a committed artifact that may not
    survive a power loss — the bytes are in place, their durability is not
    proven.

    Both cases previously raised `SegmentError("manifest publication failed:
    OSError(28, ...)")`, byte-identical at the same errno. The only carrier of
    the distinction was `failed_stage`, an in-RAM attribute that dies with the
    process and appears in no exception, no log and no file — so the
    distinction the design claimed to make was unavailable to the operator who
    needed it. The test asserting it asserted something else.
    """


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
    accepted: int = 0          # incremented ONLY after the put succeeds
    written: int = 0
    rejected: int = 0
    lost: int = 0              # accepted but never drained — must be 0 at close
    # Rejected AFTER acceptance (serialization failure, writer death). An
    # accepted event that the writer refuses is neither written nor lost, and
    # the first version of this model had nowhere to put it.
    rejected_after_accept: int = 0
    rejections: dict = field(default_factory=dict)

    def reject(self, reason: RejectReason, *, after_accept: bool = False) -> None:
        self.rejections[reason.value] = self.rejections.get(reason.value, 0) + 1
        if after_accept:
            self.rejected_after_accept += 1
        else:
            self.rejected += 1

    def reconciles(self) -> bool:
        """Two identities, not one.

        `generated == written + rejected` alone could not tell real loss from a
        producer cancelled before its put: both produced identical counters. So
        acceptance is now recorded only AFTER the queue put succeeds, and an
        accepted event that was never drained is counted as `lost` rather than
        silently vanishing into the difference.
        """
        return (self.generated == self.accepted + self.rejected
                and self.accepted == (self.written + self.rejected_after_accept
                                      + self.lost))

    def lossless(self) -> bool:
        return self.reconciles() and self.lost == 0

    def to_dict(self) -> dict:
        return {"generated": self.generated, "accepted": self.accepted,
                "written": self.written, "rejected": self.rejected,
                "rejected_after_accept": self.rejected_after_accept,
                "lost": self.lost, "rejections": dict(self.rejections),
                "reconciles": self.reconciles(), "lossless": self.lossless()}


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
                 flush_every: int = 256,
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
        # The predecessor is NOT resolved here. It used to be read from the
        # head at OPEN and then validated against the head's actual order at
        # CLOSE — but writers are created lazily and kept open, so two
        # overlapping hours both opened while the head was empty and both
        # recorded predecessor None. Every collector run crossing an hour
        # boundary therefore produced a permanently INVALID archive: intact
        # evidence that the verifier called tampered. A false positive is the
        # one thing a tamper-evidence system cannot afford.
        #
        # Ordering now lives ONLY in the head entry, resolved at commit time
        # under the head lock, where the order is actually decided.
        # Not a parameter any more. A constructor argument that is written into
        # the commit record and checked by nothing is a trap: the next caller to
        # pass it will believe it means something.
        self.previous_segment_digest = None
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
        # B3: the state check and the queue put must be ONE fact. They used to
        # be two hopeful ones, so a producer descheduled between them had its
        # event accepted into a queue nobody would drain — and close() then
        # published close_status "clean" over the loss.
        self._admission = threading.Lock()
        # Producers blocked on a full queue, counted under `_admission`. close()
        # waits for this to reach zero so a put in flight is never stranded.
        self._pending_waiters = 0
        self.queue_high_water = 0
        # Injection seams. Production leaves both None; tests use them to slow
        # the writer or fail a specific durability stage without weakening the
        # real fsync path.
        self.pre_write_hook = None
        self.durability_hooks: dict = {}

        (self.root / f"env={environment}").mkdir(parents=True, exist_ok=True)
        assert_contained(self.root, self.root / f"env={environment}")
        self.dir.mkdir(parents=True, exist_ok=True)
        assert_contained(self.root, self.dir)
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
        # Ownership is held by an flock ON TOP OF the lock file, not by the
        # file's mere existence. An O_EXCL file does not survive its owner:
        # after a SIGKILL the file remained, nothing read the pid inside it, and
        # every later process was refused — so a collector that died at 12:07
        # could not write another byte to the 12:00 partition for the rest of
        # the hour, and `append()` raised on every event. The kernel drops an
        # flock when the holding process dies, which makes "stale" and "live"
        # distinguishable instead of identical. `_head_lock` two hundred lines
        # below already did it this way.
        self._lock_path = self.dir / "writer.lock"
        self._acquire_ownership()
        # An abandoned events file with no manifest is the residue of a crash.
        # Appending to it opened a SECOND gzip member behind an unterminated
        # first one and destroyed the records that were still recoverable —
        # 2000 readable records became 1058, and on a small segment 6 became 0.
        # The prior round's claimed "tombstone" did not exist in the code.
        # O_NOFOLLOW for the same reason as the manifest temp: a symlinked
        # events file would append gzip members into an arbitrary victim file.
        # Both steps are inside the guard: ownership must not leak when
        # construction fails after the lock was taken.
        try:
            self._quarantine_abandoned_events()
            self._open_events()
        except BaseException:
            self._release_lock()
            raise
        self._since_flush = 0
        self._thread = threading.Thread(target=self._run, name="archive-writer",
                                        daemon=True)
        self._thread.start()

    def _acquire_ownership(self) -> None:
        """One segment, one live writer — enforced by the kernel, not by a file.

        Six processes each opening their own descriptor on one segment is
        precisely how the original archive destroyed 719 of 720 records. A
        second LIVE owner must fail loudly here rather than interleave gzip
        members and be discovered later by a reader that can no longer tell
        what was lost. A DEAD owner must not brick the partition forever.
        """
        fd = os.open(self._lock_path,
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise SegmentError(
                f"segment {self.segment_id!r} already has a LIVE writer "
                f"({self._lock_path} is flocked by a running process). A "
                "segment has exactly one owner; concurrent appenders interleave "
                "gzip members and destroy the file. Producers share one writer "
                "through its queue.") from None
        self._lock_fd = fd
        try:
            os.ftruncate(fd, 0)
            write_all(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:
            # The lock is what confers ownership; the pid inside it is a
            # diagnostic. Failing to record the diagnostic is not a reason to
            # refuse to write evidence.
            pass

    def _quarantine_abandoned_events(self) -> None:
        if self.manifest_path.exists():
            return
        # Refuse a symlink BEFORE touching it. Quarantining first would rename
        # the link out of the way and then open a fresh real file, turning the
        # symlink refusal into a silent success.
        if os.path.islink(self.events_path):
            raise SegmentError(
                f"{self.events_path} is a symlink; refusing to write evidence "
                "through it")
        try:
            if not self.events_path.exists() or self.events_path.stat().st_size == 0:
                return
        except OSError:
            return
        stamp = canonical_datetime(datetime.now(timezone.utc)).replace(":", "")
        target = self.events_path.with_name(
            f"{EVENTS_FILENAME}.abandoned.{stamp}")
        os.replace(self.events_path, target)
        self.quarantined_events_path = target

    quarantined_events_path: Path | None = None

    def _open_events(self) -> None:
        if os.path.islink(self.events_path):
            raise SegmentError(
                f"{self.events_path} is a symlink; refusing to write evidence "
                "through it")
        _ev_fd = os.open(self.events_path,
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600)
        self._fh = gzip.open(os.fdopen(_ev_fd, "ab"), "ab")

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
        # Admission: re-check state and enqueue under one lock, so "accepted"
        # and "will be drained" cannot come apart. The lock is held for a
        # NON-BLOCKING put only. Holding it across the timed put serialised the
        # producers behind each other's full timeouts — 8 producers at a 0.5s
        # timeout measured 4.58s per submit, and close() waited 4.55s before it
        # could even declare CLOSING. Bounded backpressure that scales with the
        # number of producers is not bounded.
        with self._admission:
            if self._shutdown.is_set() or self.state is not SegmentState.OPEN:
                with self._lock:
                    self.accounting.reject(RejectReason.SHUTDOWN_IN_PROGRESS)
                return RejectReason.SHUTDOWN_IN_PROGRESS
            try:
                self._queue.put_nowait(envelope_fields)
            except queue.Full:
                self._pending_waiters += 1
            else:
                with self._lock:
                    self.accounting.accepted += 1
                self._note_depth()
                return None
        # Queue full: wait OUTSIDE the admission gate. close() cannot finish
        # while `_pending_waiters` is non-zero, so a producer blocked here still
        # cannot be accepted into a queue nobody will drain.
        try:
            self._queue.put(envelope_fields, timeout=self._enqueue_timeout)
        except queue.Full:
            with self._admission:
                self._pending_waiters -= 1
            with self._lock:
                self.accounting.reject(RejectReason.ENQUEUE_TIMEOUT)
            return RejectReason.ENQUEUE_TIMEOUT
        with self._admission:
            self._pending_waiters -= 1
            with self._lock:
                self.accounting.accepted += 1
        self._note_depth()
        return None

    def _note_depth(self) -> None:
        with self._lock:
            depth = self._queue.qsize()
            if depth > self.queue_high_water:
                self.queue_high_water = depth

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
                    self.accounting.reject(RejectReason.WRITER_FAILED,
                                           after_accept=True)
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
                self.accounting.reject(RejectReason.SERIALIZATION_FAILURE,
                                       after_accept=True)
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
                # Seal the accounting FIRST. This branch short-circuited before
                # the drain loop ever ran, so a writer-thread failure left every
                # still-queued event — 197 of 200 in the reviewer's probe — in
                # no terminal state at all, and whether it did depended on a
                # race between two lines in `_run`. The most important failure
                # mode had nondeterministic accounting.
                self._seal_accounting()
                # Release here too. This is the path a mid-stream writer error
                # takes, and it was the one path still leaking ownership —
                # leaving the partition unwritable by every future process.
                self._release_lock()
                raise SegmentError(
                    f"segment is INVALID and cannot be closed: "
                    f"{self._writer_error!r} {self.accounting.to_dict()}")
            return self._close_locked()

    def _seal_accounting(self) -> None:
        """Drain the backlog into `lost` and stop admitting. Idempotent."""
        with self._admission:
            self._shutdown.set()
        deadline = time.monotonic() + self._enqueue_timeout + 1.0
        while time.monotonic() < deadline:
            with self._admission:
                if self._pending_waiters == 0:
                    break
            time.sleep(0.005)
        thread = getattr(self, "_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        drained = 0
        while True:
            try:
                self._queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        if drained:
            with self._lock:
                self.accounting.lost += drained

    def _close_locked(self) -> dict:
        try:
            return self._close_stages()
        except BaseException:
            # Ownership must not leak on ANY failure path, or one mid-stream
            # write error locks the partition out for every future process.
            self._release_lock()
            raise

    def _close_stages(self) -> dict:
        # Take the admission gate first: a producer already inside it finishes
        # its put and is counted, and no producer can enter after CLOSING.
        with self._admission:
            self.state = SegmentState.CLOSING
            self._shutdown.set()
        # Producers already blocked on a full queue are outside the admission
        # gate. Wait for them before joining, or their put lands after the
        # writer has returned and becomes silent loss.
        deadline = time.monotonic() + self._enqueue_timeout + 1.0
        while time.monotonic() < deadline:
            with self._admission:
                if self._pending_waiters == 0:
                    break
            time.sleep(0.005)
        self._thread.join(timeout=30)
        if self._thread.is_alive():
            # B4: do NOT release ownership here. Releasing while the writer is
            # still running admitted a second writer to a live segment — the
            # exact dual-writer condition the O_EXCL lock exists to prevent,
            # and it destroyed the segment. An unreleasable lock on a hung
            # writer is the safer failure.
            self.state = SegmentState.INVALID
            self._ownership_held_by_live_writer = True
            raise SegmentError(
                "writer did not drain within the shutdown timeout; ownership "
                "is deliberately NOT released while the writer thread is alive")
        # H2: whatever the writer never drained is LOST, and is counted rather
        # than left implicit in the difference. One WRITER_FAILED rejection
        # previously stood in for an entire abandoned backlog.
        drained = 0
        while True:
            try:
                self._queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        if drained:
            with self._lock:
                self.accounting.lost += drained
        if self._writer_error is not None:
            self.state = SegmentState.INVALID
            raise SegmentError(f"writer failed: {self._writer_error!r}")
        # B3: the accounting must hold BEFORE anything is published. close()
        # previously compared the writer against itself (records on disk vs
        # written) and never against what producers were told.
        if not self.accounting.lossless():
            self.state = SegmentState.INVALID
            raise SegmentError(
                "refusing to publish a segment whose accounting does not "
                f"reconcile losslessly: {self.accounting.to_dict()}")
        # A post-acceptance drop is a loss the PRODUCER was told did not happen.
        # `rejected_after_accept` sat on the credit side of the identity, so a
        # single unserialisable payload produced `close_status: "clean"`,
        # `lossless() == True` and archive VALID over a missing event — the
        # exact lost-but-reported-clean state the model forbids. The manifest
        # carries no accounting at all, so nothing durable could ever record it.
        if self.accounting.rejected_after_accept:
            self.state = SegmentState.INVALID
            raise SegmentError(
                f"{self.accounting.rejected_after_accept} event(s) were "
                "accepted from a producer and then dropped by the writer; "
                "refusing to publish this segment as a clean close: "
                f"{self.accounting.to_dict()}")

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
        except DurabilityNotProvenError as exc:
            # Rename succeeded: the manifest IS on disk and a reader sees a
            # committed segment. Propagate the distinct type rather than
            # flattening it into the generic failure, which is what made the
            # two indistinguishable to an operator.
            self.state = SegmentState.INVALID
            self._writer_error = exc
            self._release_lock()
            raise
        except BaseException as exc:
            # A manifest that did not publish means the segment is NOT closed.
            self.state = SegmentState.INVALID
            self._writer_error = exc
            self._release_lock()
            raise SegmentError(f"manifest publication failed at stage "
                               f"{self.failed_stage!r}: {exc!r}") from exc
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
        """Mark the stage BEFORE it runs, unconditionally.

        This used to set `failed_stage` only when an injection hook existed, so
        a real ENOSPC at rename and a real ENOSPC at directory-fsync produced
        byte-identical reports — the very distinction the design claims to make.
        The stage is cleared by the next successful stage.
        """
        self.failed_stage = name
        hook = self.durability_hooks.get(name)
        if hook is not None:
            hook()

    _ownership_held_by_live_writer = False

    def _close_fh(self) -> None:
        """Close the gzip handle. Every `raise` path out of close() leaked it.

        The leak is also what made an abandoned segment unrecoverable: the
        successor writer opened its own descriptor while the predecessor's was
        still open on the same file.
        """
        fh = getattr(self, "_fh", None)
        if fh is None:
            return
        try:
            fh.close()
        except Exception:                       # noqa: BLE001 - already failing
            pass
        self._fh = None

    def _release_lock(self) -> None:
        # Guard on the THREAD, not on a flag. The flag was set on exactly one
        # of the paths where the writer can still be running, so forcing the
        # segment INVALID from elsewhere released ownership under a live writer
        # and admitted a second one — the dual-writer condition this lock
        # exists to prevent.
        thread = getattr(self, "_thread", None)
        if thread is not None and thread.is_alive():
            self._ownership_held_by_live_writer = True
            return
        self._ownership_held_by_live_writer = False
        self._close_fh()
        fd = getattr(self, "_lock_fd", None)
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
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
    staged_bytes = Path(tmp).read_bytes()
    if staged_bytes != payload:
        raise SegmentError(
            f"staged manifest is {len(staged_bytes)} bytes, expected "
            f"{len(payload)}; refusing to publish bytes that are not the ones "
            "we meant to commit")
    check = parse_canonical(staged_bytes)
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
    try:
        _fsync_directory(directory)
    except OSError as exc:
        raise DurabilityNotProvenError(
            f"{final} was renamed into place and IS visible to readers, but the "
            f"directory fsync that would prove it survives a crash failed: "
            f"{exc!r}. The segment is committed; its durability is not proven.")
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
                # Per-CHUNK, because `decompress()` raising discards its return
                # value and the whole 64 KiB chunk with it. On mid-stream
                # corruption that cost 942 of 2000 records, and on a small
                # segment it cost every record — while the docstring claimed
                # the complete prefix was preserved. Retry the failing chunk in
                # small increments to recover the bytes before the fault.
                try:
                    out.append(dec.decompress(data[i:i + 65536]))
                except (zlib.error, EOFError):
                    for j in range(i, min(i + 65536, len(data)), 512):
                        try:
                            out.append(dec.decompress(data[j:j + 512]))
                        except (zlib.error, EOFError):
                            break
                    raise
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
ARCHIVE_HEAD_LOG_FILENAME = "archive-head.log"
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
               previous_head: dict | None, segments: list,
               updated_at: str | None = None) -> dict:
    """Build the next head from the PREVIOUS authoritative head plus a segment.

    Never from whatever segments happen to be on disk. Regenerating the head by
    discovery would certify exactly the deletion and grafting it exists to
    detect — the verifier would simply agree with whatever survived. That rule
    is now enforced rather than merely stated: see
    `_refuse_head_bootstrap_over_existing_evidence`.

    `updated_at` is the head's only non-deterministic input, and completing a
    commit that a crash interrupted has to reproduce the head the retained log
    already recorded — a head that differs only in its timestamp has a
    different digest, which would leave the log with two entries at one
    generation and the archive unrepairable. So the log carries the stamp and
    the repair path passes it back.
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
        "updated_at": updated_at or canonical_datetime(
            datetime.now(timezone.utc)),
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
    if path.is_symlink():
        # publish_head refuses to WRITE through a symlink; reading through one
        # would let the authoritative history be relocated outside the root.
        raise ArchiveHeadError(
            f"{path} is a symlink; the archive head must live inside the root")
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
    assert_contained(Path(root).resolve(), directory)
    final = directory / ARCHIVE_HEAD_FILENAME
    # Per-publisher temp name. One shared temp path meant concurrent publishers
    # unlinked and clobbered each other's staging file, which is how a
    # FileNotFoundError and a half-written head appeared under ordinary
    # concurrent close.
    tmp = directory / (f"{ARCHIVE_HEAD_FILENAME}.{os.getpid()}."
                       f"{threading.get_ident()}{MANIFEST_TEMP_SUFFIX}")
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
    staged_bytes = Path(tmp).read_bytes()
    if staged_bytes != payload:
        raise ArchiveHeadError(
            f"staged head is {len(staged_bytes)} bytes, expected "
            f"{len(payload)}; a self-consistent but DIFFERENT head would "
            "otherwise pass this check")
    staged = parse_canonical(staged_bytes)
    if not verify_head_self_digest(staged):
        raise ArchiveHeadError(
            "the staged archive head does not verify against its own digest; "
            "refusing to publish it as the history commit record")
    if staged.get("generation") != head["generation"]:
        raise ArchiveHeadError("staged head generation does not match")

    # H-1: the published head is recorded in a retained log. previous_head_digest
    # and generation were folded into head_digest and then compared to nothing,
    # because no prior head was kept — they could not be checked even in
    # principle. Without this, rolling the head back to a retained earlier
    # generation makes TAIL TRUNCATION free, which is the one deletion the
    # segment predecessor links cannot catch.
    #
    # WRITE-AHEAD, and this ordering is the whole point. The log used to be
    # appended AFTER the rename, so a crash in between — an ordinary SIGKILL,
    # or a plain ENOSPC on the log — left the head at generation N with the log
    # at N-1, which `_verify_head_against_log` read as HEAD_ROLLBACK. An intact
    # archive was permanently accused of tampering with no repair path, because
    # the head is immutable and the retry hit the duplicate-segment guard.
    # Three reviewers found it independently.
    #
    # Log-then-rename inverts the failure into the recoverable direction: the
    # log may legitimately be ONE entry ahead of the head, meaning a commit was
    # in flight. A log BEHIND the head is still a rollback and still fatal.
    _s("head_log_append")
    log = directory / ARCHIVE_HEAD_LOG_FILENAME
    if os.path.islink(log):
        raise ArchiveHeadError(
            f"{log} is a symlink; refusing to write the retained head history "
            "through it")
    already = _read_head_log(directory)
    if not (already.entries and
            already.entries[-1].get("head_digest") == head["head_digest"]):
        line = canonical_bytes(
            {"generation": head["generation"],
             "head_digest": head["head_digest"],
             "previous_head_digest": head["previous_head_digest"],
             "segment_count": head["segment_count"],
             # Which segment this generation was committing. Without it, a
             # crash mid-commit and a one-generation rollback are the same
             # three numbers and cannot be told apart.
             "terminal_segment_id": (head["segments"][-1]["segment_id"]
                                     if head.get("segments") else None),
             # The head's only non-deterministic input, so that completing an
             # interrupted commit reproduces the same head rather than a
             # second one at the same generation.
             "updated_at": head["updated_at"]}) + b"\n"
        log_existed = log.exists()
        log_fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            write_all(log_fd, line)
            os.fsync(log_fd)
        finally:
            os.close(log_fd)
        if not log_existed:
            # The log's own directory entry has to be durable too, or a crash
            # on the very first publish loses the file and the archive comes
            # back as "no archive head log" — INVALID, for a clean archive.
            _fsync_directory(directory)

    _s("head_rename")
    if os.path.islink(final):
        raise ArchiveHeadError(
            f"{final} is a symlink; refusing to publish the head through it")
    os.replace(tmp, final)
    _s("head_directory_fsync")
    _fsync_directory(directory)
    return final


def _fsync_directory(directory: Path) -> None:
    dir_fd = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


@dataclass
class _HeadLog:
    entries: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    torn_tail: bool = False


def _read_head_log(directory: Path) -> _HeadLog:
    """Parse the retained head history, tolerating exactly one torn last line.

    An append that was interrupted mid-line is the tail of the same crash the
    write-ahead ordering exists to survive, so a partial FINAL line is discarded
    as an unfinished record. A malformed INTERIOR line is not survivable and
    stays fatal — that is editing, not tearing.
    """
    out = _HeadLog()
    log = Path(directory) / ARCHIVE_HEAD_LOG_FILENAME
    if not log.exists():
        return out
    raw = [ln for ln in log.read_bytes().split(b"\n") if ln.strip()]
    for i, line in enumerate(raw):
        try:
            entry = parse_canonical(line)
        except Exception:
            if i == len(raw) - 1:
                out.torn_tail = True
                break
            out.reasons.append(
                f"archive head log line {i} is unreadable; a malformed "
                "interior entry is an edit, not a torn append")
            return out
        if not isinstance(entry, dict):
            out.reasons.append(
                f"archive head log line {i} is a {type(entry).__name__}, "
                "not an object")
            return out
        out.entries.append(entry)
    return out


def head_lock_path(root, environment: str) -> Path:
    return head_path(root, environment).parent / "head.lock"


@contextlib.contextmanager
def _head_lock(root, environment: str, timeout_s: float = 30.0):
    """Serialize the head's read-modify-write across threads AND processes.

    `commit_segment_to_head` reads the head, appends, and republishes. Without
    this, two segments closing at the same time — entirely ordinary — both read
    the same head and one commit is lost, leaving a history that omits a
    durably committed segment and cannot be repaired (the duplicate guard
    refuses a retry).
    """
    path = head_lock_path(root, environment)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise ArchiveHeadError(
                        "timed out waiting for the archive head lock")
                time.sleep(0.01)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def commit_segment_to_head(root, environment: str, *, archive_identity: str,
                           manifest: dict, stage=None) -> dict:
    """Append one committed segment to the archive's authoritative history."""
    with _head_lock(root, environment):
        return _commit_segment_to_head_locked(
            root, environment, archive_identity=archive_identity,
            manifest=manifest, stage=stage)


def _commit_segment_to_head_locked(root, environment: str, *,
                                   archive_identity: str, manifest: dict,
                                   stage=None) -> dict:
    previous = read_head(root, environment)
    if previous is not None and not verify_head_self_digest(previous):
        raise ArchiveHeadError(
            "the existing archive head fails its own digest; refusing to "
            "extend a history that is already untrustworthy")
    if previous is None:
        _refuse_head_bootstrap_over_existing_evidence(
            root, environment, segment_id=manifest["segment_id"])
    segments = list(previous["segments"]) if previous else []
    entry = {
        "segment_id": manifest["segment_id"],
        "manifest_digest": segment_commitment(manifest),
        "partition_identity": manifest["partition_identity"],
        "record_count": manifest["record_count"],
        # Resolved HERE, under the lock, where the order is actually decided.
        "previous_segment_digest": (
            previous["terminal_segment_digest"] if previous else None),
    }
    if any(e["segment_id"] == entry["segment_id"] for e in segments):
        raise ArchiveHeadError(
            f"segment {entry['segment_id']!r} is already in the archive head")
    segments.append(entry)
    # Completing a commit the crash interrupted: the retained log already
    # recorded the head that was about to be published, so reproduce THAT head
    # rather than a second one at the same generation with a later timestamp.
    resume_at = None
    tail = _read_head_log(head_path(root, environment).parent).entries
    if tail:
        last = tail[-1]
        if (last.get("previous_head_digest") == (previous or {}).get("head_digest")
                and last.get("terminal_segment_id") == entry["segment_id"]
                and last.get("generation") == (
                    (previous["generation"] + 1) if previous else 1)):
            resume_at = last.get("updated_at")
    head = build_head(environment=environment, archive_identity=archive_identity,
                      previous_head=previous, segments=segments,
                      updated_at=resume_at)
    publish_head(root, environment, head, stage=stage)
    return head


def _has_live_writer(directory: Path) -> bool:
    """Is a process holding this segment's writer lock right now?

    The lock file's existence answers nothing — it outlives its owner. The
    flock does not, so taking it and immediately dropping it is what
    distinguishes "a collector is filling this segment" from "a crash left this
    behind". Read-only: a lock we take here is released before returning.
    """
    lock = Path(directory) / "writer.lock"
    if not lock.exists() or lock.is_symlink():
        return False
    try:
        fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True                    # held by someone else: a live writer
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _refuse_head_bootstrap_over_existing_evidence(root, environment: str, *,
                                                  segment_id: str) -> None:
    """A first commit is only a first commit on an archive that has no history.

    The module claimed the head is "never rebuilt from whatever segments happen
    to be on disk", but nothing enforced it, and the commit function is
    exported. Three commands — `rm archive-head.json`, `rm archive-head.log`,
    then replay `commit_segment_to_head` over the surviving manifests —
    regenerated a history that verified VALID with a middle segment deleted, or
    with a fabricated segment substituted, which `read_verified()` then served
    as canonical. Not one record digest, chain link or manifest digest had to
    be recomputed: rewriting two files was enough.

    So bootstrapping is refused whenever the archive already carries evidence.

    SCOPE, stated honestly: this closes the rebuild through the production API.
    It does NOT make the archive tamper-PROOF. Every digest here is unkeyed and
    every commitment lives under the same root the writer can write, so an
    attacker who forges `archive-head.json` and `archive-head.log` directly
    still wins. Nothing inside the directory can fix that. Establishing a root
    of trust the writer cannot rewrite — an append-only external sink, a
    second host, or a signature whose key is not on this box — is a design
    decision, not a patch, and it is not made here.
    """
    directory = head_path(root, environment).parent
    if (directory / ARCHIVE_HEAD_LOG_FILENAME).exists():
        raise ArchiveHeadError(
            "the archive head is absent but its retained history log is "
            "present: this archive HAS published a history and the head that "
            "committed it is gone. Refusing to bootstrap a replacement — that "
            "is precisely how a rebuilt head certifies a deletion. Restore the "
            "head from backup.")
    # Only segments with NO live writer count. On a genuinely new archive
    # several writers close at once: the head lock serialises them, so the
    # first to take it can legitimately see other manifests already published
    # by peers that have not yet reached their own commit — and those peers
    # still hold their writer locks. Segments whose owner is gone are a
    # different thing entirely: history that was already committed.
    others = sorted(
        d.name.split("segment=", 1)[-1]
        for d in directory.glob("segment=*")
        if d.is_dir() and not d.is_symlink()
        and d.name.split("segment=", 1)[-1] != segment_id
        and (d / MANIFEST_FILENAME).exists()
        and not _has_live_writer(d))
    if others:
        raise ArchiveHeadError(
            f"refusing to start a new archive history over {len(others)} "
            f"segment(s) that are already committed on disk ({others[:5]}): a "
            "head built by discovery certifies exactly the deletion and "
            "grafting it exists to detect. Restore the head from backup.")


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


def verify_segment(directory, *, environment: str, allow_open: bool = False,
                   root=None) -> SegmentVerdict:
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

    # Containment on the VERIFY side. `assert_contained` was called three times,
    # all of them on the write side, so a symlink planted at the events file or
    # the manifest after the writer had gone put the evidence outside the root
    # and this function still returned valid. The write side refusing a symlink
    # says nothing about the file a reader is handed later.
    if root is not None:
        for path in (events_path, manifest_path):
            if not path.exists() and not path.is_symlink():
                continue
            reason = containment_reason(root, path)
            if reason is not None:
                return SegmentVerdict(
                    seg_id, SegmentState.INVALID, False,
                    [f"{path.name} is not bounded by the archive root: {reason}"])

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
    # The manifest envelope is CLOSED, like the record and head envelopes. It
    # was the only one left open: `manifest_digest` covers a fixed field list,
    # so an arbitrary extra key could be injected into the commit record, be
    # outside every digest and every head commitment, and still verify VALID.
    unknown = sorted(set(manifest) - set(MANIFEST_FIELDS)
                     - {"manifest_digest", "subscription_metadata"})
    if unknown:
        reasons.append(
            f"manifest carries undeclared top-level field(s) {unknown}; the "
            "commit record's envelope is closed at this schema version")

    # previous_segment_digest is NOT verified here and is not written by the
    # writer: a segment cannot know its own place in history at the moment it
    # opens. The head entry carries the link, resolved at commit time. It stays
    # in the schema for that reason and must therefore be EMPTY — left merely
    # unchecked it was a field a future caller could populate believing it
    # meant something, while nothing read it.
    if manifest.get("previous_segment_digest") is not None:
        reasons.append(
            "previous_segment_digest is set, but ordering is asserted by the "
            "head entry; a value here is not verified by anything and must "
            "not be mistaken for provenance")

    # A legitimately empty segment is valid ONLY when its manifest declares it.
    if v.records_read == 0 and v.records_expected != 0:
        reasons.append("archive is empty but the manifest expects records")

    v.valid = not reasons
    v.state = SegmentState.CLOSED if v.valid else SegmentState.INVALID
    return v


def _verify_head_against_log(root, environment: str, head: dict) -> list:
    """The head must be the newest one this archive published, and it must chain."""
    directory = head_path(root, environment).parent
    log = directory / ARCHIVE_HEAD_LOG_FILENAME
    reason = containment_reason(root, log)
    if reason is not None:
        return [f"archive head log is not bounded by the archive root: {reason}"]
    if not log.exists():
        return ["no archive head log: without a retained head history, a "
                "rolled-back or rebuilt head cannot be distinguished from the "
                "current one"]
    parsed = _read_head_log(directory)
    if parsed.reasons:
        return list(parsed.reasons)
    entries = parsed.entries
    if not entries:
        return ["archive head log is empty"]
    out = []
    expected_prev = None
    for e in entries:
        if e.get("previous_head_digest") != expected_prev:
            out.append("archive head log does not chain")
            break
        expected_prev = e.get("head_digest")
    generations = [e.get("generation") for e in entries]
    if generations != list(range(1, len(generations) + 1)):
        out.append(f"archive head log generations are not contiguous: "
                   f"{generations}")
    newest = entries[-1]
    if head.get("head_digest") == newest.get("head_digest"):
        return out
    # The log is written BEFORE the head rename, so a log exactly one entry
    # ahead of the head is EITHER a commit that was in flight when the process
    # died OR a head rolled back by one generation. Those look identical in the
    # log alone, so the log alone cannot decide it: a first attempt at this
    # accepted both and made single-generation rollback free.
    #
    # The discriminator is on disk. A torn commit leaves the segment it was
    # committing published and orphaned; a rollback-plus-deletion does not.
    # Either way the archive is NOT in a committed state and does not verify —
    # but a torn commit is REPAIRABLE by re-running the commit (the log append
    # is idempotent on head_digest), whereas the head being immutable was what
    # made the previous ordering's failure permanent.
    if (len(entries) >= 2
            and entries[-2].get("head_digest") == head.get("head_digest")
            and newest.get("previous_head_digest") == head.get("head_digest")):
        in_flight = newest.get("terminal_segment_id")
        published = (in_flight and (directory / f"segment={in_flight}"
                                    / MANIFEST_FILENAME).exists())
        if published:
            out.append(
                f"HEAD_COMMIT_INCOMPLETE: the retained history records "
                f"generation {newest.get('generation')} committing segment "
                f"{in_flight!r}, whose manifest IS published, but the head is "
                f"still at generation {head.get('generation')}. This is a crash "
                "between the log append and the head rename. Re-run the commit "
                "for that segment to roll the head forward.")
            return out
    out.append(
        f"the current head is generation {head.get('generation')} with digest "
        f"{str(head.get('head_digest'))[:12]}, but the archive last published "
        f"generation {newest.get('generation')} with digest "
        f"{str(newest.get('head_digest'))[:12]} "
        "(HEAD_ROLLBACK or a head rebuilt outside the commit path)")
    return out


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
    if env_root.is_symlink():
        return {"environment": environment, "verdict": "INVALID",
                "reasons": [f"{env_root} is a symlink; the archive root does "
                            "not bound this evidence"],
                "segments": 0, "closed_segments": 0, "open_segments": 0,
                "invalid_segments": 0, "orphaned_committed_segments": [],
                "records_expected": 0, "records_read": 0,
                "segment_verdicts": []}
    if env_root.exists():
        for d in sorted(env_root.glob("segment=*")):
            # Do not follow a symlinked segment directory: it would place
            # evidence outside the archive root while still verifying.
            if d.is_symlink() or not d.is_dir():
                reasons.append(f"{d.name} is not a real directory")
                continue
            discovered[d.name.split("segment=", 1)[-1]] = d

    try:
        head = read_head(root, environment)
    except Exception as exc:
        return {"environment": environment, "verdict": "INVALID",
                "reasons": [f"archive head is unreadable "
                            f"({type(exc).__name__})"],
                "segments": len(discovered), "closed_segments": 0,
                "open_segments": 0, "invalid_segments": 0,
                "orphaned_committed_segments": sorted(discovered),
                "records_expected": 0, "records_read": 0,
                "segment_verdicts": []}
    if head is not None and not isinstance(head, dict):
        return {"environment": environment, "verdict": "INVALID",
                "reasons": [f"archive head is a {type(head).__name__}, not an "
                            "object"],
                "segments": len(discovered), "closed_segments": 0,
                "open_segments": 0, "invalid_segments": 0,
                "orphaned_committed_segments": sorted(discovered),
                "records_expected": 0, "records_read": 0,
                "segment_verdicts": []}
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
    # H-1: the head must be the LATEST head this archive ever published, and it
    # must chain. A rolled-back head is internally valid, which is exactly why
    # self-consistency cannot be the test.
    reasons.extend(_verify_head_against_log(root, environment, head))
    if head.get("schema_version") != HEAD_SCHEMA_VERSION:
        reasons.append(
            f"head schema_version {head.get('schema_version')!r} is not "
            f"{HEAD_SCHEMA_VERSION}")
    if head.get("canonical_schema_version") != CANONICAL_SCHEMA_VERSION:
        reasons.append("head canonical_schema_version is not supported")
    unknown_head = sorted(set(head) - set(HEAD_FIELDS) - {"head_digest"})
    if unknown_head:
        reasons.append(
            f"head carries undeclared top-level field(s) {unknown_head}")
    if head.get("environment") != environment:
        reasons.append(f"head environment {head.get('environment')!r} != "
                       f"{environment!r}")

    expected = head.get("segments") or []
    # A verifier that RAISES on a malformed head is a verifier that cannot
    # report corruption. `head["segments"] = ["x"]`, a dict, or a head-log line
    # of `null` each produced an AttributeError out of the middle of the walk,
    # which `read_verified()` propagated and the CLI — catching only
    # (ArchiveError, OSError) — turned into a traceback. Corruption is a
    # verdict, not an exception.
    if not isinstance(expected, list):
        expected = []
        reasons.append(
            f"head segments is a {type(head.get('segments')).__name__}, not a list")
    elif not all(isinstance(e, dict) for e in expected):
        reasons.append("head segments contains entries that are not objects")
        expected = [e for e in expected if isinstance(e, dict)]
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
                                 allow_open=False, root=root)
        results.append(verdict)
        if not verdict.valid:
            # Carry the segment's OWN reasons up. "does not verify" alone made
            # an operator open a second tool to learn whether the evidence was
            # edited, truncated, or had walked outside the archive root.
            reasons.append(f"segment {seg_id!r} does not verify: "
                           f"{'; '.join(verdict.reasons)}")
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
        # Ordering is asserted by the HEAD ENTRY, which was resolved at commit
        # time under the head lock — not by the manifest, which is written
        # before the order is known.
        entry_prev = entry.get("previous_segment_digest")
        if entry_prev != previous_commitment:
            reasons.append(
                f"segment {seg_id!r} is recorded after predecessor "
                f"{entry_prev!r}, but the preceding committed segment is "
                f"{previous_commitment!r}")
        # M-2: the head entry's carried facts must agree with the manifest.
        if entry.get("record_count") != manifest.get("record_count"):
            reasons.append(
                f"head entry for {seg_id!r} claims {entry.get('record_count')} "
                f"records, the manifest says {manifest.get('record_count')}")
        if entry.get("partition_identity") != manifest.get("partition_identity"):
            reasons.append(
                f"head entry for {seg_id!r} claims partition "
                f"{entry.get('partition_identity')!r}, the manifest says "
                f"{manifest.get('partition_identity')!r}")
        previous_commitment = commitment
        fold = fold_segments_digest(fold, commitment)

    # Present on disk, absent from the committed history. Never silently
    # incorporated — that is how a grafted segment becomes evidence. But the
    # four ways this happens are not the same event, and collapsing them into
    # one alarm meant the NORMAL state of a running collector — one closed hour
    # plus one hour still open — reported INVALID with a tamper reason, and
    # `read_verified()` refused the committed hour. The archive was unreadable
    # through its own canonical API for as long as it was collecting, and the
    # single tamper signal fired continuously, which trains an operator to
    # ignore it.
    open_now, orphaned, abandoned = [], [], []
    for seg_id, directory in sorted(discovered.items()):
        if not (directory / MANIFEST_FILENAME).exists():
            # No commit record. A live flock means a writer is filling it right
            # now; no live writer means it was abandoned by a crash.
            (open_now if _has_live_writer(directory) else abandoned).append(seg_id)
        else:
            orphaned.append(seg_id)
    if orphaned:
        reasons.append(
            f"ORPHANED_COMMITTED_SEGMENT: {orphaned} are committed evidence on "
            "disk but are not in the archive head — either a crash between the "
            "manifest publish and the head commit (adoptable) or a graft (not). "
            "Compare each against the head's expected order before adopting.")
    if abandoned:
        reasons.append(
            f"ABANDONED_SEGMENT: {abandoned} have no commit record and no live "
            "writer; they are the residue of a crash and are not evidence")

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
        "open_segments": len(open_now),
        "uncommitted_open_segments": open_now,
        "abandoned_segments": abandoned,
        "invalid_segments": len(invalid),
        "orphaned_committed_segments": orphaned,
        "records_expected": sum(e.get("record_count") or 0 for e in expected),
        "records_read": sum(r.records_read for r in results),
        "segment_verdicts": [r.to_dict() for r in results],
        "reasons": reasons,
        "verdict": "VALID" if (expected and not reasons and not invalid)
                   else "INVALID",
    }
