"""CRYPTO-RECONCILER-GUARDED-TIMER-001 — the guard rails that make the crypto
reconciler's recurring 6-hourly timer safe to leave UNATTENDED.

WHAT THIS MODULE IS, AND WHAT IT DELIBERATELY IS NOT
----------------------------------------------------
It is a set of CIRCUIT BREAKERS around an already-measured, already-bounded
pass. It adds no budget, no ladder, no timeout, and it changes no existing
one. Everything the reconciler measures about itself — the deadline, the
write-hold SLO, the lock-wait budget, the finalize's single attempt — was
measured on production and is left byte-for-byte alone. This module only
decides, from those numbers, when a scheduled pass must not start, when a
running pass must stop, and when the SCHEDULE ITSELF must stop until a human
looks at it.

THE DECISION THIS IMPLEMENTS (Eric, 2026-08-12)
-----------------------------------------------
The original enable criterion — "essentially no >=1 s batch waits" — was too
binary. Phase attribution (CRYPTO-RECONCILER-LOCK-WAIT-PHASE-ATTRIBUTION-001)
refuted the finalize hypothesis: finalize waits are 8-12 ms, and the >=1000 ms
tail is genuine BATCH contention, present in roughly 4 of 6 counted production
passes at ~1.0-1.3 s. Across all six of those passes: zero lock failures, 0-1
retries, zero write-hold SLO violations, `write_hold_ms_max` <= 190 ms against
the 700 ms revisit trigger, and every run within ~0.5% of its 30 s deadline. A
~1.3 s wait is ~17% of the measured ~7.47 s acquisition budget and far below
the ~47 s hard bound.

    "occasional 1-1.3s waits are acceptable, escalating/repeated waits are
    not."  — Eric

Every threshold in this file is therefore an OPERATIONAL POLICY CHOICE. Not one
of them is a derived or measured constant, and none may be presented as one.
What the measurements do is bound where a chosen number can sensibly sit: a
threshold below the observed healthy band would fire on healthy runs, and a
threshold near the hard bound would fire only after the damage. Each constant
below states its chosen value, the evidence that motivated the choice, and the
fact that the choice is a choice.

THE THREE LAYERS
----------------
  1. PRE-FLIGHT SKIP   — this run must not execute at all (MarketOps degraded,
     or the health latch is tripped). A skip is a TYPED, RECORDED outcome, never
     silence: `lock_wait_distribution_eligible()` set the precedent that a
     pass excluded from a distribution must still be COUNTED, because "a zero
     row averaged in as benign is worse than a missing row".
  2. PER-RUN ABORT     — a single batch's phase-attributed lock wait crosses a
     review line (recorded, run continues), a warning line (recorded, run
     continues) or an abort line (the ladder stops; only the current batch is
     discarded, every earlier committed batch stays durable). Routed through
     the EXISTING `stop_reason="lock_wait_budget"` path in
     `crypto_tape._assemble_pass_locked` — this milestone adds a new REASON to
     stop, never a second mechanism for stopping.

     THE BUDGET CAPS THE WAIT; THE BREAKER CAPS THE LADDER. Both reviewers
     asked for this in as many words, because 7000 ms reads like a wait
     ceiling and is not one. The only thing that bounds how long any SINGLE
     acquisition may block is the lock-wait BUDGET
     (`derive_lock_wait_budget_seconds`, ~5 s at the shipped 20 s deadline,
     multiplied by the statement overshoot) — a number this milestone does not
     touch. `batch_lock_wait_escalation` classifies a wait only AFTER it has
     completed and its duration is known: a measured `abort_ms=24517` against
     the 7000 ms line is not a 7 s wait, it is a 24.5 s wait that has already
     happened and is now being judged. What the abort line therefore prevents
     is the NEXT rung — retrying into the same holder — not the wait that was
     just paid. Anyone who reads 7000 ms as "no wait can exceed 7 s" has
     misread it.
     WHAT IS DELIBERATELY *NOT* A PRE-FLIGHT SKIP, and why. The runbook's
     precondition 3 — "a calibrated `initial_per_token_cost_seconds`, with
     adaptive batching enabled" — is a real, known-unsafe condition for an
     unattended writer, and it is still enforced as an ENABLE-TIME operator
     check rather than a per-run skip. A pre-flight skip is for conditions
     that can CHANGE between two runs six hours apart; a settings value
     cannot, so re-deciding it every tick buys nothing a single enable-time
     check does not already give, while converting the flag into a silent
     no-op for every caller that does not set it (including ~150 existing
     tests and every manual pass). Stated here so the omission is a decision
     on the record, not an oversight.
  3. ROLLING HEALTH GATE — stateful across runs. One bad run is noise; a trend
     is a signal. When the trend appears, the gate TRIPS A LATCH that blocks
     further UNATTENDED runs until a human clears it. It never clears itself.

WHY THE STATE LIVES IN A FILE, NOT IN THE DATABASE
--------------------------------------------------
The outcomes the gate most needs to count are exactly the ones that write no
run row at all: `marketops_degraded`, `db_locked` in the prelude,
`skipped_contention` before the run row could be created, `skipped_overlap`.
A gate that read its history from `crypto_token_lifecycle_run` would be blind
to every one of them — it would see only the passes healthy enough to persist
themselves. A small append-bounded JSON file next to the SQLite database is
readable without taking the write lock the pass is already fighting for, and
survives a pass that never got to write anything.

The file is also the LATCH. One file, one source of truth, atomically replaced.

INERT WITHOUT A DURABLE DATABASE FILE. If `database_url` is not a file-backed
SQLite URL there is no host-scoped place to keep state that means anything —
an in-memory database has no "next scheduled run" to protect — so the gate
reports itself INERT (`health_gate_state="inert_no_durable_state"`) rather than
inventing a shared location. Production is always file-backed. This is stated,
never silent.

Measurement and coordination only. Nothing here trades, prices, sizes, or
recommends anything.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LAYER 2 — per-run abort thresholds on the batch lock ladder.
#
# THE FIGURE THESE READ IS THE PHASE-ATTRIBUTED `batch` WAIT, NEVER THE PASS
# TOTAL. That is the whole point of the phase split: the pass total contains
# one `run_row` sample and one `finalize` sample per pass, which are
# once-per-pass instrument events, not contention (measured: the total's
# >=1000 ms tail held EXACTLY one sample on every one of three production
# passes, with `measurements = batches + 1 + 1 + retries` accounting for it
# exactly). A threshold read off the total would be measuring the instrument.
#
# CHOSEN OPERATIONAL POLICY, NOT A MEASUREMENT. The evidence that motivated
# these particular numbers:
#   * measured healthy batch maxima: 16 / 514 / 1037 ms across the
#     phase-attributed production passes;
#   * the derived per-acquisition budget at the shipped 20 s deadline is 5 s,
#     and one attempt's modelled worst case is 4x that;
#   * Eric's framing above: ~1-1.3 s is acceptable, escalation is not.
# 5000 ms is ~5x the worst healthy batch wait ever observed — comfortably
# outside the band a healthy host produces, and comfortably inside the ~47 s
# hard bound, so it fires on escalation rather than on either noise or
# catastrophe. Neither number is derived from a distribution; there is no
# distribution of contended production passes yet, which is precisely why
# these are policy.
BATCH_LOCK_WAIT_WARN_MS = 5000
# The hard-abort region. Chosen at 1.4x the warning line so that "warn" and
# "abort" are not the same event with two names: a run that trips the warning
# has room to finish, and a run that trips this one has demonstrated a wait
# ~7x the worst healthy batch maximum. Stopping the ladder here is strictly
# safer than continuing blindly, because the pass is the interruptible party
# (the watcher and MarketOps are not) and its unworked tokens simply return to
# the backlog. Again: chosen, not derived.
BATCH_LOCK_WAIT_ABORT_MS = 7000

ESCALATION_WARNING = "warning"
ESCALATION_ABORT = "abort"


def batch_lock_wait_escalation(batch_lock_wait_ms: int | float | None) -> str | None:
    """Classify ONE batch-phase lock-wait sample against the chosen policy
    lines. Returns `None` (routine), `"warning"` (record it, keep going) or
    `"abort"` (stop the ladder).

    Deliberately a pure function over a single sample so the same
    classification is used by the live pass, by the recorded telemetry and by
    the tests — there is exactly one implementation of "how severe is this
    wait", and it is this one."""
    if batch_lock_wait_ms is None:
        return None
    ms = int(batch_lock_wait_ms)
    if ms >= BATCH_LOCK_WAIT_ABORT_MS:
        return ESCALATION_ABORT
    if ms >= BATCH_LOCK_WAIT_WARN_MS:
        return ESCALATION_WARNING
    return None


