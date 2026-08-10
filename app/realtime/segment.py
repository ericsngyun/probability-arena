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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path

from app.realtime.canonical import (
    CANONICAL_SCHEMA_VERSION,
    CanonicalError,
    CapabilityLimits,
    canonical_bytes,
    canonical_datetime,
    digest_hex,
    parse_canonical,
    parse_canonical_datetime,
)
from app.realtime import evidence_fs

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

    A2/A3, KALSHI-ARCHIVE-CORE-REMEDIATION-002: the containment WALK itself —
    the raw `os.lstat`/`Path.is_symlink`/`Path.resolve` calls that decide
    whether `target` is bounded by `root` — now lives in ONE place,
    `evidence_fs.assert_contained`, not here. This wrapper exists only to
    preserve the exception type every existing caller in this codebase
    already catches (`SegmentError`): `evidence_fs` raises its own
    filesystem-layer `EvidenceAccessError`, which is deliberately NOT a
    `SegmentError`, because `evidence_fs` has no notion of what a segment is
    and must not invent domain exceptions for callers it cannot see. Losing
    that type at every one of `assert_contained`'s eight call sites (three of
    them inside `SegmentWriter.__init__`'s single-writer-ownership retry
    logic in `archive.py::_writer_for`, which specifically catches
    `SegmentError` to mean "try the next candidate id") would silently change
    which retries happen. Translating it back here, once, is cheaper than
    auditing and updating every caller.
    """
    try:
        return evidence_fs.assert_contained(root, target)
    except evidence_fs.EvidenceAccessError as exc:
        raise SegmentError(str(exc)) from exc


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

    Total by contract, delegated to `evidence_fs.containment_reason`, which
    is total for the same reason this function used to need its own OSError
    arm: `assert_contained` stats the target eagerly, so it can raise EACCES
    for exactly the reason the caller's guard was meant to stop.
    """
    return evidence_fs.containment_reason(root, target)


class SegmentError(RuntimeError):
    """A segment operation that would compromise the evidence."""


class RecordSchemaError(SegmentError):
    """A record that cannot be trusted to mean what it says."""


class OrphanedCommittedSegmentError(SegmentError):
    """The segment's manifest is durable evidence that NO generation record
    -- durably on disk, right now -- commits.

    This is the genuinely ambiguous case `verify_archive` reports as
    `ORPHANED_COMMITTED_SEGMENT`: a crash between the manifest publish and
    the head commit, or a graft. Resolving it requires an explicit operator
    decision (adopt into history or discard the evidence); nothing here may
    infer which one is correct.
    """


class StaleHeadAfterCommitError(SegmentError):
    """The archive head update failed, but the durable state proves the
    segment IS already committed: a generation record naming this exact
    segment exists on disk. Only the current-head POINTER did not advance.

    This is STALE_HEAD, not an orphan, and it is automatically,
    deterministically recoverable by `recover_current_head` -- no operator
    decision is needed, only the (idempotent) recovery operation.
    """


def _durable_generation_commits(root, environment: str, segment_id: str) -> bool:
    """Whether a head generation record ALREADY DURABLE ON DISK commits this
    exact segment id, checked directly against the filesystem rather than
    inferred from which exception `commit_segment` raised.

    This is what makes the ORPHANED_COMMITTED_SEGMENT label in `close()`
    correct: `commit_segment` writes the generation record and only THEN
    advances the current-head pointer, as two separate durable steps. A
    failure in the second step leaves the first one intact -- a STALE_HEAD,
    automatically recoverable by `recover_current_head` -- not an orphan.
    Labelling every `BaseException` from `commit_segment` as an orphan
    conflated the two and sent an operator to the fictional `archive-adopt`
    for a state the existing recovery already resolves.
    """
    try:
        for generation in present_generations(root, environment):
            record = read_generation(root, environment, generation)
            if record.get("committed_segment_id") == segment_id:
                return True
    except Exception:
        # Cannot prove the segment is committed -- fail closed to the
        # ambiguous (adopt/discard) label rather than claiming an automatic
        # recoverability the filesystem did not actually demonstrate.
        return False
    return False


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --- DurabilityNotProven ----------------------------------------------------------
# `DurabilityNotProven` is defined ONCE, in `archive_head.py`, and imported
# below. It applies uniformly to the three commitment artifacts — the segment
# manifest, the head-generation record and the current-head pointer — because
# the distinction it carries is the same in all three:
#
#   before the rename/link  the artifact does not exist; nothing to reconcile
#   after it, fsync failed  a reader sees a COMMITTED artifact that may not
#                           survive a crash: the bytes are in place, their
#                           durability is not proven
#
# Both cases used to raise the identical `SegmentError("... publication failed:
# OSError(28, ...)")` at the same errno. The only carrier of the distinction was
# `failed_stage`, an in-RAM attribute that dies with the process and appears in
# no exception, no log and no file — so the distinction the design claimed to
# make was unavailable to the operator who needed it. Two separate classes with
# similar names would recreate exactly that ambiguity, so there is one.


# Sentinel so `root=None` stays meaningful ("deliberately unchecked") while an
# omitted root DERIVES one instead of silently skipping containment.
_DERIVE_ROOT = object()

# SINGLE SOURCE OF TRUTH: every bound below is imported FROM
# `canonical.CapabilityLimits`, not redeclared. Redeclaring the same number
# in two places is exactly how the Decimal-digit defect happened -- this
# module advertised 512 digits while the encoder's real working precision was
# an unrelated 28 -- so nothing here may define its own literal. A test
# (`test_kalshi_encoder_fidelity_harness_001.py`) asserts these are the same
# OBJECT as `CapabilityLimits`'s attributes, not merely equal by coincidence,
# so an admission bound and the encoder's bound can never independently drift
# again for ANY of these shapes: decimal digits, decimal exponent, int bits,
# sequence size, mapping size, string length, or nesting depth.
_MAX_DECIMAL_EXPONENT = CapabilityLimits.MAX_DECIMAL_EXPONENT
_MAX_DECIMAL_DIGITS = CapabilityLimits.MAX_DECIMAL_DIGITS
_MAX_INT_BITS = CapabilityLimits.MAX_INT_BITS
_MAX_SEQUENCE_ELEMENTS = CapabilityLimits.MAX_SEQUENCE_ELEMENTS
_MAX_MAPPING_ELEMENTS = CapabilityLimits.MAX_MAPPING_ELEMENTS
_MAX_STRING_LENGTH = CapabilityLimits.MAX_STRING_LENGTH
_MAX_DEPTH = CapabilityLimits.MAX_DEPTH


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
    # Decided BEFORE acceptance. A value that cannot be canonically represented
    # is a contract violation by the caller, not a writer failure, and the
    # producer has to learn that while it can still do something about it.
    NOT_CANONICAL = "not_canonical"


def canonicalize_or_reason(value, _path: str = ""):
    """Validate AND encode, exactly once. `(canonical_bytes, None)` on
    success, `(None, reason)` on refusal.

    THE INVARIANT: a `None` reason implies the returned bytes are the exact
    bytes `canonical_bytes(x)` would have produced. It holds by CONSTRUCTION,
    because this calls the encoder rather than re-deriving its preconditions.

    Five consecutive review rounds found a hole here and every one was the same
    mistake — a hand-maintained mirror of `canonical.py`'s rules that had
    drifted. float; then the Decimal exponent; then a lone surrogate in a str;
    then an unbounded int; then a lone surrogate in a mapping KEY; then a naive
    datetime; then a datetime at the calendar bound whose `astimezone`
    overflows. Each fix added one more precondition and the next sweep found the
    next gap. Enumeration cannot close a class whose membership is defined
    somewhere else.

    The structural walk below is kept only as a FAST PATH that yields a precise
    diagnostic path (`raw_event.depth[0].price`). The encoder is then run for
    real, and if the two disagree the ENCODER wins — which is the direction
    every one of those five bugs went.

    A4: `_admit` used to call `non_canonical_reason` (which computed and
    discarded these exact bytes) and then have the WRITER THREAD re-encode
    the producer's live object LATER, on its own thread — a live reference
    the producer could mutate in between, and a second encode of a value that
    had already been validated once. Returning the bytes here means the gate
    and the commit are the SAME encode: whatever passed admission is exactly
    what gets queued (see `_admit`'s `parse_canonical(payload_bytes)`).
    """
    try:
        # The WALK is inside the guard too. It was outside, so an exception from
        # `value.items()` or `enumerate(value)` reached the producer raw —
        # `attempted` incremented with no terminal booking, the identity
        # violated, close() refusing to publish, and the whole segment lost with
        # verify_archive reporting VALID. That is this milestone's signature
        # failure, produced by the commit whose thesis was "stop patching the
        # leaf": the `utcoffset()` leaf was guarded and the walk calling it was
        # not.
        structural = _structural_reason(value, _path)
        if structural is not None:
            return None, structural
        payload = canonical_bytes(value)
    except CanonicalError as exc:
        return None, f"{_path or 'value'} is not canonically representable: {exc}"
    except Exception as exc:              # noqa: BLE001 - a refusal, not a crash
        # A hostile `tzinfo` whose `utcoffset()` raises used to make THIS
        # function raise rather than return a reason.
        return None, (f"{_path or 'value'} could not be canonically encoded "
                      f"({type(exc).__name__}: {exc})")
    return payload, None


