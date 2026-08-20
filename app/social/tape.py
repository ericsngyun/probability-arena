"""SOCIAL-TAPE-001 — the immutable raw tape.

Deliberately the SAME shape as `app/realtime/segment.py`, which is the house
pattern for a digest-chained, segment-rotated, replayable archive. That module
is FROZEN (PROD-ACTIVITY-PROFILE-001 is capturing live Kalshi windows against
it) and is not touched here; this is a separate, smaller writer for a different
stream that reuses its ideas and its canonical-encoding primitives:

  * every record carries a ``record_digest`` over an EXPLICIT field list, so a
    field added later cannot silently fall outside the digest;
  * the chain is anchored by a ``genesis_digest`` derived from segment identity,
    so record #1 of one segment cannot be spliced into another;
  * an ``ordered_stream_digest`` folds POSITION in, so a reorder is detectable
    even though every self-digest still verifies;
  * segments rotate, close, and commit a manifest atomically (temp + rename +
    directory fsync), and each manifest names its predecessor;
  * the raw payload is preserved VERBATIM as base64 of the exact bytes received,
    beside a ``raw_content_hash`` over those same bytes, so a later re-parse can
    be audited against what actually arrived.

Record kinds
------------
The tape is not only artifacts. It records what happened to the *stream*, too,
because a gap that is never recorded is indistinguishable from a quiet market:

  ``artifact``       one collected item
  ``redelivery``     the stream handed us something we already had
  ``stream_event``   connect / disconnect / rule change / backfill boundary
  ``absence``        a typed statement that we know we were NOT collecting

CONTAINS NO SIGNAL.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from app.realtime.book import utcnow
from app.realtime.canonical import canonical_datetime, digest_hex
from app.social.artifact import SocialArtifact
from app.social.timebase import ProcessEpoch

__all__ = [
    "TAPE_RECORD_SCHEMA_VERSION",
    "TAPE_MANIFEST_SCHEMA_VERSION",
    "TAPE_WRITER_VERSION",
    "RecordKind",
    "SegmentState",
    "TapeError",
    "TapeIntegrityError",
    "TapeImmutabilityError",
    "TAPE_RECORD_FIELDS",
    "genesis_digest",
    "build_tape_record",
    "verify_record_self_digest",
    "fold_stream_digest",
    "ChainVerdict",
    "verify_chain",
    "build_manifest",
    "SocialTapeWriter",
    "read_segment_records",
    "verify_segment",
    "replay",
]

TAPE_RECORD_SCHEMA_VERSION = 1
TAPE_MANIFEST_SCHEMA_VERSION = 1
TAPE_WRITER_VERSION = "social-tape-writer/1"

EVENTS_FILENAME = "events.jsonl.gz"
MANIFEST_FILENAME = "manifest.json"
MANIFEST_TEMP_SUFFIX = ".tmp"


class TapeError(RuntimeError):
    """Base class for tape faults."""


class TapeIntegrityError(TapeError):
    """A record or chain failed verification."""


class TapeImmutabilityError(TapeError):
    """Something tried to write into a sealed segment."""


class RecordKind(str, Enum):
    ARTIFACT = "artifact"
    REDELIVERY = "redelivery"
    STREAM_EVENT = "stream_event"
    ABSENCE = "absence"


class SegmentState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


#: Digest-bearing fields, listed explicitly. ``record_digest`` is excluded
#: because it is the output. A field added later that is not added here would
#: not be bound, which is exactly the silent-drift failure this list prevents.
TAPE_RECORD_FIELDS = (
    "schema_version",
    "environment",
    "segment_id",
    "record_ordinal",
    "record_kind",
    "process_epoch_id",
    "written_at_utc",
    "ingestion_version",
    "universe_id",
    "payload",
    "previous_record_digest",
)

REQUIRED_TAPE_RECORD_FIELDS = TAPE_RECORD_FIELDS + ("record_digest",)


def genesis_digest(*, segment_id: str, environment: str) -> str:
    """Chain anchor derived from segment identity, never a constant sentinel.

    A constant would let record #1 of one segment be spliced into another and
    still chain cleanly.
    """

    return "genesis:" + digest_hex(
        {
            "schema_version": TAPE_RECORD_SCHEMA_VERSION,
            "segment_id": segment_id,
            "environment": environment,
        }
    )


def build_tape_record(
    *,
    payload: Mapping[str, Any],
    record_kind: RecordKind,
    segment_id: str,
    environment: str,
    universe_id: str,
    process_epoch_id: str,
    previous_record_digest: str,
    record_ordinal: int,
    written_at_utc: str,
    ingestion_version: str,
) -> dict[str, Any]:
    """Assemble one canonical, chained tape record.

    Nothing derived from runtime state (path, pid, buffer offset) enters the
    digest: those differ between the write and any later verification, so
    binding them would make a faithful archive fail its own check.
    """

    record: dict[str, Any] = {
        "schema_version": TAPE_RECORD_SCHEMA_VERSION,
        "environment": environment,
        "segment_id": segment_id,
        "record_ordinal": record_ordinal,
        "record_kind": record_kind.value,
        "process_epoch_id": process_epoch_id,
        "written_at_utc": written_at_utc,
        "ingestion_version": ingestion_version,
        "universe_id": universe_id,
        "payload": dict(payload),
        "previous_record_digest": previous_record_digest,
    }
    record["record_digest"] = digest_hex(
        {k: record[k] for k in TAPE_RECORD_FIELDS}
    )
    return record


def verify_record_self_digest(record: Mapping[str, Any]) -> bool:
    recorded = record.get("record_digest")
    if not isinstance(recorded, str) or len(recorded) != 64:
        return False
    try:
        return recorded == digest_hex(
            {k: record.get(k) for k in TAPE_RECORD_FIELDS}
        )
    except Exception:
        # A tamper-evidence path that CRASHES on attacker-controlled input is
        # fail-open by crash. Return a verdict instead.
        return False


def fold_stream_digest(previous: str, record_digest: str) -> str:
    """Running digest over the ORDER of records, not merely their contents.

    Two records in reversed order produce the same set of self-digests but
    a different fold, which is what makes a reorder detectable.
    """

    return hashlib.sha256(
        (previous + ":" + record_digest).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ChainVerdict:
    ok: bool
    record_count: int = 0
    first_record_digest: str | None = None
    last_record_digest: str | None = None
    ordered_stream_digest: str | None = None
    broken_at: int | None = None
    reason: str | None = None


def verify_chain(
    records: Iterable[Mapping[str, Any]],
    *,
    segment_id: str,
    environment: str,
) -> ChainVerdict:
    """Walk the chain. Self-digests alone are not enough — order is bound too."""

    expected_prev = genesis_digest(segment_id=segment_id, environment=environment)
    stream = expected_prev
    first: str | None = None
    last: str | None = None
    count = 0

    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            return ChainVerdict(
                False, count, first, last, stream, index, "record is not an object"
            )
        missing = [f for f in REQUIRED_TAPE_RECORD_FIELDS if f not in record]
        if missing:
            return ChainVerdict(
                False, count, first, last, stream, index,
                f"record is missing {missing}",
            )
        if record.get("schema_version") != TAPE_RECORD_SCHEMA_VERSION:
            return ChainVerdict(
                False, count, first, last, stream, index,
                f"unknown record schema_version {record.get('schema_version')!r}",
            )
        if record.get("segment_id") != segment_id:
            return ChainVerdict(
                False, count, first, last, stream, index,
                "record belongs to a different segment",
            )
        if record.get("record_ordinal") != index:
            return ChainVerdict(
                False, count, first, last, stream, index,
                "record_ordinal does not match position",
            )
        if not verify_record_self_digest(record):
            return ChainVerdict(
                False, count, first, last, stream, index, "self-digest mismatch"
            )
        if record.get("previous_record_digest") != expected_prev:
            return ChainVerdict(
                False, count, first, last, stream, index, "chain link broken"
            )

        digest = str(record["record_digest"])
        expected_prev = digest
        stream = fold_stream_digest(stream, digest)
        if first is None:
            first = digest
        last = digest
        count += 1

    # An EMPTY segment has no ordered stream, and must not report the genesis
    # anchor as one. The writer records `None` for a segment it closed with no
    # records, so returning the anchor here made a legitimately empty segment
    # fail its own verification — a fail-CLOSED bug, but a bug: an empty
    # segment is exactly what a clean shutdown during a quiet window produces,
    # and it must verify.
    return ChainVerdict(
        True, count, first, last, stream if count else None, None, None
    )


MANIFEST_FIELDS = (
    "manifest_schema_version",
    "environment",
    "segment_id",
    "writer_version",
    "opened_at",
    "closed_at",
    "record_count",
    "artifact_count",
    "first_record_digest",
    "last_record_digest",
    "ordered_stream_digest",
    "event_file_size_bytes",
    "event_file_sha256",
    "universe_digest",
    "process_epoch",
    "previous_segment_digest",
    "close_status",
)


def build_manifest(
    *,
    environment: str,
    segment_id: str,
    opened_at: str,
    closed_at: str | None,
    record_count: int,
    artifact_count: int,
    first_record_digest: str | None,
    last_record_digest: str | None,
    ordered_stream_digest: str | None,
    event_file_size_bytes: int,
    event_file_sha256: str | None,
    universe_fields: Mapping[str, Any],
    process_epoch: Mapping[str, Any],
    previous_segment_digest: str | None = None,
    close_status: str = "clean",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "manifest_schema_version": TAPE_MANIFEST_SCHEMA_VERSION,
        "environment": environment,
        "segment_id": segment_id,
        "writer_version": TAPE_WRITER_VERSION,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "record_count": record_count,
        "artifact_count": artifact_count,
        "first_record_digest": first_record_digest,
        "last_record_digest": last_record_digest,
        "ordered_stream_digest": ordered_stream_digest,
        "event_file_size_bytes": event_file_size_bytes,
        "event_file_sha256": event_file_sha256,
        # The source universe is PINNED into the manifest. Without it, a later
        # reader cannot tell whether a quiet segment means "nothing happened"
        # or "the rule set changed under us" — which would invalidate every
        # comparison across segments.
        "universe_digest": digest_hex(dict(universe_fields)),
        "process_epoch": dict(process_epoch),
        "previous_segment_digest": previous_segment_digest,
        "close_status": close_status,
    }
    body["universe"] = dict(universe_fields)
    body["manifest_digest"] = digest_hex({k: body[k] for k in MANIFEST_FIELDS})
    return body


def verify_manifest_self_digest(manifest: Mapping[str, Any]) -> bool:
    recorded = manifest.get("manifest_digest")
    if not isinstance(recorded, str) or len(recorded) != 64:
        return False
    try:
        return recorded == digest_hex({k: manifest.get(k) for k in MANIFEST_FIELDS})
    except Exception:
        return False


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_manifest(directory: Path, manifest: Mapping[str, Any]) -> Path:
    """Commit a manifest atomically: temp file, fsync, rename, fsync dir.

    A half-written manifest must never be observable, because a manifest is
    the statement that a segment is complete.
    """

    target = directory / MANIFEST_FILENAME
    payload = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        dir=str(directory), prefix=".manifest-", suffix=MANIFEST_TEMP_SUFFIX
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    _fsync_directory(directory)
    return target


class SocialTapeWriter:
    """Append-only, digest-chained, segment-rotated writer.

    Synchronous by design, following the house decision in
    KALSHI-ARCHIVE-SYNCHRONOUS-SIMPLIFICATION: a queue between the collector
    and the file is a place where records go to be lost on Ctrl-C, and the
    measured durability of the synchronous form was strictly better.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        environment: str,
        segment_id: str,
        universe_fields: Mapping[str, Any],
        process_epoch: ProcessEpoch,
        ingestion_version: str,
        max_records_per_segment: int = 50_000,
        previous_segment_digest: str | None = None,
    ) -> None:
        if environment not in ("test", "demo", "production"):
            raise TapeError(f"unknown environment {environment!r}")
        if max_records_per_segment < 1:
            raise TapeError("max_records_per_segment must be positive")

        self._root = Path(root)
        self._environment = environment
        self._segment_id = segment_id
        self._universe_fields = dict(universe_fields)
        self._epoch = process_epoch
        self._ingestion_version = ingestion_version
        self._max_records = max_records_per_segment
        self._previous_segment_digest = previous_segment_digest

        self._directory = self._root / environment / segment_id
        self._directory.mkdir(parents=True, exist_ok=True)
        self._events_path = self._directory / EVENTS_FILENAME
        if self._events_path.exists():
            raise TapeImmutabilityError(
                f"segment {segment_id} already has an events file; a tape "
                "segment is written once and never appended to a second time"
            )
        if (self._directory / MANIFEST_FILENAME).exists():
            raise TapeImmutabilityError(
                f"segment {segment_id} is already committed and is immutable"
            )

        self._state = SegmentState.OPEN
        self._opened_at = canonical_datetime(utcnow())
        self._ordinal = 0
        self._artifact_count = 0
        self._previous_record_digest = genesis_digest(
            segment_id=segment_id, environment=environment
        )
        self._stream_digest = self._previous_record_digest
        self._first_record_digest: str | None = None
        self._handle = gzip.open(self._events_path, "wb")

    # -- properties ---------------------------------------------------------

    @property
    def state(self) -> SegmentState:
        return self._state

    @property
    def segment_id(self) -> str:
        return self._segment_id

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def record_count(self) -> int:
        return self._ordinal

    def rotation_due(self) -> bool:
        return self._ordinal >= self._max_records

    # -- appending ----------------------------------------------------------

    def append_artifact(self, artifact: SocialArtifact) -> dict[str, Any]:
        record = self._append(
            RecordKind.ARTIFACT,
            artifact.to_json(),
            ingestion_version=artifact.ingestion_version,
        )
        self._artifact_count += 1
        return record

    def append_redelivery(
        self, artifact: SocialArtifact, *, verdict: str
    ) -> dict[str, Any]:
        """Record a duplicate delivery rather than dropping it.

        A redelivery rate that is never written cannot later be told apart from
        a delivery gap, and both look like "the market went quiet".
        """

        payload = {
            "verdict": verdict,
            "message_identity": list(artifact.message_identity),
            "delivery_sequence": artifact.delivery_sequence,
            "subscription_generation": artifact.subscription_generation,
            "our_received_at": artifact.our_received_at.to_json(),
            "raw_content_hash": artifact.raw_content_hash,
        }
        return self._append(
            RecordKind.REDELIVERY,
            payload,
            ingestion_version=artifact.ingestion_version,
        )

    def append_stream_event(
        self, event_type: str, detail: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._append(
            RecordKind.STREAM_EVENT,
            {"event_type": event_type, "detail": dict(detail)},
            ingestion_version=self._ingestion_version,
        )

    def append_absence(
        self, reason: str, detail: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Record that we know we were NOT collecting.

        Typed absence at the TAPE level, mirroring doctrine 10 at the field
        level: a window with no records is ambiguous, and a window with an
        explicit absence record is not.
        """

        return self._append(
            RecordKind.ABSENCE,
            {"reason": reason, "detail": dict(detail or {})},
            ingestion_version=self._ingestion_version,
        )

    def _append(
        self,
        kind: RecordKind,
        payload: Mapping[str, Any],
        *,
        ingestion_version: str,
    ) -> dict[str, Any]:
        if self._state is not SegmentState.OPEN:
            raise TapeImmutabilityError(
                f"segment {self._segment_id} is {self._state.value}; a sealed "
                "segment never accepts another record"
            )
        record = build_tape_record(
            payload=payload,
            record_kind=kind,
            segment_id=self._segment_id,
            environment=self._environment,
            universe_id=str(self._universe_fields.get("universe_id", "")),
            process_epoch_id=self._epoch.epoch_id,
            previous_record_digest=self._previous_record_digest,
            record_ordinal=self._ordinal,
            written_at_utc=canonical_datetime(utcnow()),
            ingestion_version=ingestion_version,
        )
        line = json.dumps(
            record, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        self._handle.write(line + b"\n")

        digest = str(record["record_digest"])
        self._previous_record_digest = digest
        self._stream_digest = fold_stream_digest(self._stream_digest, digest)
        if self._first_record_digest is None:
            self._first_record_digest = digest
        self._ordinal += 1
        return record

    # -- closing ------------------------------------------------------------

    def close(self, *, close_status: str = "clean") -> dict[str, Any]:
        """Seal the segment and commit its manifest. Idempotent-by-refusal."""

        if self._state is SegmentState.CLOSED:
            raise TapeImmutabilityError(
                f"segment {self._segment_id} is already closed"
            )
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        _fsync_directory(self._directory)

        manifest = build_manifest(
            environment=self._environment,
            segment_id=self._segment_id,
            opened_at=self._opened_at,
            closed_at=canonical_datetime(utcnow()),
            record_count=self._ordinal,
            artifact_count=self._artifact_count,
            first_record_digest=self._first_record_digest,
            last_record_digest=(
                self._previous_record_digest if self._ordinal else None
            ),
            ordered_stream_digest=self._stream_digest if self._ordinal else None,
            event_file_size_bytes=self._events_path.stat().st_size,
            event_file_sha256=file_sha256(self._events_path),
            universe_fields=self._universe_fields,
            process_epoch=self._epoch.to_json(),
            previous_segment_digest=self._previous_segment_digest,
            close_status=close_status,
        )
        publish_manifest(self._directory, manifest)
        self._state = SegmentState.CLOSED
        return manifest

    def __enter__(self) -> "SocialTapeWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._state is SegmentState.OPEN:
            self.close(close_status="clean" if exc_type is None else "aborted")


# -- reading ----------------------------------------------------------------


def read_segment_records(events_path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Read every record from a segment's event file."""

    path = Path(events_path)
    records: list[dict[str, Any]] = []
    with gzip.open(path, "rb") as handle:
        for line_no, line in enumerate(handle):
            text = line.strip()
            if not text:
                continue
            try:
                records.append(json.loads(text.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TapeIntegrityError(
                    f"{path}: record {line_no} is not readable JSON: {exc}"
                ) from exc
    return records


@dataclass(frozen=True)
class SegmentVerdict:
    ok: bool
    segment_id: str
    environment: str
    record_count: int = 0
    artifact_count: int = 0
    chain: ChainVerdict | None = None
    reason: str | None = None
    manifest: Mapping[str, Any] | None = None


def verify_segment(directory: str | os.PathLike[str]) -> SegmentVerdict:
    """Verify a committed segment end to end.

    Manifest self-digest, event-file hash, chain walk, and agreement between
    the manifest's declared counts/digests and the file's actual ones. Any
    disagreement is a failure: a manifest that says 400 records over a file of
    399 is not a rounding error, it is a truncated tape.
    """

    d = Path(directory)
    manifest_path = d / MANIFEST_FILENAME
    events_path = d / EVENTS_FILENAME

    if not manifest_path.exists():
        return SegmentVerdict(
            False, d.name, "", reason="segment has no manifest (never committed)"
        )
    if not events_path.exists():
        return SegmentVerdict(
            False, d.name, "", reason="segment has a manifest but no event file"
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return SegmentVerdict(False, d.name, "", reason=f"manifest unreadable: {exc}")

    if not verify_manifest_self_digest(manifest):
        return SegmentVerdict(
            False, d.name, str(manifest.get("environment", "")),
            reason="manifest self-digest mismatch", manifest=manifest,
        )

    segment_id = str(manifest["segment_id"])
    environment = str(manifest["environment"])

    actual_hash = file_sha256(events_path)
    if actual_hash != manifest.get("event_file_sha256"):
        return SegmentVerdict(
            False, segment_id, environment,
            reason="event file hash does not match the manifest",
            manifest=manifest,
        )

    try:
        records = read_segment_records(events_path)
    except TapeIntegrityError as exc:
        return SegmentVerdict(
            False, segment_id, environment, reason=str(exc), manifest=manifest
        )

    chain = verify_chain(records, segment_id=segment_id, environment=environment)
    if not chain.ok:
        return SegmentVerdict(
            False, segment_id, environment, len(records), chain=chain,
            reason=f"chain broken at {chain.broken_at}: {chain.reason}",
            manifest=manifest,
        )
    if chain.record_count != manifest.get("record_count"):
        return SegmentVerdict(
            False, segment_id, environment, chain.record_count, chain=chain,
            reason="manifest record_count disagrees with the event file",
            manifest=manifest,
        )
    if chain.ordered_stream_digest != manifest.get("ordered_stream_digest"):
        return SegmentVerdict(
            False, segment_id, environment, chain.record_count, chain=chain,
            reason="ordered stream digest disagrees with the manifest",
            manifest=manifest,
        )

    artifacts = sum(
        1 for r in records if r.get("record_kind") == RecordKind.ARTIFACT.value
    )
    if artifacts != manifest.get("artifact_count"):
        return SegmentVerdict(
            False, segment_id, environment, chain.record_count, artifacts, chain,
            reason="manifest artifact_count disagrees with the event file",
            manifest=manifest,
        )

    return SegmentVerdict(
        True, segment_id, environment, chain.record_count, artifacts, chain,
        manifest=manifest,
    )


def replay(directory: str | os.PathLike[str]) -> Iterator[SocialArtifact]:
    """Deterministically replay a VERIFIED segment's artifacts, in order.

    Refuses to yield anything from a segment that does not verify. A replay
    that tolerates a broken chain is not a replay, it is a guess.
    """

    d = Path(directory)
    verdict = verify_segment(d)
    if not verdict.ok:
        raise TapeIntegrityError(
            f"refusing to replay {d}: {verdict.reason}"
        )
    for record in read_segment_records(d / EVENTS_FILENAME):
        if record.get("record_kind") != RecordKind.ARTIFACT.value:
            continue
        yield SocialArtifact.from_json(record["payload"])