# ---------------------------------------------------------------------------
# LAYER 3(a) — post-run REVIEW triggers. A run that trips any of these is
# marked for review and feeds the rolling gate. Marking is not disabling: one
# run is noise (see `evaluate_health`).

# Rule 0 — THE TREND LINE, and the finding that produced it.
#
# An independent reviewer exercised `evaluate_health` as a pure function and
# found the gate BLIND to sustained SUB-THRESHOLD degradation. Four consecutive
# runs at 4,999 ms did not trip. Eight consecutive runs at 4,999 ms — two full
# days unattended — did not trip. A monotonic climb 1000 -> 2500 -> 3800 ->
# 4900 ms did not trip. The diagnosis was exact: `review_reasons`' ONLY
# wait-based trigger was `BATCH_LOCK_WAIT_WARN_MS`, so below that cliff there
# was no trend rule at all, and of Eric's criterion — "occasional 1-1.3s waits
# are acceptable, escalating/repeated waits are not" — only the first clause
# was implemented.
#
# This line implements the second clause, and it adds NO NEW RULE: a run whose
# worst batch wait reaches it is MARKED FOR REVIEW, which feeds the existing
# Rule 4 (3 of the last 4 marked runs) automatically. Every failing sequence
# above becomes a trip on the third marked run.
#
# CHOSEN OPERATIONAL POLICY, NOT A MEASUREMENT — same framing as every other
# constant in this file. 2000 ms is ~2x the worst healthy batch wait ever
# observed (1037 ms) and ~1500x the healthy median, so it sits well clear of
# the 1.0-1.3 s band Eric explicitly accepted; and it is 2.5x below the warning
# line, so "sustained mid-band" and "one severe spike" stay distinguishable
# events rather than the same event with two names. Marking is not disabling:
# ONE run at 2 s is still noise, and three in a day is the trend. Chosen.
BATCH_LOCK_WAIT_REVIEW_MS = 2000

