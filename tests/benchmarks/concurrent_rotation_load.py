#!/usr/bin/env python3
"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 -- the concurrent-rotation load test
Eric designated the DEPLOYMENT GATE for the archive core, distinct from the
core's merge gate.

WHY THIS EXISTS, VERBATIM FROM THE MANDATE IT IS ANSWERING TO: "The Solana
run just demonstrated why: synthetic contention can miss the production
interaction completely." 28 synthetic coexistence trials across three
commits on a sibling lane reported zero lock failures and zero retries, and
the first REAL production write pass then recorded 45.7 s blocked against a
30 s deadline. A harness that under-stresses the system is worse than none,
because it licenses a deployment. This file is designed against that
failure mode, not against a clean pass.

THE PRODUCTION SHAPE THIS MODELS, AND WHAT IT IS JUSTIFIED FROM
-----------------------------------------------------------------
* `app/realtime/archive.py::EventArchive` has exactly ONE production call
  site in this repository: `app/cli.py:635` (`kalshi_realtime_replay`,
  READ-ONLY -- it never appends). Nothing under `app/` currently runs a
  live websocket collector loop that calls `EventArchive.append()` in a
  tight producer loop; the write path exists and is hardened, but has not
  yet been wired to a live venue feed. That is stated here plainly because
  it changes what "the production shape" can mean: there is no deployed
  ingestion to measure a record rate or a partition count FROM. Every
  number below is either (a) a structural fact about the code as shipped,
  or (b) an explicitly labelled assumption.
* STRUCTURAL FACT: `EventArchive.partition()` keys a segment by
  `env/venue/date/hour` (`archive.py`). One collector process constructs
  ONE `EventArchive` for ONE venue (`venue="kalshi"` is the default and the
  only value the one call site ever passes). For that shipped topology,
  "multiple partitions rotating simultaneously" reduces to two concrete
  cases, BOTH reachable without any assumption about venue count:
    (1) INTRA-PARTITION, MULTI-GENERATION: one hour can rotate through
        several segment ids (`seg`, `seg.r0001`, `seg.r0002`, ...) if the
        record/byte/age bound is hit repeatedly while the producer keeps
        appending -- and `_RotationCloser` is ONE thread, so if rotations
        arrive faster than `close()` (measured 10.9-20.9 s wall under
        contention at the shipped `DEFAULT_MAX_SEGMENT_RECORDS=13_000`,
        `tests/benchmarks/segment_close_cost.py`) drains them, more than
        one retiring segment queues up. This is the literal mechanism
        `outstanding` counts.
    (2) HOUR-BOUNDARY OVERLAP: a late event for hour H arriving after the
        first event for hour H+1 keeps H's writer briefly retiring while
        H+1's writer is already admitting -- bounded to 2 partitions by
        the clock, not by load.
  Neither case requires more than one producer thread.
* ASSUMPTION, LABELLED AS SUCH: nothing in this repository shows a second
  venue, a second `EventArchive` instance sharing a root, or more than one
  producer thread feeding one archive. `run_multi_instance_load` below
  models exactly that (N independent `EventArchive`s, N producer threads,
  ONE shared root) as a deliberate EXTRAPOLATION beyond the shipped single-
  call-site topology, for two reasons stated up front rather than
  discovered by a reader: (a) the archive's own `env=/venue=/date=/hour=`
  layout already anticipates more than one venue sharing a root, so it is
  not a speculative shape; (b) it is the closest available proxy for real
  OS-level contention (disk I/O, GC pauses, thread scheduling) across
  INDEPENDENT closer threads -- the category of interaction a single-
  thread synthetic trial cannot produce, which is precisely the Solana
  lesson this gate exists to answer. Do not read its numbers as "the"
  production distribution; read them as the load-bearing test of whether
  the async rotation design holds up under contention it might one day
  actually see.
* PAYLOAD SHAPE: `envelope_for()` below reuses the exact field shape
  `tests/benchmarks/segment_close_cost.py::fields()` and
  `tests/test_kalshi_archive_replay_integrity_a8_001.py::fields()` already
  use to cost `close()` -- the one shape in this repository that has been
  called "the real Kalshi orderbook-delta envelope shape" and measured
  against, rather than invented fresh here.
