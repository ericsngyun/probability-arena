"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 Gates 2-7 — chained records, manifests,
segment lifecycle, single-writer ownership, crash consistency.

These are one design, not five, because they all have to agree on what a
*committed record* is. The archive's failures came from disagreement: the
digest was taken over one representation and the bytes written were another
(Gate 1); nothing pinned an expected record count, so deleting a whole record —
or the whole file — verified as intact; and every producer held its own file
descriptor, so concurrent appends interleaved gzip members and destroyed the
file.

The shape (KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1: synchronous, not
queue-based — see `SegmentWriter.submit`'s docstring for why the queue and
its background writer thread were removed rather than hardened further):

    N producers ─→ self._lock (serialises) ─→ ONE open segment
                                                             │
                                          records chained by previous_digest
                                                             │
                                      CLOSING → reconcile → manifest published
                                                             │
                                                          CLOSED

`submit()` canonicalises, chains and writes ONE record entirely on the
CALLING thread, inside one lock-held call — there is no queue between a
producer and the file, and no interval in which a caller can be told
ACCEPTED before the record is durably part of the segment.

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
import re
import signal
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import NamedTuple

from app.realtime.canonical import (
    CANONICAL_SCHEMA_VERSION,
    CanonicalError,
    CapabilityLimits,
    WorkBudget,
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

# KALSHI-ARCHIVE-CORE-REMEDIATION-003B A4 -- WORK-BUDGET CONSISTENCY.
#
# `build_record` (below) wraps `envelope_fields` into the 17-field `RECORD_
# FIELDS` envelope by pulling exactly these ten keys straight from it:
_ENVELOPE_SOURCED_RECORD_FIELDS = frozenset({
    "connection_generation", "subscription_id", "subscription_generation",
    "message_type", "market_ticker", "seq", "received_at_utc",
    "received_monotonic_ns", "raw_event", "normalized_event",
})
# Every OTHER field in `RECORD_FIELDS` (`schema_version`,
# `canonical_schema_version`, `environment`, `segment_id`, `receive_ordinal`,
# `previous_record_digest`) is new top-level content `build_record` adds that
# admission's own structural walk of `envelope_fields` alone never saw or
# charged a single canonical work unit for. Each is a scalar leaf (an int or
# a short str, no substructure), so each costs EXACTLY one extra
# `canonical._encode` unit when `build_record`'s wrapper is encoded --
# derived here, not hand-counted, so it cannot silently drift if
# `RECORD_FIELDS` gains or loses a field later.
#
# `_structural_reason`'s Mapping branch charges MORE per node than
# `_encode`'s (two units per key -- one for the key, one for the value --
# against `_encode`'s one), so for any value `v`,
# `structural_cost(v) >= encode_cost(v)` always. Reserving this many units
# out of admission's structural-walk ceiling therefore GUARANTEES that
# `encode_cost(envelope_fields) + _RECORD_ENVELOPE_OVERHEAD_UNITS` --
# exactly what `build_record`'s own `digest_hex` call will spend encoding
# `record` -- fits inside the SAME canonical work-unit ceiling admission
# advertises. One contract, carried from admission through commit, rather
# than two independently-fresh budgets that happen to agree on production's
# own envelope shape by accident (see `tests/meta_runtime/
# budget_consistency.py` for the measured margin this closes).
#
# KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- THE RESERVE IS 17, NOT 6.
# `len(RECORD_FIELDS) - len(_ENVELOPE_SOURCED_RECORD_FIELDS)` = 16 - 10 = 6
# is only sound when the envelope actually SUPPLIES all ten sourced keys.
# `build_record` reads them with `.get()`, so an ABSENT key costs zero units
# at admission (there is no node to walk) and one unit at commit (`_encode`
# still charges for the `None` node `build_record` inserts). Measured worst
# case on an EMPTY envelope: +17 units, matching the analytic
# `6 (new scalars) + 1 (record_digest) + 10 (absent-key None placeholders)`
# -- exactly `len(REQUIRED_RECORD_FIELDS)`, which is what the reserve is now
# derived from. The 6-unit form failed CLOSED (commit refused a value
# admission had accepted, rather than the reverse), so this was never a
# durability bug -- but the comment above states the relation as a
# GUARANTEE, and a constant that is 11 units short of the worst case does
# not guarantee it. Frozen into the contract at 6 it would have been wrong
# for every envelope that omits an optional key.
#
# The three components, named:
#   len(RECORD_FIELDS) - len(_ENVELOPE_SOURCED_RECORD_FIELDS) =  6  new scalars
#   record_digest (in REQUIRED_RECORD_FIELDS, not RECORD_FIELDS) =  1
#   len(_ENVELOPE_SOURCED_RECORD_FIELDS)                        = 10  absent-key
#                                                                     None nodes
#                                                              -----
#   len(REQUIRED_RECORD_FIELDS)                                 = 17
_RECORD_ENVELOPE_OVERHEAD_UNITS = len(REQUIRED_RECORD_FIELDS)

# KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A3 -- BOUNDED ROTATION DEFAULTS.
#
# `close()` re-reads and re-verifies the WHOLE segment (reconciliation reads
# every record back with `read_segment_records`, then `verify_segment` reads
# it AGAIN independently before it is committed to the head) -- it is not a
# cheap tail operation, and its cost grows with the segment, not with the
# marginal record.
#
# KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- RE-DERIVED, AND THE BENCHMARK IS
# NOW A COMMITTED SCRIPT (`tests/benchmarks/segment_close_cost.py`) so this
# number can be REGRESSED instead of re-invented. The previous derivation
# (~70-80 ms of close per 1,000 records) was measured with
# `commit_to_head=False` -- but `SegmentWriter` DEFAULTS that flag True, and
# `EventArchive._writer_for` has never overridden it, so production's close
# additionally runs a full independent `verify_segment` (a THIRD complete
# read of the segment) and `commit_segment`'s generation-record and
# current-head fsyncs under the archive lock. The bound was therefore
# derived from a cheaper operation than the one that actually runs.
#
# Re-measured through the committed benchmark, on the shape production uses:
#
#   records   commit_to_head   close CPU   close wall   CPU ms/1,000
#     1,000        False          0.036 s     0.039 s        36
#     1,000        True           0.099 s     0.129 s        99
#    13,000        True           2.134 s     2.497 s       164
#    20,000        True           2.332 s     4.721 s       117
#
# plus an independently measured 2.908 s CPU / 3.402 s wall at 20,000
# records, and 14.8 s of WALL at the same size under CPU contention. Call it
# ~145 ms of CPU per 1,000 records for the shape that actually ships, i.e.
# roughly double what the old comment claimed.
#
# `DEFAULT_MAX_SEGMENT_RECORDS` targets a close of roughly two seconds --
# `2000 / 145 ~= 13,800`, taken down to 13,000 for headroom. It is a bound on
# a COMMIT, not on ingestion: A8 also moved `retiring.close()` off the
# producer's thread onto `EventArchive`'s dedicated closer, so this number no
# longer decides how long a websocket reader stalls; it decides how much
# evidence one un-committed segment can be holding when the process dies, and
# how long the closer thread is busy. Both argue for the smaller bound. At the
# ~500 events/s assumed peak this milestone's performance gate used that is a
# rotation every ~26 s; at the one real measured Kalshi rate (4 records over
# ~2 minutes, DEMO) it is unreachable and `DEFAULT_MAX_SEGMENT_AGE_S` is what
# rotates. `DEFAULT_MAX_SEGMENT_AGE_S` bounds staleness
# independently of volume, so a slow trickle still rotates and commits
# periodically instead of holding one segment open indefinitely.
# `DEFAULT_MAX_SEGMENT_BYTES` is a THIRD, independent bound on the
# compressed on-disk size, so an unusually large per-record payload (a deep
# order book snapshot, say) cannot defeat the record-count bound by simply
# making each record bigger.
#
# These are `EventArchive`'s defaults (the surface a real collector
# actually constructs), not `SegmentWriter`'s -- `SegmentWriter` stays
# all-optional (`None` unless a caller asks for a bound) because it is the
# lower-level primitive many tests construct directly and deliberately
# leave unbounded. What A3 forbids is SHIPPING synchronous archival with no
# bound; it does not require every constructor at every layer to refuse to
# be unbounded when a caller explicitly asks for that.
DEFAULT_MAX_SEGMENT_RECORDS = 13_000
DEFAULT_MAX_SEGMENT_AGE_S = 900.0                     # 15 minutes
DEFAULT_MAX_SEGMENT_BYTES = 32 * 1024 * 1024          # 32 MiB, compressed

# `build_manifest` (below) nests a writer's admitted `subscription_metadata`
# ONE level deeper than admission ever walked it: `non_canonical_reason`
# validates it at ITS OWN root (depth 0), but `body["subscription_metadata"]
# = subscription_metadata or {}` then wraps it inside the manifest dict,
# which `publish_manifest` encodes whole. Reserving one level of depth at
# admission time (walking as if it were already nested one level deep)
# guarantees a value admitted at exactly `CapabilityLimits.MAX_DEPTH` cannot
# encode at `MAX_DEPTH + 1` once wrapped.
_MANIFEST_METADATA_DEPTH_RESERVE = 1

# KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- THE METADATA PATH NEEDED A WORK
# RESERVE TOO, NOT ONLY A DEPTH RESERVE.
#
# A4 gave `subscription_metadata` a DEPTH reserve and stopped there.
# `non_canonical_reason(metadata, _depth_reserve=1)` still charged a FULL,
# fresh `MAX_CANONICAL_WORK_UNITS` budget at metadata's OWN root -- but
# `publish_manifest` later runs `canonical_bytes(manifest)` over a body in
# which that same value is one entry among twenty (`MANIFEST_FIELDS`, plus
# `subscription_metadata` itself, plus `manifest_digest`), so commit spends
# metadata's own encode cost PLUS the wrapper's. Reproduced: a metadata
# value tuned to exactly the admission ceiling is ACCEPTED at construction,
# three records are written durably, and then `close()` raises
# `CanonicalError` and the segment goes INVALID -- three chain-valid records
# turned into uncommitted residue over a value the writer's own admission
# gate certified as legal.
#
# This is the IDENTICAL class `_RECORD_ENVELOPE_OVERHEAD_UNITS` exists to
# close on the record path, and strictly worse, because it strikes at close
# rather than at admission: there is no producer left to tell. Fixed the
# same way and derived from the same schema, so it cannot drift if
# `MANIFEST_FIELDS` changes: one unit for each of the manifest's own keys,
# plus the two keys `build_manifest` adds outside `MANIFEST_FIELDS`
# (`subscription_metadata`, `manifest_digest`). The constant itself is
# defined immediately below `MANIFEST_FIELDS`, which does not exist yet at
# this point in the module.


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
_MAX_CANONICAL_WORK_UNITS = CapabilityLimits.MAX_CANONICAL_WORK_UNITS


class SegmentState(str, Enum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    INVALID = "INVALID"


class RejectReason(str, Enum):
    """Why an event was not written. Every one is counted; none is silent.

    KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1 (synchronous canonical archive):
    `QUEUE_FULL` and `ENQUEUE_TIMEOUT` are RETIRED, not merely unused. They
    named outcomes of a bounded producer queue that no longer exists --
    `submit()` now canonicalises and writes before it returns, so there is no
    queue to be full and nothing to time out waiting on. Both values are kept
    in the enum (rather than deleted) only so that an OLD segment's
    `WriterAccounting.rejections` dict -- durable evidence already on disk --
    still parses if it happens to name one; no CURRENT code path can ever
    produce them again. `SHUTDOWN_IN_PROGRESS` is NOT retired: it is now the
    reason a `submit()` returns when it loses the race for `self._lock`
    against a `close()` already in `CLOSING`/`CLOSED` -- see `submit()`.
    """

    QUEUE_FULL = "queue_full"                    # retired; see class docstring
    ENQUEUE_TIMEOUT = "enqueue_timeout"           # retired; see class docstring
    SERIALIZATION_FAILURE = "serialization_failure"
    WRITER_FAILED = "writer_failed"
    SEGMENT_NOT_OPEN = "segment_not_open"
    SHUTDOWN_IN_PROGRESS = "shutdown_in_progress"
    SEGMENT_INVALID = "segment_invalid"
    # Decided BEFORE acceptance. A value that cannot be canonically represented
    # is a contract violation by the caller, not a writer failure, and the
    # producer has to learn that while it can still do something about it.
    NOT_CANONICAL = "not_canonical"


def canonicalize_or_reason(value, _path: str = "", *, _depth_reserve: int = 0,
                           _work_reserve: int = 0):
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

    KALSHI-ARCHIVE-CORE-REMEDIATION-003B A4: `_depth_reserve`/`_work_reserve`
    are the ONE budget contract, carried forward from whatever wrapping the
    caller KNOWS commit will apply on top of `value` -- not two independently
    fresh budgets that happen to agree by accident. A caller that knows its
    accepted value will later be nested one level deeper, or wrapped with N
    known extra scalar fields, reserves that here so a value admitted now is
    GUARANTEED commit-encodable later, rather than merely encodable in
    isolation today. Both default to 0 -- ordinary callers (this module's own
    tests, `legacy_import.py`) get the exact previous behaviour.
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
        budget = WorkBudget(_MAX_CANONICAL_WORK_UNITS - _work_reserve)
        structural = _structural_reason(value, _path, _depth_reserve, budget)
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


def non_canonical_reason(value, _path: str = "", *, _depth_reserve: int = 0,
                         _work_reserve: int = 0) -> str | None:
    """Why this value cannot become evidence, or None.

    Thin wrapper over `canonicalize_or_reason` for callers (and tests) that
    only want the refusal reason, not the bytes. `_admit` calls
    `canonicalize_or_reason` directly so it never encodes twice.
    """
    _, reason = canonicalize_or_reason(
        value, _path, _depth_reserve=_depth_reserve, _work_reserve=_work_reserve)
    return reason


def _structural_reason(value, _path: str = "", _depth: int = 0,
                       _budget: WorkBudget | None = None) -> str | None:
    """Why this value cannot become evidence, or None. Type walk, no encoding.

    `_budget` is the AGGREGATE work counter (defect B): shared across the
    WHOLE recursive walk, so it bounds TOTAL nodes visited, not merely each
    container's own local element count. Per-container bounds
    (`_MAX_MAPPING_ELEMENTS`, `_MAX_SEQUENCE_ELEMENTS`, `_MAX_DEPTH`) stay
    legal at every level for `x = 0; for _ in range(60): x = [x, x]` (depth
    60 < 256, width 2 per list) while the SAME two-element list is shared
    and re-walked from both parents at every level -- 2**60 total nodes,
    entirely invisible to a check that only ever looks at the container
    immediately in front of it. This walk runs pre-acceptance, inside
    `_inflight` (see `_admit`), so an unbounded admission walk is exactly as
    much a defect as an unbounded encode -- see `canonical.WorkBudget` for
    the mirrored bound applied to `canonical_bytes` itself.

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
    if _budget is None:
        _budget = WorkBudget(_MAX_CANONICAL_WORK_UNITS)
    try:
        _budget.consume(_path or "value")
    except CanonicalError as exc:
        return str(exc)
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
        seen_keys: set = set()
        for k, v in value.items():
            count += 1
            if count > _MAX_MAPPING_ELEMENTS:
                return (f"{_path or 'value'} has more than "
                        f"{_MAX_MAPPING_ELEMENTS} elements")
            if not isinstance(k, str):
                return f"{_path or 'value'} has a non-string key {k!r}"
            # KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect D, mirrored on the
            # admission side: an ORDINARY (non-hostile) Mapping backed by an
            # association list can present the same logical string key more
            # than once via `.items()` -- the raw source of truth this walk
            # (and `_encode`'s Mapping branch) both iterate. Rejected here,
            # BEFORE acceptance, matching the encoder's policy exactly (see
            # `canonical._encode`'s Mapping branch) -- PREFER REJECTION,
            # never accept-and-silently-drop.
            if k in seen_keys:
                return (f"{_path or 'value'} key {k!r} is presented more "
                        "than once by this Mapping's .items(); duplicate "
                        "logical keys are refused rather than silently "
                        "collapsed to the last value")
            seen_keys.add(k)
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
                k, f"{_path}.<key>" if _path else "<key>", _depth + 1, _budget)
            if key_problem is not None:
                return key_problem
            found = _structural_reason(
                v, f"{_path}.{k}" if _path else k, _depth + 1, _budget)
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
            found = _structural_reason(v, f"{_path}[{i}]", _depth + 1, _budget)
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


# KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8. See the block above
# `_MANIFEST_METADATA_DEPTH_RESERVE` for why this exists. `publish_manifest`
# encodes the WHOLE manifest body, and `canonical_bytes` charges exactly one
# `_encode` unit per key of that body on top of the metadata's own encode --
# so the reserve is precisely the number of keys `build_manifest` emits.
#
# ROUND 2: that number is now MEASURED from `build_manifest` itself rather
# than hand-counted as `len(MANIFEST_FIELDS) + 2`. The `+ 2` was a tally of
# the two body keys that are NOT in `MANIFEST_FIELDS` (`subscription_metadata`
# and `manifest_digest`); a third such key added later would have left the
# reserve silently one unit short, which is the exact failure mode this
# reserve exists to prevent (a value certified legal at construction that
# destroys the segment at close). Defined below `build_manifest` because it
# calls it.
_MANIFEST_METADATA_WORK_RESERVE = len(build_manifest(
    environment="", segment_id="", partition_identity="",
    opened_at="", closed_at="", record_count=0,
    first_record_digest=None, last_record_digest=None,
    ordered_stream_digest=None, event_file_size_bytes=0,
    event_file_sha256=None, subscription_metadata={}))


def verify_manifest_self_digest(manifest: dict) -> bool:
    recorded = manifest.get("manifest_digest")
    if not isinstance(recorded, str):
        return False
    try:
        return recorded == digest_hex({k: manifest.get(k) for k in MANIFEST_FIELDS})
    except CanonicalError:
        return False


def file_sha256(path: Path) -> str:
    """Routed through `evidence_fs.sha256_bounded`, not a raw `open()`.

    `verify_segment -> file_sha256 -> open(...)` was A1's named finding: a
    raw `open()` two hops below a canonical entry point, bypassing every
    containment/regular-file check the reviewed evidence-filesystem
    abstraction exists to enforce, reachable via `verify_segment`'s
    `root=None` shape (which used to skip its containment block entirely)
    and via a stat-then-open TOCTOU race in every other shape. Routing here
    through `sha256_bounded` closes the class: this function can no longer
    reach the filesystem except through the fd-based, race-free,
    size-bounded primitive.

    Raises `SegmentError` (a typed archive exception, not a raw `OSError`)
    on any refusal, so a caller that used to catch `OSError` around this
    call keeps working -- `SegmentError` is what every other canonical
    reader in this module already raises for an evidence-access refusal.
    """
    digest, reason = evidence_fs.sha256_bounded(path)
    if reason is not None:
        raise SegmentError(f"{path} could not be hashed: {reason}")
    return digest


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

    KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1 (synchronous canonical archive):
    this is the SAME identity the queue-based writer used, with the stages a
    bounded producer queue could put an event in — `pending` (accepted, sitting
    in the queue, never drained) and `dequeued` (the independent, second-sourced
    "left the queue but the writer thread died before booking an outcome"
    check) — REMOVED rather than reworked, because `submit()` no longer has an
    asynchronous gap for either of them to describe. An event's admission
    outcome was previously "wherever it ended up: the queue, the written
    stream, or the failure count", because those were three different places
    at three different times. There is now exactly one call frame, and by the
    time `submit()` returns, the event has already reached its ONE terminal
    outcome:

        attempted -> rejected_before_accept   (never durable; canonicalisation
                                                refused it, or the segment was
                                                not open, BEFORE any write)
        attempted -> written                  (durable; `submit()` returns
                                                `None` only after the bytes
                                                are handed to the gzip stream)
        attempted -> failed_after_accept      (canonicalisation succeeded but
                                                the OS write itself failed;
                                                the segment goes INVALID)

    `accepted` stays a derived property (`written + failed_after_accept`) —
    it was already correct to derive it before A1, and it stays correct now
    that there is no third place (a queue) for an accepted item to be
    "currently sitting". `clean()` — the ONLY gate on publishing a segment as
    evidence — is unchanged in what it MEANS (no accepted-and-lost event may
    be published as a clean close): it is `failed_after_accept == 0`, with
    `pending == 0` true by construction rather than by measurement, because
    nothing can be "pending" any more.
    """

    attempted: int = 0
    rejected_before_accept: int = 0
    written: int = 0
    failed_after_accept: int = 0      # accepted, then the write itself failed
    rejections: dict = field(default_factory=dict)

    @property
    def pending(self) -> int:
        """Always 0. Kept as a read-only property, not a field, so existing
        callers (and the manifest/report shapes built from `to_dict()`) stay
        source-compatible: synchronous `submit()` has no asynchronous gap in
        which an accepted event can be neither written nor failed."""
        return 0

    @property
    def accepted(self) -> int:
        return self.written + self.failed_after_accept

    def reject_before_accept(self, reason: RejectReason) -> None:
        self.rejections[reason.value] = self.rejections.get(reason.value, 0) + 1
        self.rejected_before_accept += 1

    def fail_after_accept(self, reason: RejectReason) -> None:
        self.rejections[reason.value] = self.rejections.get(reason.value, 0) + 1
        self.failed_after_accept += 1

    def admission_holds(self) -> bool:
        """`attempted == rejected_before_accept + accepted`. Synchronous
        `submit()` moves `attempted`, then reaches exactly one terminal
        booking, all under the same writer lock and the same call frame — so
        unlike the queue-based writer this identity is no longer merely a
        diagnostic that can drift from an asynchronous exception; it holds by
        construction for every call that returns normally. Kept as a method
        (not inlined) because `close()` and its callers still ask for it by
        name.
        """
        return self.attempted == self.rejected_before_accept + self.accepted

    def disposition_holds(self) -> bool:
        """Always true; kept as a method so existing callers stay
        source-compatible. See `WriterAccounting`'s docstring."""
        return True

    def reconciles(self) -> bool:
        return self.admission_holds() and self.disposition_holds()

    def clean(self) -> bool:
        """The only state in which a segment may be published as clean
        evidence. `pending` is always 0 now (see its docstring), so this
        reduces to the one condition that was ever load-bearing: no accepted
        event failed to become durable evidence.
        """
        return self.pending == 0 and self.failed_after_accept == 0

    def to_dict(self) -> dict:
        return {"attempted": self.attempted,
                "rejected_before_accept": self.rejected_before_accept,
                "accepted": self.accepted, "written": self.written,
                "failed_after_accept": self.failed_after_accept,
                "pending": self.pending,
                "rejections": dict(self.rejections),
                "admission_holds": self.admission_holds(),
                "disposition_holds": self.disposition_holds(),
                "clean": self.clean()}


# KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- SIGNAL DEFERRAL ACROSS THE
# COMMITMENT REGION.
#
# THE MEASURED REGRESSION A8 EXISTS TO CLOSE. A1 moved the actual `write()`
# from a background writer thread onto the CALLING thread. CPython raises a
# signal-derived exception (`KeyboardInterrupt` from `SIGINT`) ONLY on the
# main thread, so under the queue design an operator's Ctrl-C could never
# reach the gzip write at all -- the queue was providing signal isolation
# nobody had written down. With the write on the caller's (in any real
# collector, the MAIN) thread it reaches it constantly: an A/B of
# `tests/harness_async_accounting/fault_trial.py --n-interrupts 1
# --n-submits 20000` over seeds 0-5 measured 6/6 `close_ok: True` at
# 321c719 against 6/6 `close_ok: False, record_count: null` after A1 --
# `submit()`'s `except BaseException` correctly marks the segment INVALID
# and re-raises, and fail-closed here means every ALREADY-DURABLE record in
# the open segment becomes unpublishable residue.
#
# WHY NOT `signal.pthread_sigmask`. That was the reviewer's recommendation
# and it does not work, measured rather than argued. The measurement is a
# COMMITTED, RE-RUNNABLE HARNESS, not a number in a comment:
#
#   python tests/benchmarks/pthread_sigmask_refutation.py --rounds 300
#
# (`SIG_BLOCK` on `{SIGINT, SIGTERM}` around a spin region; darwin 25.2.0,
# CPython 3.12.3; four consecutive runs):
#
#   bomber thread started AFTER the mask   ->  0, 0, 0, 0   / 300 raised inside
#   bomber thread started BEFORE the mask  -> 25, 19, 37, 14 / 300 raised inside
#
# A8 ROUND 2 CORRECTION: this comment previously claimed `300/300` for the
# second arm. That magnitude was wrong -- an independent reviewer measured
# 14/300 on the same platform, which reproduces here (5-12%, rate-dependent:
# it is the fraction of masked windows a process-directed signal happens to
# land in, not a certainty). The DIRECTION is what the design rests on and it
# is unaffected: a non-zero rate is already fatal to the mask as a
# durability mechanism, because ONE delivery inside the commitment region is
# what destroys an open segment. The number is now cited from reproducible
# output so it cannot drift into folklore again.
#
# The first number is the trap: `threading` gives a new thread the creating
# thread's mask AT `start()`, so a bomber started inside the masked window
# inherits the block and the measurement flatters the fix. In the real
# shape -- any thread that was already running when the writer masks (a
# websocket reader, an asyncio loop, the harness's own bomber) -- the
# kernel delivers the process-directed signal to THAT unmasked thread,
# CPython's C handler trips the flag from there, and the main thread raises
# inside the "masked" region anyway. Masking the writer thread cannot stop
# a signal the kernel is free to hand to any other thread in the process.
#
# WHAT IS USED INSTEAD. The mechanism that IS authoritative in CPython is
# the Python-level handler, because it is the only thing that decides
# whether a tripped flag becomes an exception -- and it always runs on the
# main thread, whichever thread the kernel picked. For the duration of the
# commitment region the writer installs a handler that RECORDS the signal
# instead of raising, then restores the previous handlers and re-delivers
# via `os.kill(os.getpid(), signum)` -- so the original disposition
# (`default_int_handler`, `SIG_DFL`, `SIG_IGN`, or an application's own
# handler) is reproduced exactly, by the OS, rather than reinterpreted
# here, and the interrupt lands at a boundary the writer chose.
#
# A8 ROUND 2 CORRECTION: this comment used to claim "re-delivery never
# RAISES from `__exit__`, so it cannot mask an exception already propagating
# out of the region". That was never true -- `os.kill(pid, SIGINT)` with
# `default_int_handler` in place raises `KeyboardInterrupt` on the spot, and
# `contextlib.suppress(OSError)` around it does not catch that. It is also
# not the property that matters: what `__exit__` must never do is DROP a
# deferred signal or leave a handler unrestored, and both of those are now
# structural (`_restore` is idempotent, retried, and runs in a `try` whose
# `finally` still re-delivers). An exception CAN leave `__exit__` -- it is
# either the application's own `SystemExit`/`KeyboardInterrupt` arriving at
# the boundary the writer chose, which is the entire point, or a restore
# failure the caller must see. The record's bytes and its state delta are
# both already applied by then, so nothing propagating from here can leave
# the segment inconsistent.
#
# BOUNDS -- AND THE ONE HONEST CAVEAT. This is not a general "ignore Ctrl-C"
# switch: the region is one record's write plus its state delta (measured
# ~3.2 us of handler install/restore per record against a ~66 us submit,
# ~5%), and it is a no-op off the main thread, where a Python-level signal
# handler can never run in the first place.
#
# BUT THE REGION HAS NO TIMEOUT, AND IT IS NOT ONLY THE WRITE. It also spans
# `pre_write_hook` (a test seam; `None` in production) and `flush()`. Nothing
# bounds how long any of those take, and for exactly as long as they run the
# process CANNOT BE INTERRUPTED. Measured here, not argued, and stated in
# the module rather than left for an operator to discover during an
# incident: a 3-second blocking operation inside the region withheld a real
# `SIGINT` for 2.775 s (sent at +0.238 s, `KeyboardInterrupt` raised at
# +3.012 s); and the known `pre_write_hook` reentrancy deadlock (a hook that
# calls back into `submit()` and blocks on the non-reentrant `self._lock`)
# is now UN-interruptible -- two real Ctrl-Cs did not break it and an
# external watchdog had to `os._exit` the process at 4 s. That second one is
# structural, not incidental: the blocked `lock.acquire()` DOES run Python
# signal handlers, but the handler it runs is `_defer`, which records the
# signal and returns, and the acquire resumes. So: a stalled
# filesystem, an NFS mount that stops answering, or a wedged hook makes the
# collector Ctrl-C-proof for the duration of the stall. That is a deliberate
# trade -- an operator's interrupt is deferred, never dropped, so no
# already-durable record becomes unpublishable residue -- but it is a real
# loss of controllability and it is not bounded by anything in this module.
#
# It does not defend against
# `PyThreadState_SetAsyncExc` (the harness's `asyncexc` mode), which is a
# ctypes-only mechanism no signal discipline can intercept -- that class is
# handled structurally instead, by A8's second half: the state delta is
# precomputed before the write and applied in a `finally`, so an
# interruption anywhere in the region cannot leave `_ordinal`/`_prev_digest`
# behind the bytes on disk.
_COMMIT_DEFERRED_SIGNALS = tuple(
    s for s in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None))
    if s is not None)


