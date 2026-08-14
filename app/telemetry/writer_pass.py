"""One writer PASS, persisted — the shared evaluation plane (GATE2-WRITER-TELEMETRY-001).

WHAT THIS EXISTS FOR
--------------------
Two Solana writers measure their own write coordination correctly and then
throw the measurements away when the process exits:

  * the provider-free reconciler (`crypto_tape.run_scheduled_reconciliation`)
    has a 2.0 s write-hold SLO with a counter and no controller, because
    `initial_per_token_cost_seconds` has never been calibrated against a
    recorded distribution;
  * the sparse observer (`crypto_sparse_observation`) states `persisted: false`
    in its own result payload with the instruction "install no timer until it
    is".

Both gates are the same gate, so this is ONE surface serving both writers
rather than two parallel accounting systems.

WHY THIS IS THE 001A JSONL SINK AND NOT A NEW TABLE
---------------------------------------------------
The candidates were audited before anything was built:

  * `crypto_token_lifecycle_runs.config["write_coordination"]` already holds
    most of writer A's scalars at the right grain — but it is written by the
    finalize commit, which gets ONE attempt. The passes whose numbers matter
    most — CONTENDED, ABORTED and REFUSED — are exactly the passes that lose
    it. `crypto_tape.lock_wait_run_row_orphaned()` names that signature
    precisely. A store that is blind to bad passes cannot calibrate a
    contention threshold.

    **THE SINK DOES NOT SURVIVE A SIGKILL, AND AN EARLIER VERSION OF THIS
    DOCSTRING CLAIMED IT DID** (A6, security review of this branch). The emit
    is the LAST thing a pass does — after `_record_health`, after every commit
    the pass owns — so a SIGKILL mid-finalize loses the JSONL line exactly as
    it loses the run row. The advantage over the run row is real but narrower
    than that: a pass that RETURNS a terminal result having committed nothing
    (contended, aborted, refused, pre-flight-skipped) still emits, because the
    append is not part of any transaction. It is not crash durability.
  * the health-state file (`crypto_reconciler_guard`) DOES survive those
    passes, and deliberately so — but it is bounded at
    `HEALTH_HISTORY_MAX_RECORDS = 40` rolling records, holds "only scalars a
    gate rule reads" by its own stated contract, and is writer-A-only. It is
    the gate's decision state and its latch, not a calibration record.
  * every other run table (`pipeline_stage_runs`, `tick_aggregation_runs`,
    `frontier_eval_runs`, the MarketOps records) is a different lane's
    lifecycle and carries no lock-wait or write-hold column at all.

The 001A sink is the only surface in the repo that is purpose-built for this:
append-only, **non-SQLite by mandate** so it can never take the write lock it
is measuring, `durable_but_nonblocking` so a telemetry failure can never
propagate into the writer, and it survives a pass that committed nothing.
Its writer-name enum already RESERVES `crypto_tape` and
`crypto_horizon_observe` "for later slices (001B-001D)". This is that slice.

The consequence worth stating plainly: **this milestone adds no migration.**
There is no new table, no column, no `VARCHAR` to size, and therefore no
`ensure_schema_current` exposure — nothing breaks if "it" is not applied,
because there is no it. EVO stays at 0028, migration 0029 is untouched, and
the Alembic chain still has exactly one head.

Measurement only. Nothing here trades, prices, sizes, or recommends anything.
"""

from __future__ import annotations

import os

from app.telemetry.schema import (
    ALLOWED_FIELDS,
    OUTCOMES,
    RUN_STATUSES,
    STOP_REASONS,
    TABLE_GROUPS,
    build_event,
    writer_instance_id,
)
from app.telemetry.sink import get_sink

# The `invalid_*` config-refusal family collapses to one label — see
# `RUN_STATUSES` in schema.py for why enumerating it would guarantee drift.
_CONFIG_REFUSAL_PREFIX = "invalid_"
_CONFIG_REFUSAL_LABEL = "invalid_config"
_OTHER = "other"
_UNKNOWN = "unknown"

# Fields the builder itself owns and passes positionally; they must never be
# re-supplied through `**fields`, and they are not "unknown" either.
_BUILDER_OWNED_FIELDS = frozenset({
    "writer_name", "writer_class", "operation_name", "started_at",
    "finished_at", "outcome", "run_source", "run_status", "stop_reason",
    "gate_bypassed", "writer_instance_id",
})

RUN_SOURCE_SCHEDULED = "scheduled"
RUN_SOURCE_MANUAL = "manual"
RUN_SOURCE_UNKNOWN = "unknown"

