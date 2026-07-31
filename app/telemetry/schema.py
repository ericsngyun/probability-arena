"""Telemetry event envelope: schema, enums, and validation (001A).

One flat JSON object per writer operation (parent) or per nested unit
(child), correlated by ``parent_event_id`` — never open spans. The validator
is the safety boundary of the sink: it rejects unknown (high-cardinality)
fields, secret-bearing values, out-of-enum labels, and impossible timings
**before** anything reaches disk. A rejected event is counted and dropped;
it never raises into the writer.

Never emitted: secrets, provider payloads, tokens/credentials, SQL bind
parameters, raw stack traces, tickers, token IDs, cohort names, or any
market-action field (see ``docs/SAFETY_BOUNDARIES.md``).
"""

from __future__ import annotations

import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone

EVENT_VERSION = 1

# Timing-quality tiers (design §Timing). Every reported timing field
# self-discloses how it was measured; the envelope never claims a precision
# the hooks cannot deliver.
QUALITY_TIERS = frozenset(
    {"exact", "instrumented_estimate", "derived_estimate", "unknown"}
)

OUTCOMES = frozenset({
    "success", "failed_lock", "failed_other", "retried_success",
    "retried_failed", "skipped_overlap", "skipped_health", "partial_success",
    "rolled_back", "unknown",
})

EXCEPTION_CATEGORIES = frozenset({
    "database_locked", "database_busy", "disk_full", "integrity_error",
    "operational_error", "timeout", "process_interrupted", "provider_error",
    "filesystem_error", "unknown",
})

WRITER_CLASSES = frozenset({
    "scheduled_oneshot", "continuous_daemon", "manual_command",
    "dynamic_oneshot", "maintenance", "test",
})

# 001A instruments exactly these writers. The canonical 15-writer name set
# exists in the design; the sink only accepts names it can attribute.
WRITER_NAMES = frozenset({
    "tick_aggregation", "backup",
    # reserved for later slices (001B-001D); accepting the canonical names
    # keeps the label set bounded while letting tests exercise the enum:
    "marketops_core", "marketops_crypto_scan", "baseline_scanner", "watcher",
    "meme_news", "retention", "crypto_tape", "crypto_horizon_observe",
    "outcome_sync", "forecast_scoring", "polymarket", "cross_venue",
    "tennis_tape", "test_writer",
})

# ~12 fixed coarse table families — never a bare table name or row value.
TABLE_GROUPS = frozenset({
    "market_ticks", "signals", "forecasts", "outcomes", "scores",
    "crypto_discovery", "crypto_horizon", "meme", "polymarket",
    "cross_venue", "tennis", "runs_audit",
})

# The complete, closed field set. An event carrying ANY other key is
# rejected — that is the high-cardinality guard (token ids, tickers, cohort
# ids, exception messages and friends can never ride along).
ALLOWED_FIELDS = frozenset({
    "event_version", "event_id", "parent_event_id", "writer_name",
    "writer_instance_id", "writer_class", "operation_name", "source_command",
    "systemd_unit", "process_id", "host", "started_at", "first_mutation_at",
    "commit_started_at", "finished_at", "duration_ms", "transaction_hold_ms",
    "transaction_hold_quality", "lock_wait_ms", "lock_wait_quality",
    "commit_ms", "commit_quality", "rollback_ms", "rollback_quality",
    "retry_count", "retry_limit", "attempt_number", "outcome",
    "exception_class", "exception_category", "sqlite_error_code",
    "table_groups", "rows_attempted", "rows_committed", "rows_skipped",
    "partial_progress", "provider_io_during_transaction",
    "provider_io_ms_in_txn", "filesystem_io_during_transaction",
    "database_bytes", "filesystem_free_bytes", "journal_mode",
    "synchronous_mode", "external_calls", "truncated",
    # canonical v1 correlation fields (design §event model) — nullable and
    # unused by the 001A writers, pre-registered so event_version=1 keeps
    # ONE field set across 001A-001D and old readers never reject new lines:
    "marketops_cycle_id", "scanner_run_id", "cohort_id", "job_id",
})

REQUIRED_FIELDS = frozenset({
    "event_version", "event_id", "writer_name", "writer_class",
    "operation_name", "process_id", "host", "started_at", "finished_at",
    "duration_ms", "retry_count", "attempt_number", "outcome",
    "provider_io_during_transaction", "filesystem_io_during_transaction",
})

_TIMESTAMP_FIELDS = ("started_at", "first_mutation_at", "commit_started_at",
                     "finished_at")
_DURATION_FIELDS = ("duration_ms", "transaction_hold_ms", "lock_wait_ms",
                    "commit_ms", "rollback_ms", "provider_io_ms_in_txn")
# correlation ids are integers, never names/strings — keeps the "cohort
# names never emitted" boundary structural, not regex-dependent
_INT_ID_FIELDS = ("marketops_cycle_id", "scanner_run_id", "cohort_id", "job_id")
_QUALITY_FIELDS = ("transaction_hold_quality", "lock_wait_quality",
                   "commit_quality", "rollback_quality")

