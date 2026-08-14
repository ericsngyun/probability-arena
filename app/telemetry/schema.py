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

HARD RULE (GATE7-SPARSE-TELEMETRY-001): ``commit_ms`` must be filtered by
``writer_name`` before any cross-writer aggregation. Two writers put different
REDUCTIONS in that one field and both are stamped ``commit_quality="exact"``:
``tick_aggregation`` records the LAST sub-window commit of its pass, and
``crypto_horizon_observe`` records the MAXIMUM over ``batch_count`` commits.
Both labels are honest — ``QUALITY_TIERS`` describes how a sample was
MEASURED, never how samples were REDUCED, so it cannot carry the distinction —
and the reduction is recoverable only from ``writer_name`` /
``operation_name``. No field is added to carry it: this envelope is the safety
boundary for five writers and must not grow a label describing one lane's
bookkeeping. A mean of ``commit_ms`` taken across writers is a mean of a
last-value and a max, and is a number about nothing. The canonical accessor
and the full rationale are in ``docs/SQLITE_LOCK_TELEMETRY_001A.md``.
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

# GATE2-WRITER-TELEMETRY-001 — the writer-pass label sets.
#
# WHY THESE ARE CLOSED SETS AND NOT A FREE SLUG. `run_status`/`stop_reason`
# carry a writer's OWN vocabulary, which is low-cardinality by construction,
# but "low cardinality by construction" is exactly the assumption the
# high-cardinality guard exists to stop trusting. A closed set keeps the
# guarantee structural.
#
# WHY THE NORMALIZATION LIVES IN THE BUILDER, NOT HERE. A closed set on its
# own has a failure mode this milestone must not ship: a status added by some
# future milestone would fail validation, and the sink's contract is to COUNT
# AND DROP — so the whole pass's telemetry would vanish silently, which is the
# precise failure this gate exists to end. `normalize_run_status`/
# `normalize_stop_reason` in `app.telemetry.writer_pass` therefore map any
# unrecognised value to "other" BEFORE the event reaches the sink. The
# validator stays closed; the event never disappears.
RUN_STATUSES = frozenset({
    # CryptoTapeTapeRecorder / run_scheduled_reconciliation (writer A)
    "ok", "dry_run", "error", "partial", "truncated", "dry_run_partial",
    "skipped_overlap", "skipped_contention", "lock_unavailable",
    "concurrent_write_conflict", "backlog_expiring", "unsafe_host_cost",
    "marketops_degraded", "skipped_health_latch",
    # `disabled` is in the set but writer A can NEVER emit it: that status
    # returns before the terminal funnel, deliberately (see `_finish` in
    # crypto_tape.py — a flag that is off means no run happened, and recording
    # it would file four events a day describing nothing). It is carried for
    # writer B and for a hand-built event; it is not dead, but nothing in the
    # reconciler's calibration corpus will ever have it.
    "disabled",
    # The `invalid_*` config-refusal FAMILY is collapsed to this single label
    # by the builder. It is an open prefix (one member per validated setting),
    # so enumerating it here would guarantee this set drifts behind the code.
    # A refusal is a misconfiguration, not a host observation — `guard.
    # records_feed_gate` already excludes the family from the rolling gate for
    # that reason, so one label is the right resolution for it.
    "invalid_config",
    # crypto_sparse_observation (writer B)
    "db_locked", "ambiguous_cohort", "provider_policy_violation",
    "window_too_short", "no_cohort",
    # GATE7-SPARSE-UNITS-001 — THE PASS DID NOT END, IT WAS ENDED. A writer that
    # is killed from outside (systemd's `TimeoutStartSec` reaching `KillSignal`,
    # or an operator's Ctrl-C) never reaches its own terminal funnel, so before
    # this label the only two things that could be filed were nothing at all —
    # the pre-existing behaviour, which is why an overrun of the observation
    # lane was invisible in the observation lane's own telemetry — or, worse, a
    # status describing work that did not finish.
    #
    # IT IS ITS OWN LABEL AND NOT `error`, AND NOT `other`. `error` is a fault
    # the pass DIAGNOSED and returned; this is the absence of a return. `other`
    # is the normalization sentinel for a status this set has not met, and
    # filing a known, expected, externally-caused end under the sentinel would
    # make it unfindable in exactly the query an operator investigating a
    # `Failed with result 'timeout'` in journald would run. A record carrying
    # this label makes NO claim about how much of the pass completed — see the
    # termination funnel in `app/cli.py`, which deliberately reports no counters
    # because at the CLI boundary it holds no result to report them from.
    "terminated",
    # the normalization sentinel — a status this set does not know
    "other",
})

