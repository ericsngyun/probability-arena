"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1/A7 -- admission/close race, RETIRED.

This file used to reproduce the queue-ownership admission/close race:
`SegmentWriter.submit` -> `_admit` ran on a producer thread, queued the
event, and returned; `close()` -> `_seal_admissions` waited a fixed
`enqueue_timeout_s + 5.0` deadline for `_inflight` to reach zero, gave up,
and let `close()` proceed while a producer could still be genuinely
executing -- so a producer could be told ACCEPTED for an event queued after
`close()` had already published the manifest `close_status: "clean"`.

THE ARCHITECTURE THAT MADE THAT RACE POSSIBLE IS GONE. There is no queue, no
background writer thread, and no `_admit`/`_seal_admissions`/`_inflight`
protocol left to race: `SegmentWriter.submit` now canonicalises, chains and
writes ONE record entirely under `self._lock`, and `close()` takes the SAME
lock to move `self.state` off `OPEN` before it does anything else. A
`submit()` still running when `close()` is called has not released the lock
yet; `close()` cannot proceed past sealing until it does. There is no
interval, of any duration, in which a producer is "genuinely still
executing" a call that could still turn into a late ACCEPTED.

MAPPING (old property -> why gone -> replacement property -> replacement test):

  old:  `_seal_admissions` waits a fixed deadline for `_inflight == 0`, then
        gives up and calls the residue "a leaked counter, not one still
        genuinely executing".
  why:  there is no `_inflight` counter and no deadline to wait out -- a
        `submit()` still running has not released `self._lock`, so `close()`
        (which must acquire the same lock to seal) cannot observe the
        segment as sealed until every `submit()` that started before the
        seal has ALREADY reached a terminal outcome.
  now:  `close()` and `submit()` are strictly mutually exclusive on
        `self._lock` -- there is no "in between" state for either to
        observe the other in.
  test: `TestSubmitAndCloseAreMutuallyExclusive.
        test_close_never_overlaps_a_running_submit` below, using real
        threads and `pre_write_hook` (a legitimate test seam `submit()`
        already calls under the lock) to prove the two never interleave.

  old:  a producer held inside `_admit` when the seal deadline passed was
        REJECTED via a second checkpoint (`_sealed`) that raced the first.
  why:  there is only ONE checkpoint now (`self.state` read under
        `self._lock`, at the very top of `submit()`), and it cannot be
        raced: by the time `submit()` can even acquire the lock, `close()`
        has either not yet started (this `submit()` proceeds and completes
        BEFORE `close()` can begin sealing) or has already finished sealing
        (this `submit()` observes `state is not OPEN` and is rejected). Both
        outcomes are correct and there is no third one.
  now:  no producer is ever told ACCEPTED for an event that arrives after a
        clean close has been published, and no producer is ever silently
        dropped either -- every `submit()` call, no matter when it starts
        relative to a concurrent `close()`, returns either `None` (written,
        durably, before `close()` could have sealed) or a typed rejection.
  test: `TestSubmitAndCloseAreMutuallyExclusive.
        test_no_late_acceptance_after_a_clean_close` below.

`inflight_drift`, `late_admission_rejected`, `_inflight`, `_sealed`,
`_seal_admissions`, and the `_admit`/`ControlledAdmission`/
`SlowIterationMapping`/`BusyThreadPool` test harness this file used to import
from `tests/meta_runtime/admission_close_race.py` are RETIRED along with the
protocol they existed to instrument -- there is no replacement instance-level
seam to wrap, because there is no asynchronous admission step left to hold a
thread inside.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

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
    kw.setdefault("commit_to_head", False)
    return sg.SegmentWriter(root, environment=ENV, segment_id=seg_id,
                            partition_identity="p", **kw), root


class TestSubmitAndCloseAreMutuallyExclusive:
    def test_close_never_overlaps_a_running_submit(self, tmp_path):
        """`close()` must never observe (or act on) a segment while a
        `submit()` that started earlier is still writing to it. Proven with
        a REAL producer thread held, by `pre_write_hook`, at the exact point
        inside `submit()`'s critical section where the old writer thread's
        write used to happen -- `self._lock` is held throughout, so `close()`
        genuinely cannot proceed past its own lock acquisition until this
        producer's `submit()` call returns.
        """
        w, root = make_writer(tmp_path, "seg-mutex")
        entered = threading.Event()
        release = threading.Event()

        def hook(writer):
            entered.set()
            release.wait(timeout=5.0)

        w.pre_write_hook = hook
        result_holder: dict = {}

        def producer():
            result_holder["reject_reason"] = w.submit(fields(0))

        t = threading.Thread(target=producer, name="held-producer")
        t.start()
        assert entered.wait(timeout=2.0), (
            "producer never reached pre_write_hook -- submit()'s critical "
            "section shape changed")

        close_result: dict = {}

        def closer():
            close_result["manifest"] = w.close()

        close_thread = threading.Thread(target=closer, name="closer")
        close_thread.start()
        # `close()` must be BLOCKED on `self._lock` right now -- the producer
        # is still inside its critical section, holding it.
        time.sleep(0.2)
        assert close_thread.is_alive(), (
            "close() returned while a submit() that started earlier was "
            "still running -- self._lock is no longer serialising them")
        assert "reject_reason" not in result_holder

        release.set()
        t.join(timeout=5.0)
        close_thread.join(timeout=5.0)
        assert not close_thread.is_alive()

        # The held producer's event was durably written BEFORE close() could
        # seal -- it is not lost, and it is not "pending": it is part of the
        # committed segment.
        assert result_holder["reject_reason"] is None
        manifest = close_result["manifest"]
        assert manifest["close_status"] == "clean"
        assert manifest["record_count"] == 1
        verdict = sg.verify_segment(w.dir, environment=ENV, root=root)
        assert verdict.valid, verdict.reasons
        assert verdict.records_read == 1

    def test_no_late_acceptance_after_a_clean_close(self, tmp_path):
        """THE PROPERTY the retired race used to violate: a clean close must
        never be followed by a late `None` (ACCEPTED) for a `submit()` that
        started after the close. Every `submit()` that starts once `close()`
        has returned observes `state is CLOSED` and is rejected outright --
        there is no window left for it to observe anything else.
        """
        w, root = make_writer(tmp_path, "seg-no-late-accept")
        manifest = w.close()
        assert manifest["close_status"] == "clean"

        reason = w.submit(fields(0))
        assert reason is not None, (
            "a clean close was published and a submit() that started "
            "afterwards was still told ACCEPTED")
        assert reason == sg.RejectReason.SEGMENT_NOT_OPEN