class _ChainState(NamedTuple):
    """The whole of a `SegmentWriter`'s per-record chain state, in ONE value.

    KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 ROUND 2. These used to be five
    separate attributes applied by five separate `STORE_ATTR`s in
    `submit()`'s `finally`. `finally` guarantees the body is ENTERED, not
    that it COMPLETES: an asynchronous exception (a signal-derived one, or
    `PyThreadState_SetAsyncExc`) landing BETWEEN two of those stores left the
    writer with some of the delta applied and some not -- reproduced
    byte-for-byte as the round-1 wrong state (`written 194 / on_disk 200`,
    `state OPEN`, `healthy true`, `clean true`, `close()` refusing hours
    later). That is not fail-closed; it is silently poisoned now and
    fail-closed much later, with `append()` returning success for every
    record in between.

    Collapsing them into one immutable tuple makes the invariant TOTAL for
    the chain rather than dependent on nothing interrupting a five-statement
    run: there is exactly one store, so the chain either advances completely
    or not at all. `accounting.written` is the one field that cannot join
    them (it lives on a different object), and it is guarded explicitly
    instead -- see `submit()`'s `finally`.
    """

    first_digest: str | None
    last_digest: str | None
    stream_digest: str
    prev_digest: str
    ordinal: int


class _DeferCommitSignals:
    """Defer `SIGINT`/`SIGTERM` for the duration of a `with` block.

    Reusable and reused (one instance per `SegmentWriter`); every entry is
    made under the writer's own `_lock`, so it is never re-entered
    concurrently. `installed` is exposed for tests, which must be able to
    assert the deferral actually took effect rather than silently no-opping.
    """

    __slots__ = ("_pending", "_saved", "installed")

    def __init__(self):
        self._pending: list = []
        self._saved: tuple = ()
        self.installed = False

    def _defer(self, signum, frame):        # runs on the main thread only
        self._pending.append(signum)

    def _restore(self) -> None:
        """Put every handler this instance replaced back, whatever happens.

        KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 ROUND 2 -- THE PERMANENT-
        DEAFNESS DEFECT. The previous shape restored inside
        `contextlib.suppress(Exception)`, which does NOT catch
        `BaseException`, and restored SIGTERM before SIGINT. The single most
        common shutdown idiom is `def term(...): raise SystemExit(0)`, so the
        instant SIGTERM's own handler was back in place it could raise
        BETWEEN the two restores and SIGINT was left bound to `_defer`
        FOREVER: every later Ctrl-C landed in `self._pending` (reset to `[]`
        on the next entry, never drained) and the operator could no longer
        interrupt the collector at all. Measured deterministically -- a real
        SIGTERM, a real `raise SystemExit(0)` handler -- in
        `TestTheDeferralCannotDeafenTheProcessPermanently`.

        Three properties make this total rather than merely narrower:

        * IDEMPOTENT. A signal is restored only if it is still bound to
          `self._defer`, so re-running the loop can never clobber a handler
          the application installed in the meantime, and a partially-applied
          pass simply finishes on the next one.
        * BOUNDED RETRY. A signal-derived `BaseException` landing mid-pass is
          caught, held, and the pass retried -- but at most `len + 2` times,
          never `while True`, so a process under continuous signal pressure
          cannot turn a restore into a livelock.
        * THE FIRST `BaseException` IS RE-RAISED, not swallowed. Eating an
          application's `SystemExit` here would be the same class of defect
          in the opposite direction: the process would be told to stop and
          never learn it.
        """
        saved, self._saved = self._saved, ()
        self.installed = False
        held: BaseException | None = None
        for _ in range(len(saved) + 2):
            try:
                for sig, prev in reversed(saved):
                    try:
                        if signal.getsignal(sig) != self._defer:
                            continue          # already restored, or replaced
                    except Exception:         # noqa: BLE001 - best effort
                        pass
                    with contextlib.suppress(Exception):
                        signal.signal(sig, prev)
                break
            except BaseException as exc:      # noqa: BLE001 - held, re-raised
                if held is None:
                    held = exc
        if held is not None:
            raise held

    def __enter__(self):
        self._pending = []
        self._saved = ()
        self.installed = False
        if threading.current_thread() is not threading.main_thread():
            # A Python-level signal handler NEVER runs on a non-main thread,
            # so a non-main writer cannot be interrupted by one and there is
            # nothing to defer. Installing from here would raise ValueError.
            return self
        try:
            for sig in _COMMIT_DEFERRED_SIGNALS:
                prev = signal.getsignal(sig)
                if prev is None:
                    # The handler was installed from C and cannot be restored
                    # through `signal.signal`. Leave it entirely alone rather
                    # than replace something we could not put back.
                    continue
                if prev == self._defer:
                    # Already active. Nesting should be impossible -- every
                    # entry happens under `self._lock`, which is not
                    # reentrant -- but recording OUR OWN handler as the
                    # "previous" one would make the restore a no-op and leave
                    # the process permanently deaf to this signal. Refuse
                    # rather than trust the reasoning.
                    continue
                # THE RESTORE INTENT IS PUBLISHED *BEFORE* THE INSTALL, not
                # after it and not into a local list. `prev == self._defer`
                # above is a Python-level call whenever the application's
                # handler is an object with `__eq__`, and `signal.signal`
                # below is followed by an ordinary bytecode boundary -- both
                # are points at which a signal-derived `BaseException` can
                # land. Recording the intent first makes `_restore()` correct
                # for BOTH outcomes: if the install never happened the entry
                # is a no-op (the signal is not bound to `_defer`), and if it
                # did the entry is already durable in `self._saved`. Appending
                # to a LOCAL list -- and assigning `self._saved` only after
                # the loop -- is what let one escaping `SystemExit` throw the
                # restore information away.
                self._saved += ((sig, prev),)
                signal.signal(sig, self._defer)
                self.installed = True
        except (ValueError, OSError, RuntimeError):
            # Not the main interpreter, or a platform/runtime that refuses
            # the install. Undo whatever took and proceed undeferred: this is
            # a durability HARDENING, and failing to obtain it must never
            # fail the write itself.
            self._restore()
            return self
        except BaseException:
            # `SystemExit`/`KeyboardInterrupt` -- from the application's own
            # still-live handler for a signal we have not replaced yet, or
            # from `PyThreadState_SetAsyncExc`. NOT an ordinary install
            # refusal: it must propagate. What must NOT propagate with it is
            # a half-installed deferral.
            self._restore()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._restore()
        finally:
            # RESTORE FIRST, THEN re-deliver -- otherwise our own deferring
            # handler would catch the re-delivery and the signal would be
            # lost. In a `finally` so that a `BaseException` escaping the
            # restore cannot silently discard signals already deferred.
            pending, self._pending = self._pending, []
            for signum in pending:
                with contextlib.suppress(OSError):
                    os.kill(os.getpid(), signum)
        return False