# The pre-existing numeric revisit trigger from
# RECONCILE_LOCK_WAIT_DECISION_EDGE_MS's comment, reused rather than
# reinvented: re-examine the >=1000 ms decision edge if any counted pass
# reports `write_hold_ms_max > 700`. Measured context: four uncontended EVO
# passes read 479 / 544 / 92 / 532 ms, and the six counted production passes
# read 84-194 ms. CHOSEN, and chosen ELSEWHERE — this constant exists here so
# the timer reads the same number the runbook does, not so it can drift.
WRITE_HOLD_REVIEW_MS = 700

# Statuses that mean "this pass hit a real lock failure", as opposed to the
# routine `partial`(deadline)/`truncated` that fire on EVERY pass at
# production density. `lock_retry_events > 0` is deliberately NOT in here:
# the six counted healthy production passes recorded 0-1 retries, so a retry
# is inside the healthy band and a trigger on it would fire on healthy runs.
LOCK_FAILURE_STATUSES = frozenset({
    "db_locked",
    "skipped_contention",
    "lock_unavailable",
    "concurrent_write_conflict",
    "db_error",
})
# Stop reasons that mean the pass stopped BECAUSE of contention. `deadline`
# and `unsafe_host_cost` are not here: `deadline` is the normal, designed exit
# of a pass at production density, and `unsafe_host_cost` is the adaptive
# batcher's own refusal (a host-speed statement, already loudly typed).
CONTENTION_STOP_REASONS = frozenset({"contention", "lock_wait_budget"})

# The pre-flight skip statuses this module cares about. `marketops_degraded`
# is the pre-existing one (`_reconciliation_should_abort`, reused, never
# reimplemented); the latch status is new.
STATUS_MARKETOPS_DEGRADED = "marketops_degraded"
# Must fit `CryptoTokenLifecycleRun.status` (VARCHAR(32)) by the same rule as
# every other refused status, even though no skip reaches a run row today.
STATUS_HEALTH_LATCH = "skipped_health_latch"
# The outcomes where the pass REFUSED TO START. Defined here, once, and
# imported by `crypto_tape.lock_wait_distribution_eligible` — the exclusion and
# the COUNT (`skip_summary`) must never be able to disagree about which
# statuses they are talking about.
PREFLIGHT_SKIP_STATUSES = frozenset({
    STATUS_MARKETOPS_DEGRADED, STATUS_HEALTH_LATCH,
})

# Statuses that are a MISCONFIGURATION rather than a health observation. They
# are refused loudly and exit non-zero on their own; feeding them to a health
# gate about host contention would only dilute it.
_CONFIG_REFUSAL_PREFIX = "invalid_"


def review_reasons(summary: dict) -> list[str]:
    """The post-run DISABLE/REVIEW triggers, in one place.

    Each of these marks the run for review AND feeds the rolling gate; none of
    them disables anything on its own. Sorted, stable strings so a recorded
    history is diff-able and a test can assert on it."""
    reasons: list[str] = []
    status = str(summary.get("status") or "")
    stop_reason = summary.get("stop_reason")
    write_hold_ms_max = summary.get("write_hold_ms_max")
    if write_hold_ms_max is not None and int(write_hold_ms_max) > WRITE_HOLD_REVIEW_MS:
        reasons.append(f"write_hold_ms_max>{WRITE_HOLD_REVIEW_MS}")
    if summary.get("wall_time_model_exceeded") is True:
        reasons.append("wall_time_model_exceeded")
    if int(summary.get("write_hold_slo_violations") or 0) > 0:
        reasons.append("write_hold_slo_violation")
    if status in LOCK_FAILURE_STATUSES or stop_reason in CONTENTION_STOP_REASONS:
        reasons.append("lock_failure")
    if int(summary.get("batch_lock_wait_aborts") or 0) > 0:
        reasons.append("batch_lock_wait_abort")
    elif int(summary.get("batch_lock_wait_warnings") or 0) > 0:
        # A >=5 s batch wait is already far outside "occasional 1-1.3 s"; it is
        # marked even though the run was allowed to finish.
        reasons.append("batch_lock_wait_warning")
    elif int(summary.get("batch_lock_wait_ms_max") or 0) >= BATCH_LOCK_WAIT_REVIEW_MS:
        # Rule 0 — SUSTAINED SUB-THRESHOLD DEGRADATION. This is the one that
        # makes "escalating/repeated" mean anything below the 5 s cliff. One
        # such run disables nothing; three in a window do, through Rule 4.
        # Deliberately the LAST arm of the chain: a run that already carries a
        # warning or an abort is not additionally described as ">=2000ms",
        # because 5000 >= 2000 and one wait deserves one reason.
        reasons.append(f"batch_lock_wait_ms_max>={BATCH_LOCK_WAIT_REVIEW_MS}")
    return sorted(set(reasons))