* CLOSE COST: measured on the committed benchmark at
  `DEFAULT_MAX_SEGMENT_RECORDS=13_000`, `commit_to_head=True`: 1.31-2.85 s
  CPU, 10.9-20.9 s wall under contention (this milestone's own numbers, per
  the brief). `--real-close` runs the genuine cost by using real record
  counts; the default mode uses `--inject-close-delay`, an artificial sleep
  inside a monkeypatched `SegmentWriter.close`, SCALED from those measured
  numbers but applied at a much smaller record count, so the STANDING GATE
  (imported by the pytest pin) finishes in seconds rather than minutes. Both
  modes are available from this one script -- see DETERMINISM VS REALISM
  below.

DETERMINISM VS REALISM
-----------------------
Eric's brief is explicit that when the two trade off, realism wins for the
gate and a smaller deterministic pin goes alongside it. This script IS the
realism side: real threads, real filesystem writes, real scheduling, no
mocked clock. It is intentionally NOT part of the pytest collection (no
`test_` prefix) and is not deterministic -- percentiles will differ run to
run and machine to machine, which is the point; a fixed assertion here
would be exactly the "clean pass on an idle machine" this gate exists to
distrust. The deterministic pin lives in
`tests/test_kalshi_archive_concurrent_rotation_load_001.py`, imports the
same harness functions with small, fixed, barrier-forced parameters, and
asserts the INVARIANTS (no lost commit, drain, fallback, latency bound)
rather than a specific distribution.

WHAT THIS DOES NOT COVER
-------------------------
* No real websocket feed, no real venue jitter, no real disconnect/backoff
  interacting with rotation -- there is no live collector loop to attach to
  (see above). The producer threads below are a closed loop: append as fast
  as the harness allows, not as fast as Kalshi actually sends.
* No real crash. "The widened crash window" is measured by SAMPLING
  `outstanding` during sustained load, which characterises what an
  operator would face if the process died at a random instant -- it is not
  a SIGKILL against a live rotation (the existing `TestAckedIsNotDurable`
  in the A8 suite already SIGKILLs a single writer for the acked-vs-durable
  question; that is a different property from this one).
* No multi-process contention. Every scenario here runs multiple THREADS
  in one Python process. A second `probability-arena` process (e.g. a
  concurrent backup, or a second collector) contending for the same
  filesystem is not modelled -- see SQLITE-BACKUP-COORDINATION-001 and
  related work for the analogous problem on the SQLite side of this
  codebase, which this archive deliberately does not touch.
* No genuine host-load sweep. Every number this script prints is reported
  ALONGSIDE `os.getloadavg()` at the time it ran, but there is no attempt
  to hold load constant or to reproduce EVO's actual load profile; it
  reports the host it happened to run on, honestly, rather than a
  normalised figure.
* The `_RotationCloser.submit()` race documented in `archive.py` is
  observed here empirically (real thread scheduling racing a real
  concurrent `close()`), not injected at a controlled bytecode boundary.
  The reachability RATE this script measures is therefore itself a
  measurement with its own sampling error, not a proof of the rate on any
  other machine -- report it as such.

USAGE
    python tests/benchmarks/concurrent_rotation_load.py                # full report
    python tests/benchmarks/concurrent_rotation_load.py --quick        # small, still real
    python tests/benchmarks/concurrent_rotation_load.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.realtime import archive as ar                # noqa: E402
from app.realtime import archive_head as ah            # noqa: E402
from app.realtime import canonical as cn               # noqa: E402
from app.realtime import segment as sg                 # noqa: E402
from app.realtime.book import EventEnvelope            # noqa: E402
from app.realtime.archive import _quantiles            # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
ENV = "demo"

# Measured on `segment_close_cost.py` at DEFAULT_MAX_SEGMENT_RECORDS=13_000,
# commit_to_head=True (per the brief): 1.31-2.85s CPU, 10.9-20.9s wall under
# contention. Scaled per-record so a smaller test segment gets a
# proportionally smaller, but not zero, injected delay.
_MEASURED_WALL_S_AT_13000 = 20.9
_MEASURED_RECORDS = 13_000


def injected_close_delay(n_records: int, *, floor_s: float = 0.05) -> float:
    """A wall-clock delay scaled from the measured worst-case close cost,
    never below `floor_s` -- a delay of exactly 0 would defeat the whole
    point of injecting one at all."""
    scaled = _MEASURED_WALL_S_AT_13000 * (n_records / _MEASURED_RECORDS)
    return max(floor_s, scaled)