class SegmentWriter:
    """The single owner of one segment's file descriptor.

    KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1 (synchronous canonical archive):
    producers never touch the file, but there is no longer a queue or a
    background thread between them and it either. `submit()` canonicalises
    and appends the record on the CALLER'S thread, serialised against every
    other `submit()`/`close()` by one lock (`self._lock`), and returns only
    after the record has been durably handed to the writer -- never before.

    This replaces an eleven-round-hardened queue-ownership protocol
    (`_claimed`/`_inflight`/`_admission`/`_sealed`, a background writer
    thread draining a bounded `queue.Queue`) that could not be made to close
    a real gap: `queue.Queue.get()` does an irreversible `deque.popleft()`
    and then calls `self.not_full.notify()` -- a Python call, and therefore a
    real async-exception delivery point -- before it ever returns to the
    writer thread, so a producer could be told an event was ACCEPTED while
    the event itself had already, irreversibly, left the only place that
    made it recoverable. A measured performance gate found the queue was not
    even faster: synchronous append outperformed it (3,440 vs 1,927 events/s
    on `SegmentWriter`, sustained 2,500-5,000 events/s, bursts draining
    ~7,000/s), so removing the queue closes a correctness gap AND a
    performance one, not one at the cost of the other.

    One consequence worth naming explicitly: **a caller is never told
    ACCEPTED before the canonical writer owns the event.** There is no
    interval, of any size, in which `submit()` has returned `None` but the
    record is not yet durably part of this segment's gzip stream.

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
        # `queue_maxsize`/`enqueue_timeout_s` are RETIRED parameters, accepted
        # and ignored rather than removed. There is no queue left for either
        # to bound, and deleting them outright would break every existing
        # caller's keyword arguments (production and test) for no behavioural
        # gain -- a synchronous writer that silently accepts and discards a
        # now-meaningless knob is safer than one that raises `TypeError` at
        # every call site that has not been individually re-audited.
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
        # KALSHI-ARCHIVE-CORE-REMEDIATION-003B A4: `_depth_reserve` accounts
        # for `build_manifest` nesting this SAME value one level deeper
        # (`body["subscription_metadata"] = subscription_metadata or {}`,
        # then `publish_manifest` encodes the whole manifest body) than this
        # root-level admission walk would otherwise check. Without it, a
        # value admitted at exactly `CapabilityLimits.MAX_DEPTH` here
        # encodes at `MAX_DEPTH + 1` when wrapped -- destroying every
        # already-durable record in the segment at close, over a value
        # admission itself certified as legal.
        # A8 adds `_work_reserve` alongside A4's `_depth_reserve`: the depth
        # reserve alone left the identical hole one bound over. See
        # `_MANIFEST_METADATA_WORK_RESERVE`.
        bad = non_canonical_reason(
            metadata, _depth_reserve=_MANIFEST_METADATA_DEPTH_RESERVE,
            _work_reserve=_MANIFEST_METADATA_WORK_RESERVE)
        if bad is not None:
            raise SegmentError(
                f"subscription_metadata is not canonically representable: "
                f"{bad}")
        # KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect E: `self.
        # subscription_metadata = metadata` used to retain the CALLER's live
        # reference -- validated once, at construction, but never snapshotted,
        # so a caller mutating its own dict AFTER construction silently
        # mutated the manifest this writer will publish at close(). A benign
        # mutation landed in the manifest with a self-consistent digest that
        # verified VALID; a hostile one could inject content that was NEVER
        # admitted through `non_canonical_reason` above. The same principle
        # `_admit` already applies to every SUBMITTED record --
        # `parse_canonical(canonical_bytes(value))`, reconstructing the
        # accepted value from its own canonical bytes rather than keeping the
        # producer's live object -- applies here too: the stored
        # representation is a fresh structure built from the validated bytes,
        # sharing no container with the caller's `subscription_metadata`
        # argument, so no later mutation of THAT object can reach it.
        self.subscription_metadata = parse_canonical(canonical_bytes(metadata))
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

        self._flush_every = flush_every
        # THE serialization lock. Every `submit()` and every `close()` holds
        # this for the full duration of the work it does -- admission,
        # canonicalisation, the write itself, and (for `close()`) sealing the
        # segment -- so "one segment, one writer" is now enforced by ordinary
        # mutual exclusion on one Python object, not by a background thread
        # being the only one that ever touches the file descriptor. This is
        # what replaces the ENTIRE queue-ownership protocol: a `submit()`
        # that is still running when `close()` is called simply has not
        # released the lock yet, so `close()` blocks behind it exactly once
        # and then observes a state that cannot move again -- "close() waits
        # for the writer" and "append and close are mutually ordered" are now
        # the same guarantee, provided by the same lock, rather than two
        # separately-proven protocols (`_seal_admissions` + a queue join)
        # that eleven rounds of hardening never fully closed.
        self._lock = threading.Lock()
        # A8: reused rather than constructed per record. Every entry happens
        # under `self._lock`, so one instance can never be re-entered
        # concurrently. See `_DeferCommitSignals`' module comment for the
        # measured regression it closes and why `pthread_sigmask` does not.
        self._defer_commit_signals = _DeferCommitSignals()
        genesis = genesis_digest(segment_id=self.segment_id,
                                 environment=environment)
        # ONE attribute, not five -- see `_ChainState`.
        self._chain = _ChainState(first_digest=None, last_digest=None,
                                  stream_digest=genesis, prev_digest=genesis,
                                  ordinal=0)
        self._writer_error: BaseException | None = None
        # close() is reachable from several threads (a shutdown handler and an
        # application path, say). Without this the second caller finalises an
        # already-finalised file and the segment is destroyed by its own
        # shutdown.
        self._close_lock = threading.Lock()
        self.last_rejection_detail: str | None = None
        # Injection seam. Production leaves it None; tests use it to slow the
        # writer or fail a specific durability stage without weakening the
        # real fsync path. Now called synchronously, immediately before the
        # write it used to precede on the writer thread.
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
        return self.healthy

    # -- producer side -----------------------------------------------------
    def submit(self, envelope_fields: dict) -> RejectReason | None:
        """Canonicalise, chain and append ONE record. Synchronous.

        KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1: there is no queue and no
        writer thread any more. `self._lock` is held for the WHOLE call --
        the state check, the canonical walk, the encode, and the actual
        `write()` -- so `submit()` and `close()` (which takes the same lock
        to seal the segment) can never interleave: a `submit()` that is still
        running has not released the lock, and a `close()` that has taken it
        has already frozen `self.state` before the next `submit()` can even
        begin. That single fact replaces the ENTIRE `_admission`/`_inflight`/
        `_sealed` protocol: "no producer admitted before sealing can be told
        ACCEPTED afterwards" no longer needs a second checkpoint mid-call,
        because there is no interval during which this call could be
        preempted by a `close()` racing it to a DIFFERENT lock.

        Returns `None` on acceptance -- or a typed reason on rejection.
        There is no path that drops an event without returning a reason, and
        no path that returns `None` before the write has actually happened.

        WHAT `None` DOES AND DOES NOT PROMISE. It means the record's bytes
        have been handed to this segment's gzip stream and the chain state
        has advanced to include it -- NOT that they have reached the
        platter. `flush_every` defaults to 256 and `EventArchive` never
        overrides it, so between flush boundaries an acknowledged record
        lives in `GzipFile`'s zlib buffer and the kernel page cache.
        Measured against a real writer with `SIGKILL` (which, unlike a
        clean process exit, runs no `atexit`/`__del__` flush and is the
        actual crash this claim has to survive): 100 records acked -> 0
        recoverable; 600 acked -> 512 recoverable, i.e. exactly the last
        `600 % 256 == 88` were lost. The durability boundary is therefore
        THE NEXT FLUSH OR `close()`, and this is deliberate -- fsyncing per
        record would cost an fsync per event for a tape whose commit record
        is the manifest, not the individual line. What A8 guarantees at the
        per-record level is CONSISTENCY, not durability: the accepted
        prefix of the stream is always chain-valid and always matches
        `written`, whatever the process is interrupted by.

        A8 also defers `SIGINT`/`SIGTERM` across the commitment region and
        applies the whole state delta from a `finally` -- see
        `_DeferCommitSignals` for the measured regression that closes and
        why `signal.pthread_sigmask` does not close it.
        """
        with self._lock:
            self.accounting.attempted += 1
            if self._writer_error is not None:
                self.accounting.reject_before_accept(RejectReason.WRITER_FAILED)
                return RejectReason.WRITER_FAILED
            if self.state is SegmentState.INVALID:
                self.accounting.reject_before_accept(RejectReason.SEGMENT_INVALID)
                return RejectReason.SEGMENT_INVALID
            if self.state is not SegmentState.OPEN:
                # Covers both an ordinary CLOSED segment and one seen while
                # `close()` (holding this same lock a moment ago) is already
                # CLOSING -- the synchronous replacement for
                # `RejectReason.SHUTDOWN_IN_PROGRESS`'s old meaning ("a close
                # is already underway"), reached the same way every other
                # post-open rejection is: by losing the SAME lock race
                # `close()` used to seal the segment.
                reason = (RejectReason.SHUTDOWN_IN_PROGRESS
                          if self.state is SegmentState.CLOSING
                          else RejectReason.SEGMENT_NOT_OPEN)
                self.accounting.reject_before_accept(reason)
                return reason
            # A2 -- fewer encodes, but NOT one: the admission round-trip
            # below is NOT the redundant encode-and-discard step it first
            # looks like. `canonicalize_or_reason` does two things at once:
            # it validates, AND it forces `envelope_fields` through
            # `parse_canonical(canonical_bytes(...))` -- and that round trip
            # is load-bearing for CORRECTNESS, not merely for the immutable-
            # copy property A4 named it for. `canonical_bytes` is a fixpoint
            # for ORDINARY values (`canonical_bytes(x) == canonical_bytes(
            # parse_canonical(canonical_bytes(x)))`), but NOT for a
            # pathological-yet-legal one: a mapping whose `__eq__` always
            # disagrees (so two distinct keys both serialise to the SAME
            # JSON string, e.g. `_CollidingKey` in `tests/
            # test_kalshi_encoder_fidelity_harness_001.py`) is ADMITTED
            # (every individual key/value is canonical on its own terms) but
            # its raw `canonical_bytes` legitimately contains a JSON object
            # with a DUPLICATE key -- which `json.loads` collapses
            # (last-value-wins) the instant anything re-parses it. Skipping
            # this round trip (an earlier version of this method did) means
            # `build_record`'s digest is computed over the UNCOLLAPSED, two-
            # key text while the SAME bytes, read back later, decode to the
            # COLLAPSED, one-key form -- `record_digest` and the reconciled
            # self-digest permanently disagree, and `close()` refuses the
            # segment. Running the round trip HERE, once, means whatever
            # `build_record` wraps and digests is ALREADY the form a future
            # read will reproduce -- a true fixpoint -- and it is still only
            # ONE admission-time encode (not the three a naive "encode
            # envelope_fields, discard, re-encode for the record digest,
            # re-encode again for the record's own reconciliation" shape
            # would cost): `build_record`+`digest_hex` is the ONLY encode of
            # the WRAPPED 17-field envelope, and `canonical_bytes(record)`
            # below reuses that same, already-collapsed structure for the
            # line actually written -- the same two-encode shape
            # `build_manifest`/`publish_manifest` already use for a self-
            # referential digest.
            try:
                payload_bytes, bad = canonicalize_or_reason(
                    envelope_fields,
                    _work_reserve=_RECORD_ENVELOPE_OVERHEAD_UNITS)
            except BaseException:                 # noqa: BLE001 - see below
                # A signal-class exception (KeyboardInterrupt/SystemExit)
                # from a hostile `.items()`/`__iter__` escaping even
                # `canonicalize_or_reason`'s own guard (which only catches
                # `Exception`, deliberately -- see its docstring). The
                # diagnostic identity still books a matching rejection here
                # before the exception propagates, exactly as production
                # always did for this window; it is RE-RAISED, not
                # swallowed, for the same reason the write-time boundary
                # below re-raises: Ctrl-C must propagate.
                self.accounting.reject_before_accept(
                    RejectReason.SERIALIZATION_FAILURE)
                raise
            if bad is not None:
                self.last_rejection_detail = bad
                self.accounting.reject_before_accept(RejectReason.NOT_CANONICAL)
                return RejectReason.NOT_CANONICAL
            # `parse_canonical` reconstructs a FRESH object graph from the
            # accepted bytes -- new dicts, new lists, new strings, and (per
            # the fixpoint argument above) already collapsed wherever the
            # caller's input was not itself a fixpoint. Nothing downstream
            # ever reads the caller's own `envelope_fields` object again.
            accepted_fields = parse_canonical(payload_bytes)
            try:
                record = build_record(
                    envelope_fields=accepted_fields, segment_id=self.segment_id,
                    environment=self.environment,
                    previous_record_digest=self._chain.prev_digest,
                    receive_ordinal=self._chain.ordinal)
                line = canonical_bytes(record)
            except Exception as exc:              # noqa: BLE001 - a refusal
                # Nothing has been written yet: `build_record`/`canonical_
                # bytes` can only fail before the first byte reaches `self.
                # _fh`. This is therefore a REJECTION -- the caller's payload
                # was not evidence-representable -- not a writer failure, and
                # the segment stays healthy for the next `submit()`. Reaching
                # here at all would mean `accepted_fields` (already proven
                # canonical by `canonicalize_or_reason` above) somehow fails
                # to re-encode when WRAPPED with a few extra scalar fields --
                # covered defensively, per the same "the encoder wins" rule
                # `canonicalize_or_reason` documents, but not expected to
                # fire in practice given `_RECORD_ENVELOPE_OVERHEAD_UNITS`'s
                # reserved budget above.
                self.last_rejection_detail = f"{type(exc).__name__}: {exc}"
                self.accounting.reject_before_accept(RejectReason.NOT_CANONICAL)
                return RejectReason.NOT_CANONICAL
            # A8 -- THE STATE DELTA IS COMPUTED BEFORE THE WRITE, NOT AFTER
            # IT. Every one of these is a pure function of `record` and the
            # current state; computing them here means the post-write tail is
            # a sequence of plain assignments that can be applied from a
            # `finally`, with no work left in it that could itself fail. The
            # previous shape ran seven statements (`_first_digest`,
            # `_last_digest`, `_stream_digest`, `_prev_digest`, `_ordinal`,
            # `accounting.written`, `return None`) AFTER the commitment point
            # and OUTSIDE every `try`: an interrupt landing among them left
            # the writer OPEN, `healthy`, `accepting`, `accounting.clean()`
            # -- nothing signalling a fault at all -- with `_ordinal` and
            # `_prev_digest` NOT advanced, so every subsequent record carried
            # a duplicate `receive_ordinal` and a stale
            # `previous_record_digest` and the on-disk chain was permanently
            # broken. Reproduced with a real SIGINT 6/6 (`written 194,
            # on_disk 200, state OPEN, clean true`). That is the SAME
            # ownership window A1 set out to eliminate, relocated rather
            # than closed.
            digest = record["record_digest"]
            new_chain = _ChainState(
                first_digest=self._chain.first_digest or digest,
                last_digest=digest,
                stream_digest=fold_stream_digest(
                    self._chain.stream_digest, digest),
                prev_digest=digest,
                ordinal=self._chain.ordinal + 1)
            new_written = self.accounting.written + 1
            committed = False
            booked_failure = False
            with self._defer_commit_signals:
                try:
                    # `pre_write_hook` runs INSIDE this try, not before it: tests
                    # use it to inject exactly the class of failure this block
                    # exists to contain (a durability/OS-level fault at the
                    # write boundary), and it must be treated identically to a
                    # real `self._fh.write()` failure -- invalidate the segment,
                    # never let it escape as a bare, unbooked exception.
                    if self.pre_write_hook is not None:
                        self.pre_write_hook(self)
                    # THE commitment point. Nothing before this line is durable;
                    # nothing after it may fail without invalidating the segment
                    # -- a caller must never be told ACCEPTED for an event whose
                    # bytes did not reach the file.
                    self._fh.write(line + b"\n")
                    # Set the INSTANT the bytes enter the stream, so the `finally`
                    # below applies the delta for every interruption after this
                    # point. `_since_flush`/`flush()` follow, deliberately, inside
                    # the try: a flush failure is an ordinary write fault and must
                    # invalidate the segment, but it must not un-book a record
                    # whose bytes are already in the stream.
                    committed = True
                    self._since_flush += 1
                    if self._since_flush >= self._flush_every:
                        self._fh.flush()
                        self._since_flush = 0
                except BaseException as exc:        # noqa: BLE001 - recorded
                    # An OS-level write failure AFTER a value was proven canonical
                    # is the one outcome that must invalidate the whole segment --
                    # continuing would append the NEXT record after a possibly
                    # half-written one and amplify the corruption, exactly as the
                    # old writer thread's terminal `except` did.
                    self._writer_error = exc
                    self.state = SegmentState.INVALID
                    self.accounting.fail_after_accept(RejectReason.WRITER_FAILED)
                    # The tail below must not ALSO book this event as written: it
                    # has been booked as `failed_after_accept`, the segment is
                    # INVALID, and nothing further will ever be appended to it.
                    booked_failure = True
                    if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                        # A1's OWN new risk, absent from the old design: the
                        # actual disk write now runs on the PRODUCER's thread,
                        # not an isolated background writer thread a real signal
                        # could never reach. `submit()`'s docstring promises "no
                        # path drops an event without returning a reason" for
                        # ORDINARY outcomes (a canonical-but-unwritable value, a
                        # genuine OS write failure) -- it does NOT promise to
                        # swallow a signal-class exception into an ordinary
                        # return value. Doing so would mean an operator's Ctrl-C
                        # (or a `SystemExit` from elsewhere in the process)
                        # landing on exactly this line silently turns into
                        # "the writer rejected one event" and the caller's loop
                        # keeps calling `submit()` -- which now IMMEDIATELY
                        # rejects every further call as `WRITER_FAILED` (the
                        # segment is correctly INVALID either way), but the
                        # process itself never learns it was asked to stop. The
                        # segment is still marked INVALID above, exactly as for
                        # any other write fault -- fail-closed evidence handling
                        # does not change -- but the exception itself is
                        # RE-RAISED rather than absorbed, matching ordinary
                        # Python signal semantics -- consistent with the encode
                        # step just above, which catches only `Exception` (not
                        # `BaseException`), so a signal-class exception landing
                        # during canonicalisation was ALREADY never caught there
                        # in the first place; this branch gives the write step
                        # the same property explicitly, since it has to catch
                        # `BaseException` broadly for the OS-failure case.
                        raise
                    return RejectReason.WRITER_FAILED
                finally:
                    # A8 -- THE TAIL, APPLIED FROM A `finally` ON THE SAME `try`
                    # AS THE WRITE. Two plain assignments of values computed
                    # before the write; nothing here can raise on its own, and
                    # nothing here can be skipped by an exception (signal-class
                    # or otherwise) arriving after the bytes entered the stream.
                    # The invariant is exactly: the chain and `written` advance
                    # if and only if `write()` returned, whatever happens next.
                    #
                    # ROUND 2 -- `finally` GUARANTEES ENTRY, NOT COMPLETION.
                    # The five chain stores that used to live here were
                    # individually interruptible: an async exception between
                    # two of them left the writer OPEN / healthy / clean with
                    # a partly-applied delta, and `append()` kept returning
                    # success for every subsequent record even though close()
                    # would refuse the whole segment hours later. Two changes
                    # make that state unreachable rather than merely unlikely:
                    #   1. the five chain fields are ONE immutable
                    #      `_ChainState`, so a single `STORE_ATTR` applies them
                    #      -- the chain cannot be half-advanced at all;
                    #   2. `accounting.written` -- the one field that lives on
                    #      another object and so cannot join them -- is guarded
                    #      by an `except BaseException` that marks the segment
                    #      INVALID and RE-RAISES. The residual window is one
                    #      bytecode boundary wide and it is now LOUD: the
                    #      writer refuses the very next `submit()` instead of
                    #      accepting thousands of records already guaranteed to
                    #      be discarded.
                    # The `try` is OUTSIDE the `if`, deliberately: the
                    # condition test is itself an interruptible boundary.
                    try:
                        if committed and not booked_failure:
                            self._chain = new_chain
                            self.accounting.written = new_written
                    except BaseException as exc:    # noqa: BLE001 - recorded
                        self._writer_error = exc
                        self.state = SegmentState.INVALID
                        raise
            return None

    @property
    def rotation_due(self) -> bool:
        """Has this segment reached a policy bound and become due for commit?

        Cheap and side-effect free, so a collector can ask on every event. The
        thresholds are policy inputs rather than a hard-coded cadence, and with
        none set this is always False — the caller decides, and gets a
        deterministic answer either way. `EventArchive`'s own defaults (see
        `EventArchive.__init__`) are no longer `None`: A3 requires a bounded
        default, not merely a bindable one, so a caller that never thinks
        about rotation still gets it.
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
            return self._close_locked()

    def _close_locked(self) -> dict:
        try:
            # A1: sealing IS taking `self._lock` and moving `self.state` off
            # OPEN -- there is no separate admission protocol left to seal.
            # Any `submit()` still running is, structurally, still holding
            # this same lock; `close()` cannot reach this line until it does
            # not, at which point that `submit()` has ALREADY reached one of
            # its terminal states (written, or a typed rejection) with the
            # lock released. There is no "still inflight" state for a
            # producer to be in by the time `_close_locked` acquires the lock
            # below -- the old seal-then-wait-for-`_inflight` protocol
            # (`_seal_admissions`) existed only because admission and commit
            # used to run on DIFFERENT threads; they are now the same call.
            with self._lock:
                if self.state is SegmentState.INVALID:
                    raise SegmentError(
                        f"segment is INVALID and cannot be closed: "
                        f"{self._writer_error!r} {self.accounting.to_dict()}")
                self.state = SegmentState.CLOSING
            return self._close_stages()
        except BaseException:
            # Ownership must not leak on ANY failure path, or one mid-stream
            # write error locks the partition out for every future process.
            self._release_lock()
            raise

    def _close_stages(self) -> dict:
        # `self.state` moved to CLOSING under `self._lock` in `_close_locked`,
        # and every `submit()` reads `self.state` under that SAME lock before
        # it does anything else -- so by the time this runs, no `submit()`
        # that has not already returned can ever reach the write path again.
        # The accounting is therefore frozen from here on: there is no
        # background writer thread to join, no queue to drain, and no
        # "pending" state to measure -- `WriterAccounting.pending` is always
        # 0 by construction (see its docstring) rather than something this
        # method has to prove.
        snapshot = WriterAccounting(**vars(self.accounting))
        if self._writer_error is not None:
            self.state = SegmentState.INVALID
            raise SegmentError(f"writer failed: {self._writer_error!r} "
                               f"{self.accounting.to_dict()}")
        # `clean` is the ONLY state in which this may be published as evidence,
        # and it is gated on the DURABLE side of the identity only —
        # `pending == 0 and failed_after_accept == 0` — never on
        # `admission_holds()`. An accepted-but-unwritten event is a loss the
        # producer was told did not happen, and it must never appear behind
        # close_status "clean": that check stays fatal. `failed_after_accept`
        # is the only way that can happen now (a canonical value whose OS
        # write itself failed, see `submit()`) -- and `submit()` already
        # invalidates the segment the instant that occurs, so `_writer_error`
        # above will already have raised before this is even reached whenever
        # `failed_after_accept` is nonzero. This check stays as the second,
        # independent gate rather than being removed, on the same fail-closed
        # principle every other redundant check in this module follows.
        if not snapshot.clean():
            self.state = SegmentState.INVALID
            raise SegmentError(
                f"{snapshot.failed_after_accept} accepted event(s) were "
                f"not written; refusing to publish this segment as a clean "
                f"close: {snapshot.to_dict()}")
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
            # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8: the SAME deferral
            # `submit()` puts around its commitment region, applied to
            # close()'s -- once per segment, so the cost is irrelevant.
            # Measured mechanism: an asynchronous exception delivered inside
            # `GzipFile`'s internal `BufferedWriter.flush()` can leave
            # `write_pos` un-advanced after the underlying
            # `_WriteBufferStream.write` has ALREADY compressed and emitted
            # those bytes; the next flush (here, or in `_close_fh`) emits
            # them a second time. Observed on the real writer under the
            # `asyncexc` harness: 43 records with duplicate
            # `receive_ordinal`s appended after record 2,411, and
            # `verify_chain` reporting "chain break". Reconciliation catches
            # it and the segment fails CLOSED -- but "fails closed" here
            # again means every durable record becomes unpublishable, which
            # is precisely the outcome BLOCKER 1 exists to stop. An
            # operator's Ctrl-C during shutdown lands exactly here.
            with self._defer_commit_signals:
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

        # Reconcile what we believe we wrote against what is on disk. Single
        # fd-based read for size+digest -- see `evidence_fs.
        # stat_and_sha256_bounded`.
        size, file_hash, why = evidence_fs.stat_and_sha256_bounded(self.events_path)
        if why is not None:
            self.state = SegmentState.INVALID
            raise SegmentError(f"reconciliation could not read the event file: {why}")
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
        # A1: there is no background writer thread to guard against any more
        # -- `submit()` runs synchronously, under `self._lock`, on whichever
        # caller's thread invokes it. `_close_locked` takes `self._lock` to
        # move `self.state` off OPEN before this is ever reached, and every
        # `submit()` re-checks `self.state` under that SAME lock before it
        # writes a single byte -- so a `submit()` that starts AFTER the state
        # transition is rejected outright, and one already in flight when it
        # happened cannot still be running by the time `close()` observes the
        # lock is free. There is no interleaving in which a second,
        # concurrently-running `submit()` could be admitting a dual-writer
        # condition here. `_ownership_held_by_live_writer` is kept as an
        # always-False attribute, not removed, so any external check of it
        # stays source-compatible.
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

    # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8: the encode gets its OWN stage
    # marker. It can raise (`CanonicalError`, over a `subscription_metadata`
    # value that fits at its own root but not nested inside a 20-key manifest
    # body -- see `_MANIFEST_METADATA_WORK_RESERVE`), and with no marker of
    # its own `self.failed_stage` still held whatever the LAST successful
    # stage was: `close()` reported the failure as stage `'fsync'`, an
    # event-file stage that had already succeeded several steps earlier. An
    # operator was sent to look at event-file durability for a manifest
    # serialisation refusal.
    _s("manifest_encode")
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

