"""KALSHI-ARCHIVE-CORE-REMEDIATION-003B A3 -- fault-model fidelity ledger.

WHY THIS EXISTS. One reviewer found a deadlock that reproduces reliably
with this repo's `sys.settrace` injector (`tests/harness_async_accounting/
line_injector.py`) but landed 0/50 under real `ctypes`-delivered
asynchronous exceptions -- the synthetic mechanism found a REAL invariant
violation (a state the code can reach and does not handle correctly) but
said nothing true about how OFTEN, or whether AT ALL, a real process
experiences it. Every accounting-fault test in this suite has to be
classified as exactly one of the three kinds below, and no test may
present a synthetic reproduction as if it were a frequency/liveness claim.

    REALISTIC_PROCESS_FAULT   -- a real OS-level process/subprocess fault
                                 (kill, crash, resource exhaustion) that an
                                 operator could genuinely experience in
                                 production, observed end-to-end.
    REALISTIC_SIGNAL_FAULT    -- a real asynchronous-exception delivery
                                 mechanism (`SIGINT`, `PyThreadState_
                                 SetAsyncExc`) landing inside a REAL
                                 process, at a real (not fully controlled)
                                 instant -- reproduces the CLASS but the
                                 landing point/rate is empirical, not
                                 chosen.
    SYNTHETIC_STATE_MACHINE_FAULT -- `sys.settrace`-based or test-owned
                                 monkeypatch-based injection at an EXACT,
                                 chosen source line or call boundary.
                                 Deterministic and exact; proves an
                                 invariant CAN be violated in that state,
                                 says nothing about production frequency.

A synthetic fault that exposes a real invariant violation stays valuable AS
A STATE-MACHINE TEST -- it is not weakened or removed here -- but every one
of the fault tests in this suite is now attributed to exactly one of these
three, and NOTHING using a SYNTHETIC mechanism speaks in this ledger (or in
the modules it audits) as if it measured frequency or liveness.
"""

from __future__ import annotations

from dataclasses import dataclass, field


REALISTIC_PROCESS_FAULT = "REALISTIC_PROCESS_FAULT"
REALISTIC_SIGNAL_FAULT = "REALISTIC_SIGNAL_FAULT"
SYNTHETIC_STATE_MACHINE_FAULT = "SYNTHETIC_STATE_MACHINE_FAULT"

CLASSIFICATIONS = (
    REALISTIC_PROCESS_FAULT, REALISTIC_SIGNAL_FAULT,
    SYNTHETIC_STATE_MACHINE_FAULT,
)


@dataclass(frozen=True)
class FaultTestEntry:
    module: str
    test_name: str
    classification: str
    mechanism: str
    note: str = ""