def is_contention_stop(record: dict) -> bool:
    """Did this run stop because of CONTENTION, as opposed to stopping the way
    a healthy production pass always stops?

    THIS IS THE ONE PLACE THE LITERAL PHRASE "consecutive partial outcomes"
    HAD TO BE NARROWED, and the narrowing is deliberate. At production density
    EVERY pass ends `partial` with `stop_reason="deadline"` (plus `truncated`
    from the selection limit) — that is the designed, healthy exit, documented
    at `STATUS_BACKLOG_EXPIRING` as the reason a bare `partial` "carries no
    information on its own". A gate that tripped on consecutive `partial`
    outcomes would trip on the FIRST THREE HEALTHY RUNS and would be exactly
    the "alarm always on carries no information" failure this repo has already
    made once. Contention is `stop_reason in {contention, lock_wait_budget}`
    or a lock-failure status; nothing else."""
    return (
        str(record.get("status") or "") in LOCK_FAILURE_STATUSES
        or record.get("stop_reason") in CONTENTION_STOP_REASONS
    )


# ---------------------------------------------------------------------------
# LAYER 3(b) — the rolling, stateful, human-cleared health gate.
#
# THE RULE, AND WHY EACH PART OF IT IS WHAT IT IS. The window is the last
# HEALTH_WINDOW_RUNS recorded runs since the gate was last cleared, i.e. one
# day of the 6-hourly cadence. A day is the shortest span over which "repeated"
# is distinguishable from "occasional" at four runs a day, which is the
# distinction Eric's framing turns on.
HEALTH_WINDOW_RUNS = 4
# Rule 1 — REPEATED SEVERE WAITS. Two runs in the window whose worst batch
# wait reached the WARNING line. One severe wait is a co-tenant having a bad
# minute; two in a day is the escalation the framing rules out. Chosen.
HEALTH_SEVERE_WAIT_RUNS = 2
# Rule 2 — SUSTAINED CONTENTION. Three CONSECUTIVE contention stops (see
# `is_contention_stop` for why "partial" alone does not qualify). Three
# consecutive runs is 12-18 h of a host that cannot give this pass the write
# lock; at that point the pass is not the right thing to keep retrying
# unattended. Chosen.
#
# ITS ESCAPE BOUNDARY IS A STATED POLICY LINE, NOT AN EMERGENT ONE. A reviewer
# swept sustained contention patterns, evaluating after every run: 25% and 33%
# contention never trip; 50% (CH, CHCH, CCHH) never trips in 40 runs; 67% trips
# at run 4; 75% at run 3; 100% at run 3. So A HOST ON WHICH HALF OF EVERY PASS
# STOPS ON CONTENTION, INDEFINITELY, NEVER LATCHES ON THIS RULE. That is
# accepted, for reasons that have to hold for it to stay accepted: every
# contended pass still commits its durable batches and returns unworked tokens
# to the backlog (nothing is lost or corrupted), each such run is individually
# marked `review=True` and visible in `crypto-reconciler-health`, and the
# severe-wait rule (2 in 4) plus Rule 0 (>=2000 ms -> review -> 3 in 4)
# independently catch the alternating cases whose waits are long enough to hurt
# a co-tenant. A 50%-contended host that is NOT hurting anyone is a host to
# report, not a host to auto-disable. Restated in docs/EVO_X2_RUNBOOK.md so an
# operator reads the same boundary the code implements.
HEALTH_CONSECUTIVE_CONTENTION_RUNS = 3
# Rule 3 — MARKETOPS REGRESSION. Two CONSECUTIVE pre-flight skips for
# `marketops_degraded`. One skip is the pre-flight doing its job on a
# transient; two consecutive means MarketOps has been unhealthy across a whole
# 6 h interval, which is a regression in the lane this pass shares a database
# with, and adding a recurring writer to it unattended is the thing
# `_reconciliation_should_abort` exists to prevent. Chosen.
HEALTH_CONSECUTIVE_MARKETOPS_SKIP_RUNS = 2
# Rule 4 — SUSTAINED REVIEW PRESSURE. Three of the last four runs marked for
# review by ANY post-run trigger (`review_reasons`). This is how the item-4
# triggers "feed the gate" without any single one of them disabling anything:
# three quarters of a day's runs needing a human is a trend by any reading.
# It is also the rule Rule 0 (`BATCH_LOCK_WAIT_REVIEW_MS`) feeds, and therefore
# the only thing standing between a host sitting at 4,999 ms forever and an
# unattended timer that never notices. Chosen.
HEALTH_REVIEW_RUNS = 3
# How many run records the state file retains. Bounded so an unattended timer
# cannot grow a file without limit; generous enough (10 days at the 6-hourly
# cadence) that an operator arriving after a trip can see how the trend built.
HEALTH_HISTORY_MAX_RECORDS = 40
# How many cleared latches are kept for the audit trail. A cleared latch is
# never deleted, only moved here — "tripping it must be loud and recorded"
# would mean little if clearing erased the evidence.
HEALTH_LATCH_HISTORY_MAX = 10