# KALSHI-ARCHIVE-CORE-REMEDIATION-003B A5 -- RESIDUE DECOMPRESSION BOUND.
#
# `read_segment_records` is reachable over UNCOMMITTED evidence -- no
# manifest, no writer lock, no prior admission gate -- via `verify_archive`'s
# own residue-inspection loop over every uncommitted segment directory it
# finds. `evidence_fs.bounded_read` already caps the COMPRESSED bytes read
# from disk, but `zlib.decompressobj.decompress()` called with no
# `max_length` decompresses AS FAR AS THE INPUT ALLOWS, unbounded in its
# OUTPUT regardless of how small that compressed input was: a 2.18 MB
# gzip-compressed residue expanded to 1.79 GB / 20.6s peak RSS in the
# reviewer's measurement, entirely within the compressed-bytes bound. A
# hostile (or merely accidentally-highly-compressible) uncommitted
# directory must not turn a read-only verification pass into an OOM/DoS
# primitive.
#
# Chosen comfortably above any legitimate single segment's decompressed
# size (production rotation policy keeps live segments well under this) and
# far below where decompressing genuinely untrusted residue becomes a
# practical amplification attack.
_MAX_RESIDUE_DECODED_BYTES = 64 * 1024 * 1024            # 64 MiB
_MAX_RESIDUE_DECODED_LINES = 500_000
# The chunk size `dec.decompress(chunk, max_length)` is fed per call. Kept
# small relative to the byte cap so the cap is enforced with fine granularity
# rather than being able to overshoot by up to one chunk's worth of
# amplification on a single call.
_DECODE_INPUT_CHUNK = 65536


