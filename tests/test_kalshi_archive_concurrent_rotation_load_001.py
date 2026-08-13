"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 -- the DEPLOYMENT GATE's small,
deterministic pin.

The realistic, non-deterministic load test lives at
`tests/benchmarks/concurrent_rotation_load.py` and is run manually / as a
standing gate check (`python tests/benchmarks/concurrent_rotation_load.py`),
NOT as part of this collected suite -- its whole point is to measure a real
distribution on real threads under real host load, which is not a thing a
CI assertion should pin to a fixed number (see that module's own
DETERMINISM VS REALISM section).

What is collected here is the DETERMINISTIC half of the same design: the
same harness functions, imported and driven with small, FORCED (barrier- or
event-gated, never sleep-raced) parameters, so the four properties the
brief requires are pinned as fast, reliable regression tests rather than
left to chance on whichever machine happens to run them:

  1. no commit is lost under concurrent multi-partition rotation
  2. more than one segment CAN be awaiting head commit at once across
     concurrently rotating partitions (the widened crash window is a real,
     reachable state, not a hypothetical)
  3. `wait_for_rotations()` drains under concurrency, and `close()`'s inline
     fallback still catches a rotation the closer never got to run
  4. producer-visible append() latency stays bounded even while several
     partitions are rotating with slow closes AT THE SAME TIME

