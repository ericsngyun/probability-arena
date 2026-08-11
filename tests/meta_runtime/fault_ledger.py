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
    # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1/A7: `TestFourWindowsReproduce
    # Deterministically`/`TestNegativeControlsDoNotOverFire`/
    # `TestDiscriminationDeterministic` (the `# FAULT-WINDOW:`-marker-based
    # sections) are RETIRED -- the four submit()-side boundaries they
    # targeted no longer exist as separate source lines; `submit()` is now
    # one lock-held call. See that test module's own docstring for the
    # full mapping. The REALISTIC_SIGNAL_FAULT sections below (unchanged
    # mechanism, still real subprocess + real SIGINT/asyncexc against the
    # real, now-synchronous `SegmentWriter.submit()`) are kept.
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
    # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1/A7: RETIRED AND REPLACED. The
    # writer-thread-targeted `sys.settrace` entries this section used to list
    # (`TestWriterThreadFaultCatchesRealLoss`, `TestDeadWriterThreadDiagnostic
    # StateLies`, `TestTaskDoneWindow...`, `TestProducerInterruptionAlso
    # Reconciles`) targeted `SegmentWriter._run`'s background writer thread,
    # which no longer exists -- `submit()` now writes synchronously, on the
    # caller's own thread. The replacement harness
    # (`tests/meta_runtime/independent_accounting.py`) reconciles an
    # admission ledger against a FRESH RE-DECODE OF THE FILE (never
    # `writer.accounting`), and the replacement tests below are a real
    # (planted-defect / OS-failure) discrimination pair rather than a
    # `sys.settrace`-injected one -- see that test module's own docstring for
    # the full old-property -> replacement-property mapping.
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_independent_accounting_001.py",
        "TestSubmitFailureIsImmediatelyVisible."
        "test_a_write_failure_invalidates_the_segment_in_the_same_call",
        SYNTHETIC_STATE_MACHINE_FAULT,
        "test-owned replacement of self._fh with an object whose write() "
        "always raises OSError",
        "the synchronous replacement for the old writer-thread OS-failure "
        "class: a write fault is now caught, and the segment invalidated, "
        "inside the SAME call that experienced it"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_independent_accounting_001.py",
        "TestPlantedBadAccountingFailsTheMetaTest."
        "test_a_bookkeeping_only_increment_fools_disposition_holds_but_not_reconcile",
        SYNTHETIC_STATE_MACHINE_FAULT,
        "test-owned direct manipulation of WriterAccounting.written "
        "(no corresponding write)",
        "discrimination case: WriterAccounting.disposition_holds() is a "
        "tautology and cannot see this planted defect; the independent, "
        "file-re-read reconciliation does"),

    # -- tests/test_kalshi_meta_runtime_admission_close_race_001.py --------
    # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1/A7: RETIRED AND REPLACED. The
    # queue-ownership admission/close race this section used to reproduce
    # (`ControlledAdmission`/`SlowIterationMapping`/`BusyThreadPool` against
    # `_admit`/`_seal_admissions`) has no analogue: `submit()` and `close()`
    # are now mutually exclusive on one lock, with no asynchronous admission
    # step for either mechanism to hold a thread inside. The replacement
    # tests use a REAL held producer thread (via `pre_write_hook`, a
    # legitimate, already-existing test seam) to prove the two calls
    # genuinely cannot interleave.
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_admission_close_race_001.py",
        "TestSubmitAndCloseAreMutuallyExclusive."
        "test_close_never_overlaps_a_running_submit",
        SYNTHETIC_STATE_MACHINE_FAULT,
        "test-owned pre_write_hook (threading.Event-gated block) inside a "
        "real producer thread's submit() call",
        "a controlled pause, not an exception -- proves close() genuinely "
        "blocks on self._lock behind a still-running submit()"),
    FaultTestEntry(
        "tests/test_kalshi_meta_runtime_admission_close_race_001.py",
        "TestSubmitAndCloseAreMutuallyExclusive."
        "test_no_late_acceptance_after_a_clean_close",
        SYNTHETIC_STATE_MACHINE_FAULT,
        "no injection -- direct, deterministic sequencing (close(), then "
        "submit())",
        "THE property the retired race violated: no late ACCEPTED after a "
        "clean close"),
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