HEALTH_STATE_FILENAME = ".crypto-tape-reconciler-health-{chain}.json"
HEALTH_STATE_VERSION = 1

# The gate's own self-description, reported on every governed result so an
# operator never has to infer whether it was running.
GATE_STATE_ACTIVE = "active"
GATE_STATE_INERT = "inert_no_durable_state"
GATE_STATE_ERROR = "error"
# A dry run writes nothing and cannot experience a commit's lock wait, so it
# has nothing to contribute to a gate about write contention. Named rather
# than silently skipped.
GATE_STATE_NOT_RECORDED_DRY_RUN = "not_recorded_dry_run"
GATE_STATE_NOT_RECORDED_CONFIG = "not_recorded_config_refusal"


def health_state_path(database_url: str | None, chain: str) -> Path | None:
    """Where this host keeps the reconciler's rolling health state, or `None`
    when there is no durable, host-scoped place to keep it.

    Co-located with the SQLite file itself — the same reasoning
    `_resolve_lock_dir` uses for the overlap lock: host-scoped, stable across
    process restarts, and readable without touching the database. Unlike the
    overlap lock there is NO temp-directory fallback: the lock only needs to be
    unique per host for the lifetime of one pass, while this file is an
    unattended SAFETY LATCH, and putting a safety latch in a shared temp
    namespace (where an unrelated process, or an unrelated test, could create,
    trip or delete it) would be worse than not having one. No file, no gate,
    and the result says so.

    NEVER DELETE THIS FILE — CLEAR THE LATCH WITH THE CLI. "No file, no gate"
    is a disclosure of how the gate behaves, not permission. Verified: a latch
    present BLOCKS, a CORRUPT file BLOCKS (fails closed, correct), and a
    DELETED file does not block. The realistic path to that is an operator
    "fixing" a corrupt state file by removing it — the natural response to the
    corruption warning — or a cleanup/restore sweeping up a dotfile. The
    supported clear is `crypto-reconciler-health --clear --operator <name>`,
    which keeps the trip in `latch_history`; `rm` keeps nothing and is
    indistinguishable from a host that has never run. Stated as an operator
    INSTRUCTION in docs/EVO_X2_RUNBOOK.md.

    IT IS ALSO DETECTABLE AFTER THE FACT, outside this file. Every trip emits
    `logger.error("... HEALTH GATE TRIPPED ... latch at <this path>")` to the
    journal and exits the unit non-zero, so `journalctl --user -u
    probability-arena-crypto-reconcile.service | grep 'HEALTH GATE TRIPPED'`
    beside a `latch=CLEAR` reading is exactly the signature of a state file
    that went missing between the trip and the read. That evidence covers
    EVERY trip path, including the pre-flight skips (`marketops_degraded`,
    `skipped_health_latch`) which write no run row at all and therefore could
    not have been stamped into one. The runbook makes the cross-check a step,
    not a hope."""
    if not database_url:
        return None
    try:
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite" or not url.database:
            return None
        if url.database == ":memory:":
            return None
        db_path = Path(url.database).resolve()
    except Exception:  # pragma: no cover - defensive (malformed URL)
        return None
    return db_path.parent / HEALTH_STATE_FILENAME.format(chain=chain)


