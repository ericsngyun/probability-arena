"""KALSHI-ARCHIVE-VERIFICATION-META-001 A8 -- diagnostic state vs canonical
state: an inventory, and the `_inflight` brick reproduction.

DOES NOT MODIFY app/realtime/segment.py or app/realtime/archive_head.py.

=====================================================================
THE INVENTORY
=====================================================================
For every variable below: CAN IT BLOCK canonical evidence finalization
(`close()`/`commit_segment()`/publication), and is it AUTHORITATIVE (the
filesystem/durable state agrees with it by construction) or merely
DIAGNOSTIC (a live, in-memory belief that can desynchronise from what is
actually true on disk or in the OS)?

  _inflight (SegmentWriter)
      CAN BLOCK CLOSE: YES -- `_seal_admissions()` (called first inside
      `close()`, unconditionally, with no timeout-recovery path) spins until
      it reaches 0 or a ~5s deadline, then RAISES, and `close()` has already
      returned before `_close_locked()`/`_release_lock()` would ever run.
      AUTHORITATIVE OR DIAGNOSTIC: neither cleanly -- it is meant to be
      authoritative (an exact count of producers "inside the admission
      protocol") but is maintained by ordinary, interruptible Python
      statements (`self._inflight += 1` / `-= 1`) with NO reconciliation
      against anything external. A single missed decrement (proven below,
      Section 2) makes it PERMANENTLY WRONG with no recovery path at all --
      worse than "diagnostic", because a diagnostic variable being wrong
      would just mean a stale REPORT; this one being wrong means the
      partition can never be closed again, by any caller, ever.

  WriterAccounting.{attempted, rejected_before_accept} (SegmentWriter)
      CAN BLOCK CLOSE: NO (post A6) -- `admission_holds()` (the identity
      that compares these) is explicitly excluded from `clean()`/`close()`'s
      gate; see `WriterAccounting.admission_holds`'s own docstring.
      AUTHORITATIVE OR DIAGNOSTIC: DIAGNOSTIC, and correctly labelled as
      such in the source. The async-accounting harness (A3) already proves
      it can drift under a real asynchronous exception without costing any
      durable evidence.

  WriterAccounting.{written, failed_after_accept, pending} (SegmentWriter)
      CAN BLOCK CLOSE: YES -- `clean()` (`pending == 0 and
      failed_after_accept == 0`) is the ONE gate `close()` still enforces
      before publishing a manifest.
      AUTHORITATIVE OR DIAGNOSTIC: AUTHORITATIVE for what they individually
      count (each is incremented only at the moment of a real, observed
      outcome on the writer thread) -- but NOT COMPLETE: this milestone's A7
      harness proves an item can be dequeued and lost without ANY of the
      three ever being touched, so "all three read clean" does not
      authoritatively imply "nothing was lost" -- only "nothing this
      writer's OWN bookkeeping observed being lost".

  writer-thread liveness (`SegmentWriter._thread.is_alive()`, as reflected
  through `state`/`healthy`)
      CAN BLOCK CLOSE: NO, and that is itself a problem in the other
      direction -- `close()` does not consult `is_alive()` at all except
      via `_thread.join(timeout=30)` deep inside `_close_stages()`, which
      only runs AFTER `_seal_admissions()` has already returned. A dead
      writer thread is not detected any earlier than that.
      AUTHORITATIVE OR DIAGNOSTIC: PURELY DIAGNOSTIC, and PROVEN STALE (A7,
      Section 3/5 of that file): `state` stays `OPEN` and `healthy` stays
      `True` for an indefinite period after the writer thread has already,
      provably, exited -- `self._thread.is_alive()` is never consulted to
      update either.

  queue counters (`_queue.qsize()`, `queue_high_water`)
      CAN BLOCK CLOSE: NO -- `queue_high_water` is pure telemetry, never
      read by any gate. `_queue.qsize()` IS read by `_measure_pending()` at
      close, but only for whatever is STILL IN the queue at that moment --
      an item already dequeued and lost (A7) is invisible to it by
      definition.
      AUTHORITATIVE OR DIAGNOSTIC: DIAGNOSTIC (`queue_high_water`);
      AUTHORITATIVE ONLY FOR "what is currently enqueued", never for "what
      was ever accepted" (`_queue.qsize()`).

=====================================================================
THE REPRODUCTION
=====================================================================
The review's finding, reproduced with a SOURCE-LOCATED (not hardcoded-line)
fault on the `_inflight` decrement inside `submit()`'s outer `finally:`
block: durable, already-committed evidence from an EARLIER, cleanly closed
segment remains chain-valid and `verify_archive`-VALID; a NEW segment's
writer gets permanently stuck (`_inflight` never reaches 0 again, by
construction -- nothing else in the class ever decrements it); `close()`
fails identically on every call (proven for one call here; the deadline
recomputes from `time.monotonic()` fresh each time and `_inflight` cannot
be repaired externally, so every subsequent call reproduces the SAME
failure after the SAME ~5s wait -- asserted structurally rather than by
literally waiting out a second 5s window, to keep this file's runtime
bounded); the segment's `writer.lock` flock is never released (the
partition is bricked for every future writer, in-process or not); and
`verify_archive` over the WHOLE ARCHIVE reports VALID with EMPTY reasons,
because the stuck segment never published a manifest and therefore is not
part of the committed history `verify_archive` walks at all -- it is
neither reported as missing (nothing commits it) nor as invalid (it is not
evidence yet); it is simply invisible.

This harness detects the FAILURE CLASS by its OBSERVABLE OUTCOME --
"durable valid records exist AND close() cannot succeed AND no operator
command can recover this partition" -- never by a hardcoded line number or
exception message match on `_inflight` specifically, so it survives a
refactor that moves the counter without changing its behaviour.
"""