def envelope_for(i: int, *, ticker: str = "KXA", sid: int = 4) -> EventEnvelope:
    """The same orderbook-delta envelope shape `segment_close_cost.py` and
    the A8 suite already cost `close()` against -- not invented fresh here."""
    stamp = cn.canonical_datetime(NOW + timedelta(microseconds=i))
    return EventEnvelope(
        schema_version=1, venue="kalshi", environment=ENV,
        channel="orderbook_delta", event_type="orderbook_delta",
        market_ticker=ticker, market_id=ticker, sid=sid, seq=i,
        venue_time=stamp, collector_receive_time=stamp,
        normalization_time=stamp, receive_monotonic_ns=1_000_000 + i,
        normalize_monotonic_ns=1_000_100 + i, data_age_us=100,
        implementation_version="concurrent-rotation-load",
        raw={"price_dollars": "0.5100", "side": "no"},
        normalized={"raw_price_units": 5100})


# ---------------------------------------------------------------------------
# Scenario 1: one EventArchive, one producer thread, rapid intra-partition
# multi-generation rotation. STRUCTURALLY GROUNDED -- see module docstring.
# ---------------------------------------------------------------------------

def run_single_instance_load(*, root: Path, records: int,
                             max_segment_records: int,
                             inject_close_delay: bool,
                             outstanding_sample_hz: float = 200.0) -> dict:
    ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
    store = ar.EventArchive(root, environment=ENV, venue="kalshi",
                            max_segment_records=max_segment_records)
    real_close = None
    if inject_close_delay:
        real_close = sg.SegmentWriter.close

        def slow_close(self):
            time.sleep(injected_close_delay(self.accounting.written))
            return real_close(self)

        sg.SegmentWriter.close = slow_close

    outstanding_samples = []
    stop_sampler = threading.Event()

    def sampler():
        interval = 1.0 / outstanding_sample_hz
        while not stop_sampler.is_set():
            outstanding_samples.append(store._closer.outstanding)
            time.sleep(interval)

    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()

    append_latencies_ms = []
    try:
        for i in range(records):
            t0 = time.perf_counter()
            store.append(envelope_for(i))
            append_latencies_ms.append((time.perf_counter() - t0) * 1000.0)
    finally:
        stop_sampler.set()
        sampler_thread.join(timeout=5.0)
        if real_close is not None:
            sg.SegmentWriter.close = real_close

    drained = store.wait_for_rotations(120.0)
    manifests = store.close()
    verify = sg.verify_archive(root, environment=ENV)
    read_back = store.read_verified() if verify["verdict"] == "VALID" else []

    return {
        "scenario": "single_instance",
        "records_submitted": records,
        "records_read_back": len(read_back),
        "acked_equals_readable": len(read_back) == records,
        "rotations": store.rotations,
        "rotation_failures": store.rotation_failures,
        "drained_before_close": drained,
        "verify_verdict": verify["verdict"],
        "verify_reasons": verify.get("reasons", []),
        "append_latency_ms": _quantiles(append_latencies_ms),
        "outstanding_awaiting_head_commit": _quantiles(
            [float(x) for x in outstanding_samples]),
        "outstanding_samples_n": len(outstanding_samples),
        "outstanding_max_observed": max(outstanding_samples, default=0),
        "manifests_committed": len(manifests),
    }


# ---------------------------------------------------------------------------
# Scenario 2: N independent EventArchive instances (N venues), one shared
# root, N producer threads running concurrently. EXTRAPOLATION -- see module
# docstring for the justification.
# ---------------------------------------------------------------------------