WHAT THIS FILE DOES NOT COVER -- see
`tests/benchmarks/concurrent_rotation_load.py`'s module docstring for the
full list (no live venue feed anywhere in this repository, no real crash,
no multi-process contention, no host-load sweep). The one item specific to
THIS file: the `_RotationCloser.submit()`/`shutdown()` race is exercised
here only as a bounded, informational empirical probe (few trials, timeout-
bounded) that asserts the SAFETY invariant (nothing lost) and does not
assert a reachability rate -- a rate assertion here would be either flaky
(if it demands a hit) or vacuous (if it doesn't), and the honest, larger-
sample measurement of that rate is the benchmark script's job, not this
gate's.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
BENCH_DIR = REPO_ROOT / "benchmarks"
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import concurrent_rotation_load as load          # noqa: E402

from app.realtime import archive as ar            # noqa: E402
from app.realtime import archive_head as ah        # noqa: E402
from app.realtime import segment as sg             # noqa: E402

ENV = load.ENV


@pytest.fixture
def scratch_root(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    return root


# =====================================================================
# 1 & 2 -- no lost commit, and MORE THAN ONE segment genuinely awaiting
# head commit at once, across MULTIPLE concurrently rotating partitions.
# Forced with an Event gate on `close()`, not a sleep race, so the count is
# exact and the test is not flaky.
# =====================================================================

class TestNoLostCommitAndTheWidenedCrashWindowIsReal:

    def test_multiple_partitions_can_have_multiple_segments_each_awaiting_head_commit(
            self, scratch_root, monkeypatch):
        """3 partitions (simulated as 3 `EventArchive` venues sharing one
        root -- see the benchmark module's docstring for why this is the
        modelled extrapolation), each forced through 2 rotations while every
        `close()` blocks on a shared gate. Aggregate `outstanding` across
        the three closers must reach AT LEAST 6 while the gate holds --
        i.e. up to `outstanding` segments per partition, summed across
        partitions, exactly as `_RotationCloser`'s docstring describes,
        and MORE than the synchronous predecessor (which bounded a single
        instance to one outstanding close) ever allowed."""
        n_partitions = 3
        rotations_per_partition = 2
        max_records = 2   # every 2 records is a rotation

        ah.initialize_archive(scratch_root, ENV,
                              archive_identity="kalshi-realtime")
        stores = [
            ar.EventArchive(scratch_root, environment=ENV,
                            venue=f"gatev{n}", max_segment_records=max_records)
            for n in range(n_partitions)
        ]

        gate = threading.Event()
        real_close = sg.SegmentWriter.close

        def gated_close(self):
            gate.wait(timeout=30)
            return real_close(self)

        monkeypatch.setattr(sg.SegmentWriter, "close", gated_close)

        # Every rotation, once triggered, hands its retiring writer to the
        # ONE closer thread per store, which immediately blocks in
        # gated_close(). Submitting `rotations_per_partition + 1` segments'
        # worth of records per store guarantees exactly
        # `rotations_per_partition` retiring writers per store are
        # in-flight (the +1'th is the still-open live segment, never
        # retiring).
        records_per_store = max_records * (rotations_per_partition + 1) - 1
        for store in stores:
            for i in range(records_per_store):
                store.append(load.envelope_for(i))

        deadline = time.monotonic() + 10.0
        expected = n_partitions * rotations_per_partition
        while (time.monotonic() < deadline
               and sum(s._closer.outstanding for s in stores) < expected):
            time.sleep(0.01)
        observed = sum(s._closer.outstanding for s in stores)
        assert observed >= expected, (
            f"only {observed} segments were awaiting head commit across "
            f"{n_partitions} concurrently rotating partitions; expected at "
            f"least {expected} -- rotation across partitions is not "
            "actually running concurrently against the one closer per "
            "instance")

        # ...and this is exactly the widened crash window: MORE than one
        # segment (indeed more than one PER partition) can be sitting
        # unclosed at the same instant. The synchronous predecessor bounded
        # this to 1 total; here it is legitimately >= 6.
        assert observed > 1

        gate.set()   # release every blocked close at once
        for store in stores:
            assert store.wait_for_rotations(30.0), store.rotation_failures
            assert not store.rotation_failures, store.rotation_failures
            store.close()

        monkeypatch.undo()
        report = sg.verify_archive(scratch_root, environment=ENV)
        assert report["verdict"] == "VALID", report.get("reasons")
        assert (report["records_read"]
               == n_partitions * records_per_store), (
            "not every acknowledged record across the concurrently "
            "rotating partitions survived to be read back -- a commit was "
            "lost under concurrent rotation")

    def test_a_burst_across_many_partitions_loses_nothing(
            self, scratch_root):
        """The REALISTIC harness function itself, run at small-but-real
        sizes with an injected (not gated) close delay, so this also
        exercises the natural thread-scheduling path rather than only the
        controlled one above. Kept small so it stays fast and this remains
        a standing gate, not a soak."""
        result = load.run_multi_instance_load(
            root=scratch_root, n_instances=5, records_per=60,
            max_segment_records=10, inject_close_delay=True,
            outstanding_sample_hz=400.0)
        assert result["acked_equals_verified"], result
        assert result["verify_verdict"] == "VALID", result["verify_reasons"]
        assert not result["producer_errors"], result["producer_errors"]
        for store_report in result["per_store"]:
            assert not store_report["rotation_failures"], store_report
            assert store_report["drained_before_close"], store_report
        # The widened window really was exercised, not vacuously satisfied
        # by a run so small nothing overlapped.
        assert result["outstanding_max_observed_aggregate"] >= 2, (
            "no overlap was observed at all across 5 concurrently rotating "
            "partitions -- this run is not exercising the property it "
            f"exists to test: {result['outstanding_awaiting_head_commit_aggregate']}")


# =====================================================================
# 3 -- close()'s inline fallback catches what the closer thread missed,
# reproduced across MULTIPLE partitions concurrently (the single-partition
# case is already pinned in test_kalshi_archive_replay_integrity_a8_001.py;
# this is its multi-partition extension).
# =====================================================================

class TestInlineFallbackAcrossConcurrentPartitions:

    def test_closer_already_shut_down_is_caught_inline_on_every_partition(
            self, scratch_root):
        """Shut every partition's closer down FIRST (simulating each one
        racing its own `EventArchive.close()`), then force one more
        rotation on each -- `_writer_for`'s `submit()` call returns False
        for every one of them, and each must fall back to closing inline
        rather than losing the retiring segment."""
        n_partitions = 4
        ah.initialize_archive(scratch_root, ENV,
                              archive_identity="kalshi-realtime")
        stores = [
            ar.EventArchive(scratch_root, environment=ENV,
                            venue=f"fbv{n}", max_segment_records=3)
            for n in range(n_partitions)
        ]
        for store in stores:
            store.append(load.envelope_for(0))
            store._closer.shutdown(5.0)
            assert store._closer.outstanding == 0

        for store in stores:
            for i in range(1, 6):        # forces (at least) one rotation
                store.append(load.envelope_for(i))
            assert not store.rotation_failures, store.rotation_failures
            store.close()

        report = sg.verify_archive(scratch_root, environment=ENV)
        assert report["verdict"] == "VALID", report.get("reasons")
        assert report["records_read"] == n_partitions * 6


# =====================================================================
# 4 -- producer-visible latency stays bounded while SEVERAL partitions are
# rotating with slow closes AT THE SAME TIME. Single-partition version of
# this property already exists
# (test_kalshi_archive_replay_integrity_a8_001.py::
#  TestRotationDoesNotStallTheProducer); this is the concurrent extension
# the brief calls out by name.
# =====================================================================

class TestProducerLatencyUnderConcurrentMultiPartitionRotation:

    def test_append_stays_fast_while_several_partitions_close_slowly_at_once(
            self, scratch_root, monkeypatch):
        n_partitions = 4
        close_delay_s = 1.5
        ah.initialize_archive(scratch_root, ENV,
                              archive_identity="kalshi-realtime")
        stores = [
            ar.EventArchive(scratch_root, environment=ENV,
                            venue=f"latv{n}", max_segment_records=5)
            for n in range(n_partitions)
        ]
        real_close = sg.SegmentWriter.close

        def slow_close(self):
            time.sleep(close_delay_s)
            return real_close(self)

        monkeypatch.setattr(sg.SegmentWriter, "close", slow_close)

        worst_by_store = [0.0] * n_partitions
        errors: list = []

        def producer(idx: int, store: ar.EventArchive):
            try:
                for i in range(12):
                    t0 = time.monotonic()
                    store.append(load.envelope_for(i))
                    worst_by_store[idx] = max(
                        worst_by_store[idx], time.monotonic() - t0)
            except BaseException as exc:      # noqa: BLE001 - reported
                errors.append((idx, repr(exc)))

        threads = [threading.Thread(target=producer, args=(n, stores[n]))
                  for n in range(n_partitions)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        monkeypatch.undo()
        assert not errors, errors
        worst = max(worst_by_store)
        assert worst < close_delay_s / 2, (
            f"the slowest append() across {n_partitions} concurrently "
            f"rotating partitions took {worst:.2f}s against a "
            f"{close_delay_s:.1f}s close -- rotation is stalling a "
            "producer while ANOTHER partition's close (or its own) runs")

        for store in stores:
            assert store.wait_for_rotations(30.0), store.rotation_failures
            assert not store.rotation_failures, store.rotation_failures
            store.close()
        report = sg.verify_archive(scratch_root, environment=ENV)
        assert report["verdict"] == "VALID", report.get("reasons")
        assert report["records_read"] == n_partitions * 12


# =====================================================================
# `run_multi_instance_load` monkeypatches `SegmentWriter.close` at module
# scope for the duration of the run. `run_single_instance_load` restores it
# in a `finally`; this proves its twin does too -- an exception raised in
# the unguarded window (a real `Thread.start()` failure under thread/fd
# pressure, per the reviewer's reproduction) must not leave every
# subsequent `SegmentWriter.close()` in the pytest session sleeping.
# =====================================================================

class TestSegmentWriterCloseIsRestoredEvenWhenTheRunRaises:

    def test_a_mid_run_exception_still_restores_the_monkeypatch(
            self, scratch_root, monkeypatch):
        real_close = sg.SegmentWriter.close
        real_thread_start = threading.Thread.start
        starts = {"n": 0}

        def flaky_start(self):
            # Call 1 is the sampler thread `run_multi_instance_load`
            # starts internally before any producer thread; let it (and
            # nothing else) through, then fail on the first PRODUCER
            # thread's start() -- reproducing an exception raised inside
            # the window between `SegmentWriter.close` being monkeypatched
            # and the (pre-fix: missing) `finally` that restores it.
            starts["n"] += 1
            if starts["n"] == 2:
                raise RuntimeError("can't start new thread")
            return real_thread_start(self)

        monkeypatch.setattr(threading.Thread, "start", flaky_start)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            load.run_multi_instance_load(
                root=scratch_root, n_instances=4, records_per=5,
                max_segment_records=3, inject_close_delay=True)
        # Restore Thread.start before inspecting SegmentWriter.close --
        # otherwise a later real thread this test itself spins up would
        # also be victim to the injected flakiness.
        monkeypatch.undo()

        assert sg.SegmentWriter.close is real_close, (
            f"SegmentWriter.close was left monkeypatched to "
            f"{sg.SegmentWriter.close!r} after run_multi_instance_load "
            "raised mid-run -- every subsequent SegmentWriter.close() in "
            "this pytest session would sleep the injected delay")


# =====================================================================
# Informational: the submit()/shutdown() race under concurrent rotation.
# Safety-only assertion (never assert a reachability rate here -- see the
# module docstring).
# =====================================================================

class TestRaceReachabilityIsSafeEvenWhenNotObserved:

    def test_the_safety_invariant_holds_across_a_bounded_empirical_probe(
            self, tmp_path):
        """A small number of real, timing-raced trials against 1 and 4
        concurrent producers. This does NOT assert the race fires --
        `archive.py::_RotationCloser.submit`'s own docstring describes a
        window narrow enough that hitting it on uninstrumented real threads
        may take far more than a handful of trials, and asserting a hit
        here would make this gate flaky by design. What must ALWAYS hold,
        symptom or not, is durability: nothing acknowledged is ever lost.
        """
        by_count = load.run_race_reachability(
            tmp_parent=tmp_path, n_trials=5, producer_counts=(1, 4))
        for n_producers, result in by_count.items():
            assert not result["any_durability_lost"], (
                f"a commit was lost racing close() against {n_producers} "
                f"concurrent producers: {result}")