def _decompress_prefix(data: bytes, *, max_decoded_bytes=None):
    """Decompress as far as the stream is intact, OR until `max_decoded_bytes`
    of OUTPUT has been produced, whichever comes first.

    Returns `(bytes, consumed, eof, capped, errored)`.

    `capped` is True exactly when the byte ceiling stopped this call before
    the stream's own EOF (or fault) was reached -- the caller
    (`read_segment_records`) surfaces that as a residue classification
    distinct from an ordinary torn/malformed stream (see A6), not as a
    silent truncation indistinguishable from one.

    `errored` is True exactly when a genuine `zlib`/`EOFError` fault
    occurred somewhere in this call (which routes to `_salvage_prefix`, see
    below) -- as opposed to this call simply running out of INPUT with no
    fault at all, which happens on every call against a LIVE, still-growing
    segment: `gzip.GzipFile.flush()` (what `SegmentWriter` calls on its
    flush cadence) emits a `Z_SYNC_FLUSH` marker, never a gzip trailer, so
    `dec.eof` never becomes true for a segment that has not been `close()`d
    yet -- `not eof` on its own conflated "genuinely torn/corrupted" with
    "this is a live segment, working exactly as intended" (KALSHI-ARCHIVE-
    REPLAY-INTEGRITY-001 A4). `errored` is the caller's way to tell those
    apart: no fault ever occurred, so whatever prefix decoded is exactly as
    trustworthy as a fully-terminated stream's.

    The previous attempt at recovering a torn stream was dead code, and the
    measurement said so: it caught the failing 64 KiB chunk and re-fed it in
    512-byte slices **into the same decompressobj**, which is permanently in
    error state once it has raised. Every retry iteration raised immediately
    and contributed nothing, so a mid-stream fault still lost the whole
    chunk — 664 records recovered where 998 were available, and a small
    segment recovered 0. The suite asserted no recovered count, so nothing
    caught it.

    A `decompressobj` is never reused after it raises. The fast path feeds
    large chunks of COMPRESSED input; on the first fault the object is
    DISCARDED and a fresh one re-reads from the start in small increments
    (`_salvage_prefix`), so the recovered prefix is bounded by the salvage
    chunk rather than by the fast-path chunk. Both paths now ALSO bound
    DECOMPRESSED output via `max_length`, streamed rather than requested in
    one call, so neither path can be turned into an unbounded-memory read
    regardless of how the input is shaped.
    """
    import zlib

    if max_decoded_bytes is None:
        max_decoded_bytes = _MAX_RESIDUE_DECODED_BYTES

    dec = zlib.decompressobj(31)
    out = []
    total = 0
    consumed_input = 0
    try:
        pos = 0
        pending = b""
        while True:
            if not pending:
                if pos >= len(data):
                    break
                pending = data[pos:pos + _DECODE_INPUT_CHUNK]
                pos += len(pending)
            budget = max_decoded_bytes - total
            if budget <= 0:
                return b"".join(out), consumed_input, False, True, False
            piece = dec.decompress(pending, budget)
            out.append(piece)
            total += len(piece)
            consumed_input = pos - len(dec.unconsumed_tail)
            # `max_length` may leave undecoded output buffered internally
            # (`unconsumed_tail` non-empty) when this call's budget ran out
            # before `pending` was fully expanded -- keep it and ask for
            # more budget/output on the NEXT iteration rather than dropping
            # it or advancing past unconsumed compressed bytes.
            pending = dec.unconsumed_tail
            if dec.eof:
                break
        if not dec.eof:
            # Clean exhaustion, zero faults -- see `errored`'s docstring
            # above (the live/`Z_SYNC_FLUSH` case). `errored=False`.
            return b"".join(out), consumed_input, False, False, False
        out.append(dec.flush())
    except (zlib.error, EOFError):
        return _salvage_prefix(data, max_decoded_bytes=max_decoded_bytes)
    # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A4: `pos`, not `len(data)`. A
    # multi-member gzip file (a legacy collector may append several members
    # to one events file) reaches `dec.eof` after consuming only the FIRST
    # member -- `pos` is how much of `data` this call actually fed to the
    # decompressor; anything beyond `pos` (member 2..N) was never offered to
    # it at all and is NOT reflected in `dec.unused_data`. The previous
    # `len(data) - len(dec.unused_data)` treated every one of those unfed
    # trailing bytes as "consumed", so the caller's `data = data[consumed:]`
    # skipped straight past every subsequent member -- a 300-member legacy
    # file yielded 1 of 300 records through `archive-migrate-legacy`.
    consumed = pos - len(dec.unused_data)
    return b"".join(out), consumed, True, False, False


