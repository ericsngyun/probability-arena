"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 — explicit offline import of legacy evidence.

Every archive written before the genesis protocol has no root of trust, no
generation chain, and no per-record commitment. Those archives still hold real
observations, and the temptation is to let the collector pick them up and carry
on. That temptation is the defect: "the head is missing, therefore this must be
a new archive" is exactly the inference that let a rebuilt history certify its
own deletions, and an automatic migration is the same inference wearing a
friendlier name.

So there is no automatic path. Nothing in `EventArchive`, collector startup,
canonical replay, `verify_archive` or the commit path reaches this module —
asserted by a test, not by convention. An operator runs it deliberately:

    kalshi-realtime-archive-migrate-legacy --source <dir> --dest <root> --dry-run
    kalshi-realtime-archive-migrate-legacy --source <dir> --dest <root> --confirm

What it does, and what it refuses to pretend:

* The legacy source is opened read-only and is never written, moved or deleted.
* Every legacy artifact is digested as found, so the import records what it
  actually read rather than what it hoped to read.
* Legacy evidence is verified to the MAXIMUM the old format supports, which is
  materially less than the new format supports, and the limitations are recorded
  as data rather than omitted.
* The destination is a NEW governed archive with its own genesis. Imported
  records land in segments committed through the ordinary protocol, so they
  carry the new guarantees FROM THE IMPORT FORWARD — and the provenance record
  says exactly that.

The one thing this must never do is imply the historical evidence had
guarantees it did not have. An imported segment is trustworthy as "these are
the bytes that were on disk at import time, and here is their digest". It is
not trustworthy as "these bytes were chained when they were written", because
they were not.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.realtime.archive_head import (
    ArchiveHeadError,
    genesis_path,
    initialize_archive,
    read_genesis,
)
from app.realtime.canonical import (
    CanonicalError,
    canonical_bytes,
    canonical_datetime,
    digest_hex,
)
from app.realtime.segment import (
    EVENTS_FILENAME,
    SegmentError,
    SegmentWriter,
    non_canonical_reason,
)

LEGACY_IMPORT_SCHEMA_VERSION = 1
PROVENANCE_FILENAME = "legacy-import-provenance.json"

# What the pre-genesis format could and could not prove. Recorded verbatim into
# the provenance record so a later reader is never left to assume.
LEGACY_LIMITATIONS = (
    "no archive genesis: the source had no root of trust, so nothing binds it "
    "to a particular archive identity",
    "no generation chain: the source had no per-transition commitment, so a "
    "deleted or reordered file cannot be detected from the source alone",
    "no per-record chain digest: records were not linked, so a deleted interior "
    "record leaves no contradiction in the source",
    "no authoritative record count: the source carried no committed count, so "
    "a truncated file and a short file are indistinguishable",
    "record order is file order: the source did not pin an ordering commitment",
)


class LegacyImportError(RuntimeError):
    """The legacy source cannot be imported as evidence."""


@dataclass
class LegacyScan:
    """What the source actually contains, measured rather than assumed."""

    source: str
    source_format: str
    artifact_digests: dict = field(default_factory=dict)
    records_readable: int = 0
    records_rejected: int = 0
    rejection_reasons: dict = field(default_factory=dict)
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    files: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "source_format": self.source_format,
            "artifact_digests": dict(self.artifact_digests),
            "records_readable": self.records_readable,
            "records_rejected": self.records_rejected,
            "rejection_reasons": dict(self.rejection_reasons),
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "files": list(self.files),
        }


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _legacy_files(source: Path) -> list:
    """Every gzipped event file under the source, in a deterministic order."""
    if not source.is_dir():
        raise LegacyImportError(f"{source} is not a directory")
    out = [p for p in sorted(source.rglob(EVENTS_FILENAME))
           if p.is_file() and not p.is_symlink()]
    if not out:
        raise LegacyImportError(
            f"{source} contains no {EVENTS_FILENAME} files; this does not look "
            "like a legacy archive")
    return out


def _read_legacy_records(path: Path) -> tuple:
    """Decode a legacy gzip event file. Returns (records, rejected, reasons)."""
    records, rejected, reasons = [], 0, {}
    try:
        with gzip.open(path, "rb") as fh:
            raw = fh.read()
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        return [], 0, {f"unreadable:{type(exc).__name__}": 1}
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except Exception as exc:              # noqa: BLE001 - counted
            rejected += 1
            key = f"undecodable:{type(exc).__name__}"
            reasons[key] = reasons.get(key, 0) + 1
            continue
        if not isinstance(value, dict):
            rejected += 1
            reasons["not_an_object"] = reasons.get("not_an_object", 0) + 1
            continue
        records.append(value)
    return records, rejected, reasons


def scan_legacy(source) -> LegacyScan:
    """Read-only measurement of a legacy archive. Writes nothing, anywhere."""
    source = Path(source)
    scan = LegacyScan(source=str(source), source_format="pre-genesis-jsonl-gzip")
    stamps = []
    for path in _legacy_files(source):
        rel = str(path.relative_to(source))
        scan.files.append(rel)
        scan.artifact_digests[rel] = _file_digest(path)
        records, rejected, reasons = _read_legacy_records(path)
        scan.records_readable += len(records)
        scan.records_rejected += rejected
        for k, v in reasons.items():
            scan.rejection_reasons[k] = scan.rejection_reasons.get(k, 0) + v
        for rec in records:
            stamp = rec.get("collector_receive_time") or rec.get("received_at_utc")
            if isinstance(stamp, str):
                stamps.append(stamp)
    if stamps:
        scan.first_timestamp = min(stamps)
        scan.last_timestamp = max(stamps)
    return scan