def non_canonical_reason(value, _path: str = "") -> str | None:
    """Why this value cannot become evidence, or None.

    Thin wrapper over `canonicalize_or_reason` for callers (and tests) that
    only want the refusal reason, not the bytes. `_admit` calls
    `canonicalize_or_reason` directly so it never encodes twice.
    """
    _, reason = canonicalize_or_reason(value, _path)
    return reason


def _structural_reason(value, _path: str = "", _depth: int = 0) -> str | None:
    """Why this value cannot become evidence, or None. Type walk, no encoding.

    `canonical.py` refuses `float` because a float written bare and re-read as
    `Decimal` re-serialises differently, so its digest can never match. That
    refusal is right. What was wrong was WHERE it fired: inside the writer,
    after the producer had been told the event was accepted. An attempt to
    rescue that by coercing floats to `Decimal(repr(f))` at ingress then made it
    worse — `canonical_decimal` quantizes any positive exponent, so a perfectly
    ordinary `1e30` raised `decimal.InvalidOperation`, which is an
    `ArithmeticError` and not a `CanonicalError`, and it killed the writer
    thread and destroyed the hour.

    The venue transport is the correct place to produce `Decimal` — it parses
    the JSON, and `json.loads(..., parse_float=Decimal)` costs nothing there. A
    Python float reaching submission is a contract violation, and this says so
    before anything is accepted.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        return (f"{_path or 'value'} is a float ({value!r}); floats are not "
                "canonically representable. Parse venue JSON with "
                "parse_float=Decimal before submitting.")
    if isinstance(value, Decimal):
        # Bound the exponent HERE, before acceptance. A canonically-typed
        # Decimal with a huge exponent was admitted and then destroyed the whole
        # segment at close, which made the "clean or nothing" close policy
        # indefensible: it is only defensible if admission is total.
        if not value.is_finite():
            return f"{_path or 'value'} is a non-finite Decimal ({value!r})"
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and not (-_MAX_DECIMAL_EXPONENT
                                              <= exponent
                                              <= _MAX_DECIMAL_EXPONENT):
            return (f"{_path or 'value'} has decimal exponent {exponent}, "
                    f"outside the canonical range +/-{_MAX_DECIMAL_EXPONENT}")
        if len(value.as_tuple().digits) > _MAX_DECIMAL_DIGITS:
            return (f"{_path or 'value'} has more than {_MAX_DECIMAL_DIGITS} "
                    "significant digits")
        return None
    if isinstance(value, str):
        # A lone surrogate is legal JSON and venue-controlled, and it was
        # ADMITTED and then killed the segment at close: 501 accepted events
        # lost, `verify_archive` reporting VALID with empty reasons — a total
        # collector failure indistinguishable from an idle collector. Admission
        # is only a contract if it is total.
        try:
            value.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            return (f"{_path or 'value'} is a str that is not UTF-8 encodable "
                    f"({exc.reason} at {exc.start})")
        if len(value) > _MAX_STRING_LENGTH:
            return (f"{_path or 'value'} is a string of length {len(value)}, "
                    f"beyond the canonical bound of {_MAX_STRING_LENGTH} "
                    "characters")
        return None
    if isinstance(value, int):
        # The int twin of the Decimal digit bound, which was added while this
        # was left open. Reachable from any normalizer that computes an integer.
        if value.bit_length() > _MAX_INT_BITS:
            return (f"{_path or 'value'} is an integer of {value.bit_length()} "
                    f"bits, beyond the canonical bound of {_MAX_INT_BITS}")
        return None
    if isinstance(value, datetime):
        # `canonical_datetime` refuses a naive value (it would read as LOCAL
        # time and canonicalise differently per host). Admission has to mirror
        # that precondition or it is not total.
        try:
            offset = value.utcoffset()
        except Exception as exc:          # noqa: BLE001 - hostile tzinfo
            return (f"{_path or 'value'} has a tzinfo whose utcoffset() raised "
                    f"({type(exc).__name__}: {exc})")
        if value.tzinfo is None or offset is None:
            return (f"{_path or 'value'} is a naive datetime; it must be "
                    "timezone-aware to be canonically representable")
        return None
    if isinstance(value, Mapping):
        if _depth >= _MAX_DEPTH:
            return (f"{_path or 'value'} nesting exceeds the canonical depth "
                    f"bound of {_MAX_DEPTH}")
        # BOUNDED, the same way the Sequence branch below always was. Before
        # this, a Mapping's `.items()` had NO counter at all — only the
        # Sequence branch did — so an admission-time Mapping whose iteration
        # never terminates (a hostile `__iter__` that ignores its own
        # `__len__`) hung the admission walk forever. That asymmetry is what
        # this closes: both container branches now count their own elements
        # rather than trusting `__len__`.
        count = 0
        for k, v in value.items():
            count += 1
            if count > _MAX_MAPPING_ELEMENTS:
                return (f"{_path or 'value'} has more than "
                        f"{_MAX_MAPPING_ELEMENTS} elements")
            if not isinstance(k, str):
                return f"{_path or 'value'} has a non-string key {k!r}"
            # KEYS go through the same check as values. The UTF-8 guard was
            # added to the value branch only, so a lone surrogate in a KEY
            # position — which `json.loads` decodes without complaint — was
            # still admitted and still destroyed the whole segment: 501 events
            # lost, `verify_archive` VALID with empty reasons. Patching the
            # leaf instead of the invariant is why this recurred.
            # Recurse into the WALK, not the wrapper. Recursing into the
            # wrapper re-ran `canonical_bytes` over every subtree — 603 full
            # encodes for one orderbook snapshot, 3.4x the bytes re-encoded,
            # end-to-end throughput 946 -> 362 rec/s on venue-controlled input.
            # The invariant is established by the single root-level encode.
            key_problem = _structural_reason(
                k, f"{_path}.<key>" if _path else "<key>", _depth + 1)
            if key_problem is not None:
                return key_problem
            found = _structural_reason(
                v, f"{_path}.{k}" if _path else k, _depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple)) or (
            isinstance(value, Sequence) and not isinstance(value, (str, bytes))):
        if _depth >= _MAX_DEPTH:
            return (f"{_path or 'value'} nesting exceeds the canonical depth "
                    f"bound of {_MAX_DEPTH}")
        # Mirror `_encode`'s Sequence branch — but BOUNDED. Mirroring it
        # faithfully moved unbounded iteration into the pre-acceptance path:
        # `range(10**9)`, or any registered Sequence whose `__getitem__` never
        # raises, walked forever inside `_inflight`, so close() timed out, the
        # writer kept its lock, 200 accepted-and-written records were lost, and
        # verify_archive reported VALID with empty reasons. An admission gate
        # that can fail to return is not total, which is the property the whole
        # design rests on.
        for i, v in enumerate(value):
            if i >= _MAX_SEQUENCE_ELEMENTS:
                return (f"{_path or 'value'} has more than "
                        f"{_MAX_SEQUENCE_ELEMENTS} elements")
            found = _structural_reason(v, f"{_path}[{i}]", _depth + 1)
            if found is not None:
                return found
        return None
    # Anything else: say nothing here and let the encoder decide, so the walk
    # can never be the stricter of the two.
    return None


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
        except CanonicalError as exc:
            # A tampered numeric literal made this PUBLIC per-segment verifier
            # die instead of returning a verdict. `canonical.py` names the
            # anti-pattern: a tamper-evidence path that crashes on
            # attacker-controlled input is fail-open by crash. `verify_archive`
            # caught it one layer up; every direct caller did not.
            return ChainVerdict(False, index, first, last, stream, index,
                                f"record is not canonically encodable: {exc}")
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
    """Explicit stages, because one identity could not say what happened.

    `generated == written + rejected` cannot distinguish a producer cancelled
    before its event was ever taken from an accepted event that was then lost —
    both produce identical counters, and the second is the only one that
    matters. So acceptance is a stage, not an inference, and every event moves
    through exactly one path:

        attempted -> rejected_before_accept
        attempted -> accepted -> written
        attempted -> accepted -> failed_after_accept
        attempted -> accepted -> pending          (only before close completes)

    `pending` is not a counter kept in step with the others — it is measured
    from the queue at close and cross-checked against the identity, so a drift
    between what the writer believes and what is actually undrained is a
    detected failure rather than an invisible one.

    A6: `accepted` is DERIVED, not an independently incremented field. It used
    to be `self.accounting.accepted += 1`, mutated by `_admit` on the producer
    thread AFTER `queue.put_nowait()` had already, durably, committed the
    event to the queue — a real source-line gap an asynchronous exception
    (SIGINT, `PyThreadState_SetAsyncExc`) could land inside, leaving the event
    genuinely queued (and later genuinely WRITTEN) while `accepted` stayed one
    short forever. `disposition_holds()` then failed at close() — not because
    any evidence was wrong, but because a DIAGNOSTIC counter missed an
    increment — and `close()` treated that as grounds to invalidate every
    other record in the segment along with it.

    Defining `accepted := written + failed_after_accept + pending` removes the
    independent counter (and the gap after it) entirely: an event's admission
    outcome is measured from where it actually ended up — the queue, the
    written stream, or the failure count — never tracked separately from that
    and hoped to stay in step. `disposition_holds()` is therefore true by
    construction; the identity it used to prove is now a tautology, which is
    exactly the point (see `close()`'s comment on `admission_holds()`).
    """

    attempted: int = 0
    rejected_before_accept: int = 0
    written: int = 0
    failed_after_accept: int = 0      # accepted, then the writer could not write it
    pending: int = 0                  # accepted, never drained. 0 at a clean close
    rejections: dict = field(default_factory=dict)

    # Not a durability field: a live, read-only hook into the writer's queue
    # depth, so `accepted` reflects events sitting in the queue BEFORE close()
    # ever measures `pending` (which is only populated by `_measure_pending`
    # at close). Reading a queue's size mutates nothing and is safe to call
    # from any thread at any time, so it cannot itself become a source of
    # torn state the way an independently incremented counter was.
    live_queue_depth: object = field(default=None, repr=False, compare=False)

    @property
    def accepted(self) -> int:
        live = self.live_queue_depth() if self.live_queue_depth is not None else 0
        return self.written + self.failed_after_accept + self.pending + live

    def reject_before_accept(self, reason: RejectReason) -> None:
        self.rejections[reason.value] = self.rejections.get(reason.value, 0) + 1
        self.rejected_before_accept += 1

    def fail_after_accept(self, reason: RejectReason) -> None:
        self.rejections[reason.value] = self.rejections.get(reason.value, 0) + 1
        self.failed_after_accept += 1

    def admission_holds(self) -> bool:
        """`attempted == rejected_before_accept + accepted` — a DIAGNOSTIC
        identity. It can still be violated by a producer-side asynchronous
        exception (see the async accounting harness's four windows), because
        `attempted` and `rejected_before_accept` are both mutated on the
        producer thread and no pure-Python critical section can be made
        immune to an interrupt at every possible instruction boundary. What
        changed is that this identity no longer gates `clean()` or `close()`:
        it cannot, because it can never diverge from what is actually
        durable (see `accepted`'s docstring) — only from how well `submit()`
        counted its OWN attempts.
        """
        return self.attempted == self.rejected_before_accept + self.accepted

    def disposition_holds(self) -> bool:
        """Always true. `accepted` IS `written + failed_after_accept +
        pending` (+ whatever is live in the queue right now, while OPEN — see
        `live_queue_depth`) — there is no independently mutated counter left
        for this to compare against, so the comparison this method used to
        perform is now a tautology BY DEFINITION, not an observation that can
        pass or fail. Kept as a method, not deleted, so existing callers stay
        source-compatible."""
        return True

    def reconciles(self) -> bool:
        return self.admission_holds() and self.disposition_holds()

    def clean(self) -> bool:
        """The only state in which a segment may be published as clean
        evidence — gated on the DURABLE/DISPOSITION side only.

        `admission_holds()` is deliberately NOT part of this. It is a
        diagnostic identity about how many `submit()` attempts were counted,
        not about what is durable. A SIGINT may abort a caller and even cost
        `submit()`'s own bookkeeping an increment, but it must never be able
        to turn thousands of already-written, already-verified records into
        an unpublishable segment — that is the defect this replaces.
        """
        return self.pending == 0 and self.failed_after_accept == 0

    def to_dict(self) -> dict:
        return {"attempted": self.attempted,
                "rejected_before_accept": self.rejected_before_accept,
                "accepted": self.accepted, "written": self.written,
                "failed_after_accept": self.failed_after_accept,
                "pending": self.pending, "rejections": dict(self.rejections),
                "admission_holds": self.admission_holds(),
                "disposition_holds": self.disposition_holds(),
                "clean": self.clean()}


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
                 expected_archive_id: str | None = None,
                 max_records: int | None = None,
                 max_age_s: float | None = None,
                 max_bytes: int | None = None,
                 commit_to_head: bool = True):
        if environment not in _ENVIRONMENTS:
            raise SegmentError(f"unknown environment {environment!r}")
        self.environment = environment
        self.segment_id = safe_segment_id(segment_id)
        self.partition_identity = partition_identity
        # ROUTED THROUGH THE SAME BOUNDED CANONICALIZATION CONTRACT as every
        # event `submit()`s. `subscription_metadata` used to bypass admission
        # entirely: it flowed straight from this constructor argument into
        # `build_manifest` -> `digest_hex` -> `canonical_bytes` at close()
        # time, with no normalization above it and nothing to convert a deep
        # `RecursionError` (or, before A3, an unbounded hostile container)
        # into anything but a bare builtin exception out of close(). Gating it
        # HERE, with the identical `non_canonical_reason` admission uses for
        # `envelope_fields`, means there is no separate, weaker metadata
        # serializer: the same contract, the same bounds, the same typed
        # rejection. A failure now surfaces at construction, as a
        # `SegmentError`, before any record is ever accepted.
        metadata = subscription_metadata or {}
        bad = non_canonical_reason(metadata)
        if bad is not None:
            raise SegmentError(
                f"subscription_metadata is not canonically representable: "
                f"{bad}")
        self.subscription_metadata = metadata
        self.archive_identity = archive_identity
        # The archive this writer is configured for. A root that holds a
        # different archive_id is not this archive, however consistent it looks
        # internally — which is what makes "build a history elsewhere and move
        # it into place" detectable rather than invisible.
        self.expected_archive_id = expected_archive_id
        self.commit_to_head = commit_to_head
        # Rotation policy. Deliberately all-optional and off by default: the
        # production thresholds should come from measurement, not from a number
        # invented here. What is NOT acceptable is the previous behaviour, where
        # nothing was committed until process shutdown, so a collector running
        # for a day held every hour open and a SIGKILL lost all of them.
        self.max_records = max_records
        self.max_age_s = max_age_s
        self.max_bytes = max_bytes
        self._opened_monotonic = time.monotonic()
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
        # See `WriterAccounting.live_queue_depth`'s docstring: a read-only
        # hook, not a counter another thread mutates.
        self.accounting.live_queue_depth = self._queue.qsize
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
        # Producers currently INSIDE the admission protocol — between
        # `attempted` and a terminal stage — counted under `_admission`.
        # close() seals, then waits for this to reach zero, so it can never
        # reconcile against counters a producer is still moving.
        self._inflight = 0
        self._sealed = False
        self.queue_high_water = 0
        self.last_rejection_detail: str | None = None
        # A6 diagnostic: set at close() if `WriterAccounting.admission_holds()`
        # is false. Never gates publication — see `_close_stages`.
        self.admission_drift = False
        self.admission_drift_detail: dict | None = None
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

        A6: `attempted` and `rejected_before_accept`, both mutated here on the
        PRODUCER thread, are DIAGNOSTIC counters — how many calls were made
        and how many were explicitly refused. Neither can be made immune to an
        asynchronous exception landing between two Python statements (the
        four windows the async accounting harness reproduces: after
        `attempted` moves but before `_inflight` does; inside this method's
        own exception handler while it books a rejection; and their
        variants) — no pure-Python critical section can promise that. What
        makes that acceptable is `accepted` (see `WriterAccounting`): it is
        DERIVED from `written`/`failed_after_accept`/`pending`, never from a
        counter this method increments, so nothing this method's own
        bookkeeping misses can ever touch the durable/queued state `close()`
        actually gates on. A missed increment here is a diagnostic gap, not
        data loss, and `close()` no longer treats the two as the same thing.
        """
        # ENTER the admission protocol. `attempted` and `_inflight` move
        # together under one lock, and `_inflight` is not released until this
        # call has reached a terminal stage. Previously `generated` was
        # published to the reconciliation identity before the event had any
        # outcome, so close() could evaluate a TORN counter: one collector
        # thread plus a shutdown handler destroyed the segment 18 times in 30.
        with self._admission:
            if self._sealed:
                # Counted INSIDE `_inflight` too. A post-seal rejection used to
                # move `attempted` after close() had observed `_inflight == 0`,
                # so close() reconciled against counters that were still
                # changing and refused provably clean segments 8-18% of the time
                # under contention.
                self._inflight += 1
                try:
                    with self._lock:
                        self.accounting.attempted += 1
                        self.accounting.reject_before_accept(
                            RejectReason.SHUTDOWN_IN_PROGRESS)
                finally:
                    self._inflight -= 1
                return RejectReason.SHUTDOWN_IN_PROGRESS
            with self._lock:
                self.accounting.attempted += 1
            # FAULT-WINDOW: window-a — a fault landing on the NEXT line, after
            # `attempted` is durably counted but before `_inflight` is, is
            # outside the `try` below entirely and escapes straight to the
            # caller. `attempted` is now one ahead of what `rejected_before_
            # accept`/`accepted` will ever show for this call. Diagnostic-only
            # (see this method's docstring): nothing was queued, nothing was
            # written, there is no durable evidence to lose.
            self._inflight += 1
        try:
            # FAULT-WINDOW: safe-before-admit — a fault landing on the NEXT
            # line is fully covered by this `try`: `attempted` has moved, and
            # the `except` below books a matching `rejected_before_accept`
            # for it in the SAME call. A true negative — admission_holds()
            # must stay true.
            return self._admit(envelope_fields)
        except BaseException:
            # THE IDENTITY IS ENFORCED HERE, not inside the gate. Six rounds
            # were spent hardening `non_canonical_reason` so it could never
            # raise, and each round found another way in — a hostile dunder, a
            # naive datetime, a mapping key, and finally a real SIGINT, which no
            # amount of `except Exception` can catch. Booking a terminal state
            # on the exceptional exit keeps the DIAGNOSTIC identity as close to
            # true as a single Python statement can, and demotes any future
            # gate defect from data loss to a diagnostic-quality problem.
            with self._lock:
                # FAULT-WINDOW: window-d — a SECOND fault landing on the NEXT
                # line, while this handler is still booking the FIRST fault's
                # rejection, loses that booking too: `attempted` moved for the
                # original call, and now neither `rejected_before_accept` nor
                # `accepted` will ever reflect it. Still diagnostic-only: the
                # original call never reached the queue (that gate is gone by
                # the time `_admit` raises, or the payload was refused before
                # ever being queued), so nothing durable is unaccounted for.
                self.accounting.reject_before_accept(
                    RejectReason.SERIALIZATION_FAILURE)
                # FAULT-WINDOW: already-terminal — a fault landing HERE, after
                # the line above completed, is the true-negative twin of
                # window (d): the booking already happened, there is nothing
                # left for a second fault to interrupt.
            raise
        finally:
            with self._admission:
                self._inflight -= 1

    def _reject(self, reason: RejectReason) -> RejectReason:
        with self._lock:
            self.accounting.reject_before_accept(reason)
        return reason

    def _admit(self, envelope_fields: dict) -> RejectReason | None:
        if self._writer_error is not None:
            return self._reject(RejectReason.WRITER_FAILED)
        if self.state is SegmentState.INVALID:
            return self._reject(RejectReason.SEGMENT_INVALID)
        if self.state is not SegmentState.OPEN:
            return self._reject(RejectReason.SEGMENT_NOT_OPEN)
        # Canonical admissibility is decided BEFORE acceptance, and decided
        # ONCE: `canonicalize_or_reason` computes `canonical_bytes` and this
        # keeps it, rather than discarding it and having the writer thread
        # encode the producer's live object again later. A value the writer
        # cannot serialise used to be discovered after the producer had been
        # told the event was recorded, and the only outcomes left were a
        # silent drop or destroying the whole hour. The contract is uniform
        # with `canonical.py`: a float is refused, and it is refused here.
        payload_bytes, bad = canonicalize_or_reason(envelope_fields)
        if bad is not None:
            self.last_rejection_detail = bad
            return self._reject(RejectReason.NOT_CANONICAL)
        # A4: IMMUTABLE SUBMISSION. `parse_canonical` reconstructs a FRESH
        # object graph from the accepted bytes — new dicts, new lists, new
        # strings — sharing NO reference with the caller's `envelope_fields`.
        # This is what gets queued and, later, what the writer thread encodes
        # on its own thread. A producer that mutates its own `envelope_fields`
        # after `submit()` returns cannot reach the accepted evidence: the
        # exact bytes accepted are the exact bytes that will be committed,
        # subject only to the writer's deterministic archive framing
        # (`build_record` wrapping this value with `receive_ordinal` and
        # `previous_record_digest`, assigned in write order).
        accepted_fields = parse_canonical(payload_bytes)
        try:
            # FAULT-WINDOW: safe-before-enqueue — a fault landing on the NEXT
            # line, before `put_nowait` ever runs, means nothing was queued;
            # `submit()`'s handler books the matching rejection. A true
            # negative — admission_holds() must stay true.
            self._queue.put_nowait(accepted_fields)
        except queue.Full:
            pass
        else:
            # FAULT-WINDOW: window-b — a fault landing on the NEXT line, after
            # the item is durably in the queue (and so WILL be written), used
            # to land between a successful `put_nowait` and a SEPARATE
            # `accepted += 1`, making `written` exceed `accepted` once the
            # item drained. There is no such statement here any more:
            # `accepted` is derived from `written`/`failed_after_accept`/
            # `pending` (see `WriterAccounting`), so there is nothing left
            # for this window to desynchronise — a fault here can only cost
            # `_note_depth`'s high-water bookkeeping and this call's own
            # diagnostic booking, never the item itself.
            self._note_depth()
            return None
        # Queue full: wait OUTSIDE the admission gate, so N producers do not
        # serialise behind each other's full timeouts (8 producers at a 0.5s
        # timeout measured 4.58s per submit, and close() waited 4.55s before it
        # could even declare CLOSING). `_inflight` still covers this wait, so
        # close() cannot seal while it is outstanding.
        try:
            self._queue.put(accepted_fields, timeout=self._enqueue_timeout)
        except queue.Full:
            return self._reject(RejectReason.ENQUEUE_TIMEOUT)
        self._note_depth()
        return None

    def _note_depth(self) -> None:
        with self._lock:
            # FAULT-WINDOW: window-c (note-depth-l1) — the event is already
            # durably queued by the time any statement in this method runs.
            # A fault landing at any of the three points marked in this
            # method used to reach `_admit`'s caller and get double-booked
            # `rejected_before_accept` even though `accepted` (the OLD,
            # independently-tracked field) had already been incremented.
            # `queue_high_water` is diagnostic bookkeeping only; losing an
            # update to it costs nothing durable.
            depth = self._queue.qsize()
            # FAULT-WINDOW: window-c (note-depth-l2)
            if depth > self.queue_high_water:
                # FAULT-WINDOW: window-c (note-depth-l3)
                self.queue_high_water = depth

    @property
    def rotation_due(self) -> bool:
        """Has this segment reached a policy bound and become due for commit?

        Cheap and side-effect free, so a collector can ask on every event. The
        thresholds are policy inputs rather than a hard-coded cadence, and with
        none set this is always False — the caller decides, and gets a
        deterministic answer either way.
        """
        if self.state is not SegmentState.OPEN:
            return False
        if self.max_records is not None and self.accounting.accepted >= self.max_records:
            return True
        if self.max_age_s is not None and (
                time.monotonic() - self._opened_monotonic) >= self.max_age_s:
            return True
        if self.max_bytes is not None:
            try:
                if self.events_path.stat().st_size >= self.max_bytes:
                    return True
            except OSError:
                return False
        return False

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
                    self.accounting.fail_after_accept(RejectReason.WRITER_FAILED)
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
        except Exception:                       # noqa: BLE001 - booked, not raised
            # Any serialisation failure, not only CanonicalError. Catching the
            # narrow type let `decimal.InvalidOperation` — an ArithmeticError —
            # escape into the writer thread and destroy the whole segment over
            # one payload. Admission already refuses non-canonical values, so
            # reaching here is a defect; it must still be contained.
            with self._lock:
                self.accounting.fail_after_accept(
                    RejectReason.SERIALIZATION_FAILURE)
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
            self._seal_admissions()
            if self.state is SegmentState.INVALID:
                # Seal the disposition too. This branch short-circuited before
                # the drain ever ran, so a writer-thread failure left every
                # still-queued event — 197 of 200 in the reviewer's probe — in
                # no terminal stage at all, and whether it did depended on a
                # race between two lines in `_run`.
                self._shutdown.set()
                thread = getattr(self, "_thread", None)
                if thread is not None and thread.is_alive():
                    thread.join(timeout=1.0)
                self._measure_pending()
                # Release here too. This is the path a mid-stream writer error
                # takes, and it was the one path still leaking ownership —
                # leaving the partition unwritable by every future process.
                self._release_lock()
                raise SegmentError(
                    f"segment is INVALID and cannot be closed: "
                    f"{self._writer_error!r} {self.accounting.to_dict()}")
            return self._close_locked()

    def _seal_admissions(self) -> None:
        """Stop new admissions, then WAIT for the ones already inside.

        Steps 1 and 2 of the close sequence, and the reason they are separate:
        a producer between "attempted" and its terminal stage is not visible in
        `_pending_waiters` (which only counted producers blocked on a full
        queue), so close() reconciled against a counter that was still moving.
        Sealing is idempotent — close() is reachable from several threads.
        """
        with self._admission:
            self._sealed = True
        deadline = time.monotonic() + self._enqueue_timeout + 5.0
        while True:
            with self._admission:
                if self._inflight == 0:
                    return
            if time.monotonic() > deadline:
                # Never silently continue with producers still inside. The
                # accounting that follows would be exactly the torn snapshot
                # this exists to prevent.
                raise SegmentError(
                    f"{self._inflight} producer(s) are still inside the "
                    "admission protocol after the seal deadline; refusing to "
                    "reconcile against a counter that is still moving")
            time.sleep(0.002)

    def _measure_pending(self) -> int:
        """Whatever the writer never drained. Measured, not inferred."""
        drained = 0
        while True:
            try:
                self._queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        with self._lock:
            self.accounting.pending += drained
        return drained

    def _close_locked(self) -> dict:
        try:
            return self._close_stages()
        except BaseException:
            # Ownership must not leak on ANY failure path, or one mid-stream
            # write error locks the partition out for every future process.
            self._release_lock()
            raise

    def _close_stages(self) -> dict:
        # Admissions are already sealed and every producer that was inside the
        # protocol has reached a terminal stage (close() did that before this
        # runs). The acceptance counters are therefore frozen from here on, and
        # the reconciliation below is against a snapshot that cannot move.
        with self._admission:
            self.state = SegmentState.CLOSING
            self._shutdown.set()
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
        # Whatever the writer never drained is PENDING, and it is measured from
        # the queue rather than inferred from the difference between counters —
        # so a drift between what the writer believes and what is actually
        # undrained is detected here instead of disappearing into the identity.
        # ONE snapshot, taken under the lock. `reconciles()`/`clean()` read
        # three fields without synchronisation, so the reconciliation could
        # observe a torn triple even when every identity held.
        self._measure_pending()
        with self._lock:
            snapshot = WriterAccounting(**{
                k: v for k, v in vars(self.accounting).items()})
        if self._writer_error is not None:
            self.state = SegmentState.INVALID
            raise SegmentError(f"writer failed: {self._writer_error!r} "
                               f"{self.accounting.to_dict()}")
        # `clean` is the ONLY state in which this may be published as evidence,
        # and it is gated on the DURABLE side of the identity only —
        # `pending == 0 and failed_after_accept == 0` — never on
        # `admission_holds()`. An accepted-but-unwritten event is a loss the
        # producer was told did not happen, and it must never appear behind
        # close_status "clean": that check stays fatal.
        if not snapshot.clean():
            self.state = SegmentState.INVALID
            raise SegmentError(
                f"{snapshot.failed_after_accept} accepted event(s) were "
                f"not written and {snapshot.pending} were never drained; "
                "refusing to publish this segment as a clean close: "
                f"{snapshot.to_dict()}")
        # A6 CORE PRINCIPLE: the durable/queued event state is authoritative;
        # a DIAGNOSTIC counter must never be able to make already-durable
        # evidence disappear. `admission_holds()` can still be false — a real
        # asynchronous exception can land between two Python statements in
        # `submit()`'s own bookkeeping (see the four windows the async
        # accounting harness reproduces) — but nothing that can happen there
        # can touch `written`, `failed_after_accept` or `pending`, because
        # `accepted` is DERIVED from them rather than tracked by a fourth,
        # independently-mutated counter (see `WriterAccounting`). A drift here
        # means `submit()` under-counted its OWN attempts or rejections; it
        # never means an event was fabricated, lost, or double-counted in the
        # evidence itself. It is recorded, not silently dropped, and it does
        # NOT block publication — the exact defect this replaces destroyed
        # thousands of good records over a diagnostic-quality gap.
        self.admission_drift = not snapshot.admission_holds()
        if self.admission_drift:
            self.admission_drift_detail = snapshot.to_dict()

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
        try:
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
        except CanonicalError as exc:
            # `subscription_metadata` is admitted at construction (above), so
            # reaching here means it was mutated in place after admission --
            # the same live-reference class of defect `submit()` has (out of
            # this remediation's scope to fix generally), just on the
            # metadata path instead of an event payload. Whatever the cause,
            # `build_manifest` -> `digest_hex` -> `canonical_bytes` is now
            # BOUNDED (A3): it can never escape as a bare `RecursionError` or
            # an unbounded hang, only as a `CanonicalError` here, which this
            # normalises into the same typed failure every other close()-time
            # defect surfaces as.
            self.state = SegmentState.INVALID
            self._writer_error = exc
            self._release_lock()
            raise SegmentError(
                f"subscription_metadata could not be canonically digested "
                f"at close: {exc!r}") from exc
        try:
            publish_manifest(self.dir, manifest, stage=self._stage)
        except DurabilityNotProven as exc:
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
            independent = verify_segment(self.dir, environment=self.environment,
                                         root=self.root)
            if not independent.valid:
                self.state = SegmentState.INVALID
                self._release_lock()
                raise SegmentError(
                    "segment does not verify after publication; refusing to "
                    f"commit it to the archive head: {independent.reasons}")
            try:
                commit_segment(self.root, self.environment, manifest=manifest,
                               expected_archive_id=self.expected_archive_id,
                               stage=self._stage)
            except DurabilityNotProven:
                # Committed and visible; only its durability is unproven. NOT
                # an orphan — reporting it as one sent an operator to hunt a
                # graft that does not exist while never naming the real
                # condition.
                self.state = SegmentState.INVALID
                self._release_lock()
                raise
            except BaseException as exc:
                # Not every failure here is the same durable state. The
                # generation record and the current-head pointer are two
                # separate durable writes inside `commit_segment` -- the
                # record first, the pointer second -- so a failure can land
                # in either window, and only ONE of them is genuinely an
                # orphan. Ask the filesystem which state actually happened
                # instead of assuming the second (rarer, worse) one every
                # time.
                self.state = SegmentState.INVALID
                self._release_lock()
                if _durable_generation_commits(self.root, self.environment,
                                               manifest["segment_id"]):
                    # The generation record durably names this segment; only
                    # the current-head pointer failed to advance. This is
                    # STALE_HEAD, and `recover_current_head` -- the
                    # `archive-recover-head` operator command -- finishes the
                    # transition deterministically. No adopt/discard decision
                    # applies here; there is nothing ambiguous to decide.
                    raise StaleHeadAfterCommitError(
                        f"segment {manifest['segment_id']!r} is already "
                        "committed by a durable generation record; only the "
                        "archive head pointer failed to advance at stage "
                        f"{self.failed_stage!r} ({exc!r}). Run "
                        "recover_current_head (the 'archive-recover-head' "
                        "operator command) to finish this transition -- do "
                        "not adopt or discard this segment.") from exc
                # No generation record commits this segment yet: the manifest
                # is committed evidence the history does not mention at all.
                # That is the genuinely ambiguous ORPHANED_COMMITTED_SEGMENT
                # case `verify_archive` reports, and it requires an explicit
                # operator decision.
                raise OrphanedCommittedSegmentError(
                    f"archive head update failed after segment commit at stage "
                    f"{self.failed_stage!r} (ORPHANED_COMMITTED_SEGMENT): "
                    f"{exc!r}") from exc
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
        if fd is None:
            return
        # The lock FILE is never unlinked. Unlinking on release was a defect
        # dressed as tidiness: the unlink sat outside the `fd is not None`
        # guard, so a SECOND call to this method — which close() on an INVALID
        # writer makes on every invocation — deleted whatever was at that path,
        # including a live successor's lock file. The next writer then created
        # a fresh inode, took its flock trivially, and two owners appended to
        # one segment: six records in, none readable out.
        #
        # Ownership is the flock. A stale lock file is inert, and the kernel
        # drops the flock when the holder dies, so nothing needs cleaning up.
        try:
            os.close(fd)
        except OSError:
            pass
        self._lock_fd = None

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
    try:
        _s("directory_fsync")
        _fsync_directory(directory)
    except OSError as exc:
        raise DurabilityNotProven(
            f"{final} was renamed into place and IS visible to readers, but the "
            f"directory fsync that would prove it survives a crash failed: "
            f"{exc!r}. The segment is committed; its durability is not proven.")
    return final


_SALVAGE_CHUNK = 512
# `<partition>.rNNNN` — a rotated segment of the same partition.
_ROTATION_SUFFIX_RE = re.compile(r"\.r\d{4,}$")


def _decompress_prefix(data: bytes):
    """Decompress as far as the stream is intact. Returns (bytes, consumed, eof).

    The previous attempt at this was dead code, and the measurement said so: it
    caught the failing 64 KiB chunk and re-fed it in 512-byte slices **into the
    same decompressobj**, which is permanently in error state once it has
    raised. Every retry iteration raised immediately and contributed nothing, so
    a mid-stream fault still lost the whole chunk — 664 records recovered where
    998 were available, and a small segment recovered 0. The suite asserted no
    recovered count, so nothing caught it.

    A `decompressobj` is never reused after it raises. The fast path feeds large
    chunks; on the first fault the object is DISCARDED and a fresh one re-reads
    from the start in small increments, so the recovered prefix is bounded by
    the salvage chunk rather than by the fast-path chunk.
    """
    import zlib

    dec = zlib.decompressobj(31)
    out = []
    try:
        for i in range(0, len(data), 65536):
            out.append(dec.decompress(data[i:i + 65536]))
        out.append(dec.flush())
    except (zlib.error, EOFError):
        return _salvage_prefix(data)
    consumed = len(data) - len(dec.unused_data) if dec.eof else len(data)
    return b"".join(out), consumed, dec.eof


def _salvage_prefix(data: bytes):
    import zlib

    dec = zlib.decompressobj(31)
    out = []
    for i in range(0, len(data), _SALVAGE_CHUNK):
        try:
            out.append(dec.decompress(data[i:i + _SALVAGE_CHUNK]))
        except (zlib.error, EOFError):
            break                    # terminal: STOP, never reuse this object
        if dec.eof:
            break
    if dec.eof:
        try:
            out.append(dec.flush())
        except (zlib.error, EOFError):
            pass
        consumed = len(data) - len(dec.unused_data)
        return b"".join(out), consumed, True
    return b"".join(out), len(data), False


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
    try:
        if not events_path.is_file():
            return []
        data = events_path.read_bytes()
    except OSError:
        # Mode-0, a directory in place of the file, or any other filesystem
        # refusal. A reader that raises here takes the whole verdict down.
        return []
    text = ""
    while data:
        decoded, consumed, eof = _decompress_prefix(data)
        try:
            text += decoded.decode("utf-8")
        except UnicodeDecodeError:
            text += decoded.decode("utf-8", errors="ignore")
        if not eof:
            break                    # stream ended mid-member: nothing follows
        data = data[consumed:] if consumed else b""
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

# --- Gate 5: the archive head -----------------------------------------------------
#
# The head protocol lives in `archive_head.py` now. It used to be an
# append-only JSONL log next to the evidence, and that shape produced two
# whole classes of defect that could not be patched out of it: truncating the
# log's tail rolled history back for free, and a torn final line poisoned every
# future append. Both are gone with the file.
#
# What replaced it: an explicit genesis marker minted once by an init operation
# the collector cannot reach, immutable create-once records one per generation,
# and a small current-head pointer. The collector consumes an initialized
# archive and can never bring one into existence.

from app.realtime.archive_head import (             # noqa: E402
    ARCHIVE_LOCK_FILENAME,
    CURRENT_HEAD_FILENAME,
    GENESIS_FILENAME,
    HEADS_DIRNAME,
    ArchiveHeadError,
    ArchiveIdentityMismatch,
    ArchiveNotInitializedError,
    DurabilityNotProven,
    HeadRecoveryRequired,
    archive_lock,
    commit_segment,
    current_head_path,
    fold_segments_digest,
    generation_path,
    genesis_path,
    head_digest_of,
    head_state,
    heads_dir,
    initialize_archive,
    present_generations,
    read_generation,
    load_authoritative_head,
    present_generations,
    read_current_head,
    read_genesis,
    read_generation,
    verify_transition,
    recover_current_head,
    segment_commitment,
)



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
    # A rotated segment stays in ITS partition — rotation is about bounding how
    # much uncommitted evidence a crash can cost, not about moving where the
    # evidence lives. The suffix distinguishes segment ids, which are immutable
    # and may never be reopened; the partition is unchanged by it.
    hour = _ROTATION_SUFFIX_RE.sub("", hour)
    if not (venue and date and hour) or "-" not in date:
        return None
    return f"env={environment}/venue={venue}/date={date}/hour={hour}"


def _presence(path) -> tuple:
    """(present, reason). Never raises — a verdict function cannot afford to.

    Delegates to `evidence_fs.presence`, the one primitive every canonical
    archive module now shares. `Path.exists()` propagates EACCES;
    `os.path.lexists` swallows it and answers False, which is a different
    lie — that distinction is exactly why this indirection exists rather
    than every module re-deriving it from `os.lstat` on its own.
    """
    return evidence_fs.presence(path)


def verify_segment(directory, *, environment: str, allow_open: bool = False,
                   root=_DERIVE_ROOT) -> SegmentVerdict:
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
    #
    # The root is DERIVED when the caller does not name one, because making the
    # safe behaviour the thing you have to remember to ask for is how the one
    # in-module production caller ended up omitting it. `<root>/env=<name>/
    # segment=<id>` is the layout, so the root is two levels up.
    if root is _DERIVE_ROOT:
        root = directory.parent.parent
    if root is not None:
        for path in (events_path, manifest_path):
            present, why = _presence(path)
            if why is not None:
                return SegmentVerdict(seg_id, SegmentState.INVALID, False, [why])
            if not present:
                continue
            reason = containment_reason(root, path)
            if reason is not None:
                return SegmentVerdict(
                    seg_id, SegmentState.INVALID, False,
                    [f"{path.name} is not bounded by the archive root: {reason}"])

    # THESE are the calls that actually raise. The previous fix wrapped
    # `os.path.lexists`, which swallows OSError internally and can never raise,
    # so the guard was unreachable dead code nine lines above the defect — and
    # `lexists` returning False on EACCES also made the containment loop skip
    # past a path it could not stat. Two reviewers reproduced it verbatim.
    has_events, why = _presence(events_path)
    if why is not None:
        return SegmentVerdict(seg_id, SegmentState.INVALID, False, [why])
    manifest_present, why = _presence(manifest_path)
    if why is not None:
        return SegmentVerdict(seg_id, SegmentState.INVALID, False, [why])
    if not manifest_present:
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

    manifest_bytes, why = evidence_fs.bounded_read(manifest_path)
    if why is not None:
        return SegmentVerdict(seg_id, SegmentState.INVALID, False,
                              [f"manifest is unreadable ({why})"])
    try:
        manifest = parse_canonical(manifest_bytes)
    except Exception as exc:
        return SegmentVerdict(seg_id, SegmentState.INVALID, False,
                              [f"manifest is unreadable ({type(exc).__name__})"])
    if not isinstance(manifest, dict):
        # Valid JSON, wrong container. `archive_head._read_json` already carried
        # this guard; the one artifact a reader is handed directly did not.
        return SegmentVerdict(
            seg_id, SegmentState.INVALID, False,
            [f"manifest is a {type(manifest).__name__}, not an object"])

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

    try:
        size = events_path.stat().st_size
        actual_digest = file_sha256(events_path)
    except OSError as exc:
        # A mode-0 or directory events file used to raise straight out of the
        # public verifier and out of `recover_current_head`, so the documented
        # repair path died with a raw OSError that no operator tool catches.
        reasons.append(f"event file is unreadable ({type(exc).__name__}: {exc})")
        v.valid = False
        v.state = SegmentState.INVALID
        return v
    v.file_size_match = size == manifest.get("event_file_size_bytes")
    v.file_digest_match = actual_digest == manifest.get("event_file_sha256")
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
    # Decoded records are attacker-controllable bytes on disk; a gzip of
    # `null` or `[1,2]` made this raise AttributeError. "Total" is a stronger
    # claim than "handles EACCES", and only the latter had been tested.
    times = [r.get("received_at_utc") for r in records
             if isinstance(r, Mapping) and isinstance(r.get("received_at_utc"), str)]
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
    meta = manifest.get("subscription_metadata")
    if meta is not None and not isinstance(meta, Mapping):
        reasons.append("subscription_metadata is not an object")
        meta = None
    meta_venue = (meta or {}).get("venue")
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



# --- Gate 6: whole-archive verification -------------------------------------------


_VERDICT_KEYS = (
    "environment", "archive_id", "head_state", "head_generation", "head_digest",
    "generations_present", "segments", "closed_segments", "open_segments",
    "uncommitted_segments", "abandoned_segments", "invalid_segments",
    "orphaned_committed_segments", "records_expected", "records_read",
    "segment_verdicts", "reasons", "warnings", "missing_committed_segments",
    "verdict",
)


def _abandoned_residue(env_dir: Path) -> list:
    """Quarantined crash residue anywhere under the environment root.

    Enumeration goes through `evidence_fs.safe_enumerate`, not `Path.glob`:
    `Path.glob` swallows the `PermissionError` `os.scandir` raises
    internally, so a mode-0 directory returned an empty list with no error
    and 7,000 bytes of real residue vanished under a VALID verdict — the
    per-file guard below was right; the ENUMERATION guard never fired
    because it could never fire.

    A symlinked segment directory is reported as an incomplete scan rather
    than silently skipped (the twin of the fix in `_verify_archive_inner`):
    residue hidden behind a symlinked directory is neither confirmed absent
    nor examined, and a scan that cannot tell the difference must say so.
    """
    out, errors = [], []
    children, err = evidence_fs.safe_enumerate(env_dir, "segment=*")
    if err is not None:
        return [], [err]
    for d in sorted(children):
        if d.is_symlink():
            errors.append(
                f"{d.name}: symlinked segment directory is not bounded by "
                "the archive root and is not scanned for quarantined evidence")
            continue
        if not d.is_dir():
            continue
        try:
            # `os.scandir` here, not `evidence_fs.safe_enumerate`: the filter
            # is on FILENAME PREFIX, not a glob pattern, and this loop already
            # handles its own OSError -- see the identical honesty argument in
            # `safe_enumerate`'s docstring, which is what this call mirrors.
            files = sorted(
                Path(e.path) for e in os.scandir(d)
                if e.name.startswith(f"{EVENTS_FILENAME}.abandoned."))
        except OSError as exc:
            errors.append(f"{d.name}: {exc!r}")
            continue
        for f in files:
            # Per FILE. The guard was outside both loops, so one dangling
            # symlink dropped 7,000 bytes of real residue in LATER directories
            # and the sentinel it left was then counted as a file — a wrong
            # count and a wrong byte total, which is worse than the silent
            # truncation it replaced.
            try:
                size = f.stat().st_size
            except OSError as exc:
                errors.append(f"{d.name}/{f.name}: {exc!r}")
                continue
            out.append({"segment_id": d.name.split("segment=", 1)[-1],
                        "file": f.name, "bytes": size})
    return out, errors


def _empty_verdict(environment: str, **over) -> dict:
    """One shape, always.

    The early returns used to each build their own dict, so the four states an
    operator dashboard most needs to query — the ones reached when the head is
    unreadable — were exactly the ones missing `abandoned_segments` and
    `head_generation`. A `KeyError` in the monitoring path is not a monitoring
    path.
    """
    out = {"environment": environment, "archive_id": None,
           "head_state": None, "head_generation": None, "head_digest": None,
           "generations_present": [], "segments": 0, "closed_segments": 0,
           "open_segments": 0, "uncommitted_segments": [],
           "abandoned_segments": [], "invalid_segments": 0,
           "orphaned_committed_segments": [], "records_expected": 0,
           "records_read": 0, "segment_verdicts": [], "reasons": [],
           "warnings": [], "missing_committed_segments": [],
           "verdict": "INVALID"}
    out.update(over)
    return out


def verify_archive(root, *, environment: str, expected_archive_id: str | None = None,
                   minimum_generation: int | None = None,
                   expected_head: tuple | None = None) -> dict:
    """Verify the archive against its COMMITTED history, rooted in its genesis.

    The history is reconstructed by walking the generation chain from 0 to N and
    applying each delta, and every step must be exactly one valid transition
    from the one before it. That is what stops a reduced history from being
    re-minted: dropping a segment drops a GENERATION, and a no-op generation
    minted to restore the counter contradicts its own segment count.

    `minimum_generation` and `expected_head` are the hooks for an anchor
    OUTSIDE this root. Everything else lives under a directory the writer can
    write. A monitor that remembers `(generation, head_digest)` pins content;
    one that remembers only the generation pins a counter, which is weaker and
    is why both are accepted.

    This function does not raise. Corruption is a verdict.
    """
    try:
        return _verify_archive_inner(
            root, environment=environment, expected_archive_id=expected_archive_id,
            minimum_generation=minimum_generation,
            expected_head=expected_head)
    except Exception as exc:                  # noqa: BLE001 - reported as a verdict
        # A verifier that raises cannot report corruption, and every caller that
        # catches only the domain error turns tampering into a traceback. Five
        # shapes reached this before it existed: a mode-0 or directory events
        # file, a mode-0 segment directory, a mode-0 `heads/`, and a manifest
        # that is valid JSON but not an object.
        # Carry what the head DOES state, so a partial-visibility failure never
        # reports "records_expected: 0" — that asserts "nothing was lost" by
        # omission, which is the class this milestone exists to remove.
        # A RECORD count, summed over the generation chain. The first attempt
        # put `segment_count` here — a 12-segment / 6000-record archive reported
        # `records_expected: 12`, which the facade then turned into a shortfall
        # of 12. `0` read as "unknown"; `12` reads as a small, real, nearly
        # satisfied expectation. That is a worse lie, on a path no test in the
        # module ever executed.
        expected_records = None
        try:
            rec = head_state(root, environment,
                             expected_archive_id=expected_archive_id)
            if rec.get("state") == "CURRENT":
                total = 0
                for gen in range(1, rec["head"].generation_record["generation"] + 1):
                    total += read_generation(
                        root, environment, gen).get("committed_record_count") or 0
                expected_records = total
        except Exception:                     # noqa: BLE001 - best effort only
            expected_records = None
        return _empty_verdict(
            environment, head_state="VERIFICATION_FAILED",
            records_expected=expected_records,
            reasons=[f"verification could not complete: "
                     f"{type(exc).__name__}: {exc}"])


def _verify_archive_inner(root, *, environment: str, expected_archive_id,
                          minimum_generation, expected_head) -> dict:
    root = Path(root)
    env_dir = root / f"env={environment}"
    env_present, env_why = evidence_fs.presence(env_dir)
    if env_why is not None:
        return _empty_verdict(
            environment, head_state="ROOT_UNREADABLE", reasons=[env_why])
    if env_dir.is_symlink():
        return _empty_verdict(
            environment, head_state="ROOT_NOT_CONTAINED",
            reasons=[f"{env_dir} is a symlink; the archive root does not bound "
                     "this evidence"])
    reason = containment_reason(root, env_dir) if env_present else None
    if reason is not None:
        return _empty_verdict(environment, head_state="ROOT_NOT_CONTAINED",
                              reasons=[reason])

    state = head_state(root, environment, expected_archive_id=expected_archive_id)
    discovered = {}
    # A symlinked segment directory is FATAL, not invisible. It used to be
    # skipped by this same loop (`d.is_symlink(): continue`) before `d` was
    # ever added to `discovered`, so it never reached the orphan check below
    # -- a grafted (uncommitted) segment directory was `ORPHANED_COMMITTED_
    # SEGMENT` as a real directory and silently VALID, zero reasons, as a
    # symlink to identical content. Enumeration itself goes through
    # `evidence_fs.safe_enumerate`, not `Path.glob`, for the same reason
    # `_abandoned_residue` does: `Path.glob` swallows the `PermissionError`
    # `os.scandir` raises internally, so an execute-only environment
    # directory returned an empty list with no error and every segment in it
    # vanished from accounting with no diagnostic that anything was missed.
    symlinked_segments: list = []
    if env_dir.is_dir():
        children, err = evidence_fs.safe_enumerate(env_dir, "segment=*")
        if err is not None:
            return _empty_verdict(
                environment, head_state="ROOT_UNREADABLE", reasons=[err])
        for d in sorted(children):
            if d.is_symlink():
                symlinked_segments.append(d.name.split("segment=", 1)[-1])
                continue
            if not d.is_dir():
                continue
            discovered[d.name.split("segment=", 1)[-1]] = d

    symlink_reasons = [
        f"SYMLINKED_SEGMENT_DIRECTORY: {seg!r} is a symlink; the archive "
        "root does not bound this evidence, so it is never adopted as "
        "committed or uncommitted evidence -- resolve it explicitly before "
        "this archive can verify"
        for seg in symlinked_segments]

    if state["state"] != "CURRENT":
        return _empty_verdict(
            environment, head_state=state["state"],
            archive_id=state.get("archive_id"),
            generations_present=state.get("generations", []),
            reasons=[f"{state['state']}: {state.get('reason', '')}"]
            + symlink_reasons)

    head = state["head"]
    record = head.generation_record
    archive_id = head.genesis["archive_id"]
    generations = state["generations"]
    reasons: list = list(symlink_reasons)

    if minimum_generation is not None and record["generation"] < minimum_generation:
        reasons.append(
            f"HISTORY_TRUNCATED: the archive is at generation "
            f"{record['generation']} but was last observed at generation "
            f"{minimum_generation}; the tail of this history is missing")
    # `expected_head` is a (generation, digest) PAIR. The first version of this
    # compared only against the CURRENT head while its message claimed "at or
    # before this generation", so one honest commit after the anchor was taken
    # produced a false HISTORY_REWRITTEN and no monitor could use it on a live
    # archive. Because generations chain, pinning generation g pins everything
    # 0..g while permitting honest growth past it.
    anchor_gen = anchor_digest = None
    if expected_head is not None:
        anchor_gen, anchor_digest = expected_head
        # `expected_head` is the ONE control the accepted guarantee depends on.
        # A negative generation passed the "< anchor_gen" test and never matched
        # `gen == anchor_gen`, so the anchor was silently inert while a monitor
        # believed it was pinning.
        if (not isinstance(anchor_gen, int) or isinstance(anchor_gen, bool)
                or anchor_gen < 0 or not isinstance(anchor_digest, str)):
            return _empty_verdict(
                environment, head_state="INVALID_ANCHOR",
                reasons=[f"expected_head={expected_head!r} is not a "
                         "(non-negative int, str) pair; refusing to verify "
                         "against an anchor that cannot be applied"])
        if record["generation"] < anchor_gen:
            reasons.append(
                f"HISTORY_TRUNCATED: the archive is at generation "
                f"{record['generation']} but the anchor records generation "
                f"{anchor_gen}")

    # Walk the chain and REBUILD the committed order from the deltas. Each step
    # must be one valid transition; nothing is taken on the record's own word.
    expected: list = []
    missing = [g for g in range(record["generation"] + 1) if g not in generations]
    if missing:
        reasons.append(
            f"head generation record(s) {missing[:8]} are missing; the chain "
            "from genesis to the current head is not whole")
    else:
        previous = None
        for gen in range(record["generation"] + 1):
            try:
                rec = read_generation(root, environment, gen)
            except ArchiveHeadError as exc:
                reasons.append(f"head generation {gen}: {exc}")
                break
            if rec.get("archive_id") != archive_id:
                reasons.append(
                    f"head generation {gen} belongs to archive "
                    f"{rec.get('archive_id')!r}, not {archive_id!r}")
                break
            if rec.get("environment") != environment:
                reasons.append(
                    f"head generation {gen} declares environment "
                    f"{rec.get('environment')!r}, not {environment!r}")
                break
            if gen == 0:
                if rec.get("segment_count") != 0 or rec.get("committed_segment_id"):
                    reasons.append("generation 0 is not an empty archive")
                    break
            else:
                problems = verify_transition(previous, rec)
                if problems:
                    reasons.extend(problems)
                    break
                expected.append({
                    "segment_id": rec["committed_segment_id"],
                    "manifest_digest": rec["committed_segment_digest"],
                    "record_count": rec["committed_record_count"],
                    "partition_identity": rec["committed_partition_identity"],
                    "previous_segment_digest": rec["previous_segment_digest"],
                })
            if anchor_gen is not None and gen == anchor_gen \
                    and rec.get("head_digest") != anchor_digest:
                reasons.append(
                    f"HISTORY_REWRITTEN: generation {gen} has digest "
                    f"{str(rec.get('head_digest'))[:12]}, but the anchor "
                    f"records {str(anchor_digest)[:12]} for that generation")
            previous = rec
        if previous is not None and previous.get("head_digest") != record["head_digest"]:
            reasons.append("the walked chain does not end at the current head")

    results, previous_commitment, missing_committed = [], None, []
    fold = digest_hex({"archive_id": archive_id, "environment": environment,
                       "purpose": "archive-segments-fold"})
    for index, entry in enumerate(expected):
        seg_id = entry["segment_id"]
        directory = discovered.pop(seg_id, None)
        if directory is None:
            missing_committed.append(seg_id)
            reasons.append(
                f"segment {seg_id!r} is committed in the head at position "
                f"{index} but is MISSING from the archive")
            continue
        verdict = verify_segment(directory, environment=environment,
                                 allow_open=False, root=root)
        results.append(verdict)
        if not verdict.valid:
            reasons.append(f"segment {seg_id!r} does not verify: "
                           f"{'; '.join(verdict.reasons)}")
            continue
        manifest_bytes, why = evidence_fs.bounded_read(directory / MANIFEST_FILENAME)
        if why is not None:
            reasons.append(f"segment {seg_id!r} manifest unreadable: {why}")
            continue
        try:
            manifest = parse_canonical(manifest_bytes)
        except Exception as exc:              # noqa: BLE001 - reported
            reasons.append(f"segment {seg_id!r} manifest unreadable: {exc!r}")
            continue
        if not isinstance(manifest, dict):
            reasons.append(f"segment {seg_id!r} manifest is not an object")
            continue
        commitment = segment_commitment(manifest)
        if commitment != entry["manifest_digest"]:
            reasons.append(
                f"segment {seg_id!r} does not match the commitment the head "
                "records for it (substituted or rebuilt)")
        if entry["previous_segment_digest"] != previous_commitment:
            reasons.append(
                f"segment {seg_id!r} is recorded after predecessor "
                f"{entry['previous_segment_digest']!r}, but the preceding "
                f"committed segment is {previous_commitment!r}")
        if entry["record_count"] != manifest.get("record_count"):
            reasons.append(
                f"head entry for {seg_id!r} claims {entry['record_count']} "
                f"records, the manifest says {manifest.get('record_count')}")
        if entry["partition_identity"] != manifest.get("partition_identity"):
            reasons.append(
                f"head entry for {seg_id!r} claims partition "
                f"{entry['partition_identity']!r}, the manifest says "
                f"{manifest.get('partition_identity')!r}")
        previous_commitment = commitment
        fold = fold_segments_digest(fold, commitment)

    # A segment with a MANIFEST is committed evidence the history does not
    # mention — a crash between the manifest publish and the head commit, or a
    # graft. Genuinely ambiguous, so it stays fatal and is never adopted
    # silently. A segment WITHOUT one is not evidence at all: it cannot hide a
    # deletion (the head states what must exist) and it cannot be grafted in.
    orphaned, unexaminable = [], []
    for seg, d in sorted(discovered.items()):
        present, why = _presence(d / MANIFEST_FILENAME)
        if why is not None:
            # `_presence` answers (None, why); `None` is falsy, so taking only
            # [0] silently reclassified an unexaminable manifest from ORPHANED
            # (fatal, "never adopted silently") to uncommitted (non-gating). A
            # grafted committed segment plus one chmod turned INVALID into VALID
            # with zero reasons — a fail-open introduced by the helper written
            # to prevent exactly this.
            unexaminable.append(f"{seg}: {why}")
        elif present:
            orphaned.append(seg)
    if unexaminable:
        reasons.append(
            "UNEXAMINABLE_SEGMENT: the manifest presence of "
            f"{[u.split(':')[0] for u in unexaminable]} could not be "
            f"determined ({unexaminable[:3]}); an evidence directory the "
            "verifier cannot examine is never certified as absent")
    uncommitted = sorted(s for s in discovered if s not in orphaned)
    if orphaned:
        reasons.append(
            f"ORPHANED_COMMITTED_SEGMENT: {orphaned} are committed evidence on "
            "disk that the archive head does not mention — either a crash "
            "between the manifest publish and the head commit, or a graft. "
            "This is decided per segment, never automatically: commit it into "
            "history explicitly with the 'archive-adopt' operator command "
            "(bounded to exactly this state -- it refuses any segment "
            "verify_archive does not itself report here), or, if it is a "
            "graft rather than a crash residue, remove it from disk with "
            "direct, reviewed operator access. There is no 'archive-discard' "
            "command -- deleting evidence is deliberately not a scripted, "
            "one-line operation.")

    if not missing and record.get("archive_segments_digest") != fold and expected:
        reasons.append(
            "archive_segments_digest does not match the ordered segments "
            "(deletion, insertion or reorder present this way)")

    # Residue is REPORTED, never gating. Making it a `reason` turned an ordinary
    # OOM-kill or deploy into a total replay outage: the quarantine file is
    # written by the writer itself on every crash-and-restart, so the verifier
    # condemned the archive for the writer's own correct behaviour and
    # `read_verified()` then refused untouched, fully committed segments. It is
    # not evidence and it cannot hide a deletion, so it is a warning.
    residue, residue_errors = _abandoned_residue(env_dir)
    warnings = []
    if residue:
        warnings.append(
            f"ABANDONED_EVIDENCE: {len(residue)} quarantined event file(s) "
            f"({sum(r.get('bytes') or 0 for r in residue)} bytes) remain from "
            "an interrupted writer; they are not covered by any manifest. "
            "Salvage or discard them explicitly before removal.")
    if residue_errors:
        warnings.append(
            f"RESIDUE_SCAN_INCOMPLETE: {len(residue_errors)} path(s) could not "
            f"be scanned for quarantined evidence: {residue_errors[:3]}")
    invalid = [r for r in results if r.state is SegmentState.INVALID]
    return _empty_verdict(
        environment,
        archive_id=archive_id,
        head_state="CURRENT",
        head_generation=record["generation"],
        head_digest=record["head_digest"],
        generations_present=generations,
        segments=len(expected),
        closed_segments=len([r for r in results if r.state is SegmentState.CLOSED]),
        open_segments=len(uncommitted),
        uncommitted_segments=uncommitted,
        # Every segment directory, not the residue of `discovered.pop(...)`.
        # Quarantine happens when a crashed segment id is REUSED, and the
        # successor then commits it — so the residue always lands in a
        # committed directory, which the pop had already removed. 256
        # recoverable records were reported as VALID with empty reasons.
        abandoned_segments=residue,
        warnings=warnings,
        invalid_segments=len(invalid),
        orphaned_committed_segments=orphaned,
        missing_committed_segments=missing_committed,
        records_expected=sum(e["record_count"] or 0 for e in expected),
        records_read=sum(r.records_read for r in results),
        segment_verdicts=[r.to_dict() for r in results],
        reasons=reasons,
        verdict="VALID" if (not reasons and not invalid) else "INVALID",
    )