def _salvage_prefix(data: bytes, *, max_decoded_bytes=None):
    """`errored` is always `True` in every return from this function: it is
    reachable ONLY from `_decompress_prefix`'s `except (zlib.error,
    EOFError)` handler, so by construction a genuine fault already occurred
    before this ever runs.
    """
    import zlib

    if max_decoded_bytes is None:
        max_decoded_bytes = _MAX_RESIDUE_DECODED_BYTES

    dec = zlib.decompressobj(31)
    out = []
    total = 0
    fed = 0
    for i in range(0, len(data), _SALVAGE_CHUNK):
        pending = data[i:i + _SALVAGE_CHUNK]
        fed = i + len(pending)
        try:
            while True:
                budget = max_decoded_bytes - total
                if budget <= 0:
                    return b"".join(out), i, False, True, True
                piece = dec.decompress(pending, budget)
                out.append(piece)
                total += len(piece)
                pending = dec.unconsumed_tail
                if dec.eof or not pending:
                    break
        except (zlib.error, EOFError):
            break                    # terminal: STOP, never reuse this object
        if dec.eof:
            break
    if dec.eof:
        try:
            out.append(dec.flush())
        except (zlib.error, EOFError):
            pass
        # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A4: `fed`, not `len(data)` --
        # the SAME multi-member miscount as `_decompress_prefix`'s fix above,
        # mirrored here: `fed` is how many bytes THIS loop actually offered
        # to the decompressor before it reached `dec.eof`, not how many bytes
        # `data` happens to hold in total.
        consumed = fed - len(dec.unused_data)
        return b"".join(out), consumed, True, False, True
    return b"".join(out), len(data), False, False, True


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
    # Routed through `evidence_fs.bounded_read`: the previous shape
    # (`events_path.is_file()` then `events_path.read_bytes()`) was a
    # check-then-use TOCTOU on the SAME symlink-to-FIFO/regular-file swap
    # `verify_segment`'s containment block exists to refuse, plus no size
    # bound at all -- a 1.5 GiB events file was read whole into memory here
    # even though `evidence_fs.bounded_read` refuses the same artifact at
    # 1 GiB everywhere else in this module.
    data, why = evidence_fs.bounded_read(events_path)
    if why is not None:
        # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A4: this branch used to be
        # reached identically by "the file does not exist" (an ordinary,
        # brand-new OPEN segment with nothing written yet) AND "the file
        # exists but could not be read" (permission denied, or any other
        # `evidence_fs.bounded_read` refusal) -- both left every diagnostic
        # attribute at the SAME "nothing to see" defaults, including
        # `stream_fully_decoded = True`. `_classify_residue` (below) trusted
        # that flag, so a chmod-000 residue -- a file that VISIBLY EXISTS
        # (`verify_segment`'s `allow_open` branch only reaches this function
        # after its own `presence()` check already confirmed that) but
        # cannot be READ -- verified `chain_ok=True` over zero recovered
        # records and was classified `RECOVERABLE_INTACT`: a fail-OPEN
        # verdict for evidence nobody could actually prove the content of.
        # `unreadable=True` here is what `_classify_residue` now checks
        # FIRST, ahead of everything else, so this case is FAIL-CLOSED
        # (`RESIDUE_UNREADABLE`) instead of indistinguishable from empty.
        read_segment_records.last_unreadable = 0
        read_segment_records.capped = False
        read_segment_records.stream_fully_decoded = True
        read_segment_records.decoded_bytes = 0
        read_segment_records.original_size = 0
        read_segment_records.decode_had_error = False
        read_segment_records.unreadable = True
        return []
    original_size = len(data)
    text = ""
    total_decoded = 0
    capped = False
    # KALSHI-ARCHIVE-CORE-REMEDIATION-003B A6: True unless the loop below
    # breaks EARLY (a decoded-byte cap, or a stream that ended before its
    # own EOF) -- i.e. True exactly when every gzip member in this file was
    # decompressed to genuine completion. A residue whose bytes decode
    # fully but whose CONTENT is still broken (a deleted middle record) is
    # a different, chain-level finding `verify_segment`'s residue
    # classification checks separately -- this flag is about the BYTES
    # only, never inferred from parsed record content.
    #
    # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A4: `stream_fully_decoded` alone
    # can no longer decide "torn" vs "intact" -- see `decode_had_error`
    # below, and `_decompress_prefix`'s docstring, for why a LIVE segment
    # legitimately never reaches `dec.eof` (`Z_SYNC_FLUSH`, not a trailer)
    # and must not be classified the same way a genuinely corrupted one is.
    stream_fully_decoded = True
    # True the instant ANY `_decompress_prefix` call in this loop routed
    # through `_salvage_prefix` -- i.e. a REAL `zlib`/`EOFError` fault
    # occurred somewhere in this file, as opposed to this read simply
    # running out of available bytes with zero faults (a live, still-open
    # segment; see A4). `_classify_residue` uses this, not
    # `stream_fully_decoded`, to decide whether a prefix that did not reach
    # a formal end-of-stream is corruption or merely "not finalised yet".
    decode_had_error = False
    if original_size == 0:
        # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 ROUND 2 -- THE ONE INPUT
        # THAT VIOLATED `RESIDUE_RECOVERABLE_INTACT`'S OWN NEW DEFINITION.
        # A8 redefined that label to mean "the compressed stream reached a
        # real gzip trailer" and `verify_segment` prints that sentence
        # verbatim. A zero-byte file contains no trailer -- the loop below
        # simply never runs, so `stream_fully_decoded` stayed at its `True`
        # default and a 0-byte events file was certified `recoverable_intact,
        # chain_valid=True`, the single input for which the label was a
        # provable falsehood. It is not "ambiguous, so leave it": ambiguity
        # is exactly what `RESIDUE_UNTERMINATED` was added for. Its reason
        # text -- "never reached a gzip trailer, so its END IS UNKNOWN. This
        # is the NORMAL shape of a live collector's currently-open segment"
        # -- describes a brand-new open segment's empty file precisely.
        stream_fully_decoded = False
    while data:
        # KALSHI-ARCHIVE-CORE-REMEDIATION-003B A5: the budget is CUMULATIVE
        # across every gzip member in this one events file (see
        # `TestLegacyMultiMember` -- a legacy collector can append several
        # members to one file), not reset per `_decompress_prefix` call, or
        # an attacker could evade the cap entirely by splitting the bomb
        # across many small members.
        remaining_budget = _MAX_RESIDUE_DECODED_BYTES - total_decoded
        if remaining_budget <= 0:
            capped = True
            stream_fully_decoded = False
            break
        decoded, consumed, eof, hit_cap, errored = _decompress_prefix(
            data, max_decoded_bytes=remaining_budget)
        decode_had_error = decode_had_error or errored
        total_decoded += len(decoded)
        try:
            text += decoded.decode("utf-8")
        except UnicodeDecodeError:
            text += decoded.decode("utf-8", errors="ignore")
        if hit_cap:
            capped = True
            stream_fully_decoded = False
            break
        if not eof:
            stream_fully_decoded = False
            break                    # stream ended mid-member: nothing follows
        data = data[consumed:] if consumed else b""
    records = []
    lines = [ln for ln in text.split("\n") if ln.strip()]
    for i, line in enumerate(lines):
        # KALSHI-ARCHIVE-CORE-REMEDIATION-003B A5: a record-count cap
        # alongside the byte cap -- a decoded stream near the byte ceiling
        # but built of pathologically short lines could still produce an
        # unbounded number of parsed record objects in memory otherwise.
        if i >= _MAX_RESIDUE_DECODED_LINES:
            capped = True
            break
        try:
            records.append(parse_canonical(line))
        except Exception:
            break                    # an unparseable line ends the readable prefix
    # How many decodable lines the reader had to abandon. Reported rather than
    # silently dropped: a torn tail and a clean file must not look alike.
    read_segment_records.last_unreadable = len(lines) - len(records)
    # KALSHI-ARCHIVE-CORE-REMEDIATION-003B A5/A6: whether this read was cut
    # off by the decoded-bytes/record-count ceiling rather than reaching a
    # genuine stream EOF or the first unparseable line. A6's residue
    # classification reads this to distinguish "unsafe, over the limit"
    # residue from an ordinary torn or malformed one -- the two look
    # identical from `records`/`last_unreadable` alone.
    read_segment_records.capped = capped
    read_segment_records.stream_fully_decoded = stream_fully_decoded
    read_segment_records.decoded_bytes = total_decoded
    read_segment_records.original_size = original_size
    read_segment_records.decode_had_error = decode_had_error
    read_segment_records.unreadable = False
    return records