STOP_REASONS = frozenset({
    # writer A
    "overlap", "contention", "deadline", "lock_wait_budget",
    "unsafe_host_cost",
    # writer B
    "observe_limit", "complete",
    "other",
})

# DERIVED, NOT ASSERTED — see `app.telemetry.writer_pass.resolve_run_source`.
# "scheduled" is claimed only on the strength of systemd's own
# `INVOCATION_ID`, which systemd sets on every unit invocation and a shell
# does not.
RUN_SOURCES = frozenset({"scheduled", "manual", "unknown"})

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
    # `commit_ms` CARRIES A PER-WRITER REDUCTION — filter by `writer_name`
    # before aggregating it. See the HARD RULE in this module's docstring.
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
    # ---- GATE2-WRITER-TELEMETRY-001: the writer-PASS fields --------------
    # All optional and nullable; REQUIRED_FIELDS is deliberately unchanged, so
    # every event the 001A writers emit today still validates byte-identically.
    # These are the quantities the two Solana writers already measure per pass
    # and previously discarded.
    #
    # THE FOUR LOCK-WAIT FIELDS ARE NOT INTERCHANGEABLE AND MUST NOT BE MERGED.
    # `lock_wait_ms` (above, pre-existing) is the PASS TOTAL, and it carries a
    # known systematic floor: one `run_row` sample and one `finalize` sample
    # per pass are once-per-pass bookkeeping commits, not contention. The
    # reconciler's breakers read `batch_lock_wait_ms_max` and ONLY that. The
    # other two phases are recorded in their own right — a rising `finalize`
    # wait is what `TimeoutStartSec` has to cover, and a rising `run_row` wait
    # is a real report about the host — but neither is contention and neither
    # may be summed into the batch figure. Storing all four separately is what
    # keeps that distinction from being re-flattened by a later reader.
    #
    # `write_hold_ms_max` IS MEANINGLESS WITHOUT `write_hold_measured`, AND THE
    # PAIR MUST TRAVEL TOGETHER (A1, security review of this branch). Three
    # different passes persist `write_hold_ms_max=0`: one that never opened a
    # write transaction (contended/refused/skipped), one that measured a
    # genuine sub-millisecond hold (`int(0.0004 * 1000) == 0` truncates), and
    # one that measured a true zero. An operator averaging the field to derive
    # `initial_per_token_cost_seconds` would fold the first kind — which
    # measured NOTHING — into the mean, pull it down, and set the constant too
    # AGGRESSIVE: larger batches, longer holds, on a live writer. That is the
    # survivorship bias this milestone rejected `crypto_token_lifecycle_runs`
    # to avoid, re-entering as ZERO-INFLATION. The emitter therefore OMITS
    # `write_hold_ms_max` entirely when nothing was measured and carries
    # `write_hold_measured` alongside it when it did, so "not measured" and
    # "sub-millisecond" are distinguishable in the persisted record. The
    # calibration filter is stated in docs/EVO_X2_RUNBOOK.md.
    "write_hold_measured", "write_hold_slo_violations",
    # The SLO counter this gate exists to hand the controller, plus the two
    # denominators any average over the fields above needs, plus the
    # distribution the thresholds are actually derived from (the class
    # docstring on `LockWaitAccounting` is explicit that a threshold must come
    # from the histogram tail, never from a scalar — so the histogram has to be
    # the thing that survives the pass).
    "lock_wait_measurements", "batch_lock_wait_warnings",
    "batch_lock_wait_aborts", "lock_wait_histogram_ms",
    "run_status", "stop_reason", "run_source", "gate_bypassed",
    "batch_count", "batch_lock_wait_ms_max", "run_row_lock_wait_ms",
    "finalize_lock_wait_ms", "write_hold_ms_max", "lock_failures",
    "adaptive_batching_active", "adaptive_batch_size_max",
    "adaptive_time_budget_ms", "adaptive_initial_per_token_cost_ms",
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
                    "commit_ms", "rollback_ms", "provider_io_ms_in_txn",
                    # GATE2-WRITER-TELEMETRY-001 — same impossible-timing rule
                    # (non-negative int or absent) as every other duration.
                    "batch_lock_wait_ms_max", "run_row_lock_wait_ms",
                    "finalize_lock_wait_ms", "write_hold_ms_max",
                    "adaptive_time_budget_ms",
                    "adaptive_initial_per_token_cost_ms")