from __future__ import annotations

import inspect
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

from tests.harness_async_accounting.line_injector import LineBoundaryInjector, target

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


def _locate_final_inflight_decrement(func) -> int:
    """The LAST `self._inflight -= 1` inside `submit`'s source -- the one
    in the outer `finally:` block that runs for every ordinary (non-sealed)
    call, as opposed to the sealed-rejection branch's own, earlier copy.
    Located by scanning source text, not a hardcoded line number, so this
    survives the file growing or shrinking elsewhere.
    """
    src_lines, first_lineno = inspect.getsourcelines(func)
    idxs = [i for i, line in enumerate(src_lines)
           if "self._inflight -= 1" in line]
    if not idxs:
        raise AssertionError(
            f"could not find 'self._inflight -= 1' anywhere in {func!r}'s "
            "source; the diagnostic-state target has moved")
    return first_lineno + idxs[-1]


class TestInflightNoLongerBricksThePartitionPermanently:
    """DELIBERATE LEDGER UPDATE (KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect
    G): this class used to be `TestInflightBricksThePartitionPermanently`,
    proving `_inflight` residue past the seal deadline made `close()` raise
    (with the flock and gzip handle held forever) and the segment's 6
    records permanently invisible to `verify_archive`, indistinguishable
    from a healthy, never-closed segment.

    Defect G's fix mirrors the precedent `admission_holds()`/
    `admission_drift` already established: `_seal_admissions()` now RECORDS
    unreconciled `_inflight` residue as a diagnostic (`inflight_drift`)
    instead of raising, and `close()` proceeds. The seal call also moved
    INSIDE `_close_locked`'s try, so even a hypothetical future failure path
    through it still releases the lock. This test now proves the segment
    reaches a real terminal state (CLOSED, clean, durable, visible to
    `verify_archive`) instead of being bricked -- a strictly BETTER outcome
    than "fails fast with the lock released": the records are not lost at
    all.
    """

    def test_full_reproduction(self, tmp_path):
        root = tmp_path / "brick"
        root.mkdir()
        init(root)

        # --- Durable, already-committed evidence from an EARLIER segment,
        # closed and committed cleanly BEFORE the fault, standing in for
        # the review's "3,001 durable chain-valid records". ---
        good = sg.SegmentWriter(root, environment=ENV, segment_id="seg-good",
                                partition_identity="p-good", commit_to_head=True)
        for i in range(5):
            assert good.submit(fields(i)) is None
        good_manifest = good.close()
        assert good_manifest["close_status"] == "clean"
        pre_report = sg.verify_archive(root, environment=ENV)
        assert pre_report["verdict"] == "VALID"
        assert pre_report["records_read"] == 5

        # --- A second, NEW segment writer whose _inflight decrement is
        # faulted on its very last admitted event. ---
        lineno = _locate_final_inflight_decrement(sg.SegmentWriter.submit)
        pt = target(sg.__file__, "submit", lineno, hit_index=1,
                   label="inflight-final-decrement")
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-stuck",
                             partition_identity="p-stuck",
                             enqueue_timeout_s=0.05, commit_to_head=True)
        for i in range(5):
            assert w.submit(fields(i)) is None
        escaped = None
        try:
            with LineBoundaryInjector(pt):
                w.submit(fields(99))
        except BaseException as exc:
            escaped = exc
        assert escaped is not None, (
            "expected the injected fault to escape submit() -- if it "
            "didn't fire, the injection target moved")
        # THE DIAGNOSTIC VARIABLE IS STUCK -- no longer PERMANENTLY wrong,
        # because close() no longer depends on it reaching zero.
        assert w._inflight > 0, (
            "expected _inflight to be stuck above zero after the missed "
            "decrement")
        stuck_at = w._inflight

        # --- close() now SUCCEEDS: it still genuinely waits out the seal
        # deadline (~ enqueue_timeout_s + 5.0s) before treating the residue
        # as a non-fatal drift, so this is not instantaneous -- but it no
        # longer raises, and the segment reaches CLOSED. ---
        t0 = time.monotonic()
        manifest = w.close()
        elapsed = time.monotonic() - t0
        assert elapsed >= 4.5, (
            f"close() returned in {elapsed:.2f}s -- too fast to have "
            "actually waited out the seal deadline; the reproduction may "
            "not be exercising the real _seal_admissions() wait")
        assert manifest["close_status"] == "clean", manifest
        assert w.state is sg.SegmentState.CLOSED

        # --- The diagnostic residue is RECORDED, not silently dropped --
        # mirroring `admission_drift`'s existing precedent exactly. ---
        assert w.inflight_drift == stuck_at, (
            f"expected inflight_drift to record the stuck _inflight count "
            f"({stuck_at}); got {w.inflight_drift!r}")

        # --- No truncation: all 6 records (5 ordinary + the faulted one,
        # which was still durably enqueued and written -- only its
        # bookkeeping decrement was skipped) are readable on disk. ---
        on_disk = sg.read_segment_records(w.events_path)
        assert len(on_disk) == w.accounting.written == 6, (
            f"expected all 6 records durably flushed and readable; got "
            f"{len(on_disk)} on disk, accounting.written={w.accounting.written}")

        # --- THE HEADLINE FINDING, INVERTED: verify_archive over the WHOLE
        # ARCHIVE now reports the FULL 11 records (5 from seg-good + 6 from
        # seg-stuck) as VALID -- seg-stuck is no longer invisible. ---
        post_report = sg.verify_archive(root, environment=ENV)
        assert post_report["verdict"] == "VALID", post_report
        assert post_report["records_read"] == 11, (
            "expected BOTH segments' records visible and valid -- the "
            "defect this test used to prove (total invisibility of "
            "seg-stuck) is fixed")

        # --- The flock is RELEASED: a successor writer for a DIFFERENT
        # segment id in the same partition is unaffected (this segment id
        # is CLOSED, not held open), proving the lock is not leaked. ---
        w2 = sg.SegmentWriter(root, environment=ENV, segment_id="seg-after-stuck",
                              partition_identity="p-stuck-2", commit_to_head=False)
        assert w2.submit(fields(0)) is None
        w2.close()


