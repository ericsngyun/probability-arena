"""KALSHI-ARCHIVE-VERIFICATION-META-001 A7 -- independent accounting
harness: two independently sourced observations, reconciled, catching real
writer-side loss production's own derived identity cannot see BY
CONSTRUCTION.

DOES NOT MODIFY app/realtime/segment.py. Fault injection targets
`SegmentWriter._thread`/`_thread.ident` specifically -- the WRITER thread,
never the producer/main thread `tests/harness_async_accounting/` already
covers exhaustively (see that package's module docstring and this file's
`tests/meta_runtime/independent_accounting.py` for exactly why that
distinction matters and where `_run`'s one totally-unguarded gap is).

Structure:
  Section 1 -- positive control: a healthy writer, no fault, the two
               independent sources agree.
  Section 2 -- the writer-thread-targeted fault: an item is dequeued and
               then genuinely lost, counted in NEITHER `written` nor
               `failed_after_accept` nor a still-queued item -- and the
               reconciliation catches it while production's own identity
               (`disposition_holds()`/`reconciles()`) cannot, by
               construction.
  Section 3 -- the diagnostic-state consequence: the writer thread is
               provably dead, yet `state`/`healthy` say otherwise (fed
               forward into the A8 diagnostic-state inventory).
  Section 4 -- discrimination: a PLANTED implementation that derives
               `accepted` from `written + failed_after_accept + pending`
               (i.e. reproduces production's actual A6 design, deliberately,
               as the explicit planted case the brief asks for) is shown to
               FAIL this meta-test's reconciliation, proving the harness
               catches the exact class of design the brief names.
  Section 5 -- a second writer-thread fault window (`task_done`), and the
               `producer interruption` window already covered by the
               existing A3 harness, reconciled through the SAME independent
               two-source check for completeness.

Every writer-thread-targeted fault below deliberately kills a daemon thread
with an unhandled exception -- that IS the reproduction, not an accident --
so pytest's `PytestUnhandledThreadExceptionWarning` is expected noise on
every such test and is filtered at module scope rather than asserted
against.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

from tests.harness_async_accounting.line_injector import (
    InjectedFault, LineBoundaryInjector, target,
)
from tests.meta_runtime.independent_accounting import (
    AdmissionLedger,
    DurableDisposition,
    locate_task_done_line,
    locate_writer_run_gap_line,
    read_durable_disposition,
    reconcile,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning")

UTC = timezone.utc
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
ENV = "demo"


def fields(i):
    return {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": "KXA", "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {"price_dollars": "0.5100", "side": "no"},
        "normalized_event": {"raw_price_units": 5100},
    }


def init(root):
    return ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")


def _wait_thread_dead(w, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while w._thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)


# =====================================================================
# 1. POSITIVE CONTROL: healthy writer, no fault, the two independent
#    sources agree exactly.
# =====================================================================

class TestPositiveControlSourcesAgree:
    def test_two_independent_sources_reconcile_for_a_healthy_writer(self, tmp_path):
        root = tmp_path / "healthy"
        root.mkdir()
        init(root)
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-healthy",
                             partition_identity="p", commit_to_head=False)
        ledger = AdmissionLedger()
        for i in range(20):
            ledger.record(w.submit(fields(i)))
        # let the writer thread fully drain before reading the durable side
        deadline = time.monotonic() + 2.0
        while w.accounting.written < 20 and time.monotonic() < deadline:
            time.sleep(0.005)
        report = reconcile(ledger, w)
        assert report.matches, report.to_dict()
        assert report.gap == 0
        manifest = w.close()
        assert manifest["record_count"] == 20
        assert ledger.accepted == 20


# =====================================================================
# 2. THE WRITER-THREAD FAULT: real, silent loss the derived identity
#    cannot see, caught by the independent reconciliation.
# =====================================================================

class TestWriterThreadFaultCatchesRealLoss:
    """The `_run`-gap fault: an item is durably dequeued and then lost
    with no terminal booking anywhere. Deterministic: the writer thread's
    trace hook is installed via `threading.settrace` BEFORE the writer is
    constructed (and therefore before its thread starts), which is what
    makes this reliably land on the writer thread specifically rather than
    the producer/main thread."""

    def test_ledger_disagrees_with_the_durable_disposition(self, tmp_path):
        root = tmp_path / "faulted"
        root.mkdir()
        init(root)
        lineno = locate_writer_run_gap_line(sg.SegmentWriter._run)
        pt = target(sg.__file__, "_run", lineno, hit_index=1,
                   label="writer-thread-run-gap")
        ledger = AdmissionLedger()
        with LineBoundaryInjector(pt):
            w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-fault",
                                 partition_identity="p", commit_to_head=False)
            for i in range(5):
                ledger.record(w.submit(fields(i)))
            _wait_thread_dead(w)
        assert not w._thread.is_alive(), (
            "the writer thread should have died from the injected fault; "
            "if it did not, the injection point moved and this test's "
            "reproduction is stale")
        report = reconcile(ledger, w)
        assert not report.matches, (
            "expected the independent sources to DISAGREE (real writer-"
            "side loss) -- if they now match, either the fault stopped "
            "landing (the injection target moved -- see "
            "locate_writer_run_gap_line) or production closed this gap, "
            "in which case update this test to assert reconciliation "
            f"instead of a mismatch: {report.to_dict()}")
        assert report.gap == 1, (
            f"expected exactly one silently-lost item (the one dequeued "
            f"right as the writer thread died); got gap={report.gap}: "
            f"{report.to_dict()}")
        print(f"\n[A7] independent reconciliation caught {report.gap} "
              f"silently-lost item(s): {report.to_dict()}")

    def test_productions_own_derived_identity_reports_nothing_wrong(self, tmp_path):
        """THE POINT: `WriterAccounting.disposition_holds()`/`reconciles()`
        are tautologies (`accepted := written + failed_after_accept +
        pending`), so they report TRUE even while the SAME writer has just
        silently lost a real, ledger-confirmed item. This is not a defect
        in `disposition_holds()`'s arithmetic -- the arithmetic is correct
        by definition -- it is a demonstration that an identity defined
        FROM one side of a fact can never contradict that same fact,
        which is exactly why an INDEPENDENT second source (Section 2's
        ledger) is required at all."""
        root = tmp_path / "faulted-tautology"
        root.mkdir()
        init(root)
        lineno = locate_writer_run_gap_line(sg.SegmentWriter._run)
        pt = target(sg.__file__, "_run", lineno, hit_index=1,
                   label="writer-thread-run-gap-tautology")
        ledger = AdmissionLedger()
        with LineBoundaryInjector(pt):
            w = sg.SegmentWriter(root, environment=ENV,
                                 segment_id="seg-fault-tautology",
                                 partition_identity="p", commit_to_head=False)
            for i in range(5):
                ledger.record(w.submit(fields(i)))
            _wait_thread_dead(w)
        # production's OWN identity: True, always, by construction.
        assert w.accounting.disposition_holds() is True
        assert w.accounting.reconciles() or not w.accounting.admission_holds()
        # yet the independent ledger proves real loss:
        report = reconcile(ledger, w)
        assert not report.matches, (
            "production's tautological identity staying green is only "
            "meaningful alongside the independent ledger catching the "
            f"real loss -- but this run's reconciliation also matched: "
            f"{report.to_dict()}")

    def test_close_undercounts_the_loss_in_its_own_error_message(self, tmp_path):
        """Even the FAILURE close() eventually reports (via the 'not
        clean' pending check, once the surviving un-drained items are
        measured) does not mention the one item that fell into this gap:
        close() sees 4 undrained items, never 5. The ledger is the only
        thing that ever knew 5 were accepted."""
        root = tmp_path / "faulted-close"
        root.mkdir()
        init(root)
        lineno = locate_writer_run_gap_line(sg.SegmentWriter._run)
        pt = target(sg.__file__, "_run", lineno, hit_index=1,
                   label="writer-thread-run-gap-close")
        ledger = AdmissionLedger()
        with LineBoundaryInjector(pt):
            w = sg.SegmentWriter(root, environment=ENV,
                                 segment_id="seg-fault-close",
                                 partition_identity="p", commit_to_head=False)
            for i in range(5):
                ledger.record(w.submit(fields(i)))
            _wait_thread_dead(w)
        try:
            w.close()
            raise AssertionError(
                "expected close() to refuse a segment with undrained "
                "items -- if it now succeeds, the loss became invisible "
                "at an even earlier point than before")
        except sg.SegmentError as exc:
            msg = str(exc)
            assert "never drained" in msg or "not written" in msg, msg
            assert "4" in msg, (
                f"expected close()'s own error to name 4 undrained items "
                f"(not 5 -- the fifth is INVISIBLE even to the failure "
                f"report), got: {msg!r}")
        assert ledger.accepted == 5, (
            "the independent ledger is the ONLY record anywhere that 5 "
            "items were ever accepted")


# =====================================================================
# 3. THE DIAGNOSTIC-STATE CONSEQUENCE, fed forward to A8: the writer
#    thread is dead but `state`/`healthy` do not reflect that.
# =====================================================================

class TestDeadWriterThreadDiagnosticStateLies:
    def test_state_and_healthy_do_not_reflect_a_dead_writer_thread(self, tmp_path):
        root = tmp_path / "dead-thread-diagnostic"
        root.mkdir()
        init(root)
        lineno = locate_writer_run_gap_line(sg.SegmentWriter._run)
        pt = target(sg.__file__, "_run", lineno, hit_index=1,
                   label="writer-thread-run-gap-diagnostic")
        with LineBoundaryInjector(pt):
            w = sg.SegmentWriter(root, environment=ENV,
                                 segment_id="seg-dead-diag",
                                 partition_identity="p", commit_to_head=False)
            w.submit(fields(0))
            _wait_thread_dead(w)
        assert not w._thread.is_alive()
        assert w.state is sg.SegmentState.OPEN, (
            "production's `state` should be lying here -- it stays OPEN "
            "even though the writer thread that services it is provably "
            "dead; if this now reports something else, the diagnostic "
            "surface changed and A8's inventory needs revisiting")
        assert w.healthy is True, (
            "`healthy` is defined as `state is OPEN and writer_error is "
            "None` -- both true here despite a dead writer thread, "
            "confirming `healthy` is a DIAGNOSTIC read of state that "
            "never observed the thread, not an AUTHORITATIVE liveness "
            "check")
        assert w._writer_error is None


# =====================================================================
# 4. DISCRIMINATION: a PLANTED implementation that derives `accepted`
#    exactly the way the brief describes -- `written + failed + pending`
#    -- must FAIL this meta-test's reconciliation the same way production
#    does. Production itself already fits this description (A6's actual
#    design), so this section demonstrates the SAME reconciliation applied
#    directly to production is what fails, without introducing a second,
#    separate toy implementation that could be accused of not matching
#    production's real shape.
# =====================================================================

class TestPlantedDerivedIdentityFailsTheMetaTest:
    def test_the_derived_accepted_identity_is_the_exact_design_that_fails(
            self, tmp_path):
        """Restates Section 2's finding as the explicit discrimination
        claim: `WriterAccounting.accepted` IS `written + failed_after_
        accept + pending` (see `app/realtime/segment.py`'s
        `WriterAccounting.accepted` property) -- literally the planted-bad
        shape the brief names -- and it is caught failing this meta-test's
        independent reconciliation, not merely differing from it by
        construction in the abstract."""
        import inspect
        src = inspect.getsource(sg.WriterAccounting.accepted.fget)
        assert "written" in src and "failed_after_accept" in src \
            and "pending" in src, (
            "WriterAccounting.accepted no longer derives from these three "
            "fields -- the planted-bad shape this section targets has "
            "changed; re-derive this assertion against the new shape")

        root = tmp_path / "planted-derived"
        root.mkdir()
        init(root)
        lineno = locate_writer_run_gap_line(sg.SegmentWriter._run)
        pt = target(sg.__file__, "_run", lineno, hit_index=1,
                   label="writer-thread-run-gap-planted")
        ledger = AdmissionLedger()
        with LineBoundaryInjector(pt):
            w = sg.SegmentWriter(root, environment=ENV,
                                 segment_id="seg-planted-derived",
                                 partition_identity="p", commit_to_head=False)
            for i in range(5):
                ledger.record(w.submit(fields(i)))
            _wait_thread_dead(w)
        # The planted-bad identity (production's actual `accepted`
        # property) reports itself internally consistent...
        assert w.accounting.accepted == (
            w.accounting.written + w.accounting.failed_after_accept
            + w.accounting.pending + w._queue.qsize())
        # ...while the independent, two-source reconciliation this
        # milestone requires FAILS it:
        report = reconcile(ledger, w)
        assert not report.matches, (
            "the meta-test's independent reconciliation should have "
            f"FAILED the derived-identity design; it did not: "
            f"{report.to_dict()}")


# =====================================================================
# 5. A SECOND WRITER-THREAD WINDOW (task_done) -- no evidence loss (the
#    record was already durably written before this point), but the
#    writer thread still dies unnoticed, and PRODUCER-side interruption is
#    already covered by the existing A3 harness -- reconciled here through
#    the SAME independent check for completeness, not re-litigated.
# =====================================================================

class TestTaskDoneWindowDoesNotLoseEvidenceButStillKillsTheThread:
    def test_fault_at_task_done_writes_the_record_but_still_kills_the_thread(
            self, tmp_path):
        root = tmp_path / "task-done-window"
        root.mkdir()
        init(root)
        lineno = locate_task_done_line(sg.SegmentWriter._run)
        pt = target(sg.__file__, "_run", lineno, hit_index=1,
                   label="writer-thread-task-done")
        ledger = AdmissionLedger()
        with LineBoundaryInjector(pt):
            w = sg.SegmentWriter(root, environment=ENV,
                                 segment_id="seg-task-done",
                                 partition_identity="p", commit_to_head=False)
            ledger.record(w.submit(fields(0)))
            _wait_thread_dead(w)
        # The record itself made it to `written` -- no data was lost here,
        # unlike the run-gap window above.
        assert w.accounting.written == 1
        assert not w._thread.is_alive()
        report = reconcile(ledger, w)
        assert report.matches, (
            "this window should NOT lose the record (it fires AFTER "
            f"_write_one already completed): {report.to_dict()}")
        # But the diagnostic surface still lies about liveness, same as
        # Section 3 -- feeds the same A8 finding from a second angle.
        assert w.state is sg.SegmentState.OPEN
        assert w.healthy is True


class TestProducerInterruptionAlsoReconciles:
    """The producer/main-thread windows are the existing A3 harness's
    territory (`tests/test_kalshi_async_accounting_harness_001.py`); this
    is a single, brief confirmation that the SAME independent two-source
    reconciliation this file introduces also holds for that class (no
    loss expected there post-A6 -- see that file's own required-outcome
    assertions), for completeness of the accounting picture, not a
    re-implementation of that harness's own campaign."""

    def test_a_producer_side_fault_still_reconciles_cleanly(self, tmp_path):
        from tests.harness_async_accounting.line_injector import target_marker

        root = tmp_path / "producer-side"
        root.mkdir()
        init(root)
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-producer",
                             partition_identity="p", commit_to_head=False)
        ledger = AdmissionLedger()
        for i in range(5):
            ledger.record(w.submit(fields(i)))
        pt = target_marker(sg.__file__, sg.SegmentWriter.submit, "window-a")
        try:
            with LineBoundaryInjector(pt):
                ledger.record(w.submit(fields(99)))
        except BaseException:
            pass
        deadline = time.monotonic() + 2.0
        while w.accounting.written < 5 and time.monotonic() < deadline:
            time.sleep(0.005)
        report = reconcile(ledger, w)
        assert report.matches, (
            "a producer-side (main-thread) fault at window (a) should "
            f"not desynchronise the independent sources post-A6: "
            f"{report.to_dict()}")