def run_multi_instance_load(*, root: Path, n_instances: int, records_per: int,
                            max_segment_records: int,
                            inject_close_delay: bool,
                            outstanding_sample_hz: float = 200.0) -> dict:
    ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
    stores = [
        ar.EventArchive(root, environment=ENV, venue=f"loadv{n:02d}",
                        max_segment_records=max_segment_records)
        for n in range(n_instances)
    ]

    real_close = None
    if inject_close_delay:
        real_close = sg.SegmentWriter.close

        def slow_close(self):
            time.sleep(injected_close_delay(self.accounting.written))
            return real_close(self)

        sg.SegmentWriter.close = slow_close

    outstanding_samples = []       # aggregate across all instances, per tick
    stop_sampler = threading.Event()

    def sampler():
        interval = 1.0 / outstanding_sample_hz
        while not stop_sampler.is_set():
            outstanding_samples.append(
                sum(s._closer.outstanding for s in stores))
            time.sleep(interval)

    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()

    per_thread_latencies = [[] for _ in range(n_instances)]
    errors: list = []

    def producer(idx: int, store: ar.EventArchive):
        try:
            for i in range(records_per):
                t0 = time.perf_counter()
                store.append(envelope_for(i, ticker=f"KXA{idx:02d}"))
                per_thread_latencies[idx].append(
                    (time.perf_counter() - t0) * 1000.0)
        except BaseException as exc:      # noqa: BLE001 - reported, not lost
            errors.append((idx, repr(exc)))

    try:
        t_start = time.perf_counter()
        threads = [threading.Thread(target=producer, args=(n, stores[n]))
                   for n in range(n_instances)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=600)
        wall_s = time.perf_counter() - t_start
    finally:
        stop_sampler.set()
        sampler_thread.join(timeout=5.0)
        if real_close is not None:
            sg.SegmentWriter.close = real_close

    per_store = []
    all_latencies = []
    for idx, store in enumerate(stores):
        drained = store.wait_for_rotations(120.0)
        manifests = store.close()
        all_latencies.extend(per_thread_latencies[idx])
        per_store.append({
            "venue": store.venue,
            "records_submitted": records_per,
            "rotations": store.rotations,
            "rotation_failures": store.rotation_failures,
            "drained_before_close": drained,
            "manifests_committed": len(manifests),
        })

    verify = sg.verify_archive(root, environment=ENV)
    total_submitted = n_instances * records_per
    # `verify_archive`/`verify()["records"]` walks the WHOLE environment's
    # committed generation chain, across every venue sharing this root --
    # not per-venue -- so this one count is already the combined figure.
    read_back = verify["records_read"] if verify["verdict"] == "VALID" else 0

    return {
        "scenario": "multi_instance",
        "n_instances": n_instances,
        "wall_s": wall_s,
        "total_records_submitted": total_submitted,
        "total_records_verified": read_back,
        "acked_equals_verified": read_back == total_submitted,
        "producer_errors": errors,
        "per_store": per_store,
        "append_latency_ms": _quantiles(all_latencies),
        "outstanding_awaiting_head_commit_aggregate": _quantiles(
            [float(x) for x in outstanding_samples]),
        "outstanding_samples_n": len(outstanding_samples),
        "outstanding_max_observed_aggregate": max(outstanding_samples,
                                                   default=0),
        "verify_verdict": verify["verdict"],
        "verify_reasons": verify.get("reasons", []),
    }


# ---------------------------------------------------------------------------
# Scenario 3: empirical reachability of the documented `_RotationCloser
# .submit()` race, under increasing producer-thread (partition) concurrency.
# Real threads, real timing -- NOT an injected bytecode-boundary repro. See
# `archive.py::_RotationCloser.submit`'s docstring for the mechanism.
# ---------------------------------------------------------------------------

def _race_trial(root: Path, n_producers: int, max_segment_records: int
                ) -> dict:
    ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
    store = ar.EventArchive(root, environment=ENV, venue="kalshi",
                            max_segment_records=max_segment_records,
                            max_segment_age_s=None, max_segment_bytes=None)
    store.rotation_close_timeout_s = 0.05    # small: reveal a stuck drain fast

    stop = threading.Event()
    submitted = [0] * n_producers

    def producer(idx: int):
        i = 0
        while not stop.is_set():
            try:
                store.append(envelope_for(idx * 1_000_000 + i,
                                          ticker=f"KXR{idx:02d}"))
                submitted[idx] += 1
                i += 1
            except ar.ArchiveError:
                return

    threads = [threading.Thread(target=producer, args=(n,))
               for n in range(n_producers)]
    for t in threads:
        t.start()
    # Race the close against live producers -- a randomised short delay so
    # the close lands at a different point in the rotation cycle each trial,
    # rather than always at the same (easy or hard) phase.
    time.sleep(random.uniform(0.0005, 0.01))
    manifests_or_exc = None
    try:
        manifests_or_exc = store.close()
    except ar.ArchiveError as exc:
        manifests_or_exc = exc
    stop.set()
    for t in threads:
        t.join(timeout=10)

    # The documented symptom needs a SECOND close() to surface (see the
    # docstring: the closer-failure line is appended on a SUBSEQUENT
    # close(), not the one that raced). Idempotent per `SegmentWriter.close`.
    second_failures_before = len(store.rotation_failures)
    store.close()
    symptom = len(store.rotation_failures) > second_failures_before

    total_submitted = sum(submitted)
    verify = sg.verify_archive(root, environment=ENV)
    lost = (verify["verdict"] != "VALID"
            or verify["records_read"] < total_submitted)
    return {
        "n_producers": n_producers,
        "total_submitted": total_submitted,
        "records_verified": verify["records_read"],
        "verify_verdict": verify["verdict"],
        "durability_lost": lost,
        "race_symptom_observed": symptom,
        "first_close_raised": isinstance(manifests_or_exc, Exception),
    }


