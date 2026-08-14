"""Append-only JSONL telemetry sink (001A) — non-SQLite by mandate.

The one hard constraint of the milestone: the sink must never take the
SQLite lock it measures, so events go to a local append-only JSONL file
outside the git tree (`~/probability-arena-telemetry/sqlite-writes.jsonl`,
overridable via ``SQLITE_TELEMETRY_DIR``). Each emit is exactly one
unbuffered ``os.write()`` on a raw ``O_APPEND|O_WRONLY`` fd — POSIX atomic
append plus the local-FS inode lock keep whole lines from interleaving; the
4096-byte line cap is a defensive margin, not the correctness basis.

Failure contract (``durable_but_nonblocking``):
- a telemetry failure can NEVER propagate into the writer;
- a malformed event is rejected by the validator, counted, and then RETRIED
  ONCE stripped to ``REQUIRED_FIELDS`` + ``truncated=true`` (see ``_degrade``)
  — the validator stays strict, but a rejection degrades the record instead
  of deleting the pass; only a record whose identity itself fails is dropped;
- a short write counts as a dropped event and is never resumed;
- on sink failure there is AT MOST one fallback emission (a single bounded
  stderr/journald line), never a retry loop, never a DB write, and the
  fallback path never calls back into instrumented code.

No fsync per event (design default): loss window on host crash is the
unsynced tail, acceptable for disposable measurement data.

Rotation/retention are deliberately NOT implemented here: the design gives
rotation to a single owner (the 001E collector/maintenance step), never the
writers — two writers tripping the threshold concurrently must not race a
``rename()``. **Until 001E lands the file has NO size bound, NO rotation, NO
retention and NO alert**, and it is SHARED with the 001A writers rather than
being this milestone's alone. Growth per writer is small — measured 1,168 B
per governed reconciler pass, ~4.6 KB/day at the 6-hourly cadence — but it is
monotonic. ``read_events`` below reads the WHOLE file into memory and is
unsuitable for a large one: measured 420 MB peak Python heap on a 60 MB file
(7.0x amplification, 8.5 s). Check the file's size before reading it, and
prefer streaming (``jq``) on a host. See docs/EVO_X2_RUNBOOK.md.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from app.telemetry.schema import (
    REQUIRED_FIELDS,
    TelemetryValidationError,
    validate_event,
)

DEFAULT_DIRNAME = "probability-arena-telemetry"
ACTIVE_FILENAME = "sqlite-writes.jsonl"
MAX_LINE_BYTES = 4096
DIR_MODE = 0o700
FILE_MODE = 0o600

# Optional fields dropped (in order) when a serialized line exceeds the cap;
# the re-serialized event then carries truncated=true. Never split a line.
_TRUNCATION_ORDER = (
    "table_groups", "source_command", "systemd_unit", "exception_class",
    "journal_mode", "synchronous_mode",
    # GATE2-WRITER-TELEMETRY-001 — the one nested field, and the largest
    # optional one. Last in the order on purpose: it is a calibration input,
    # so it is shed only after every cheap field has already gone.
    "lock_wait_histogram_ms",
)


def telemetry_dir() -> Path:
    override = os.environ.get("SQLITE_TELEMETRY_DIR")
    if override:
        return Path(override)
    return Path.home() / DEFAULT_DIRNAME


class TelemetrySink:
    """Emit validated envelopes as single-line JSONL appends. Never raises."""

    def __init__(self, directory: Path | None = None):
        self._dir = Path(directory) if directory is not None else telemetry_dir()
        self._lock = threading.Lock()
        self.emitted = 0
        self.dropped = 0
        self.rejected = 0
        # GATE2-WRITER-TELEMETRY-001 — how many events landed only in degraded
        # form. Counted separately so "the record survived" and "the record
        # survived INTACT" are never the same number to a reader.
        self.degraded = 0
        self._fallback_used = False

    @property
    def path(self) -> Path:
        return self._dir / ACTIVE_FILENAME

    # -- internal ----------------------------------------------------------

    def _serialize(self, event: dict) -> bytes:
        line = json.dumps(event, sort_keys=True, separators=(",", ":"))
        data = (line + "\n").encode("utf-8")
        if len(data) <= MAX_LINE_BYTES:
            return data
        # oversize: truncate optional fields and re-serialize to valid JSON
        slim = dict(event)
        slim["truncated"] = True
        for field in _TRUNCATION_ORDER:
            slim.pop(field, None)
            data = (json.dumps(slim, sort_keys=True, separators=(",", ":"))
                    + "\n").encode("utf-8")
            if len(data) <= MAX_LINE_BYTES:
                return data
        raise TelemetryValidationError("event exceeds line cap after truncation")

    def _write_line(self, data: bytes) -> bool:
        """One unbuffered append. True only when the FULL line landed."""
        self._dir.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        # enforce 0700 even when the directory pre-existed with looser perms.
        # `mkdir(exist_ok=True)` applies `mode` ONLY when it creates, so
        # without this line a pre-existing 0777 telemetry dir stays 0777.
        # PINNED by test_a_directory_permissions_are_reenforced_on_a_loose_dir.
        os.chmod(self._dir, DIR_MODE)
        # O_NOFOLLOW: a pre-planted symlink at the sink path must fail the
        # emit (dropped event) rather than append into its target — the one
        # filesystem route to the T1 recursive-contention/corruption trap.
        # Concretely: a symlink at ~/probability-arena-telemetry/
        # sqlite-writes.jsonl pointing at the live SQLite database would make
        # this append JSON INTO THE DATABASE FILE — corruption, and the
        # non-SQLite sink writing to SQLite, which is this milestone's one hard
        # mandate. PINNED by test_a_symlinked_sink_path_is_refused_not_followed.
        fd = os.open(
            self.path,
            os.O_APPEND | os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
            FILE_MODE,
        )
        try:
            written = os.write(fd, data)
        finally:
            os.close(fd)
        # a short write is a dropped event, never resumed (resuming would let
        # a second appender interleave between the two syscalls)
        return written == len(data)

    def _fallback(self) -> None:
        """Single bounded stderr/journald line; never a DB write, never a
        retry loop, never re-entry into instrumented code."""
        if self._fallback_used:
            return
        self._fallback_used = True
        try:
            sys.stderr.write("telemetry_sink_unavailable\n")
        except Exception:
            pass  # even the fallback may not raise into the writer

    # -- public ------------------------------------------------------------

    def _degrade(self, event: dict) -> dict | None:
        """GATE2-WRITER-TELEMETRY-001 — A DEGRADED RECORD BEATS A DELETED ONE.

        The validator is the safety boundary and stays STRICT: a closed field
        and label set is what keeps token ids, tickers and exception messages
        off the disk, and loosening it to save a record would trade the
        guarantee for the thing the guarantee protects. But "reject" must not
        mean "delete the pass", which is exactly the failure this milestone
        exists to end — the whole reason `crypto_token_lifecycle_runs` was
        rejected is that the passes carrying the most information are the ones
        that lose their record.

        Builder-side normalization closes only some of the doors (an unknown
        `run_status`, an unknown `stop_reason`, an unknown `outcome`, an
        unknown `table_groups` family, an unknown `**fields` key). It cannot
        close them all: a float in a duration field, a `1` where a bool is
        required, or any field a future writer adds without touching the
        builder will still fail here. So on failure the event is stripped to
        `REQUIRED_FIELDS` — identity, timing and outcome, the fields a
        calibration pass needs to know the pass HAPPENED — marked
        `truncated=true`, and retried ONCE. If the stripped form still fails,
        the identity itself is unusable and the drop is correct.

        One retry, never a loop, and no second validator: the degraded event
        goes through the same `validate_event` as everything else."""
        try:
            slim = {k: event[k] for k in REQUIRED_FIELDS if k in event}
            slim["truncated"] = True
            validate_event(slim)
            return slim
        except Exception:
            return None

    def emit(self, event: dict) -> bool:
        """Validate + append one event. Returns True when the event landed
        (intact OR degraded). NEVER raises — every failure is counted and
        swallowed."""
        was_degraded = False
        try:
            validate_event(event)
        except Exception:
            self.rejected += 1
            degraded = self._degrade(event if isinstance(event, dict) else {})
            if degraded is None:
                return False
            event = degraded
            was_degraded = True
        try:
            data = self._serialize(event)
            with self._lock:
                if self._write_line(data):
                    self.emitted += 1
                    if was_degraded:
                        self.degraded += 1
                    return True
            self.dropped += 1
            return False
        except Exception:
            self.dropped += 1
            self._fallback()
            return False


_sink: TelemetrySink | None = None
_sink_guard = threading.Lock()


def get_sink() -> TelemetrySink:
    """Process-wide sink (writers here are short-lived oneshots)."""
    global _sink
    with _sink_guard:
        if _sink is None or _sink._dir != telemetry_dir():
            _sink = TelemetrySink()
        return _sink


def read_events(path: Path | str) -> tuple[list[dict], int]:
    """Reader used by TESTS, and only tests: parse a telemetry JSONL file,
    skipping (and counting) malformed lines and discarding a trailing partial
    line (crash mid-write). Returns (events, malformed_lines). Read-only.

    NOT FOR A HOST. This slurps the entire file with `read_bytes()` and then
    holds every parsed event — measured 420 MB peak Python heap on a 60 MB
    file (7.0x amplification, 8.5 s) — and the sink file does not rotate until
    001E. On EVO, stream it with `jq` instead."""
    path = Path(path)
    events: list[dict] = []
    malformed = 0
    if not path.exists():
        return events, malformed
    raw = path.read_bytes()
    complete, _, partial = raw.rpartition(b"\n")
    if partial:
        malformed += 1  # trailing line without newline: discarded
    for line in complete.split(b"\n"):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line.decode("utf-8"))
            validate_event(parsed)
            events.append(parsed)
        except Exception:
            malformed += 1
    return events, malformed