read_segment_records.last_unreadable = 0
read_segment_records.capped = False
read_segment_records.stream_fully_decoded = True
read_segment_records.decoded_bytes = 0
read_segment_records.original_size = 0
read_segment_records.decode_had_error = False
read_segment_records.unreadable = False


# --- verification -----------------------------------------------------------------


# KALSHI-ARCHIVE-CORE-REMEDIATION-003B A6 -- RESIDUE SEMANTICS.
#
# Before this, EVERY uncommitted (manifest-less) segment directory presented
# identically in three ways, regardless of what was actually on disk:
#
#   1. `chain_valid` was hardcoded `False` -- an actually intact,
#      chain-valid residue and one with a deliberately deleted middle
#      record were INDISTINGUISHABLE (both `False`), because `verify_chain`
#      was never even called for the `allow_open=True` path.
#   2. A malformed (not-even-gzip) residue reported `records_read: 0` with
#      the SAME boilerplate `reasons` an ordinary, brand-new, genuinely
#      empty OPEN segment gets -- byte-for-byte identical to "nothing
#      written yet".
#   3. A torn (mid-write-crash) residue silently recovered whatever prefix
#      it could and reported it as ordinary `records_read`, with no
#      "torn"/"truncated" signal anywhere in the returned shape.
#
# These four labels are the minimum the milestone brief names, derived from
# `read_segment_records`'s own diagnostics (`stream_fully_decoded`,
# `decoded_bytes`, `original_size`, `capped`, `last_unreadable`) plus a REAL
# `verify_chain` call over whatever records were actually recovered -- never
# inferred from a hardcoded default.
RESIDUE_RECOVERABLE_INTACT = "recoverable_intact"
RESIDUE_TORN_PARTIAL = "torn_partial"
RESIDUE_MALFORMED = "malformed"
RESIDUE_UNSAFE_OVER_LIMIT = "unsafe_over_limit"
# KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A4: a FIFTH, FAIL-CLOSED classification
# KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- A SIXTH LABEL: "the prefix is
# readable, the END IS UNKNOWN".
#
# A4 was right that a LIVE segment must stop being reported as corruption
# (`GzipFile.flush()` emits `Z_SYNC_FLUSH`, never a trailer, so a healthy
# open segment never reaches `dec.eof`), and it fixed that by requiring
# `decode_had_error` before calling a stream torn. But a CLEAN BYTE
# TRUNCATION produces no zlib fault either: measured `(eof=False,
# errored=False)` on 696 of 6,000 randomly corrupted streams, and
# `errored=False` for EVERY pure tail truncation. So A4 did not merely
# reclassify live segments -- it made truncation report
# `RESIDUE_RECOVERABLE_INTACT, chain_valid=True`. A 30-record segment
# truncated after record 6 (24 records physically removed) reported
# `recoverable_intact, chain_valid=True, records_read=6`; so did 2-byte,
# 8-byte and 0-byte files. Every one of those was `torn_partial` at
# 321c719. And because `verify_segment` appended an explanatory reason for
# UNREADABLE/UNSAFE_OVER_LIMIT/MALFORMED/TORN_PARTIAL but NOT for
# RECOVERABLE_INTACT, an operator looking at a residue truncated to HIDE
# records saw only "segment has no manifest".
#
# Truncation is not exotic. Page-granular writeback, a partial `write()`
# and filesystem crash-truncation all leave a byte-perfect prefix; so does
# anyone deleting the tail on purpose. It is genuinely indistinguishable
# from a live segment from the bytes alone -- which is exactly why the
# honest label is neither "intact" nor "torn" but "unterminated": the
# prefix decoded with zero faults and every line parsed and the chain held,
# AND the stream never reached a gzip trailer, so nothing establishes that
# what was read is all there was. `RESIDUE_RECOVERABLE_INTACT` now means
# what its name claims: a real trailer was reached.
RESIDUE_UNTERMINATED = "unterminated"
# A4's FIFTH, FAIL-CLOSED classification
# for a residue that VISIBLY EXISTS (`verify_segment`'s `presence()` check
# already confirmed that before this is ever reached) but could not be READ
# at all -- permission denied, or any other `evidence_fs.bounded_read`
# refusal. Previously indistinguishable from a genuinely empty, brand-new
# OPEN segment (both left `read_segment_records`'s diagnostics at the same
# "nothing to see" defaults), which let a chmod-000 residue certify
# `chain_valid=True` and classify `RECOVERABLE_INTACT` -- a fail-OPEN verdict
# for evidence nobody could prove the content of. This is fail-CLOSED: an
# operator must never read "intact" for content that was never examined.
RESIDUE_UNREADABLE = "unreadable"

# The closed set `_classify_residue` can return. Declared once so nothing has
# to hand-enumerate it (`SegmentVerdict.residue_classification`'s comment did,
# and silently omitted `RESIDUE_UNTERMINATED` in the commit that added it).
RESIDUE_CLASSIFICATIONS = (
    RESIDUE_RECOVERABLE_INTACT,
    RESIDUE_UNTERMINATED,
    RESIDUE_TORN_PARTIAL,
    RESIDUE_MALFORMED,
    RESIDUE_UNSAFE_OVER_LIMIT,
    RESIDUE_UNREADABLE,
)