# Mirrors the AGENTS.md / TESTING_POLICY secret grep plus credential shapes.
# Applied to every string VALUE in the envelope; one hit rejects the event.
_SECRET_VALUE_RE = re.compile(
    r"api[_-]?key|secret|private[_-]?key|authorization|password|bearer\s|"
    r"raw_payload|database_url|postgresql\+|sqlite:///|token=|--key",
    re.IGNORECASE,
)


class TelemetryValidationError(ValueError):
    """A telemetry event failed schema validation. Never propagates past the
    sink boundary — the sink counts and drops."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def writer_instance_id() -> str:
    """pid + monotonic start; distinguishes overlapping runs of one writer."""
    return f"{os.getpid()}-{int(time.monotonic() * 1000)}"


def build_event(
    *,
    writer_name: str,
    writer_class: str,
    operation_name: str,
    started_at: str,
    finished_at: str | None = None,
    outcome: str = "success",
    **fields,
) -> dict:
    """Assemble an envelope with identity/context defaults filled in. The
    result still goes through ``validate_event`` at the sink boundary."""
    finished = finished_at or _utcnow_iso()
    event = {
        "event_version": EVENT_VERSION,
        "event_id": str(uuid.uuid4()),
        "writer_name": writer_name,
        "writer_class": writer_class,
        "operation_name": operation_name,
        "process_id": os.getpid(),
        "host": socket.gethostname().split(".")[0],
        "source_command": fields.pop("source_command", None),
        "systemd_unit": fields.pop(
            "systemd_unit",
            os.environ.get("SQLITE_TELEMETRY_SYSTEMD_UNIT")
            or (None if "INVOCATION_ID" not in os.environ else "systemd"),
        ),
        "started_at": started_at,
        "finished_at": finished,
        "duration_ms": fields.pop(
            "duration_ms",
            max(0, int((_parse_ts(finished) - _parse_ts(started_at))
                       .total_seconds() * 1000)),
        ),
        "retry_count": fields.pop("retry_count", 0),
        "attempt_number": fields.pop("attempt_number", 1),
        "outcome": outcome,
        "provider_io_during_transaction": fields.pop(
            "provider_io_during_transaction", False),
        "filesystem_io_during_transaction": fields.pop(
            "filesystem_io_during_transaction", False),
    }
    event.update(fields)
    return event


def validate_event(event: dict) -> dict:
    """Validate one envelope. Returns the event on success; raises
    ``TelemetryValidationError`` otherwise (the sink catches, counts, drops).
    """
    if not isinstance(event, dict):
        raise TelemetryValidationError("event must be a dict")

    unknown = set(event) - ALLOWED_FIELDS
    if unknown:
        raise TelemetryValidationError(
            f"unknown/high-cardinality fields rejected: {sorted(unknown)}"
        )
    missing = REQUIRED_FIELDS - set(event)
    if missing:
        raise TelemetryValidationError(f"missing required fields: {sorted(missing)}")

    if event["event_version"] != EVENT_VERSION:
        raise TelemetryValidationError("unsupported event_version")
    if event["writer_name"] not in WRITER_NAMES:
        raise TelemetryValidationError("writer_name outside bounded label set")
    if event["writer_class"] not in WRITER_CLASSES:
        raise TelemetryValidationError("writer_class outside enum")
    if event["outcome"] not in OUTCOMES:
        raise TelemetryValidationError("outcome outside enum")
    category = event.get("exception_category")
    if category is not None and category not in EXCEPTION_CATEGORIES:
        raise TelemetryValidationError("exception_category outside enum")
    for field in _QUALITY_FIELDS:
        value = event.get(field)
        if value is not None and value not in QUALITY_TIERS:
            raise TelemetryValidationError(f"{field} outside quality tiers")

    groups = event.get("table_groups")
    if groups is not None:
        if (not isinstance(groups, list)
                or any(g not in TABLE_GROUPS for g in groups)):
            raise TelemetryValidationError(
                "table_groups must be a list of fixed coarse families"
            )

    # impossible timings are rejected, not emitted
    started = _parse_ts(event["started_at"])
    finished = _parse_ts(event["finished_at"])
    if finished < started:
        raise TelemetryValidationError("impossible timing: finished_at < started_at")
    for field in _TIMESTAMP_FIELDS:
        value = event.get(field)
        if value is not None:
            _parse_ts(value)  # must parse
    for field in _DURATION_FIELDS:
        value = event.get(field)
        if value is not None and (not isinstance(value, int) or value < 0):
            raise TelemetryValidationError(f"impossible timing: {field}={value!r}")
    for field in _INT_ID_FIELDS:
        value = event.get(field)
        if value is not None and not isinstance(value, int):
            raise TelemetryValidationError(f"{field} must be an integer id")

    # secret scan on every string value (schema-controlled fields only is the
    # first line of defense; this is the second)
    for key, value in event.items():
        if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
            raise TelemetryValidationError(
                f"secret-bearing value rejected in field {key}"
            )
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and _SECRET_VALUE_RE.search(item):
                    raise TelemetryValidationError(
                        f"secret-bearing value rejected in field {key}"
                    )
    return event