def _empty_state(chain: str) -> dict:
    return {
        "version": HEALTH_STATE_VERSION,
        "chain": chain,
        "note": (
            "CRYPTO-RECONCILER-GUARDED-TIMER-001 unattended health state. "
            "`latch` blocks further scheduled reconciler runs and is cleared "
            "ONLY by a human (`crypto-reconciler-health --clear`)."
        ),
        "seq": 0,
        # Records at or below this sequence number are BEFORE the last human
        # clear and are excluded from the rolling evaluation. Without this a
        # cleared latch would re-trip on the very next run against the same
        # history that tripped it — a latch nobody can actually clear.
        "gate_window_start_seq": 0,
        "runs": [],
        "latch": None,
        "latch_history": [],
    }


def load_state(path: Path | None, chain: str) -> dict:
    """Read the state file, or an empty state. A corrupt/unreadable file
    yields an EMPTY state rather than an exception — but never yields a
    CLEARED latch: see `latch_read_failed`, which the caller surfaces. Losing
    telemetry must not be able to fail a pass; losing a LATCH silently must
    not be able to happen quietly."""
    state = _empty_state(chain)
    if path is None:
        return state
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return state
    except OSError as exc:
        logger.warning("crypto reconciler health: state unreadable at %s (%s)", path, exc)
        state["latch_read_failed"] = True
        return state
    try:
        loaded = json.loads(raw)
    except ValueError as exc:
        logger.warning("crypto reconciler health: state corrupt at %s (%s)", path, exc)
        state["latch_read_failed"] = True
        return state
    if not isinstance(loaded, dict):
        state["latch_read_failed"] = True
        return state
    state.update({k: v for k, v in loaded.items() if k in state or k == "latch_read_failed"})
    state.setdefault("runs", [])
    state.setdefault("latch_history", [])
    return state