def run_race_reachability(*, tmp_parent: Path, n_trials: int,
                          producer_counts: tuple) -> dict:
    by_count = {}
    for n_producers in producer_counts:
        symptoms = 0
        lost_any = False
        trial_reports = []
        for t in range(n_trials):
            root = Path(tempfile.mkdtemp(dir=tmp_parent,
                                         prefix=f"race-{n_producers}-{t}-"))
            try:
                r = _race_trial(root, n_producers, max_segment_records=5)
            finally:
                shutil.rmtree(root, ignore_errors=True)
            trial_reports.append(r)
            if r["durability_lost"]:
                lost_any = True
            if r["race_symptom_observed"]:
                symptoms += 1
        by_count[n_producers] = {
            "n_trials": n_trials,
            "race_symptom_count": symptoms,
            "race_symptom_rate": symptoms / n_trials if n_trials else None,
            "any_durability_lost": lost_any,
        }
        if lost_any:
            by_count[n_producers]["trials"] = trial_reports
    return by_count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _loadavg() -> dict:
    try:
        one, five, fifteen = os.getloadavg()
        return {"1m": one, "5m": five, "15m": fifteen,
                "cpu_count": os.cpu_count()}
    except (OSError, AttributeError):
        return {"1m": None, "5m": None, "15m": None,
                "cpu_count": os.cpu_count()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="smaller, still-real sizes for a fast smoke run")
    ap.add_argument("--real-close", action="store_true",
                    help="use the genuine SegmentWriter.close cost instead "
                         "of an injected, scaled delay")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    inject = not args.real_close
    n_instances = 4 if args.quick else 12
    records_per = 300 if args.quick else 4_000
    max_seg = 25 if args.quick else 150
    race_trials = 10 if args.quick else 60

    report = {"loadavg_before": _loadavg()}
    parent = Path(tempfile.mkdtemp(prefix="concurrent-rotation-load-"))
    try:
        print(f"host load average (1m/5m/15m): {report['loadavg_before']}")

        root1 = parent / "single"
        root1.mkdir()
        print("\n-- scenario 1: single-instance rapid intra-partition "
              "rotation --")
        s1 = run_single_instance_load(
            root=root1, records=records_per,
            max_segment_records=max_seg, inject_close_delay=inject)
        report["single_instance"] = s1
        print(json.dumps({k: v for k, v in s1.items()
                          if k not in ("rotation_failures",)}, indent=2,
                         default=str))

        root2 = parent / "multi"
        root2.mkdir()
        print(f"\n-- scenario 2: {n_instances} concurrent partitions "
              "(multi-instance, shared root) --")
        s2 = run_multi_instance_load(
            root=root2, n_instances=n_instances, records_per=records_per,
            max_segment_records=max_seg, inject_close_delay=inject)
        report["multi_instance"] = s2
        print(json.dumps({k: v for k, v in s2.items()
                          if k != "per_store"}, indent=2, default=str))

        print("\n-- scenario 3: submit()/shutdown() race reachability "
              f"({race_trials} trials per producer count) --")
        race = run_race_reachability(
            tmp_parent=parent, n_trials=race_trials,
            producer_counts=(1, 4, 12))
        report["race_reachability"] = race
        print(json.dumps(race, indent=2, default=str))

        report["loadavg_after"] = _loadavg()
        print(f"\nhost load average after (1m/5m/15m): "
              f"{report['loadavg_after']}")
    finally:
        shutil.rmtree(parent, ignore_errors=True)

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str))
        print(f"\nwrote {args.json}")

    ok = (s1["acked_equals_readable"] and s2["acked_equals_verified"]
         and not any(v["any_durability_lost"] for v in race.values()))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