def _classify_residue(*, chain_ok: bool) -> str:
    """The residue classification for the file `read_segment_records` JUST
    read -- reads its diagnostic attributes (set fresh on every call, see
    `read_segment_records`), so this must be called immediately after, on
    the SAME file, with nothing else calling `read_segment_records` in
    between.
    """
    if read_segment_records.unreadable:
        # Checked FIRST, ahead even of `capped`: a file that could not be
        # read at all was never examined enough to know whether it is over
        # any limit. See `RESIDUE_UNREADABLE`'s module-level comment.
        return RESIDUE_UNREADABLE
    if read_segment_records.capped:
        # A decoded-byte/record-count ceiling is a safety refusal, not an
        # ordinary content finding -- it must never be downgraded to "torn"
        # or "intact" just because SOME prefix was recoverable before the
        # ceiling fired.
        return RESIDUE_UNSAFE_OVER_LIMIT
    if not read_segment_records.stream_fully_decoded:
        # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A4: `stream_fully_decoded` on
        # its own used to mean "torn" here -- but a LIVE, still-open segment
        # NEVER reaches a formal gzip trailer (`GzipFile.flush()` emits
        # `Z_SYNC_FLUSH`, not one), so `RESIDUE_RECOVERABLE_INTACT` was
        # structurally unreachable for the single most common residue an
        # operator actually inspects: a collector's current-hour segment
        # while it is still running. `decode_had_error` is what actually
        # distinguishes them: it is True only when a REAL `zlib`/`EOFError`
        # fault occurred (routing through `_salvage_prefix`); a clean read
        # that simply ran out of available bytes with zero faults is not
        # corruption, it is "not finalised yet" -- and falls through to the
        # SAME last-line/chain checks an intact read gets, below.
        if read_segment_records.decode_had_error:
            if (read_segment_records.decoded_bytes == 0
                    and read_segment_records.original_size > 0):
                # Nothing at all was recoverable from a genuinely non-empty
                # input, and a fault occurred before any of it decoded -- the
                # signature of input that was never a valid gzip stream to
                # begin with (a stray/corrupted file), not a real write that
                # simply stopped partway through one.
                return RESIDUE_MALFORMED
            return RESIDUE_TORN_PARTIAL
        # No fault occurred; this read merely ran out of bytes. Fall through
        # to the SAME content-level checks a fully-decoded stream gets --
        # but A8 does NOT let it reach `RESIDUE_RECOVERABLE_INTACT`, because
        # nothing here established that the prefix read is the whole prefix
        # written. See `RESIDUE_UNTERMINATED`.
        unterminated = True
    else:
        unterminated = False
    if read_segment_records.last_unreadable > 0:
        if not (unterminated and read_segment_records.last_unreadable == 1):
            # The compressed bytes decoded to completion, but at least one
            # decoded LINE was not parseable canonical JSON -- content-level
            # corruption inside an otherwise-complete gzip stream.
            return RESIDUE_TORN_PARTIAL
        # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 ROUND 2 -- THE PRODUCTION
        # FLUSH CADENCE. Exactly ONE unreadable line at the very end of a
        # stream that never reached a trailer is the ORDINARY shape of a
        # healthy live segment at the shipped default (`flush_every=256`,
        # which `EventArchive` never overrides): zlib spills its buffer on
        # whatever byte boundary it likes, so the readable prefix genuinely
        # ends inside a record. Measured over 40 inspections of a healthy,
        # uncorrupted 4,000-record open segment: 12/40 with 128-char record
        # bodies, 34/40 with 1024-char bodies -- and every one of them was
        # reported `torn_partial`, identically at d004c01. So A4's stated
        # goal ("a live segment must not be reported as corruption") was
        # defeated on every real collector, and only held in the tests
        # because every one of them used `flush_every=1`, where a partial
        # line cannot exist.
        #
        # This is NOT a downgrade of a corruption finding: a truncated file
        # whose tail happens to cut one record is byte-for-byte identical to
        # this, which is the entire reason `RESIDUE_UNTERMINATED` exists --
        # the end is unknown either way, and claiming "torn" asserts more
        # than the bytes support. TWO or more unreadable lines cannot come
        # from a single interrupted spill and stay `TORN_PARTIAL`.
    if not chain_ok:
        # The bytes decoded fully AND every line parsed -- but the records
        # do not chain (a deletion, insertion, reorder, or a copied
        # record). Real, structural brokenness that "torn" already names;
        # this is not a fifth category, it is the SAME finding reached
        # through a different mechanism than a truncated write.
        return RESIDUE_TORN_PARTIAL
    # A8: the two remaining outcomes differ ONLY in whether the compressed
    # stream reached a real gzip trailer. Everything else about them is
    # identical -- zero decode faults, every line parsed, the chain held --
    # which is precisely why they must not share a label.
    if unterminated:
        return RESIDUE_UNTERMINATED
    return RESIDUE_RECOVERABLE_INTACT


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
    # KALSHI-ARCHIVE-CORE-REMEDIATION-003B A6 / A4 / A8: only set for a
    # residue verdict (`allow_open=True`, no manifest). Exactly one member of
    # `RESIDUE_CLASSIFICATIONS` -- do not re-enumerate them here, which is
    # how this comment came to omit `RESIDUE_UNTERMINATED` in the very commit
    # that added it -- or `None` for an ordinary (committed,
    # manifest-bearing) verdict where the concept does not apply.
    residue_classification: str | None = None
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
    #
    # `root=None` is NOT a supported way to skip containment. It used to be:
    # `if root is not None:` let a caller pass `root=None` explicitly and
    # silently disable the entire containment-and-symlink block below,
    # reaching `file_sha256`'s raw `open()` (before this milestone's fix) on
    # a symlink-to-FIFO events path with zero containment checking -- a
    # documented, reachable public argument shape that hung forever. There is
    # no legitimate reason for a canonical verifier to run unbounded, so the
    # sentinel meaning "derive" and the value meaning "explicitly skip" are
    # no longer the same falsy check: an explicit `None` is refused outright.
    if root is _DERIVE_ROOT:
        root = directory.parent.parent
    elif root is None:
        raise SegmentError(
            "verify_segment(root=None) is refused: None is not a supported "
            "way to skip containment checking. Omit `root` to derive it, or "
            "pass the archive root explicitly.")
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
            # KALSHI-ARCHIVE-CORE-REMEDIATION-003B A6: a REAL `verify_chain`
            # call over whatever was actually recovered -- never a
            # hardcoded `False` -- plus the byte/content-level diagnostics
            # `read_segment_records` now exposes, classify what kind of
            # residue this actually is rather than presenting every
            # uncommitted directory identically. This does NOT bless the
            # residue as committed evidence (still not entered anywhere
            # `expected`/`records_expected` reads) and does NOT change
            # `valid` (residue is never gating) -- it makes the difference
            # between "an ordinary in-progress OPEN segment", "a crash left
            # a partial write", "this is not even a gzip file", and "this
            # exceeded the safety ceiling and was only partially read"
            # actually visible instead of collapsing to one shape.
            residue_records = read_segment_records(events_path)
            residue_chain = verify_chain(
                residue_records, segment_id=seg_id, environment=environment)
            classification = _classify_residue(chain_ok=residue_chain.ok)
            residue_reasons = [
                "segment has no manifest and is therefore not committed"]
            if classification == RESIDUE_UNREADABLE:
                residue_reasons.append(
                    "UNREADABLE_RESIDUE: the event file exists but could not "
                    "be read (permission denied, or another filesystem "
                    "refusal) -- FAIL-CLOSED. This is NOT reported as "
                    "RECOVERABLE_INTACT: content that was never examined "
                    "must never be certified as intact")
            elif classification == RESIDUE_UNSAFE_OVER_LIMIT:
                residue_reasons.append(
                    "UNSAFE_OVER_LIMIT_RESIDUE: this residue's decoded size "
                    "or record count exceeded the safety ceiling "
                    f"({_MAX_RESIDUE_DECODED_BYTES} decoded bytes / "
                    f"{_MAX_RESIDUE_DECODED_LINES} records) -- decompression "
                    "was deliberately stopped early, so only a PARTIAL, "
                    "capped prefix was read; this is a refusal to keep "
                    "decompressing untrusted residue, not a report that the "
                    "residue itself only holds this much evidence")
            elif classification == RESIDUE_MALFORMED:
                residue_reasons.append(
                    "MALFORMED_RESIDUE: the event file is not a valid gzip "
                    "stream at all -- a stray or corrupted file, not a real "
                    "write that stopped partway through one")
            elif classification == RESIDUE_TORN_PARTIAL:
                residue_reasons.append(
                    "TORN_PARTIAL_RESIDUE: only a partial prefix could be "
                    "recovered -- the compressed stream ended before its "
                    "own EOF, a decoded line was not parseable canonical "
                    "JSON, or the recovered records do not chain (a "
                    "deletion, insertion, reorder, or a copied record)")
            elif classification == RESIDUE_UNTERMINATED:
                residue_reasons.append(
                    "UNTERMINATED_RESIDUE: the prefix read is readable and "
                    "chain-valid, but the compressed stream never reached a "
                    "gzip trailer, so its END IS UNKNOWN. This is the NORMAL "
                    "shape of a live collector's currently-open segment "
                    "(GzipFile.flush() emits Z_SYNC_FLUSH, never a trailer) "
                    "-- and it is also, byte for byte, the shape of a "
                    "segment whose tail was TRUNCATED, by a crash or "
                    "deliberately. Nothing in the bytes distinguishes them, "
                    "so this is NOT reported as RECOVERABLE_INTACT: how many "
                    "records were written and lost cannot be established "
                    "from this file alone. At the shipped flush cadence "
                    "(flush_every=256) the readable prefix routinely ends "
                    "INSIDE a record, so one trailing unparseable line is "
                    "expected here and is not itself evidence of corruption")
            elif classification == RESIDUE_RECOVERABLE_INTACT:
                # A8: RECOVERABLE_INTACT had NO reason line of its own, so a
                # residue truncated to hide records presented to an operator
                # as the bare "segment has no manifest" boilerplate. Now
                # every classification says what it means.
                residue_reasons.append(
                    "RECOVERABLE_INTACT_RESIDUE: the compressed stream "
                    "reached a real gzip trailer, every decoded line parsed "
                    "as canonical JSON, and the recovered records chain. "
                    "The segment is still UNCOMMITTED -- there is no "
                    "manifest -- but its content is complete as written")
            v = SegmentVerdict(
                seg_id, SegmentState.OPEN, False, residue_reasons,
                None, len(residue_records))
            # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- `chain_valid` MUST
            # NOT BE True FOR A RESIDUE NOBODY COULD READ. `verify_chain([])`
            # returns `ok=True` (an empty chain is trivially consistent) and
            # that value was copied here unconditionally, so a live segment
            # holding 4 durable records, `chmod 000`, verified as
            # `records_read: 0, chain_valid: true, verdict: VALID,
            # warnings: []`. The free-text reason above said "unreadable",
            # but a consumer branching on the FIELD -- the entire reason the
            # field exists -- read `true` for evidence nobody opened. The
            # same argument applies to a residue only PARTIALLY read because
            # it blew the safety ceiling: the chain held over the capped
            # prefix, which says nothing about the rest.
            if classification in (RESIDUE_UNREADABLE, RESIDUE_UNSAFE_OVER_LIMIT):
                v.chain_valid = False
            else:
                v.chain_valid = residue_chain.ok
            v.residue_classification = classification
            return v
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

    # A SINGLE fd-based read proves the size and the digest are of the
    # identical bytes (see `evidence_fs.stat_and_sha256_bounded`) -- the
    # previous shape (`events_path.stat()` then `file_sha256`'s own
    # `open()`) was two independent filesystem accesses, each its own
    # TOCTOU window, only one of which routed through the reviewed
    # evidence-filesystem abstraction at all.
    size, actual_digest, why = evidence_fs.stat_and_sha256_bounded(events_path)
    if why is not None:
        # A mode-0 or directory events file used to raise straight out of the
        # public verifier and out of `recover_current_head`, so the documented
        # repair path died with a raw OSError that no operator tool catches.
        reasons.append(f"event file is unreadable ({why})")
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
           "uncommitted_segment_detail": [], "uncommitted_records_present": 0,
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
    # `warnings` is initialized HERE, before the residue-diagnostics below,
    # rather than at its historical spot beside `_abandoned_residue` --
    # both write into the SAME list now.
    warnings: list = []
    # KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect G (part 2): a durable,
    # chain-valid, but never-manifested segment (the review's "3,001
    # durable chain-valid records that never published a manifest") used to
    # be visible ONLY as a bare id in `uncommitted_segments` -- no record
    # count, no chain-validity, `records_read` summed only the COMMITTED
    # segments so it stayed 0 regardless of how much real evidence was
    # sitting right there, and the overall `verdict` stayed VALID with
    # EMPTY `reasons`. That is "structurally ignoring it": nothing in the
    # returned shape distinguished "an empty stub directory" from "3,001
    # real records an operator needs to resolve". This does NOT
    # automatically bless the residue as committed evidence (it still never
    # enters `expected`/`results`/`records_expected` -- only the head can do
    # that) and does NOT gate `verdict` (an in-progress OPEN segment from a
    # live collector is a completely ordinary state, not a defect) -- it
    # makes the residue INSPECTABLE as a typed per-segment state instead of
    # a name with no properties.
    uncommitted_detail = []
    for seg_id in uncommitted:
        residue_dir = discovered[seg_id]
        rv = verify_segment(residue_dir, environment=environment,
                            allow_open=True, root=root)
        uncommitted_detail.append({
            "segment_id": seg_id, "state": rv.state.value,
            "records_read": rv.records_read, "chain_valid": rv.chain_valid,
            "reasons": rv.reasons,
            # KALSHI-ARCHIVE-CORE-REMEDIATION-003B A6: exposed here too, not
            # only inside `reasons`' free text, so a caller reading this
            # structured shape (rather than grepping strings) can branch on
            # it directly.
            "residue_classification": rv.residue_classification,
        })
    uncommitted_records_present = sum(
        d["records_read"] for d in uncommitted_detail)
    if uncommitted_records_present:
        # KALSHI-ARCHIVE-CORE-REMEDIATION-003B A6: this warning used to
        # name 'archive-adopt' as the resolution path -- but `archive-adopt`
        # is bounded (by its own docstring, deliberately) to
        # ORPHANED_COMMITTED_SEGMENT (a segment WITH a manifest the head
        # does not mention) and structurally refuses EVERY uncommitted
        # residue, every time, because manifest-less residue is not the
        # state it exists for. That sent an operator to a command that
        # cannot act on the state it was named for. No command in this
        # codebase accepts manifest-less residue today -- so this says
        # that plainly instead of naming one that does not apply.
        warnings.append(
            f"UNCOMMITTED_SEGMENT_RESIDUE: {uncommitted_records_present} "
            f"durable record(s) across {len(uncommitted_detail)} "
            "uncommitted segment(s) are on disk but not part of the "
            "committed history -- never automatically adopted. There is NO "
            "operator command that accepts this state: 'archive-adopt' is "
            "bounded to ORPHANED_COMMITTED_SEGMENT (a manifest-bearing "
            "segment the head does not mention) and refuses manifest-less "
            "residue like this unconditionally. Resolving it -- whether "
            "that means letting the writer reopen and continue the "
            "segment, or discarding it -- requires direct, reviewed "
            f"operator access, not a scripted command: {uncommitted_detail}")
    # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- AN UNREADABLE OR ONLY
    # PARTIALLY-READ RESIDUE MUST CROSS THE ARCHIVE LEVEL. The warning above
    # is gated on `uncommitted_records_present`, i.e. on records the verifier
    # SUCCEEDED in reading -- which is exactly zero for a residue it could
    # not open. A `chmod 000` events file holding four durable records
    # therefore produced `verdict: VALID, warnings: []`: the whole finding
    # lived in a per-segment `reasons` string an archive-level consumer never
    # looks at. "Nothing could be read" is not the same shape of fact as
    # "nothing was there", and the archive summary has to be able to say so.
    # It stays a WARNING, not a `reason` -- residue never gates the verdict,
    # by the same design decision A6 documented -- but it can no longer be
    # invisible.
    unproven_residue = [
        d for d in uncommitted_detail
        if d["residue_classification"] in (RESIDUE_UNREADABLE,
                                           RESIDUE_UNSAFE_OVER_LIMIT)]
    if unproven_residue:
        warnings.append(
            f"UNPROVEN_RESIDUE_CONTENT: {len(unproven_residue)} uncommitted "
            "segment(s) hold residue whose content could NOT be established "
            "-- unreadable (permission or another filesystem refusal), or "
            "only partially decoded because it exceeded the safety ceiling. "
            "`records_read` is a floor, not a count, and `chain_valid` is "
            "reported False for these because no chain was ever verified "
            "over the whole file. Do not read a low record count here as "
            "evidence that little was written: "
            f"{[(d['segment_id'], d['residue_classification']) for d in unproven_residue]}")
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
        # KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect G (part 2): the TYPED
        # per-segment state `uncommitted_segments` (a bare id list) never
        # carried -- one entry per id in `uncommitted`, each independently
        # verified with `allow_open=True` so a durable-but-never-manifested
        # segment's record count and chain validity are inspectable, not
        # merely its existence.
        uncommitted_segment_detail=uncommitted_detail,
        uncommitted_records_present=uncommitted_records_present,
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
