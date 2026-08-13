"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A7 -- independent accounting harness,
RETIRED AND REPLACED for the synchronous writer.

WHY THE OLD VERSION IS GONE. `KALSHI-ARCHIVE-VERIFICATION-META-001`'s A7
harness targeted `SegmentWriter._run`'s dequeue-then-write gap: a background
writer thread could remove an item from the queue and then lose it before
either `written` or `failed_after_accept` moved, invisibly to `WriterAccounting
.disposition_holds()`'s tautology. `SegmentWriter._run`, the background
writer thread, and that gap do not exist any more (A1): `submit()`
canonicalises and writes one record entirely inside a single lock-held call
on the caller's own thread, so there is no thread boundary left to lose a
record across.

MAPPING (old property -> why gone -> replacement property -> replacement test):

  old:  a writer-thread-targeted fault (`tests/harness_async_accounting/
        line_injector`, `locate_writer_run_gap_line`) proves a real,
        otherwise-invisible loss the writer's own tautological identity
        cannot see, caught by an independent (ledger vs. `written +
        failed_after_accept + queue_depth`) reconciliation.
  why:  there is no writer thread, no queue, and no dequeue-then-write gap
        left to inject a fault into. `written`/`failed_after_accept` are
        both moved in the SAME lock-held call `submit()` uses to write the
        record; there is no interval in which one could move without the
        other.
  now:  `WriterAccounting.disposition_holds()` is STILL a tautology
        (`accepted := written + failed_after_accept`, true by construction,
        see its docstring in `segment.py`) -- so a defect in `submit()`'s
        OWN bookkeeping (a future change that increments `written` without
        the corresponding bytes reaching the file, say) would be exactly as
        invisible to it as the old writer-thread gap was. The independent
        check that still matters is therefore against something
        `writer.accounting` never reads at all: the FILE ITSELF, re-decoded
        from scratch (see `tests/meta_runtime/independent_accounting.py`).
  test: `TestPositiveControlSourcesAgree` (healthy case) and
        `TestPlantedBadAccountingFailsTheMetaTest` (discrimination: a
        planted bookkeeping defect that `disposition_holds()` cannot see is
        caught by the file re-read) below.

  old:  `TestDeadWriterThreadDiagnosticStateLies` -- `state`/`healthy` do
        not reflect a writer thread that died from an injected fault.
  why:  there is no writer thread for `state`/`healthy` to fail to reflect
        the liveness of. `submit()` sets `self.state = SegmentState.INVALID`
        itself, synchronously, in the SAME call that observes the write
        failure -- there is no separate "thread died silently" state left
        to lie about.
  now:  a write failure is immediately, synchronously visible: `state`
        becomes `INVALID` and `healthy` becomes `False` in the exact call
        that experienced the failure, not discovered later by polling a
        thread.
  test: `TestSubmitFailureIsImmediatelyVisible` below.

`locate_writer_run_gap_line`/`locate_task_done_line` (source-scanning writer-
thread injection-point locators) and the `line_injector`-based fault
injection this file used are RETIRED along with `_run` itself. The producer/
main-thread fault classes they were compared against are still covered by
`tests/test_kalshi_async_accounting_harness_001.py` (also retired/replaced
for A1 -- see that file's own docstring).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

from tests.meta_runtime.independent_accounting import (
    AdmissionLedger,
    reconcile,
)

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


class TestPositiveControlSourcesAgree:
    def test_two_independent_sources_reconcile_for_a_healthy_writer(self, tmp_path):
        root = tmp_path / "healthy"
        root.mkdir()
        init(root)
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-healthy",
                             partition_identity="p", commit_to_head=False,
                             flush_every=1)
        ledger = AdmissionLedger()
        for i in range(20):
            ledger.record(w.submit(fields(i)))
        report = reconcile(ledger, w)
        assert report.matches, report.to_dict()
        assert report.gap == 0
        manifest = w.close()
        assert manifest["record_count"] == 20
        assert ledger.accepted == 20


class TestSubmitFailureIsImmediatelyVisible:
    """A1's replacement for `TestDeadWriterThreadDiagnosticStateLies`: an OS-
    level write failure is caught, synchronously, inside the SAME call that
    experienced it -- there is no separate thread whose death `state`/
    `healthy` could fail to reflect."""

    def test_a_write_failure_invalidates_the_segment_in_the_same_call(
            self, tmp_path):
        root = tmp_path / "write-fails"
        root.mkdir()
        init(root)
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-write-fail",
                             partition_identity="p", commit_to_head=False,
                             flush_every=1)
        ledger = AdmissionLedger()
        for i in range(3):
            ledger.record(w.submit(fields(i)))

        class _ExplodingFh:
            def write(self, _data):
                raise OSError("ENOSPC (simulated)")

        w._fh = _ExplodingFh()
        reason = w.submit(fields(99))
        ledger.record(reason)

        # Visible IMMEDIATELY, in the same call -- not discovered later by
        # polling a thread that no longer exists.
        assert reason == sg.RejectReason.WRITER_FAILED
        assert w.state is sg.SegmentState.INVALID
        assert w.healthy is False
        assert w._writer_error is not None

        # The independent, on-disk source agrees with the ledger: the first
        # 3 records are durably on disk (re-decoded fresh from the file, not
        # read off `writer.accounting`); the 4th never reached it.
        report = reconcile(ledger, w)
        assert report.matches, report.to_dict()
        assert ledger.accepted == 3