# GATE2-WRITER-TELEMETRY-001 — non-negative integer counts. Separate from the
# durations only so the rejection message names the right kind of impossible.
_COUNT_FIELDS = ("batch_count", "lock_failures", "adaptive_batch_size_max",
                 "lock_wait_measurements", "write_hold_slo_violations",
                 "batch_lock_wait_warnings", "batch_lock_wait_aborts")
# GATE2-WRITER-TELEMETRY-001 — strict booleans. `1`/`0` are rejected rather
# than coerced: a field that silently accepts both is a field two readers will
# eventually disagree about.
_BOOL_FIELDS = ("gate_bypassed", "adaptive_batching_active",
                "write_hold_measured")

# GATE2-WRITER-TELEMETRY-001 — the ONE nested field, and the only one this
# envelope will ever carry. Validated STRUCTURALLY rather than against an
# imported bucket list, because importing the reconciler's edges into the
# schema would invert the dependency (the safety boundary would then depend on
# the code it is guarding). A histogram key is a bucket LABEL — `<1`, `1-10`,
# `>=30000` — which is derived from the edge tuple and can never carry a token
# id, ticker or cohort name; anything else is rejected, so the
# high-cardinality guarantee survives the nesting.
_HISTOGRAM_FIELDS = ("lock_wait_histogram_ms",)
_HISTOGRAM_KEY_RE = re.compile(r"^(?:<\d{1,7}|\d{1,7}-\d{1,7}|>=\d{1,7})$")
MAX_HISTOGRAM_BUCKETS = 16
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

    # GATE2-WRITER-TELEMETRY-001 — writer-pass label sets. Unrecognised values
    # are normalized to "other" by `app.telemetry.writer_pass` BEFORE they get
    # here, so reaching this raise means a caller bypassed the builder.
    run_status = event.get("run_status")
    if run_status is not None and run_status not in RUN_STATUSES:
        raise TelemetryValidationError("run_status outside bounded label set")
    stop_reason = event.get("stop_reason")
    if stop_reason is not None and stop_reason not in STOP_REASONS:
        raise TelemetryValidationError("stop_reason outside bounded label set")
    run_source = event.get("run_source")
    if run_source is not None and run_source not in RUN_SOURCES:
        raise TelemetryValidationError("run_source outside enum")

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
    # GATE2-WRITER-TELEMETRY-001. `isinstance(True, int)` is True in Python, so
    # the count check excludes bools explicitly — otherwise `batch_count=True`
    # would validate as the integer 1.
    for field in _COUNT_FIELDS:
        value = event.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise TelemetryValidationError(f"impossible count: {field}={value!r}")
    for field in _BOOL_FIELDS:
        value = event.get(field)
        if value is not None and not isinstance(value, bool):
            raise TelemetryValidationError(f"{field} must be a bool")
    # GATE2-WRITER-TELEMETRY-001 — bounded bucket map: bucket-label keys only,
    # non-negative integer counts, and a hard bucket ceiling so a malformed
    # producer cannot smuggle cardinality in through the one nested field.
    for field in _HISTOGRAM_FIELDS:
        value = event.get(field)
        if value is None:
            continue
        if not isinstance(value, dict) or len(value) > MAX_HISTOGRAM_BUCKETS:
            raise TelemetryValidationError(
                f"{field} must be a bounded bucket map"
            )
        for label, count in value.items():
            if not isinstance(label, str) or not _HISTOGRAM_KEY_RE.match(label):
                raise TelemetryValidationError(
                    f"{field} key is not a bucket label: {label!r}"
                )
            if (isinstance(count, bool) or not isinstance(count, int)
                    or count < 0):
                raise TelemetryValidationError(
                    f"impossible count: {field}[{label}]={count!r}"
                )

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