# Coarse bucket only. `run_status` on the same event carries the writer's
# exact status, so this mapping never has to guess to preserve information:
# anything not listed falls to "unknown" rather than being forced into a
# neighbouring bucket it does not belong in.
_STATUS_TO_OUTCOME = {
    "ok": "success",
    "dry_run": "success",
    "partial": "partial_success",
    "truncated": "partial_success",
    "dry_run_partial": "partial_success",
    # the pass did bounded work; the FRONTIER (oldest unreconciled evidence)
    # is what is at risk, not the pass
    "backlog_expiring": "partial_success",
    "skipped_overlap": "skipped_overlap",
    "skipped_health_latch": "skipped_health",
    "marketops_degraded": "skipped_health",
    "disabled": "skipped_health",
    # real write-lock loss
    "skipped_contention": "failed_lock",
    "db_locked": "failed_lock",
    # NOT lock loss: an IntegrityError, or the overlap lock FILE being
    # unopenable (permissions/missing dir). Bucketing either as failed_lock
    # would inflate the contention rate this gate exists to measure.
    "concurrent_write_conflict": "failed_other",
    "lock_unavailable": "failed_other",
    "error": "failed_other",
    "unsafe_host_cost": "failed_other",
    _CONFIG_REFUSAL_LABEL: "failed_other",
}


def normalize_run_status(status: str | None) -> str | None:
    """Map a writer's own status onto the bounded label set, BEFORE the sink
    sees it. `None` stays `None` (the field is optional).

    The whole point is that an unrecognised status must never cost the pass
    its telemetry: the sink's contract on a validation failure is count-and-
    drop, so a status this set has not met yet would silently delete the very
    record this gate exists to keep. It becomes "other" instead."""
    if status is None:
        return None
    text = str(status)
    if text.startswith(_CONFIG_REFUSAL_PREFIX):
        return _CONFIG_REFUSAL_LABEL
    return text if text in RUN_STATUSES else _OTHER


def normalize_stop_reason(reason: str | None) -> str | None:
    """Same contract as `normalize_run_status`, for the stop-reason set."""
    if reason is None:
        return None
    text = str(reason)
    return text if text in STOP_REASONS else _OTHER


def outcome_for_status(status: str | None) -> str:
    """Coarse `OUTCOMES` bucket for a writer status. Deliberately lossy and
    deliberately not clever — `run_status` carries the exact value."""
    return _STATUS_TO_OUTCOME.get(normalize_run_status(status) or "", "unknown")


def normalize_outcome(outcome) -> str:
    """`outcome` is REQUIRED and enum-checked, so an unrecognised one deletes
    the whole event at the sink. It was the only caller-supplied label reaching
    the envelope un-normalized (A3, security review of this branch): unlike
    `run_status`/`stop_reason` it was popped straight out of `**fields` and
    forwarded. It becomes "unknown" — which is a real member of `OUTCOMES` and
    the honest label for "this producer named a bucket we do not have"."""
    if outcome is None:
        return _UNKNOWN
    text = str(outcome)
    return text if text in OUTCOMES else _UNKNOWN


def normalize_table_groups(groups):
    """Keep the recognised coarse families, drop the rest, and return None
    when nothing recognised survives (the field is optional, so absent is
    better than an empty list that reads as "touched no tables").

    Same reason as every other normalizer here: `table_groups` is validated
    against a closed set, so ONE unrecognised family would cost the pass its
    entire record."""
    if groups is None:
        return None
    if isinstance(groups, str) or not isinstance(groups, (list, tuple, set)):
        groups = [groups]
    kept = [g for g in groups if isinstance(g, str) and g in TABLE_GROUPS]
    # dedupe, order-stable
    seen = []
    for g in kept:
        if g not in seen:
            seen.append(g)
    return seen or None