def _import_segment_id(index: int) -> str:
    return f"legacy.import-{index:04d}"


def migrate_legacy_archive(source, dest, *, environment: str,
                           archive_identity: str = "kalshi-realtime",
                           confirm: bool = False) -> dict:
    """Import a legacy archive into a NEW governed archive. Never in place.

    `confirm=False` is a dry run: it measures the source, reports exactly what
    would be imported, and touches nothing.
    """
    source, dest = Path(source), Path(dest)
    if source.resolve() == dest.resolve():
        raise LegacyImportError(
            "source and destination are the same directory; a legacy import "
            "creates a NEW governed archive and never rewrites the source")
    scan = scan_legacy(source)

    plan = {
        "schema_version": LEGACY_IMPORT_SCHEMA_VERSION,
        "mode": "confirm" if confirm else "dry-run",
        "environment": environment,
        "destination": str(dest),
        "source_limitations": list(LEGACY_LIMITATIONS),
        "guarantee_boundary": (
            "Records imported here carry the new archive's guarantees FROM THE "
            "IMPORT FORWARD ONLY. They were not chained, counted or committed "
            "when they were originally written, and this import does not and "
            "cannot make them so. The source digests below state what was read; "
            "they do not state that what was read was complete."),
        **scan.to_dict(),
    }
    if not confirm:
        plan["records_imported"] = 0
        plan["would_import"] = scan.records_readable
        return plan

    if os.path.lexists(genesis_path(dest, environment)):
        raise LegacyImportError(
            f"{dest} is already an initialized archive; a legacy import creates "
            "its own governed genesis and will not extend an existing history")
    genesis = initialize_archive(dest, environment,
                                 archive_identity=archive_identity)

    imported, refused = 0, 0
    refusal_reasons: dict = {}
    for index, rel in enumerate(scan.files):
        records, _, _ = _read_legacy_records(source / rel)
        if not records:
            continue
        writer = SegmentWriter(
            dest, environment=environment, segment_id=_import_segment_id(index),
            partition_identity=f"env={environment}/legacy-import/file={index:04d}",
            # venue is "legacy", not "kalshi", and the segment id says so too.
            # These records were IMPORTED, not collected, and labelling them as
            # ordinary kalshi evidence is precisely the implication this module
            # exists to avoid. The originating venue is preserved in metadata.
            subscription_metadata={"venue": "legacy",
                                   "origin_venue": "kalshi",
                                   "legacy_source_file": rel,
                                   "legacy_source_digest": scan.artifact_digests[rel]},
            expected_archive_id=genesis["archive_id"])
        try:
            for rec in records:
                fields = _legacy_to_envelope(rec)
                reason = non_canonical_reason(fields)
                if reason is not None:
                    refused += 1
                    refusal_reasons["not_canonical"] = \
                        refusal_reasons.get("not_canonical", 0) + 1
                    continue
                if writer.submit(fields) is None:
                    imported += 1
                else:
                    refused += 1
                    refusal_reasons["writer_rejected"] = \
                        refusal_reasons.get("writer_rejected", 0) + 1
            writer.close()
        except (SegmentError, ArchiveHeadError, CanonicalError):
            try:
                writer.close()
            except Exception:                  # noqa: BLE001 - already failing
                pass
            raise

    provenance = {
        **plan,
        "archive_id": genesis["archive_id"],
        "records_imported": imported,
        "records_refused_at_import": refused,
        "import_refusal_reasons": refusal_reasons,
        "migration_timestamp": canonical_datetime(datetime.now(timezone.utc)),
    }
    provenance["provenance_digest"] = digest_hex(
        {k: v for k, v in provenance.items() if k != "provenance_digest"})
    out = dest / f"env={environment}" / PROVENANCE_FILENAME
    out.write_bytes(canonical_bytes(provenance))
    return provenance


def _legacy_to_envelope(rec: dict) -> dict:
    """Map a legacy record onto the current envelope. Lossy fields stay opaque."""
    return {
        "connection_generation": rec.get("connection_id") or rec.get(
            "connection_generation"),
        "subscription_id": rec.get("sid") or rec.get("subscription_id"),
        "subscription_generation": rec.get("subscription_generation"),
        "message_type": rec.get("event_type") or rec.get("message_type"),
        "market_ticker": rec.get("market_ticker"),
        "seq": rec.get("seq"),
        "received_at_utc": rec.get("collector_receive_time") or rec.get(
            "received_at_utc"),
        "received_monotonic_ns": rec.get("receive_monotonic_ns") or rec.get(
            "received_monotonic_ns"),
        "raw_event": rec.get("raw"),
        # The whole legacy record is kept opaque so nothing is silently dropped.
        "normalized_event": {k: v for k, v in rec.items() if k != "raw"},
    }