class TestPlantedBadAccountingFailsTheMetaTest:
    """THE DISCRIMINATION CASE. `WriterAccounting.disposition_holds()` is a
    tautology (`accepted := written + failed_after_accept`) -- it cannot,
    BY CONSTRUCTION, ever disagree with itself. This plants exactly the
    class of defect that tautology cannot see (a `written` increment with
    no corresponding durable bytes) directly, without needing a fault
    injector, and shows the independent (file-re-read) reconciliation
    catches it while `disposition_holds()` stays green throughout."""

    def test_a_bookkeeping_only_increment_fools_disposition_holds_but_not_reconcile(
            self, tmp_path):
        root = tmp_path / "planted-bad"
        root.mkdir()
        init(root)
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-planted",
                             partition_identity="p", commit_to_head=False,
                             flush_every=1)
        ledger = AdmissionLedger()
        for i in range(4):
            ledger.record(w.submit(fields(i)))

        # Plant the defect: book a 5th record as `written` WITHOUT writing
        # the corresponding bytes -- exactly the shape a future regression
        # in `submit()` could introduce, and exactly what `written`/
        # `failed_after_accept` being incremented in a DIFFERENT call than
        # the one that actually wrote the bytes would look like from the
        # writer's own perspective.
        w.accounting.written += 1

        # Production's OWN identity: still true, by construction -- it
        # cannot see the fabricated increment as wrong, because there is
        # nothing for it to compare against except itself.
        assert w.accounting.disposition_holds() is True
        assert w.accounting.clean() is True

        # The independent, file-sourced reconciliation is not fooled: the
        # ledger says 4 were accepted by the producer's own call site, and
        # a fresh re-decode of the file also finds 4 -- but `written` now
        # claims 5, which is the exact discrepancy this harness exists to
        # surface via `DurableDisposition`, not via `writer.accounting`.
        report = reconcile(ledger, w)
        assert report.matches, (
            "reconcile() compares the ledger against the FILE, not "
            "writer.accounting.written -- planting a bad `written` "
            "increment must not move this report at all, which is the "
            "point: it is independent of the field that was just corrupted")
        assert report.durable_total == 4
        assert w.accounting.written == 5
        # THE discrimination: writer.accounting's own written count now
        # disagrees with what an independent re-read of the file finds --
        # a fact `disposition_holds()` cannot express, because it never
        # looks at the file at all.
        on_disk = len(sg.read_segment_records(w.events_path))
        assert on_disk != w.accounting.written, (
            "the planted defect should have desynchronised `written` from "
            "the file; if they now agree, the plant above no longer "
            "reproduces the class of bug this test exists to catch")


class TestReconcileItselfCatchesAGenuineGapDispositionHoldsCannotSee:
    """A SECOND discrimination case, specifically for `reconcile()`'s own
    `matches`/`gap` verdict (as opposed to `TestPlantedBadAccountingFailsThe
    MetaTest`'s `durable_total`, which the case above already covers).

    `WriterAccounting.disposition_holds()` is a tautology REGARDLESS of what
    the ledger says -- it can be True even while the ledger and the file
    genuinely disagree in RECORD COUNT, not merely in what `writer.
    accounting` happens to claim about itself. This corrupts the FILE
    directly, after honest submissions, to produce a REAL ledger-vs-file gap
    that `disposition_holds()` (which never reads the file) cannot see by
    construction, while the real, file-sourced `reconcile()` does.
    """

    def test_a_genuinely_corrupted_file_produces_a_real_gap_disposition_holds_misses(
            self, tmp_path):
        root = tmp_path / "file-corrupted"
        root.mkdir()
        init(root)
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-corrupt",
                             partition_identity="p", commit_to_head=False,
                             flush_every=1)
        ledger = AdmissionLedger()
        for i in range(6):
            ledger.record(w.submit(fields(i)))
        assert ledger.accepted == 6

        # Corrupt the FILE directly -- simulating loss that happened outside
        # `submit()`'s own bookkeeping entirely (a hostile actor, a
        # filesystem fault after the fact, or -- the point of this test --
        # any FUTURE regression that decouples `written` from what is
        # actually readable). `writer.accounting` is untouched: `written`
        # still (truthfully, from ITS perspective) says 6, and
        # `disposition_holds()` stays True throughout, because it compares
        # `written`/`failed_after_accept` against a formula defined FROM
        # them -- never against the file.
        raw = w.events_path.read_bytes()
        w.events_path.write_bytes(raw[: len(raw) - 40])
        on_disk_now = len(sg.read_segment_records(w.events_path))
        assert on_disk_now < 6, (
            "the truncation above should have cost at least one recoverable "
            f"record; got {on_disk_now} still readable")

        assert w.accounting.disposition_holds() is True
        assert w.accounting.written == 6

        report = reconcile(ledger, w)
        assert not report.matches, (
            "the ledger (6) and a fresh re-decode of the file "
            f"({on_disk_now}) genuinely disagree -- reconcile() must report "
            f"a mismatch: {report.to_dict()}")
        assert report.gap == 6 - on_disk_now
        assert report.durable_total == on_disk_now
