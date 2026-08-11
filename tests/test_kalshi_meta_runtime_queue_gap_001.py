"""KALSHI-ARCHIVE-CORE-REMEDIATION-003B A2 -- the queue-get/disposition gap,
targeted at its TRUE (semantically located) window rather than the nearest-
`try:`-derived one the existing A7 harness uses.

See `tests/meta_runtime/queue_gap_locator.py` for the full argument. Short
version: `independent_accounting.locate_writer_run_gap_line` now resolves to
the `try:` line guarding `self._write_one(item)` -- ONE STATEMENT AFTER
defect F's own `self.accounting.dequeued += 1` counter. This file targets
the counter's OWN statement directly (a genuinely, currently-uncovered
window between `queue.get()` returning and that increment), and shows
`WriterAccounting.dequeue_disposition_gap()` -- the exact check `close()`
gates publication on -- CANNOT see the loss this window produces, unlike the
narrower window the existing harness already covers.

DOES NOT MODIFY `app/realtime/segment.py`.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

from tests.harness_async_accounting.line_injector import (
    InjectedFault, LineBoundaryInjector, target,
)
from tests.meta_runtime.independent_accounting import (
    AdmissionLedger, locate_writer_run_gap_line, reconcile,
)
from tests.meta_runtime.queue_gap_locator import (
    locate_predequeue_gap_line, nearest_try_before_write_one,
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
# 1. THE LOCATOR ITSELF: the existing (nearest-`try:`) locator and the new
#    semantic one now disagree -- concrete, source-level proof that the
#    existing locator's docstring claim is false for 63bf0d1.
# =====================================================================

class TestLocatorSemanticsHaveDrifted:
    def test_nearest_try_no_longer_finds_the_predequeue_line(self):
        nearest_try_line = locate_writer_run_gap_line(sg.SegmentWriter._run)
        # Restated standalone (not re-imported) so this assertion does not
        # depend on `independent_accounting`'s own internals staying
        # byte-identical to this module's re-implementation.
        standalone_nearest_try = nearest_try_before_write_one(sg.SegmentWriter._run)
        assert nearest_try_line == standalone_nearest_try, (
            "the two nearest-try walk-backs disagree with each other -- "
            "re-derive before trusting either")

        predequeue_line = locate_predequeue_gap_line(sg.SegmentWriter._run)

        assert predequeue_line != nearest_try_line, (
            "the semantic pre-dequeue-gap locator now agrees with the "
            "nearest-try locator -- either `_run`'s shape changed such "
            "that the two windows coincide again (in which case this test "
            "and its docstring are stale and should be simplified), or "
            "this test's own locator logic is broken")
        assert predequeue_line < nearest_try_line, (
            f"expected the pre-dequeue-gap line ({predequeue_line}) to "
            f"come BEFORE the nearest-try line ({nearest_try_line}) -- "
            "defect F's counter sits between `queue.get()` and the `try:` "
            "guarding `_write_one`, so its own statement must be earlier")

        src_lines, first_lineno = __import__("inspect").getsourcelines(
            sg.SegmentWriter._run)
        nearest_try_text = src_lines[nearest_try_line - first_lineno].strip()
        assert nearest_try_text == "try:", (
            f"expected the nearest-try line to literally read 'try:', got "
            f"{nearest_try_text!r} -- the walk-back algorithm's own "
            "precondition changed")
        predequeue_text = src_lines[predequeue_line - first_lineno].strip()
        assert "dequeued" in predequeue_text and "+=" in predequeue_text, (
            f"expected the pre-dequeue-gap line to be the dequeued counter "
            f"increment, got {predequeue_text!r}")


# =====================================================================
# 2. THE REAL, NARROWER GAP: a fault between `queue.get()` returning and
#    `dequeued += 1` is invisible to `dequeue_disposition_gap()`, unlike
#    the wider window the existing A7 harness (correctly) already catches.
# =====================================================================

class TestPredequeueGapEscapesProductionsOwnSecondSourcedCheck:
    def test_dequeued_never_moves_for_the_lost_item(self, tmp_path):
        root = tmp_path / "predequeue"
        root.mkdir()
        init(root)
        lineno = locate_predequeue_gap_line(sg.SegmentWriter._run)
        pt = target(sg.__file__, "_run", lineno, hit_index=1,
                   label="predequeue-gap")
        with LineBoundaryInjector(pt):
            w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-pdq",
                                 partition_identity="p", commit_to_head=False)
            w.submit(fields(0))
            _wait_thread_dead(w)
        assert not w._thread.is_alive(), (
            "the writer thread should have died from the injected fault; "
            "if it did not, the injection point moved")
        # THE POINT: unlike the run-gap window (`dequeued` reaches 1 before
        # the fault, per the A7 harness), THIS window fires before the
        # counter itself, so `dequeued` never moves for the lost item.
        assert w.accounting.dequeued == 0, (
            f"expected dequeued == 0 (the fault fires strictly before its "
            f"own increment) -- got {w.accounting.dequeued}; if this is "
            "now 1, the injection landed after the counter and this test "
            "needs re-pointing at the actual pre-dequeue-gap line")
        assert w.accounting.written == 0
        assert w.accounting.failed_after_accept == 0

    def test_dequeue_disposition_gap_reports_zero_over_a_real_loss(
            self, tmp_path):
        """THE DEFECT, STATED AS PRODUCTION'S OWN CHECK: `close()` gates
        publication on `dequeue_disposition_gap() != 0`
        (`_close_stages`'s `independent_gap` check). For THIS window that
        check reports 0 -- agreement -- despite one item having been
        irreversibly removed from the queue and never written, never
        booked as failed, and never counted as dequeued either."""
        root = tmp_path / "predequeue-gap-invisible"
        root.mkdir()
        init(root)
        lineno = locate_predequeue_gap_line(sg.SegmentWriter._run)
        pt = target(sg.__file__, "_run", lineno, hit_index=1,
                   label="predequeue-gap-invisible")
        ledger = AdmissionLedger()
        with LineBoundaryInjector(pt):
            w = sg.SegmentWriter(root, environment=ENV,
                                 segment_id="seg-pdq-invisible",
                                 partition_identity="p", commit_to_head=False)
            ledger.record(w.submit(fields(0)))
            _wait_thread_dead(w)
        assert not w._thread.is_alive()

        # Production's OWN gate, computed exactly the way `_close_stages`
        # computes it: zero, even though a real item is gone.
        assert w.accounting.dequeue_disposition_gap() == 0, (
            "expected production's own dequeue_disposition_gap() to REPORT "
            "ZERO over this real loss -- if it is now nonzero, the "
            "pre-dequeue window has been closed and this test should be "
            "updated to assert detection instead")

        # KALSHI-ARCHIVE-CORE-REMEDIATION-003B A2 REPAIRED: `dequeued` above
        # is UNCHANGED -- still 0, still never moved earlier, exactly as
        # required. What changed is that the item is no longer invisible
        # to `_measure_pending`: `_run` now durably claims the item (`self.
        # _claimed`, set in the SAME statement as the queue removal) before
        # anything else can happen to it, and `_measure_pending` counts a
        # still-claimed item the same way it counts one still sitting in the
        # queue. `clean()` therefore correctly refuses this loss.
        w._measure_pending()
        assert w.accounting.pending == 1, (
            "expected the structurally-claimed item to be found and "
            "counted as pending -- if this is 0 again, the A2 repair's "
            "`_claimed` handoff is no longer being measured at close")
        assert w.accounting.clean() is False, (
            "expected clean() to report False now that the claimed-but-"
            "undisposed item is counted as pending -- a real loss must "
            "never present as close_status: 'clean'")

        # The INDEPENDENT ledger (never reads dequeued, written,
        # failed_after_accept via any derived/self-referential path) is the
        # only thing here that still knows one item was truly accepted.
        assert ledger.accepted == 1
        report = reconcile(ledger, w)
        assert not report.matches, (
            "the independent two-source reconciliation SHOULD still catch "
            f"this (it never reads `dequeued`): {report.to_dict()}")
        assert report.gap == 1

    def test_desired_property_close_must_refuse_this_loss_on_its_own(
            self, tmp_path):
        root = tmp_path / "predequeue-desired"
        root.mkdir()
        init(root)
        lineno = locate_predequeue_gap_line(sg.SegmentWriter._run)
        pt = target(sg.__file__, "_run", lineno, hit_index=1,
                   label="predequeue-desired")
        with LineBoundaryInjector(pt):
            w = sg.SegmentWriter(root, environment=ENV,
                                 segment_id="seg-pdq-desired",
                                 partition_identity="p", commit_to_head=False)
            w.submit(fields(0))
            _wait_thread_dead(w)
        try:
            w.close()
        except sg.SegmentError:
            return  # desired: close() refuses on its own, no external ledger needed
        raise AssertionError(
            "close() published a segment over a real, silently-lost item "
            "using only its own accounting -- no independent ledger was "
            "needed to prove the loss existed, yet close() did not detect "
            "it")


# =====================================================================
# 3. CONTRAST: the WIDER window the existing A7 harness targets (nearest-
#    try:) IS still caught by `dequeue_disposition_gap()`, confirming the
#    fix is real but narrower than claimed, not absent.
# =====================================================================

class TestExistingRunGapWindowStillCaughtByProductionsOwnCheck:
    def test_the_wider_window_is_still_visible_to_dequeue_disposition_gap(
            self, tmp_path):
        root = tmp_path / "wider-window-still-caught"
        root.mkdir()
        init(root)
        lineno = locate_writer_run_gap_line(sg.SegmentWriter._run)
        pt = target(sg.__file__, "_run", lineno, hit_index=1,
                   label="wider-window-control")
        with LineBoundaryInjector(pt):
            w = sg.SegmentWriter(root, environment=ENV,
                                 segment_id="seg-wider-control",
                                 partition_identity="p", commit_to_head=False)
            w.submit(fields(0))
            _wait_thread_dead(w)
        assert not w._thread.is_alive()
        assert w.accounting.dequeued == 1, (
            "the wider (existing-harness) window fires AFTER the dequeued "
            "counter -- it should still be 1")
        assert w.accounting.dequeue_disposition_gap() == 1, (
            "production's own check DOES catch this wider window -- "
            "confirming the pre-dequeue window this file adds is a "
            "genuinely NARROWER, additional gap, not a restatement of an "
            "already-caught one")