def save_state(path: Path, state: dict) -> None:
    """Atomically replace the state file. Owner-only, same as the overlap
    lock: this is a safety latch on a shared host."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".crypto-health-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True, default=str)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:  # pragma: no cover - defensive
            pass
        raise


def run_record(summary: dict, *, at: datetime | None = None) -> dict:
    """The typed, bounded record of ONE run — including a run that was SKIPPED
    before it did anything. A skipped run is data, not silence.

    Only scalars a gate rule reads. The full evidence stays where it already
    lives (the returned summary, the run row's `write_coordination`, the
    journal); duplicating it here would make the state file a second, drifting
    copy of the telemetry contract."""
    reasons = review_reasons(summary)
    return {
        "at": (at or datetime.now(timezone.utc)).isoformat(),
        "status": summary.get("status"),
        "stop_reason": summary.get("stop_reason"),
        "gate_bypassed": summary.get("gate_bypassed"),
        "tape_run_id": summary.get("tape_run_id"),
        "duration_ms": summary.get("duration_ms"),
        "batch_lock_wait_ms_max": int(summary.get("batch_lock_wait_ms_max") or 0),
        "batch_lock_wait_warnings": int(summary.get("batch_lock_wait_warnings") or 0),
        "batch_lock_wait_aborts": int(summary.get("batch_lock_wait_aborts") or 0),
        "write_hold_ms_max": int(summary.get("write_hold_ms_max") or 0),
        "write_hold_slo_violations": int(summary.get("write_hold_slo_violations") or 0),
        "lock_retry_events": int(summary.get("lock_retry_events") or 0),
        "wall_time_model_exceeded": summary.get("wall_time_model_exceeded"),
        "lock_wait_distribution_eligible": summary.get("lock_wait_distribution_eligible"),
        # OBSERVATION ONLY — no behaviour, no enforcement, no gate rule reads
        # it. Precondition 3 ("a calibrated `initial_per_token_cost_seconds`,
        # with adaptive batching enabled") is deliberately an ENABLE-TIME
        # operator check rather than a per-run skip, and that reasoning is
        # sound: a settings value cannot change between two runs six hours
        # apart, and a per-run skip would silently no-op the flag for every
        # caller that does not set it. But enable-time checks are HUMAN steps,
        # and human steps drift. Recording what was actually in force on each
        # pass makes a drifted step visible in the gate history instead of
        # invisible — an operator reading `crypto-reconciler-health` after a
        # trip can see whether the host was running the shape it was enabled
        # under. `None` on a refusal (nothing was resolved).
        "adaptive_batching_active": summary.get("adaptive_batching_active"),
        "review": bool(reasons),
        "review_reasons": reasons,
    }


def skip_summary(records: list[dict]) -> dict:
    """How often the pass REFUSED TO START, over the supplied records.

    THE OTHER HALF OF THE EXCLUSION, AND IT HAD TO BE BUILT. `crypto_tape.
    lock_wait_distribution_eligible` excludes pre-flight skips from the
    lock-wait distribution and states, correctly, that they are "excluded and
    COUNTED — the skip rate is a result in its own right". It was not counted
    anywhere. A reviewer built a degraded week — 9 `marketops_degraded` skips
    in 28 recorded runs, a 32% skip rate — and `crypto-reconciler-health`
    reported `latch=CLEAR  review_runs=0  consecutive_marketops_skips=0`,
    because `consecutive_marketops_skips` is a TRAILING count and the last run
    happened to be healthy. An operator read a clean bill of health while a
    third of the week's passes did no work: the exact failure the exclusion
    exists to prevent, displaced one level up.

    A RATE, NOT A TRAILING COUNT — that is the whole point. The gate rules are
    deliberately trailing/windowed (a trend is what disables); this is
    deliberately cumulative over whatever history it is handed (a rate is what
    an operator judges). Neither substitutes for the other.

    `distribution_excluded_*` is the wider tally: every recorded run whose
    `lock_wait_distribution_eligible` is False, which is the pre-flight skips
    PLUS the prelude-blocked `db_locked` passes the same precedent already
    counted separately. Reported alongside rather than merged, because a pass
    blocked in the prelude did try and a skip did not."""
    total = len(records)
    skips_by_status: dict[str, int] = {}
    excluded_by_status: dict[str, int] = {}
    for record in records:
        status = str(record.get("status") or "unknown")
        if status in PREFLIGHT_SKIP_STATUSES:
            skips_by_status[status] = skips_by_status.get(status, 0) + 1
        if record.get("lock_wait_distribution_eligible") is False:
            excluded_by_status[status] = excluded_by_status.get(status, 0) + 1
    skips_total = sum(skips_by_status.values())
    excluded_total = sum(excluded_by_status.values())
    return {
        "runs_total": total,
        "skips_total": skips_total,
        "skip_rate": round(skips_total / total, 4) if total else 0.0,
        "skips_by_status": dict(sorted(skips_by_status.items())),
        "distribution_excluded_total": excluded_total,
        "distribution_excluded_by_status": dict(sorted(excluded_by_status.items())),
    }


def records_feed_gate(status: str | None) -> bool:
    """Which outcomes belong in the rolling history at all.

    A configuration refusal (`invalid_*`) is a misconfiguration, not a host
    observation: it exits non-zero on its own and would only dilute a gate
    about contention. `disabled` never reaches here (the flag is off, no run
    happened). Everything else — including every skip — is recorded."""
    if not status:
        return False
    if status.startswith(_CONFIG_REFUSAL_PREFIX):
        return False
    return status != "disabled"


def _severe_wait(record: dict) -> bool:
    return (
        int(record.get("batch_lock_wait_aborts") or 0) > 0
        or int(record.get("batch_lock_wait_warnings") or 0) > 0
        or batch_lock_wait_escalation(record.get("batch_lock_wait_ms_max")) is not None
    )


def evaluate_health(records: list[dict]) -> dict:
    """THE RULE. Pure function over the ordered (oldest-first) records since
    the last human clear; returns the verdict and, when it trips, every reason
    with the arithmetic that produced it.

    Stateful by construction — every rule counts across RUNS, never within
    one, because a single bad run is noise and a trend is a signal. Nothing
    here reads a clock: a gate whose verdict depended on wall time could
    "recover" while nobody was looking, and this gate must not be able to
    clear itself."""
    window = records[-HEALTH_WINDOW_RUNS:] if records else []
    reasons: list[str] = []

    severe = [r for r in window if _severe_wait(r)]
    if len(severe) >= HEALTH_SEVERE_WAIT_RUNS:
        reasons.append(
            f"repeated severe batch lock waits: {len(severe)} of the last "
            f"{len(window)} runs reached >={BATCH_LOCK_WAIT_WARN_MS}ms "
            f"(policy: {HEALTH_SEVERE_WAIT_RUNS} in {HEALTH_WINDOW_RUNS})"
        )

    trailing_contention = 0
    for record in reversed(records):
        if is_contention_stop(record):
            trailing_contention += 1
            continue
        break
    if trailing_contention >= HEALTH_CONSECUTIVE_CONTENTION_RUNS:
        reasons.append(
            f"sustained contention: {trailing_contention} consecutive runs "
            f"stopped on contention (policy: "
            f"{HEALTH_CONSECUTIVE_CONTENTION_RUNS} consecutive)"
        )

    trailing_marketops = 0
    for record in reversed(records):
        if record.get("status") == STATUS_MARKETOPS_DEGRADED:
            trailing_marketops += 1
            continue
        break
    if trailing_marketops >= HEALTH_CONSECUTIVE_MARKETOPS_SKIP_RUNS:
        reasons.append(
            f"MarketOps regression: {trailing_marketops} consecutive pre-flight "
            f"skips for {STATUS_MARKETOPS_DEGRADED} (policy: "
            f"{HEALTH_CONSECUTIVE_MARKETOPS_SKIP_RUNS} consecutive)"
        )

    flagged = [r for r in window if r.get("review")]
    if len(flagged) >= HEALTH_REVIEW_RUNS:
        reasons.append(
            f"sustained review pressure: {len(flagged)} of the last "
            f"{len(window)} runs were marked for review (policy: "
            f"{HEALTH_REVIEW_RUNS} in {HEALTH_WINDOW_RUNS})"
        )

    return {
        "tripped": bool(reasons),
        "reasons": reasons,
        "window_runs": len(window),
        "severe_wait_runs": len(severe),
        "consecutive_contention_runs": trailing_contention,
        "consecutive_marketops_skips": trailing_marketops,
        "review_runs": len(flagged),
    }


def gate_window(state: dict) -> list[dict]:
    """The records the gate may evaluate: everything appended since the last
    human clear."""
    start = int(state.get("gate_window_start_seq") or 0)
    return [r for r in state.get("runs", []) if int(r.get("seq") or 0) > start]


def append_run(state: dict, record: dict) -> dict:
    """Append one run to the (bounded) history, stamping it with the monotonic
    sequence number the clear-point is expressed in."""
    seq = int(state.get("seq") or 0) + 1
    stamped = {**record, "seq": seq}
    state["seq"] = seq
    runs = list(state.get("runs", []))
    runs.append(stamped)
    state["runs"] = runs[-HEALTH_HISTORY_MAX_RECORDS:]
    return stamped


def trip_latch(state: dict, verdict: dict, *, at: datetime | None = None) -> dict:
    """Trip the latch. Idempotent: an already-tripped latch keeps its ORIGINAL
    trip time and reasons — the first trip is the finding, and overwriting it
    with every subsequent run would erase when the trend actually started."""
    if state.get("latch"):
        return state["latch"]
    latch = {
        "tripped_at": (at or datetime.now(timezone.utc)).isoformat(),
        "reasons": list(verdict.get("reasons") or []),
        "verdict": {k: v for k, v in verdict.items() if k != "reasons"},
        "at_seq": int(state.get("seq") or 0),
        "cleared_at": None,
        "cleared_by": None,
    }
    state["latch"] = latch
    return latch


def clear_latch(state: dict, *, operator: str, note: str | None = None,
                at: datetime | None = None) -> dict | None:
    """A HUMAN clears the latch. Nothing in this module ever calls this — it
    exists only behind an explicit CLI action that requires an operator name.

    Clearing does two things, both necessary: it moves the latch into
    `latch_history` (the trip is never erased), and it advances
    `gate_window_start_seq` past every record that existed at clear time. That
    second part is not cosmetic — without it the same history would re-trip the
    gate on the very next run, and the latch would be uncleanable.

    A BLANK OPERATOR IS REFUSED HERE, not only at the CLI. `app/cli.py` already
    declines `--clear` without `--operator`, so the shipped path was safe — but
    the invariant that makes the audit trail mean anything ("a clear is a NAMED
    human decision") lived in one caller instead of in the function, and a
    second caller would have silently recorded `cleared_by: ""`. An unnamed
    clear is not a decision; it raises."""
    if not operator or not str(operator).strip():
        raise ValueError(
            "clear_latch requires a named operator — the latch exists to force "
            "a human decision, and an unnamed clear is not one"
        )
    latch = state.get("latch")
    if not latch:
        return None
    cleared = {
        **latch,
        "cleared_at": (at or datetime.now(timezone.utc)).isoformat(),
        "cleared_by": operator,
        "clear_note": note,
    }
    history = list(state.get("latch_history", []))
    history.append(cleared)
    state["latch_history"] = history[-HEALTH_LATCH_HISTORY_MAX:]
    state["latch"] = None
    state["gate_window_start_seq"] = int(state.get("seq") or 0)
    return cleared


def latch_blocks_run(state: dict, *, forced: bool, dry_run: bool) -> bool:
    """Does the latch stop THIS invocation?

    It blocks the UNATTENDED path — the timer — and nothing else. An operator
    running `--force` or `--dry-run` is a human already looking at the problem,
    which is exactly the state the latch is trying to summon; blocking them
    would make the latch harder to diagnose and harder to clear. Their output
    still reports the latch loudly (`health_latched=True`), so a bypass is
    never a silent one.

    A state file that could not be READ blocks too: an unattended safety latch
    whose file is corrupt or unreadable must fail CLOSED, because the one thing
    it cannot do is prove it was not tripped."""
    if forced or dry_run:
        return False
    return bool(state.get("latch")) or bool(state.get("latch_read_failed"))


def latch_summary(state: dict) -> dict | None:
    """The latch, flattened for the result dict and the CLI."""
    latch = state.get("latch")
    if latch:
        return {
            "tripped_at": latch.get("tripped_at"),
            "reasons": list(latch.get("reasons") or []),
            "at_seq": latch.get("at_seq"),
        }
    if state.get("latch_read_failed"):
        return {
            "tripped_at": None,
            "reasons": [
                "health state file unreadable or corrupt — failing CLOSED; "
                "the gate cannot prove it was not tripped"
            ],
            "at_seq": None,
        }
    return None