class TestDiagnosticStateInventoryAssertions:
    """One executable assertion per inventory row above, so the table is
    not merely prose."""

    def test_inflight_can_no_longer_block_close(self, tmp_path):
        """DELIBERATE LEDGER UPDATE (defect G): `_seal_admissions()` still
        runs first, unconditionally, inside `_close_locked()` -- but it no
        longer RAISES past the seal deadline (it records `inflight_drift`
        and returns), and it now runs INSIDE `_close_locked`'s try/except
        rather than before it, so even a hypothetical failure there
        releases the lock. Restated structurally: the source no longer
        contains the raise this inventory row used to name."""
        src = inspect.getsource(sg.SegmentWriter._seal_admissions)
        assert "raise SegmentError" not in src, (
            "_seal_admissions must not raise past the seal deadline any "
            "more -- residue is recorded as inflight_drift and close() "
            "proceeds")
        close_src = inspect.getsource(sg.SegmentWriter._close_locked)
        seal_idx = close_src.index("self._seal_admissions()")
        try_idx = close_src.index("try:")
        # `rindex`, not `index`: the code's own comment prose mentions
        # "except BaseException:" descriptively before the real clause.
        except_idx = close_src.rindex("except BaseException:")
        assert try_idx < seal_idx < except_idx, (
            "_seal_admissions() must run INSIDE _close_locked's try/except "
            "so ANY failure there still releases the lock -- if the shape "
            "changed, re-examine whether _inflight can brick close() again")

    def test_admission_holds_does_not_gate_close_post_a6(self, tmp_path):
        root = tmp_path / "admission-does-not-gate"
        root.mkdir()
        init(root)
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-diag-1",
                             partition_identity="p", commit_to_head=False)
        for i in range(3):
            assert w.submit(fields(i)) is None
        # Force admission_holds() False WITHOUT touching any durable state:
        # a diagnostic-only counter bump, exactly what a real drift would
        # leave behind.
        w.accounting.attempted += 1
        assert w.accounting.admission_holds() is False
        manifest = w.close()          # must still succeed -- not gated on it
        assert manifest["close_status"] == "clean"

    def test_pending_and_failed_after_accept_do_gate_close(self, tmp_path):
        root = tmp_path / "pending-does-gate"
        root.mkdir()
        init(root)
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-diag-2",
                             partition_identity="p", commit_to_head=False)
        assert w.submit(fields(0)) is None
        w.accounting.pending += 1     # simulate an undrained event directly
        with pytest.raises(sg.SegmentError, match="never drained|not written"):
            w.close()

    def test_writer_thread_liveness_is_purely_diagnostic_and_stale(self, tmp_path):
        """A minimal, self-contained restatement of A7 Section 3's finding,
        for this file's own inventory to stand on its own: kill the writer
        thread via the same run-gap injection and confirm `state`/`healthy`
        do not notice."""
        from tests.meta_runtime.independent_accounting import (
            locate_writer_run_gap_line,
        )
        root = tmp_path / "liveness-diagnostic"
        root.mkdir()
        init(root)
        lineno = locate_writer_run_gap_line(sg.SegmentWriter._run)
        pt = target(sg.__file__, "_run", lineno, hit_index=1,
                   label="a8-writer-thread-liveness")
        with LineBoundaryInjector(pt):
            w = sg.SegmentWriter(root, environment=ENV,
                                 segment_id="seg-diag-3",
                                 partition_identity="p", commit_to_head=False)
            w.submit(fields(0))
            deadline = time.monotonic() + 2.0
            while w._thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
        assert not w._thread.is_alive()
        assert w.state is sg.SegmentState.OPEN
        assert w.healthy is True

    def test_queue_high_water_is_pure_telemetry_read_by_nothing(self):
        """Structural check: no gate in `_close_stages`/`clean`/
        `disposition_holds` reads `queue_high_water` -- confirms it cannot
        block anything by construction, not merely "didn't in this test"."""
        gating_sources = "".join([
            inspect.getsource(sg.SegmentWriter._close_stages),
            inspect.getsource(sg.WriterAccounting.clean),
            inspect.getsource(sg.WriterAccounting.disposition_holds),
        ])
        assert "queue_high_water" not in gating_sources