def resolve_run_source() -> str:
    """Scheduled vs manual, DERIVED rather than asserted by the caller.

    THIS FIELD IS NOT BOOKKEEPING. The reconciler's rolling health latch must
    not be tripped — or cleared — by an attended `--force` pass, so "was this
    unattended?" has to be answerable from the record without trusting whoever
    wrote it. A flag on the command line (`--scheduled`) would be exactly as
    forgeable as typing it, and worse, it would be forgeable BY ACCIDENT: an
    operator copying the unit's ExecStart line out of the service file to
    reproduce a failure by hand would file their manual pass as a timer run.

    `INVOCATION_ID` is set by systemd itself on every unit invocation and is
    absent from an ordinary interactive shell, so it cannot be reproduced by
    copying a command line. It is not unforgeable — anyone may `export` it —
    but forging it takes a deliberate act that cannot happen by accident, and
    it is the same signal `build_event` already trusts for `systemd_unit`.
    Using one signal for both keeps the two fields from ever disagreeing.

    `gate_bypassed` (whether `--force` was passed) is recorded ALONGSIDE this,
    never merged into it: `run_source="scheduled"` with `gate_bypassed=True`
    is a real anomaly, and it stays visible only while the two remain separate
    fields.

    FORGEABLE, PLAINLY, AND WIDER THAN JUST THIS FUNCTION (noted by the
    security review of this branch, deliberately NOT enforced here). Three
    routes: `emit_writer_pass(run_source="scheduled")` takes a caller-asserted
    value with no provenance check at all and bypasses this function entirely;
    `export INVOCATION_ID=anything` satisfies the check below; and
    `SQLITE_TELEMETRY_SYSTEMD_UNIT` independently sets `systemd_unit`.

    That is not a live risk today, but NOT for the reason the review gave. The
    review concluded "no consumer of the JSONL exists anywhere in `app/` or
    `scripts/`"; THAT IS WRONG — `scripts/sqlite_analyze_maintenance.py::
    _lock_tally` reads this file and its `lock_events` count is a governed
    stop condition (see the runbook). The conclusion survives on a narrower
    and checkable fact: that consumer reads `lock_wait_ms`, `retry_count`,
    `started_at`, `operation_name`, `writer_name` and `outcome`, and NOT
    `run_source`. Nothing anywhere reads `run_source`. Combined with the health
    latch being driven by real Python arguments and `_record_health` writing
    BEFORE the emit, a forged value cannot reach any gate.

    Enforcement belongs to whoever builds the first reader OF THIS FIELD; it
    must not be assumed to exist before then."""
    return (
        RUN_SOURCE_SCHEDULED
        if os.environ.get("INVOCATION_ID")
        else RUN_SOURCE_MANUAL
    )


def emit_writer_pass(
    *,
    writer_name: str,
    operation_name: str,
    started_at: str,
    finished_at: str | None = None,
    run_status: str | None = None,
    stop_reason: str | None = None,
    gate_bypassed: bool | None = None,
    run_source: str | None = None,
    **fields,
) -> str | None:
    """Emit ONE writer-pass event. Returns the event_id, or None if it did not
    land. NEVER raises into the writer — every failure path here is swallowed,
    exactly as the 001A sink contract requires.

    Called AFTER the pass has finished all of its own commits, so the single
    `os.write()` append this performs is outside every transaction the writer
    owns and takes no SQLite lock at any point.

    `writer_class` is derived from `run_source` rather than accepted from the
    caller, so the two can never disagree.

    EVERY LABEL THAT REACHES THE ENVELOPE IS NORMALIZED HERE FIRST, and any key
    the schema does not know is DROPPED rather than forwarded (A3, security
    review of this branch). The sink's validator is strict by design, so an
    un-normalized label or a stray key does not fail one field — it deletes the
    whole pass record. Five reachable classes did exactly that through this
    entry point: an unrecognised `outcome`, an unrecognised `table_groups`
    family, any unknown key arriving via `**fields` (writer B's `commit_ms_max`
    is the concrete case), a float in a duration/count field, and
    `gate_bypassed=1` instead of `True`. The first three are closed here; the
    last two are structurally unclosable at the builder (a value can be the
    wrong TYPE in any field), which is why the sink also degrades rather than
    deletes — see `TelemetrySink._degrade`. Normalization and degradation are
    two halves of one guarantee; neither alone is sufficient."""
    try:
        source = run_source or resolve_run_source()
        status = normalize_run_status(run_status)
        outcome = fields.pop("outcome", None)
        groups = normalize_table_groups(fields.pop("table_groups", None))
        # DROP, do not forward, anything the closed field set does not know.
        # A dropped field costs one number; a forwarded one costs the record.
        extra = {
            k: v for k, v in fields.items()
            if k in ALLOWED_FIELDS and k not in _BUILDER_OWNED_FIELDS
        }
        event = build_event(
            writer_name=writer_name,
            writer_class=(
                "scheduled_oneshot" if source == RUN_SOURCE_SCHEDULED
                else "manual_command"
            ),
            operation_name=operation_name,
            started_at=started_at,
            finished_at=finished_at,
            # Falsy (absent/empty) keeps the pre-existing behaviour of DERIVING
            # from the status; a supplied one is normalized, never forwarded.
            outcome=(
                normalize_outcome(outcome) if outcome
                else outcome_for_status(run_status)
            ),
            writer_instance_id=fields.pop(
                "writer_instance_id", writer_instance_id()),
            run_source=source,
            **({"run_status": status} if status is not None else {}),
            **(
                {"stop_reason": normalize_stop_reason(stop_reason)}
                if stop_reason is not None else {}
            ),
            **({"gate_bypassed": gate_bypassed}
               if gate_bypassed is not None else {}),
            **({"table_groups": groups} if groups is not None else {}),
            **extra,
        )
        if get_sink().emit(event):
            return event["event_id"]
        return None
    except Exception:
        return None
