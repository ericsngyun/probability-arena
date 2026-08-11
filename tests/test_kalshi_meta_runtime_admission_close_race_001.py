"""KALSHI-ARCHIVE-CORE-REMEDIATION-003B A1 -- the admission/close race.

Reproduces, deterministically, the chain three independent A9 reviews of
63bf0d1 all converged on:

    1. a producer is genuinely still executing inside the admission
       protocol (`SegmentWriter.submit` -> `_admit`) when close() runs;
    2. `_seal_admissions` waits its fixed `enqueue_timeout_s + 5.0`
       deadline, gives up, and records `inflight_drift` -- its own
       docstring calls the residue "a leaked counter... not one still
       genuinely executing", which this file empirically falsifies for
       every mechanism below;
    3. `close()` proceeds: the writer thread (which was already idle,
       waiting only on `shutdown.is_set() and queue.empty()`) joins almost
       instantly, the manifest is published `close_status: "clean"`, and
       the segment is marked CLOSED;
    4. the still-executing producer's `_admit` call THEN resumes,
       `self._queue.put_nowait(...)` succeeds into a queue with no
       consumer, and `submit()` returns `None` (ACCEPTED) to its caller;
    5. the record never reaches disk. `verify_segment` still reports VALID
       (it verifies exactly what WAS published, correctly) -- the defect is
       that a producer was told ACCEPTED after that verdict was already
       final.

DOES NOT MODIFY `app/realtime/segment.py`. Every mechanism below is
test-side instrumentation only (see `tests/meta_runtime/
admission_close_race.py` for why instance-level `_admit` wrapping is the
same seam class as production's own `pre_write_hook`/`durability_hooks`).

Every test in this file genuinely waits out the real `enqueue_timeout_s +
5.0` seal deadline -- there is no way to shorten the hard-coded `+ 5.0`
floor without editing `segment.py`, which is out of scope. `enqueue_timeout_s`
is set small (0.2s) so the deadline is ~5.2s rather than the ~6s default,
keeping the file's total runtime bounded without touching production.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

from tests.meta_runtime.admission_close_race import (
    BusyThreadPool,
    ControlledAdmission,
    SlowIterationMapping,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
ENV = "demo"


def fields(i=0, **extra):
    base = {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": "KXA", "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {"price_dollars": "0.5100", "side": "no"},
        "normalized_event": {"raw_price_units": 5100},
    }
    base.update(extra)
    return base


def make_writer(tmp_path, seg_id, **kw):
    root = tmp_path / seg_id
    root.mkdir()
    ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
    kw.setdefault("enqueue_timeout_s", 0.2)
    kw.setdefault("commit_to_head", False)
    return sg.SegmentWriter(root, environment=ENV, segment_id=seg_id,
                            partition_identity="p", **kw), root


# =====================================================================
# 1. CONTROLLED PAUSE -- a producer thread held, by an explicit test-owned
#    Event, genuinely inside `_admit` (real `_inflight` accounting, real
#    admission-protocol lock acquisition -- only the resumption point is
#    test-controlled).
# =====================================================================

class TestControlledPauseAcceptsAfterCloseIsClean:
    def test_seal_gives_up_while_the_producer_is_provably_still_alive(
            self, tmp_path):
        w, root = make_writer(tmp_path, "seg-controlled-pause")
        ctl = ControlledAdmission(sg)
        result_holder: dict = {}

        def producer():
            result_holder["reject_reason"] = w.submit(fields(0))

        t = threading.Thread(target=producer, name="held-producer")
        t.start()
        assert ctl.wait_until_entered(timeout_s=2.0), (
            "producer never reached the patched _admit -- instrumentation "
            "did not attach where expected")

        # The producer is now durably inside the admission protocol:
        # `_inflight` was incremented by REAL, unmodified `submit()` code
        # before this wrapper ever ran.
        assert w._inflight == 1

        close_result: dict = {}

        def closer():
            close_result["manifest"] = w.close()

        close_thread = threading.Thread(target=closer, name="closer")
        t0 = time.monotonic()
        close_thread.start()
        close_thread.join(timeout=15.0)
        close_elapsed = time.monotonic() - t0
        assert not close_thread.is_alive(), (
            "close() did not return within 15s -- the seal deadline should "
            "be ~5.2s (enqueue_timeout_s=0.2 + 5.0)")

        # THE DISTINGUISHING ASSERTION: at the exact moment close() gave up
        # and published, the producer was NOT dead, NOT finished, NOT a
        # leaked counter with no thread behind it -- it is a live Python
        # thread, still parked inside `_admit`, that has not yet reached
        # ANY terminal stage (accepted or rejected).
        assert t.is_alive(), (
            "the producer thread died before close() gave up on the seal "
            "-- this run does not reproduce the race; rerun or check timing")
        assert "reject_reason" not in result_holder, (
            "submit() already returned before close() gave up -- the race "
            "window closed before this test could observe it")
        assert close_elapsed >= 5.0, (
            f"close() returned in {close_elapsed:.2f}s, faster than the "
            "~5.2s seal deadline -- either the deadline shortened or "
            "close() no longer waits for _inflight")

        # `_seal_admissions` recorded exactly the "leaked counter" diagnosis
        # its own docstring uses -- while the producer is proven alive.
        assert w.inflight_drift == 1, (
            f"expected inflight_drift == 1 (the one producer this test "
            f"held), got {w.inflight_drift!r}")

        manifest = close_result["manifest"]
        assert manifest["close_status"] == "clean"
        assert manifest["record_count"] == 0
        assert w.state is sg.SegmentState.CLOSED

        # `verify_segment` certifies exactly what was published -- correctly,
        # for what it can see. The defect is not in this verdict; it is that
        # a producer is about to be told ACCEPTED after this verdict is final.
        verdict = sg.verify_segment(w.dir, environment=ENV, root=root)
        assert verdict.valid, verdict.reasons
        assert verdict.records_read == 0

        # NOW let the producer resume -- genuinely still executing, exactly
        # as proven above, calling straight into a writer that is CLOSED.
        ctl.release()
        t.join(timeout=5.0)
        assert not t.is_alive(), "producer did not finish after release"

        # THE VIOLATION: submit() returns None -- ACCEPTED -- to a producer
        # calling into a writer whose manifest was already published clean,
        # over zero records, and whose writer thread is provably dead.
        assert result_holder["reject_reason"] is None, (
            f"expected the producer to receive ACCEPTED (None) after the "
            f"writer had already closed clean -- got "
            f"{result_holder['reject_reason']!r}. If this is no longer "
            "None, either the race no longer reproduces (fixed) or the "
            "shape of the failure moved and this assertion needs revisiting")
        assert not w._thread.is_alive(), (
            "the writer thread should already be dead (joined during "
            "close()) at the moment the late admission completes")
        # The record is durably enqueued -- and nothing will ever consume
        # it. It is not on disk, not in the published manifest, and the
        # producer was never told it was lost.
        assert w._queue.qsize() == 1, (
            "the late-accepted record should be sitting, unconsumed, in "
            "the writer's queue -- this is the leaked evidence")

    @pytest.mark.xfail(
        strict=True,
        reason="KALSHI-ARCHIVE-CORE-REMEDIATION-003B A1: no producer may "
        "receive ACCEPTED after the writer has permanently stopped "
        "accepting durable evidence, and a close that cannot prove "
        "admissions finished must not publish 'clean'. 63bf0d1 violates "
        "both halves of this property (see the reproduction above) -- this "
        "is the same scenario restated as the DESIRED, currently-failing "
        "property so the repair has an exact target to turn green.")
    def test_desired_property_no_late_acceptance_after_a_clean_close(
            self, tmp_path):
        w, root = make_writer(tmp_path, "seg-desired-property")
        ctl = ControlledAdmission(sg)
        result_holder: dict = {}

        def producer():
            result_holder["reject_reason"] = w.submit(fields(0))

        t = threading.Thread(target=producer, name="held-producer")
        t.start()
        assert ctl.wait_until_entered(timeout_s=2.0)
        manifest = w.close()
        ctl.release()
        t.join(timeout=5.0)

        if manifest["close_status"] == "clean":
            # THE PROPERTY: a clean close must never be followed by a late
            # ACCEPTED for a producer that was still inside admission when
            # the seal deadline passed.
            assert result_holder["reject_reason"] is not None, (
                "a clean close was published while a producer that was "
                "still inside admission at the seal deadline later "
                "received ACCEPTED -- exactly the violation this property "
                "forbids")


# =====================================================================
# 2. A GENUINELY SLOW, ORDINARY PAYLOAD -- no wrapper, no artificial sleep
#    around `_admit`; the delay is intrinsic to iterating a real (if
#    unusual) Mapping value, reached through completely unmodified
#    `submit()` -> `_admit` -> `canonicalize_or_reason` ->
#    `segment._structural_reason` code.
# =====================================================================

class TestGenuinelySlowPayloadAlsoRaces:
    def test_a_legal_but_slow_iterating_payload_still_races_the_seal(
            self, tmp_path):
        w, root = make_writer(tmp_path, "seg-slow-payload")
        # 60 keys * 0.1s = 6s of GENUINE admission-walk time -- longer than
        # the ~5.2s seal deadline, and every key/value pair is individually
        # canonical (str key, int value); nothing here is refused on its
        # own merits.
        slow_meta = SlowIterationMapping(n_keys=60, per_key_delay_s=0.1)
        envelope = fields(0, normalized_event=slow_meta)
        # `canonicalize_or_reason` walks a Mapping's `.items()` TWICE for
        # every admitted value -- once structurally (`_structural_reason`)
        # and once again inside the real encode (`canonical_bytes` ->
        # `_encode`, only reached once the structural walk has already
        # approved the value) -- so the genuine admission-walk time is
        # roughly DOUBLE one pass over `slow_meta`.
        assert slow_meta.minimum_duration_s * 2 >= 5.5

        result_holder: dict = {}
        entered = threading.Event()

        def producer():
            entered.set()
            result_holder["reject_reason"] = w.submit(envelope)

        t = threading.Thread(target=producer, name="slow-producer")
        t.start()
        assert entered.wait(timeout=2.0)
        # Give the walk a brief head start so `_inflight` is durably 1
        # before close() runs its own first check.
        time.sleep(0.05)

        t0 = time.monotonic()
        manifest = w.close()
        close_elapsed = time.monotonic() - t0

        # The producer is STILL running its real, unmodified structural
        # walk -- this is what makes it a true positive, not an artefact of
        # test instrumentation.
        assert t.is_alive(), (
            "the slow producer finished before close() returned -- the "
            "per-key delay was not long enough to outlast the seal "
            "deadline on this run; rerun or increase per_key_delay_s")
        assert close_elapsed >= 5.0
        assert w.inflight_drift == 1
        assert manifest["close_status"] == "clean"
        assert manifest["record_count"] == 0

        # Generous: this host's scheduler/timer resolution has been observed
        # to inflate `time.sleep` durations by ~2x under load, so the
        # nominal ~12s (2 passes * 6s) can genuinely take ~30s wall-clock.
        t.join(timeout=60.0)
        assert not t.is_alive(), (
            "the slow producer's real admission walk did not finish within "
            "60s -- either this host is unusually loaded or the mapping's "
            "own iteration is slower than expected")
        # The producer's own, real structural walk eventually finishes and
        # (because the payload is canonically legal) is accepted --
        # into a queue nobody drains, after a clean close.
        assert result_holder["reject_reason"] is None
        assert w._queue.qsize() == 1


# =====================================================================
# 3. GIL-CONTENTION COMBINATION -- the same controlled-pause reproduction,
#    run concurrently with real CPU-bound scheduling pressure, to show the
#    property is not an artefact of a quiet, single-threaded test process.
# =====================================================================

class TestUnderSchedulerContention:
    def test_race_reproduces_under_real_thread_contention(self, tmp_path):
        w, root = make_writer(tmp_path, "seg-contention")
        ctl = ControlledAdmission(sg)
        result_holder: dict = {}

        def producer():
            result_holder["reject_reason"] = w.submit(fields(0))

        with BusyThreadPool() as pool:
            pool.start(n=4)
            t = threading.Thread(target=producer, name="held-producer-2")
            t.start()
            assert ctl.wait_until_entered(timeout_s=5.0)
            assert w._inflight == 1

            t0 = time.monotonic()
            manifest = w.close()
            close_elapsed = time.monotonic() - t0

            assert t.is_alive(), (
                "the producer finished before close() gave up, even under "
                "contention -- rerun")
            assert close_elapsed >= 5.0
            assert w.inflight_drift == 1
            assert manifest["close_status"] == "clean"

            ctl.release()
            t.join(timeout=5.0)

        assert result_holder["reject_reason"] is None
        assert w._queue.qsize() == 1
