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


class TestInflightBricksThePartitionPermanently:
    """The full reproduction: prior durable evidence untouched, this
    segment permanently unclosable, the partition bricked, and the whole
    archive still reporting VALID with empty reasons."""

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
        # THE DIAGNOSTIC VARIABLE IS NOW PERMANENTLY WRONG.
        assert w._inflight > 0, (
            "expected _inflight to be stuck above zero after the missed "
            "decrement")
        stuck_at = w._inflight

        # --- close() fails, and takes roughly the seal deadline to do so
        # (~ enqueue_timeout_s + 5.0s) -- it is not instantaneous, it
        # genuinely waits, which is part of what makes this an operational
        # brick and not merely a fast-failing error. ---
        t0 = time.monotonic()
        with pytest.raises(sg.SegmentError, match="still inside the admission"):
            w.close()
        elapsed = time.monotonic() - t0
        assert elapsed >= 4.5, (
            f"close() returned in {elapsed:.2f}s -- too fast to have "
            "actually waited out the seal deadline; the reproduction may "
            "not be exercising the real _seal_admissions() wait")

        # --- PERMANENT: nothing decremented _inflight during that wait,
        # and nothing else in SegmentWriter ever decrements it outside the
        # two source locations `_locate_final_inflight_decrement`-adjacent
        # code already executed (both already ran: this call's own, and
        # the sealed-rejection branch, which was never taken). A second
        # close() call would recompute a fresh ~5s deadline and fail
        # identically -- asserted structurally here (no external state
        # changed that could possibly unstick it) rather than by paying a
        # second ~5s wait in every run of this suite. ---
        assert w._inflight == stuck_at, (
            "expected _inflight to remain stuck at the same value after "
            "the failed close() attempt -- if it changed, some new path "
            "decrements it and this reproduction is stale")
        assert w.state is sg.SegmentState.OPEN, (
            "close() bailed out inside _seal_admissions(), BEFORE ever "
            "reaching the code that would set state to CLOSING/INVALID/"
            "CLOSED -- the writer's own diagnostic state still claims "
            "OPEN, indistinguishable from a healthy, never-closed segment")

        # --- The flock is retained: no successor writer, in this process
        # or any other, can ever open this segment id again. ---
        with pytest.raises(sg.SegmentError, match="already has a LIVE writer"):
            sg.SegmentWriter(root, environment=ENV, segment_id="seg-stuck",
                             partition_identity="p-stuck", commit_to_head=True)

        # --- ~185-record-class truncation: whatever was submitted since
        # the last periodic flush is sitting unflushed inside the gzip
        # stream, invisible to any reader, because the gzip handle is
        # never closed (close() bailed out before reaching that stage). A
        # `flush_every` default of 256 with only 6 submissions here means
        # NONE of them have been flushed -- the whole segment's content is
        # invisible on disk right now, standing in for the review's
        # partial-truncation finding at any scale. ---
        on_disk = sg.read_segment_records(w.events_path)
        assert len(on_disk) < w.accounting.written, (
            f"expected the on-disk readable record count ({len(on_disk)}) "
            f"to undercount what the writer believes it wrote "
            f"({w.accounting.written}) -- the gzip handle is still open "
            "and its buffered tail was never flushed")

        # --- No operator command exists to recover this. `recover_current_
        # head` operates on the ARCHIVE HEAD (genesis/generation/pointer
        # triple), not on a live SegmentWriter's in-memory `_inflight` --
        # there is no public API anywhere in this module that resets it. ---
        public_writer_api = {name for name in dir(sg.SegmentWriter)
                             if not name.startswith("_")}
        assert not any("inflight" in name.lower() or "recover" in name.lower()
                      or "reset" in name.lower() for name in public_writer_api), (
            f"expected no public recovery surface on SegmentWriter for "
            f"this condition; found candidates in {sorted(public_writer_api)}"
            " -- if one now exists, this finding may already be fixed; "
            "update this test to exercise and assert the recovery path "
            "instead of asserting its absence")

        # --- THE HEADLINE FINDING: verify_archive over the WHOLE ARCHIVE
        # still reports VALID with EMPTY reasons. The 5 records from
        # `seg-good` are the durable, chain-valid evidence; `seg-stuck`'s
        # manifest was never published, so it is not part of the committed
        # history at all -- neither missing (nothing commits it) nor
        # invalid (it was never evidence to begin with). It is simply
        # absent from the verdict, exactly as the review describes. ---
        post_report = sg.verify_archive(root, environment=ENV)
        assert post_report["verdict"] == "VALID", post_report
        assert post_report["reasons"] == [], post_report
        assert post_report["records_read"] == 5, (
            "the 5 durably committed records from seg-good remain exactly "
            "as valid as before the fault -- the bug is total invisibility "
            "of seg-stuck, not corruption of seg-good")

        # Detected BY OBSERVABLE OUTCOME, independent of _inflight's exact
        # line or even its name: durable valid records exist, close()
        # cannot succeed, and no operator command exists to recover.
        durable_valid_records_exist = post_report["records_read"] > 0
        close_cannot_succeed = True          # proven by the raises block above
        no_recovery_command_exists = True    # proven by the public-API sweep above
        assert (durable_valid_records_exist and close_cannot_succeed
               and no_recovery_command_exists), (
            "this is the exact failure CLASS this test exists to detect")


class TestDiagnosticStateInventoryAssertions:
    """One executable assertion per inventory row above, so the table is
    not merely prose."""

    def test_inflight_can_block_close(self, tmp_path):
        """Restated minimally: `_seal_admissions()` runs FIRST inside
        `close()`, unconditionally, and can raise before anything else in
        `close()` ever executes."""
        src = inspect.getsource(sg.SegmentWriter.close)
        seal_idx = src.index("self._seal_admissions()")
        state_check_idx = src.index("SegmentState.INVALID")
        assert seal_idx < state_check_idx, (
            "_seal_admissions() must run before the INVALID-state branch "
            "for this inventory row's claim to hold -- if the order "
            "changed, re-examine whether _inflight can still block close()")

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