# Every accounting/writer-thread fault-injection test currently in the
# suite, attributed by hand after reading its mechanism (not inferred from
# its name). New fault tests MUST be added here -- see
# `test_kalshi_meta_runtime_fault_classification_001.py`'s completeness
# check, which fails when a fault-shaped test exists in the suite (by
# structural signature: imports `line_injector`, `fault_trial`, or
# `writer_thread_asyncexc_trial`) with no ledger entry.
LEDGER: tuple[FaultTestEntry, ...] = (
    # -- tests/test_kalshi_async_accounting_harness_001.py -----------------
    FaultTestEntry(
        "tests/test_kalshi_async_accounting_harness_001.py",
        "TestFourWindowsReproduceDeterministically",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (LineBoundaryInjector)",
        "the four submit()-side FAULT-WINDOW boundaries, each an exact "
        "chosen source line"),
    FaultTestEntry(
        "tests/test_kalshi_async_accounting_harness_001.py",
        "TestNegativeControlsDoNotOverFire",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (LineBoundaryInjector)",
        "negative control: injection at a line NOT inside a FAULT-WINDOW"),
    FaultTestEntry(
        "tests/test_kalshi_async_accounting_harness_001.py",
        "TestDiscriminationDeterministic",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (LineBoundaryInjector)",
        "planted-bad reference shim vs production, same deterministic "
        "mechanism applied to both for a fair comparison"),
    FaultTestEntry(
        "tests/test_kalshi_async_accounting_harness_001.py",
        "TestRealSignalReproducesTheSameClass."
        "test_a_single_sigint_lands_inside_submit_and_is_caught_cleanly",
        REALISTIC_SIGNAL_FAULT, "real SIGINT via os.kill, subprocess",
        "producer/main-thread only (Python only delivers a real signal "
        "handler on the main thread) -- a genuine, empirically-landing "
        "measurement, not a chosen line"),
    FaultTestEntry(
        "tests/test_kalshi_async_accounting_harness_001.py",
        "TestRealSignalReproducesTheSameClass."
        "test_ctrl_c_causes_zero_identity_violations[sigint]",
        REALISTIC_SIGNAL_FAULT, "real SIGINT via os.kill, subprocess",
        "REQUIRED_DELIVERABLE: zero identity violations across "
        "REAL_FAULT_TRIALS trials, producer-thread only"),
    FaultTestEntry(
        "tests/test_kalshi_async_accounting_harness_001.py",
        "TestRealSignalReproducesTheSameClass."
        "test_ctrl_c_causes_zero_identity_violations[asyncexc]",
        REALISTIC_SIGNAL_FAULT,
        "real PyThreadState_SetAsyncExc against the main thread, subprocess",
        "second, independent real-delivery mechanism for the same claim"),
    FaultTestEntry(
        "tests/test_kalshi_async_accounting_harness_001.py",
        "TestFullCampaign.test_three_interrupt_campaign_zero_identity_violations",
        REALISTIC_SIGNAL_FAULT, "real SIGINT via os.kill, subprocess",
        "gated behind KALSHI_ASYNC_ACCOUNTING_CAMPAIGN=1; the larger "
        "statistical campaign the report's rate numbers were measured with"),

    # -- tests/test_kalshi_meta_runtime_independent_accounting_001.py ------
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_independent_accounting_001.py",
        "TestWriterThreadFaultCatchesRealLoss."
        "test_ledger_disagrees_with_the_durable_disposition",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (LineBoundaryInjector)",
        "writer-thread run-gap; exact chosen line (nearest try: to "
        "_write_one)"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_independent_accounting_001.py",
        "TestWriterThreadFaultCatchesRealLoss."
        "test_productions_own_derived_identity_reports_nothing_wrong",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (LineBoundaryInjector)",
        "same injection as above, different assertion"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_independent_accounting_001.py",
        "TestWriterThreadFaultCatchesRealLoss."
        "test_close_undercounts_the_loss_in_its_own_error_message",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (LineBoundaryInjector)",
        "same injection as above, different assertion"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_independent_accounting_001.py",
        "TestDeadWriterThreadDiagnosticStateLies."
        "test_state_and_healthy_do_not_reflect_a_dead_writer_thread",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (LineBoundaryInjector)",
        "same injection, diagnostic-surface assertion"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_independent_accounting_001.py",
        "TestPlantedDerivedIdentityFailsTheMetaTest."
        "test_the_derived_accepted_identity_is_the_exact_design_that_fails",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (LineBoundaryInjector)",
        "same injection, discrimination-proof assertion"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_independent_accounting_001.py",
        "TestTaskDoneWindowDoesNotLoseEvidenceButStillKillsTheThread."
        "test_fault_at_task_done_writes_the_record_but_still_kills_the_thread",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (LineBoundaryInjector)",
        "second writer-thread window, task_done()"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_independent_accounting_001.py",
        "TestProducerInterruptionAlsoReconciles."
        "test_a_producer_side_fault_still_reconciles_cleanly",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (target_marker)",
        "producer-side window-a, restated for the independent-ledger check"),

    # -- this milestone's own new fault tests -------------------------------
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_admission_close_race_001.py",
        "TestControlledPauseAcceptsAfterCloseIsClean."
        "test_seal_gives_up_while_the_producer_is_provably_still_alive",
        SYNTHETIC_STATE_MACHINE_FAULT,
        "test-owned module-level monkeypatch of canonicalize_or_reason "
        "(threading.Event-gated block)",
        "a controlled pause, not an exception -- proves the SCHEDULING "
        "shape can occur, not how often a real process hits it"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_admission_close_race_001.py",
        "TestControlledPauseAcceptsAfterCloseIsClean."
        "test_desired_property_no_late_acceptance_after_a_clean_close",
        SYNTHETIC_STATE_MACHINE_FAULT,
        "test-owned module-level monkeypatch of canonicalize_or_reason",
        "xfail(strict=True): the desired property, currently false"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_admission_close_race_001.py",
        "TestGenuinelySlowPayloadAlsoRaces."
        "test_a_legal_but_slow_iterating_payload_still_races_the_seal",
        SYNTHETIC_STATE_MACHINE_FAULT,
        "genuinely slow (real wall-clock) but test-constructed Mapping "
        "iteration, no exception injected",
        "no exception mechanism at all -- included for completeness, not "
        "a signal/process fault; the delay is real wall-clock time but the "
        "PAYLOAD SHAPE is synthetic, not measured from production traffic"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_admission_close_race_001.py",
        "TestUnderSchedulerContention.test_race_reproduces_under_real_thread_contention",
        SYNTHETIC_STATE_MACHINE_FAULT,
        "controlled pause + real CPU-bound contending threads",
        "the block is test-controlled; only the SCHEDULING pressure "
        "around it is real"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_queue_gap_001.py",
        "TestPredequeueGapEscapesProductionsOwnSecondSourcedCheck."
        "test_dequeued_never_moves_for_the_lost_item",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (LineBoundaryInjector)",
        "exact chosen line: the dequeued-counter statement itself"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_queue_gap_001.py",
        "TestPredequeueGapEscapesProductionsOwnSecondSourcedCheck."
        "test_dequeue_disposition_gap_reports_zero_over_a_real_loss",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (LineBoundaryInjector)",
        "same injection, second assertion"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_queue_gap_001.py",
        "TestPredequeueGapEscapesProductionsOwnSecondSourcedCheck."
        "test_desired_property_close_must_refuse_this_loss_on_its_own",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (LineBoundaryInjector)",
        "xfail(strict=True): the desired property, currently false"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_queue_gap_001.py",
        "TestExistingRunGapWindowStillCaughtByProductionsOwnCheck."
        "test_the_wider_window_is_still_visible_to_dequeue_disposition_gap",
        SYNTHETIC_STATE_MACHINE_FAULT, "sys.settrace (LineBoundaryInjector)",
        "control: the OLD, wider window, confirming it is still caught"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_fault_classification_001.py",
        "TestRealAsyncExcAgainstTheWriterThreadItself."
        "test_measure_real_asyncexc_landing_rate_against_the_writer_thread",
        REALISTIC_SIGNAL_FAULT,
        "real ctypes.pythonapi.PyThreadState_SetAsyncExc targeted at the "
        "WRITER thread's own OS thread id (not the main/producer thread), "
        "subprocess + parent-enforced timeout",
        "NEW measurement: unlike fault_trial.py's sigint/asyncexc (main "
        "thread only), this targets the background writer thread directly "
        "-- reports whatever landing rate is empirically observed, no "
        "target rate is asserted"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_fault_classification_001.py",
        "TestRealProcessKillMidWrite."
        "test_a_hard_kill_mid_stream_leaves_recoverable_uncommitted_evidence",
        REALISTIC_PROCESS_FAULT,
        "real SIGKILL (process-group-wide), delivered by the OS to a real "
        "child process actively submitting events",
        "no exception is ever raised inside the process under test -- it "
        "simply stops existing; verifies the crash-consistency property "
        "against whatever was durably fsynced at that instant"),
)


def entries_for(classification: str) -> tuple[FaultTestEntry, ...]:
    return tuple(e for e in LEDGER if e.classification == classification)


def all_test_names() -> set[str]:
    return {f"{e.module}::{e.test_name}" for e in LEDGER}
