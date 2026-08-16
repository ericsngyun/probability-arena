#!/usr/bin/env python3
"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- the rotation-cost benchmark,
committed as a runnable script so it can be REGRESSED rather than re-invented.

WHY THIS EXISTS AS A FILE. `DEFAULT_MAX_SEGMENT_RECORDS` was derived from an
ad hoc benchmark that was never committed ("reproducible by submitting N
records to a fresh `SegmentWriter` and timing `close()`"), and it was run
with `commit_to_head=False`. `SegmentWriter` DEFAULTS that flag to True and
`EventArchive._writer_for` never overrides it, so production's close does
strictly more work than the number in the source comment describes: after
reconciliation it runs a full independent `verify_segment` (a THIRD complete
read of the segment) and then `commit_segment`, which fsyncs a generation
record, the current-head pointer and their directories under the archive
lock. Benchmarking the cheaper of the two shapes and shipping the bound
derived from it is how a ~3 s stall got documented as ~1.4 s.

USAGE

    python tests/benchmarks/segment_close_cost.py                 # default sweep
    python tests/benchmarks/segment_close_cost.py --records 20000 --repeat 3
    python tests/benchmarks/segment_close_cost.py --json out.json

Reports, per record count and per `commit_to_head` setting: append
throughput, close WALL time, close CPU time, and the implied close latency
per 1,000 records. Wall and CPU are reported separately on purpose -- the
gap between them is the part a loaded host inflates, and it is the reason
the bound is set from the CPU figure with headroom rather than from a
best-case wall figure.

--------------------------------------------------------------------------
KALSHI-LIVE-TAPE-COLLECTOR-001 CP5 -- THE INSTRUMENTATION-OVERHEAD GATE
--------------------------------------------------------------------------

    python tests/benchmarks/segment_close_cost.py --mode cp5
    python tests/benchmarks/segment_close_cost.py --mode cp5 \
        --cp5-records 40000 --cp5-reps 8 --json cp5.json

CP5 (section 9) runs the SAME fixture load with the measurement lane enabled
and disabled and compares append throughput and append latency DISTRIBUTION.
The gate: the overhead is stated as a number and is a small single-digit
percentage of append cost, or the instrumentation is redesigned. It lives in
this file, not a second benchmark, because section 9 says so and because the
close-cost numbers above are the other half of the same producer budget.

It is extended here rather than written fresh for a second reason: the
rotation cost measured above is paid ON the producer thread (section 8.4
point 2) and the closer thread it spawns contends with the producer for the
GIL. A CP5 run that never rotated would measure the cheap half of the path.

WHY THE NUMBER CP4 ALREADY REPORTED IS NOT THE GATE. CP4 reported ~395 ns per
event against its own null lane. That figure was taken with no archive at all,
so it omits real I/O, real segment rotation and the closer thread, and CP4
labelled it NOT the gate itself. This mode measures `on_frame` + `on_append`
in the real per-frame path against a real `EventArchive`, because
`archive.append()` is synchronous and caller-threaded: anything the
measurement lane does is on the critical path by construction.

THE THREE ARMS, AND WHY THERE ARE THREE

    real     `CollectorMetrics`     -- the shipped instrumentation
    null_a   `NULL_METRICS`         -- the same code, no-op hooks
    null_b   `NULL_METRICS`         -- a SECOND identical null arm

`null_b` exists to establish the NOISE FLOOR. Two runs of identical code
differ by some amount on any real host; a `real - null` difference smaller
than the `null_b - null_a` spread has not been resolved by this rig and is
reported as such rather than as a number. Without it, a 2% laptop difference
gets published as a finding.

There is NO `if metrics` branch anywhere in the loop: `NULL_METRICS` (CP4)
implements the identical surface as no-ops precisely so both arms execute the
same code and the comparison isolates the metrics WORK rather than a branch.

THREE INDEPENDENT ESTIMATORS OF THE SAME QUANTITY

    E1 (direct)      one extra `monotonic_ns()` after the two metric calls,
                     present in EVERY arm, so the clock read cancels. Gives a
                     per-frame instrumentation-block cost. SHARP, but it sees
                     only what happens between those two clock reads.
    E2 (throughput)  wall time of the whole timed loop, per event. Sees
                     everything, including second-order allocator and GC
                     effects E1 structurally cannot -- and is also the
                     estimator a noisy neighbour destroys first.
    E3 (cpu-time)    `process_time` of the same loop, per event. Counts only
                     THIS process's threads (producer plus closer, so GIL
                     contention still lands in it) and therefore keeps E2's
                     coverage while dropping most of E2's sensitivity to
                     whatever else the host is running.

They are computed from the same run and reported side by side. Agreement is
the evidence that none is an artefact of its own scaffolding; E1 alone would
be a measurement of the instrumentation's inside only.

DESIGN AGAINST THERMAL AND PAGE-CACHE DRIFT. Arm order is SHUFFLED inside
every repetition from a seeded RNG, so no arm systematically runs on a warmer
machine, and the headline statistic is the PAIRED per-repetition difference
(`real` minus the mean of the two nulls within the same repetition), which
cancels any drift that moves a whole repetition. A discarded warmup
repetition precedes the measured ones. The CI is over repetitions, not over
frames: frames within one run are not independent samples of the thing that
varies.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.realtime import archive_head as ah        # noqa: E402
from app.realtime import canonical as cn           # noqa: E402
from app.realtime import segment as sg             # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
ENV = "demo"


def fields(i: int) -> dict:
    """The real Kalshi orderbook-delta envelope shape, not a toy payload."""
    return {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": "KXA", "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {"price_dollars": "0.5100", "side": "no"},
        "normalized_event": {"raw_price_units": 5100},
    }


def one_trial(*, n: int, commit_to_head: bool) -> dict:
    root = Path(tempfile.mkdtemp(prefix="segclose-"))
    try:
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg",
                             partition_identity="p",
                             commit_to_head=commit_to_head)
        t0, c0 = time.perf_counter(), time.process_time()
        for i in range(n):
            assert w.submit(fields(i)) is None
        append_wall = time.perf_counter() - t0
        append_cpu = time.process_time() - c0

        t1, c1 = time.perf_counter(), time.process_time()
        manifest = w.close()
        close_wall = time.perf_counter() - t1
        close_cpu = time.process_time() - c1
        assert manifest["record_count"] == n
        return {
            "records": n,
            "commit_to_head": commit_to_head,
            "append_wall_s": append_wall,
            "append_cpu_s": append_cpu,
            "append_ev_per_s": n / append_wall if append_wall else None,
            "close_wall_s": close_wall,
            "close_cpu_s": close_cpu,
            "close_wall_ms_per_1000": 1000.0 * close_wall / n * 1000 / 1000,
            "close_cpu_ms_per_1000": 1000.0 * close_cpu / n * 1000 / 1000,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


# =========================================================================
# CP5 -- the instrumentation-overhead gate
# =========================================================================

from app.realtime.archive import EventArchive             # noqa: E402
from app.realtime.book import make_envelope               # noqa: E402
from app.realtime.collector import normalize_frame        # noqa: E402
from app.realtime.collector_metrics import (              # noqa: E402
    NULL_METRICS,
    CollectorMetrics,
)

VENUE = "kalshi"

# Two-sided 95% t critical values by degrees of freedom. Hardcoded because
# numpy/scipy are deliberately absent from this venv and a normal 1.96 on
# seven degrees of freedom understates the interval by 17%.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
        7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179,
        13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101,
        19: 2.093, 20: 2.086, 25: 2.060, 30: 2.042}


def _t95(df: int) -> float:
    if df < 1:
        return float("nan")
    if df in _T95:
        return _T95[df]
    if df > 30:
        return 1.960
    return _T95[max(k for k in _T95 if k <= df)]


def _pct(sorted_values: list, q: float) -> float:
    """Nearest-rank percentile. `sorted_values` must already be sorted."""
    if not sorted_values:
        return float("nan")
    k = max(0, min(len(sorted_values) - 1,
                   int(round(q * (len(sorted_values) - 1)))))
    return float(sorted_values[k])


def _mean_ci95(samples: list) -> tuple:
    """(mean, half-width of the 95% CI, n). Half-width is nan below n=2."""
    n = len(samples)
    if n == 0:
        return (float("nan"), float("nan"), 0)
    mean = sum(samples) / n
    if n < 2:
        return (mean, float("nan"), n)
    sd = statistics.stdev(samples)
    return (mean, _t95(n - 1) * sd / (n ** 0.5), n)


# The four DEMO-wire frame shapes, cycled so the fixture is not one repeated
# payload. Sizes and field sets follow `book.py:97-106`'s recorded wire
# observations; `ts_ms` is present because `make_envelope` reads it FIRST and
# a fixture without it would exercise the ISO fallback the venue does not use.
_FRAME_TEMPLATES = (
    ("orderbook_delta", 4, {"market_ticker": "KXBTCD-26AUG1417-T119999.99",
                            "market_id": "b8d2f0e4", "price": 51, "delta": 3,
                            "side": "no", "ts_ms": 1786150148065}),
    ("ticker", 1, {"market_ticker": "KXBTCD-26AUG1417-T119999.99",
                   "market_id": "b8d2f0e4", "yes_bid": 48, "yes_ask": 52,
                   "last_price": 50, "volume": 10432, "open_interest": 5120,
                   "ts_ms": 1786150148065}),
    ("trade", 3, {"market_ticker": "KXBTCD-26AUG1417-T119999.99",
                  "market_id": "b8d2f0e4", "yes_price": 51, "no_price": 49,
                  "count": 7, "taker_side": "yes", "ts_ms": 1786150148065}),
    ("market_lifecycle_v2", 2, {"market_ticker": "KXBTCD-26AUG1417-T119999.99",
                                "market_id": "b8d2f0e4", "is_deactivated": False,
                                "open_ts": 1786100000, "close_ts": 1786200000,
                                "ts_ms": 1786150148065}),
)

# Wire byte counts, computed ONCE. The real collector takes this from the
# transport's byte counter, not from a `json.dumps` in the frame path (7.1
# forbids one there), so the benchmark must not introduce one either.
_TEMPLATE_BYTES = tuple(
    len(json.dumps({"type": t, "sid": s, "seq": 1, "msg": m}).encode())
    for t, s, m in _FRAME_TEMPLATES)


def cp5_one_run(*, n: int, arm: str, max_segment_records: int,
                markets: int = 8) -> dict:
    """One arm, one repetition: `n` frames through a REAL `EventArchive`.

    The timed loop is step 1 through step 5 of section 6.3 with step 4
    (sequence validation) omitted -- it is not on the archive path and is not
    what this gate is about. Every arm runs this identical body; only the
    object bound to `metrics` differs, which is the whole point of
    `NULL_METRICS` existing.
    """
    root = Path(tempfile.mkdtemp(prefix="cp5-overhead-"))
    try:
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        if arm == "real":
            metrics = CollectorMetrics(environment=ENV,
                                       markets_subscribed=markets)
        else:
            metrics = NULL_METRICS
        # KALSHI-TAPE-CLOSE-CALLBACK: the archive carries the close-latency
        # seam the collector now wires, so this stays "the shape `collector.py`
        # contains" — the rule CP3.5 set for this benchmark. It matters here
        # even though close runs off the producer thread: the closer contends
        # with the producer for the GIL (the reason this mode rotates at all),
        # so the real arm's histogram work on the closer thread is part of
        # what the producer pays for. BOTH arms pass a callable, so the
        # difference is the instrumentation and not the seam's existence.
        archive = EventArchive(root, environment=ENV,
                               max_segment_records=max_segment_records,
                               on_segment_closed=metrics.on_segment_closed)

        # Pre-allocated sample arrays. Assignment by index only: a growing
        # list would put an amortised realloc inside the timed loop, and it
        # would land in whichever arm happened to cross a growth boundary.
        append_ns = [0] * n
        instr_ns = [0] * n
        unit_ns = [0] * n

        # Local rebinds: attribute lookups inside the loop would be charged to
        # both arms equally, but keeping them out makes the append and metrics
        # windows narrower and therefore the measurement sharper.
        mono = time.monotonic_ns
        now_utc = _cp5_utcnow
        templates = _FRAME_TEMPLATES
        tbytes = _TEMPLATE_BYTES
        env_name = ENV
        do_append = archive.append
        on_frame = metrics.on_frame
        on_append = metrics.on_append

        prev_path = None
        rejected = 0
        metrics_errors = 0
        gc.collect()

        t_start, c_start = time.perf_counter(), time.process_time()
        for i in range(n):
            k = i & 3
            etype, sid, msg_tmpl = templates[k]
            msg = dict(msg_tmpl)
            msg["ts_ms"] = 1786150148065 + i
            frame = {"type": etype, "sid": sid, "seq": i, "msg": msg}

            # -- step 1: both stamps before any processing
            t_recv = mono()
            receive_time = now_utc()

            # -- step 2: normalize + envelope (identical in every arm)
            observation = normalize_frame(message=frame,
                                          receive_time=receive_time)
            envelope = make_envelope(
                venue=VENUE, environment=env_name,
                channel=observation["channel"], message=frame,
                receive_time=receive_time, receive_mono=t_recv,
                normalized=observation)

            # -- step 3: ARCHIVE. Synchronous, caller-threaded, lock-serialised.
            t0 = mono()
            try:
                path = do_append(envelope)
            except Exception:                       # noqa: BLE001
                rejected += 1
                path = prev_path
            t1 = mono()

            # -- step 5: the measurement block, in THE SHAPE `collector.py`
            #    ACTUALLY CONTAINS since CP3.5: typed direct calls, each inside
            #    its own inline `try/except`. Before CP3.5 this loop measured a
            #    shape no caller used, because there was no caller.
            #    `rotated` is computed in EVERY arm so the path comparison sits
            #    in the common baseline and the real-minus-null difference is
            #    the two metric CALLS plus their two boundaries alone.
            rotated = path != prev_path and prev_path is not None
            try:
                on_frame(t_recv, tbytes[k])
            except Exception:                   # noqa: BLE001
                metrics_errors += 1
            try:
                on_append(t1 - t0, rotated=rotated)
            except Exception:                   # noqa: BLE001
                metrics_errors += 1
            t2 = mono()

            prev_path = path
            append_ns[i] = t1 - t0
            instr_ns[i] = t2 - t1
            unit_ns[i] = t2 - t_recv
        loop_wall = time.perf_counter() - t_start
        loop_cpu = time.process_time() - c_start

        rotations = archive.rotations
        t_close = time.perf_counter()
        manifests = archive.close()
        close_wall = time.perf_counter() - t_close

        append_sorted = sorted(append_ns)
        instr_sorted = sorted(instr_ns)
        unit_sorted = sorted(unit_ns)
        return {
            "arm": arm,
            "records": n,
            "rejected": rejected,
            # Anti-vacuity for the boundary: it must be present and must never
            # have fired. A non-zero here means the arms are not comparable.
            "metrics_errors": metrics_errors,
            "rotations": rotations,
            "segments": len(manifests) if hasattr(manifests, "__len__") else None,
            "loop_wall_s": loop_wall,
            "loop_cpu_s": loop_cpu,
            "ev_per_s": n / loop_wall if loop_wall else None,
            "ns_per_event": 1e9 * loop_wall / n,
            # CPU time, not wall. On a host running someone else's 100%-CPU
            # job, wall time per event measures the neighbour as much as it
            # measures us; `process_time` counts only THIS process's threads
            # (producer plus closer, so GIL contention still shows). It is
            # the estimator that survives a contended rig.
            "cpu_ns_per_event": 1e9 * loop_cpu / n,
            "close_wall_s": close_wall,
            "append_p50_ns": _pct(append_sorted, 0.50),
            "append_p95_ns": _pct(append_sorted, 0.95),
            "append_p99_ns": _pct(append_sorted, 0.99),
            "append_max_ns": append_sorted[-1],
            "append_mean_ns": sum(append_ns) / n,
            "instr_p50_ns": _pct(instr_sorted, 0.50),
            "instr_p95_ns": _pct(instr_sorted, 0.95),
            "instr_p99_ns": _pct(instr_sorted, 0.99),
            "instr_mean_ns": sum(instr_ns) / n,
            "unit_p50_ns": _pct(unit_sorted, 0.50),
            "unit_p95_ns": _pct(unit_sorted, 0.95),
            "unit_p99_ns": _pct(unit_sorted, 0.99),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _cp5_utcnow() -> datetime:
    return datetime.now(UTC)


def clock_pair_cost_ns(iterations: int = 200_000) -> dict:
    """What two back-to-back `monotonic_ns()` calls cost on THIS host.

    Section 7.4 records "two clock reads per frame" as an ASSUMPTION TO VERIFY
    -- that it is a low-tens-of-nanoseconds cost and therefore negligible
    against a measured append. It is verified here rather than asserted, and
    it is also the resolution floor of every per-frame number above: an
    instrumentation block that costs less than one clock pair cannot be
    resolved by a clock-pair measurement.
    """
    mono = time.monotonic_ns
    samples = [0] * iterations
    gc.collect()
    for i in range(iterations):
        a = mono()
        b = mono()
        samples[i] = b - a
    s = sorted(samples)
    return {"iterations": iterations, "p50_ns": _pct(s, 0.50),
            "p95_ns": _pct(s, 0.95), "p99_ns": _pct(s, 0.99),
            "min_ns": s[0], "mean_ns": sum(samples) / iterations}


def path_compare_cost_ns(iterations: int = 200_000) -> dict:
    """What `path != previous_path` costs -- the design's ONLY rotation signal.

    CP4's docstring fixes the orchestrator's rotation detection as "the `Path`
    returned by `append()` differs from the previous one -- the only rotation
    signal available without reading a private archive attribute". That
    comparison is per-frame instrumentation too, even though it sits in the
    orchestrator rather than in `collector_metrics.py`, so pricing it is part
    of pricing the measurement lane. It is charged to BOTH arms in
    `cp5_one_run` (it has to be: taking it out of the null arm would make the
    arms different code), which is exactly why it needs its own number here --
    otherwise it hides inside the shared baseline and never gets reported.
    """
    a = Path("/tmp/x/env=demo/segment=2026-08-14T15/events.jsonl.gz")
    b = Path("/tmp/x/env=demo/segment=2026-08-14T16/events.jsonl.gz")
    mono = time.monotonic_ns
    samples = [0] * iterations
    gc.collect()
    for i in range(iterations):
        t0 = mono()
        _ = a != b
        t1 = mono()
        samples[i] = t1 - t0
    s = sorted(samples)
    return {"iterations": iterations, "p50_ns": _pct(s, 0.50),
            "p95_ns": _pct(s, 0.95), "mean_ns": sum(samples) / iterations}


class _SeamProbe:
    """Prices the three seam shapes this milestone has actually proposed.

    History, because the numbers only mean something against it. CP3 and CP4
    shipped different interfaces for the same seam: CP3 called
    `observe_frame(**kwargs)` inside a `try/except`, CP4 exposed
    `on_frame(...)` and expected a direct call, and nothing in `app/` bridged
    the two -- `CollectorMetrics` had no caller outside its own tests. CP5
    priced CP3's shape at +250 ns p50 over a direct call and recommended
    against it (§13, "An actionable constraint for CP3<->CP4 wiring").

    **CP3.5 took a third option and this probe is what says it was the right
    one.** The wired seam calls the typed method DIRECTLY and puts only that
    call inside an inline `try/except` -- so it pays the exception handler
    (which CPython 3.11+ makes zero-cost on the non-raising path) but not the
    keyword dict and not the extra Python call frame. `guarded` measures
    exactly the shape `collector.py` now contains; `wrapped` is kept so the
    thing that was rejected stays measurable rather than becoming folklore.
    """

    def __init__(self) -> None:
        self.errors = 0
        self.n = 0

    def observe_frame(self, *, event_type, archived, append_ns, rotations):
        self.n += 1

    def on_frame(self, received_mono_ns: int, wire_bytes: int = 0) -> None:
        self.n += 1

    def wrapped(self, **kwargs) -> None:
        try:
            self.observe_frame(**kwargs)
        except Exception:                       # noqa: BLE001
            self.errors += 1


def seam_wrapper_cost_ns(iterations: int = 200_000) -> dict:
    """Three shapes: typed direct, CP3.5's inline-guarded, CP3's kwargs wrapper.

    All three are timed in the SAME loop iteration, one clock pair each, so a
    scheduler excursion lands on all of them rather than on whichever arm ran
    during it.
    """
    probe = _SeamProbe()
    mono = time.monotonic_ns
    direct = [0] * iterations
    guarded = [0] * iterations
    wrapped = [0] * iterations
    errors = 0
    gc.collect()
    for i in range(iterations):
        t0 = mono()
        probe.on_frame(1_234_567, 480)
        t1 = mono()
        # THE WIRED SHAPE, character for character as `collector.py` has it.
        try:
            probe.on_frame(1_234_567, 480)
        except Exception:                       # noqa: BLE001
            errors += 1
        t2 = mono()
        probe.wrapped(event_type="orderbook_delta", archived=True,
                      append_ns=1234, rotations=0)
        t3 = mono()
        direct[i] = t1 - t0
        guarded[i] = t2 - t1
        wrapped[i] = t3 - t2
    d, g, w = sorted(direct), sorted(guarded), sorted(wrapped)
    return {"iterations": iterations,
            "boundary_errors": errors,
            "direct_p50_ns": _pct(d, 0.50),
            "guarded_p50_ns": _pct(g, 0.50),
            "wrapped_p50_ns": _pct(w, 0.50),
            "direct_mean_ns": sum(direct) / iterations,
            "guarded_mean_ns": sum(guarded) / iterations,
            "wrapped_mean_ns": sum(wrapped) / iterations,
            # CP3.5's cost: what the exception boundary alone adds.
            "guard_overhead_p50_ns": _pct(g, 0.50) - _pct(d, 0.50),
            "guard_overhead_mean_ns": (sum(guarded) - sum(direct)) / iterations,
            # CP3's cost, the number CP3.5 had to come in under.
            "wrapper_overhead_p50_ns": _pct(w, 0.50) - _pct(d, 0.50),
            "wrapper_overhead_mean_ns": (sum(wrapped) - sum(direct)) / iterations}


def _load_snapshot() -> dict:
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError):        # pragma: no cover - non-POSIX
        return {"available": False}
    return {"available": True, "load_1m": one, "load_5m": five,
            "load_15m": fifteen, "cpu_count": os.cpu_count()}


def run_cp5(*, n: int, reps: int, max_segment_records: int, seed: int,
            markets: int) -> dict:
    """The gate. Three arms, shuffled per repetition, one discarded warmup."""
    rng = random.Random(seed)
    load_before = _load_snapshot()
    clock = clock_pair_cost_ns()
    pathcmp = path_compare_cost_ns()
    seam = seam_wrapper_cost_ns()

    print(f"CP5 instrumentation-overhead gate  "
          f"n={n} reps={reps} max_segment_records={max_segment_records} "
          f"seed={seed}")
    print(f"  clock pair : p50={clock['p50_ns']:.0f}ns "
          f"p95={clock['p95_ns']:.0f}ns min={clock['min_ns']:.0f}ns "
          f"(this is the quantum every per-frame ns figure is rounded to)")
    print(f"  path!=path : p50={pathcmp['p50_ns']:.0f}ns "
          f"p95={pathcmp['p95_ns']:.0f}ns mean={pathcmp['mean_ns']:.0f}ns "
          f"(charged to BOTH arms; includes one clock pair)")
    print(f"  seam shape : direct p50={seam['direct_p50_ns']:.0f}ns | "
          f"CP3.5 guarded p50={seam['guarded_p50_ns']:.0f}ns "
          f"(+{seam['guard_overhead_p50_ns']:.0f}ns) | "
          f"CP3 try/except+**kwargs p50={seam['wrapped_p50_ns']:.0f}ns "
          f"(+{seam['wrapper_overhead_p50_ns']:.0f}ns)")
    print(f"               the guarded shape IS what the arms below run; the "
          f"wrapper is priced but not wired")
    print(f"  load before: {load_before}")
    print()
    if reps <= 0:
        # Calibration-only run. Useful on its own: these four numbers are the
        # floor and the units of everything else, and they are cheap.
        return {"summary": {"calibration_only": True, "clock_pair": clock,
                            "path_compare": pathcmp, "seam_wrapper": seam,
                            "load_before": load_before,
                            "verdict": "CALIBRATION ONLY -- NO GATE VERDICT"},
                "runs": []}

    # Warmup: page cache, import-time laziness, the closer thread's first
    # spawn. Discarded, and stated as discarded.
    print("  [warmup, discarded]", flush=True)
    cp5_one_run(n=min(n, 5000), arm="null_a",
                max_segment_records=max_segment_records, markets=markets)

    runs = []
    header = (f"{'rep':>4} {'arm':>7} {'ev/s':>9} {'ns/ev':>8} "
              f"{'app p50':>8} {'app p95':>9} {'app p99':>9} "
              f"{'instr p50':>10} {'instr p95':>10} {'rot':>4}")
    print(header)
    for rep in range(reps):
        order = ["real", "null_a", "null_b"]
        rng.shuffle(order)
        for arm in order:
            r = cp5_one_run(n=n, arm=arm,
                            max_segment_records=max_segment_records,
                            markets=markets)
            r["rep"] = rep
            r["order_index"] = order.index(arm)
            # Load is sampled PER RUN, not once for the session. A contended
            # host inflates both arms, and it does not inflate them equally
            # over time -- the record of what the machine was doing during
            # each individual arm is what makes the paired design auditable
            # rather than merely asserted.
            r["load"] = _load_snapshot()
            runs.append(r)
            print(f"{rep:>4} {arm:>7} {r['ev_per_s']:>9.0f} "
                  f"{r['ns_per_event']:>8.0f} "
                  f"{r['append_p50_ns']:>8.0f} {r['append_p95_ns']:>9.0f} "
                  f"{r['append_p99_ns']:>9.0f} {r['instr_p50_ns']:>10.0f} "
                  f"{r['instr_p95_ns']:>10.0f} {r['rotations']:>4}",
                  flush=True)
    load_after = _load_snapshot()

    by = {a: [r for r in runs if r["arm"] == a]
          for a in ("real", "null_a", "null_b")}

    # PAIRED differences, within a repetition. This is the headline estimator:
    # anything that moves a whole repetition -- a background build, a thermal
    # step, page-cache state -- moves all three arms together and cancels.
    e1_signal, e2_signal, e3_signal = [], [], []
    e1_floor, e2_floor, e3_floor = [], [], []
    for rep in range(reps):
        real = by["real"][rep]
        na, nb = by["null_a"][rep], by["null_b"][rep]
        null_instr = (na["instr_p50_ns"] + nb["instr_p50_ns"]) / 2
        null_nsev = (na["ns_per_event"] + nb["ns_per_event"]) / 2
        null_cpu = (na["cpu_ns_per_event"] + nb["cpu_ns_per_event"]) / 2
        e1_signal.append(real["instr_p50_ns"] - null_instr)
        e2_signal.append(real["ns_per_event"] - null_nsev)
        e3_signal.append(real["cpu_ns_per_event"] - null_cpu)
        e1_floor.append(nb["instr_p50_ns"] - na["instr_p50_ns"])
        e2_floor.append(nb["ns_per_event"] - na["ns_per_event"])
        e3_floor.append(nb["cpu_ns_per_event"] - na["cpu_ns_per_event"])

    # Denominator: the append cost the overhead is a percentage OF. Taken from
    # the null arms, because dividing by an inflated append would flatter the
    # ratio -- the arm with the overhead in it must not also set the scale.
    null_runs = by["null_a"] + by["null_b"]
    append_p50_null = sum(r["append_p50_ns"] for r in null_runs) / len(null_runs)
    append_mean_null = sum(r["append_mean_ns"] for r in null_runs) / len(null_runs)
    nsev_null = sum(r["ns_per_event"] for r in null_runs) / len(null_runs)
    cpu_nsev_null = sum(r["cpu_ns_per_event"] for r in null_runs) / len(null_runs)

    e1 = _mean_ci95(e1_signal)
    e2 = _mean_ci95(e2_signal)
    e3 = _mean_ci95(e3_signal)
    f1 = _mean_ci95([abs(x) for x in e1_floor])
    f2 = _mean_ci95([abs(x) for x in e2_floor])
    f3 = _mean_ci95([abs(x) for x in e3_floor])

    summary = {
        "n": n, "reps": reps, "seed": seed,
        "max_segment_records": max_segment_records,
        "clock_pair": clock,
        "path_compare": pathcmp,
        "seam_wrapper": seam,
        "load_before": load_before, "load_after": load_after,
        "load_1m_over_runs": sorted(
            r["load"].get("load_1m") for r in runs
            if r["load"].get("load_1m") is not None),
        "append_p50_ns_null": append_p50_null,
        "append_mean_ns_null": append_mean_null,
        "ns_per_event_null": nsev_null,
        "cpu_ns_per_event_null": cpu_nsev_null,
        "e1_overhead_ns": e1[0], "e1_ci95_ns": e1[1],
        "e2_overhead_ns": e2[0], "e2_ci95_ns": e2[1],
        "e3_overhead_ns": e3[0], "e3_ci95_ns": e3[1],
        "noise_floor_e1_ns": f1[0], "noise_floor_e1_ci95_ns": f1[1],
        "noise_floor_e2_ns": f2[0], "noise_floor_e2_ci95_ns": f2[1],
        "noise_floor_e3_ns": f3[0], "noise_floor_e3_ci95_ns": f3[1],
        "e1_pct_of_append_p50": 100.0 * e1[0] / append_p50_null,
        "e2_pct_of_append_p50": 100.0 * e2[0] / append_p50_null,
        "e3_pct_of_append_p50": 100.0 * e3[0] / append_p50_null,
        "e2_pct_of_producer_step": 100.0 * e2[0] / nsev_null,
    }

    def band(label, arm_key, field):
        vals = [r[field] for r in by[arm_key]]
        m, ci, _ = _mean_ci95(vals)
        return f"{label:>10} {m:>10.0f} +/- {ci:<8.0f}"

    print()
    print("  APPEND LATENCY DISTRIBUTION (ns), mean over reps +/- 95% CI")
    print(f"  {'arm':>8} {'p50':>20} {'p95':>20} {'p99':>20}")
    for arm in ("real", "null_a", "null_b"):
        vals = {q: _mean_ci95([r[f"append_{q}_ns"] for r in by[arm]])
                for q in ("p50", "p95", "p99")}
        print(f"  {arm:>8} " + " ".join(
            f"{vals[q][0]:>11.0f} +/-{vals[q][1]:<6.0f}"
            for q in ("p50", "p95", "p99")))
    print()
    print("  PRODUCER-STEP LATENCY (ns, step1..step5 inclusive)")
    print(f"  {'arm':>8} {'p50':>20} {'p95':>20} {'p99':>20}")
    for arm in ("real", "null_a", "null_b"):
        vals = {q: _mean_ci95([r[f"unit_{q}_ns"] for r in by[arm]])
                for q in ("p50", "p95", "p99")}
        print(f"  {arm:>8} " + " ".join(
            f"{vals[q][0]:>11.0f} +/-{vals[q][1]:<6.0f}"
            for q in ("p50", "p95", "p99")))
    print()
    print(f"  THROUGHPUT (events/s): "
          + ", ".join(f"{a}={_mean_ci95([r['ev_per_s'] for r in by[a]])[0]:.0f}"
                      f"+/-{_mean_ci95([r['ev_per_s'] for r in by[a]])[1]:.0f}"
                      for a in ("real", "null_a", "null_b")))
    print()
    print("  ESTIMATORS (paired, per repetition)")
    for lbl, est, flr in (("E1 direct    ", e1, f1),
                          ("E2 throughput", e2, f2),
                          ("E3 cpu-time  ", e3, f3)):
        print(f"    {lbl} : {est[0]:+10.1f} ns/event  95% CI +/-{est[1]:.1f}"
              f"   noise floor {flr[0]:.1f} +/-{flr[1]:.1f}")
    print()
    print(f"  DENOMINATOR: append p50 (null arms) = {append_p50_null:.0f} ns; "
          f"append mean = {append_mean_null:.0f} ns; "
          f"producer step = {nsev_null:.0f} ns")
    print(f"    E1 as % of append p50      : "
          f"{summary['e1_pct_of_append_p50']:+.2f}%")
    print(f"    E2 as % of append p50      : "
          f"{summary['e2_pct_of_append_p50']:+.2f}%")
    print(f"    E3 as % of append p50      : "
          f"{summary['e3_pct_of_append_p50']:+.2f}%")
    print(f"    E2 as % of producer step   : "
          f"{summary['e2_pct_of_producer_step']:+.2f}%")
    print(f"  CPU per event (null arms)  : {cpu_nsev_null:.0f} ns "
          f"(vs {nsev_null:.0f} ns wall -- the gap is the contended host)")
    print(f"  load after: {load_after}")

    # -- the verdict, computed, not asserted ---------------------------------
    #
    # AN ESTIMATOR THAT CANNOT RESOLVE THE EFFECT DOES NOT GET TO SET THE
    # BOUND. Each estimator is first asked whether it resolved anything at
    # all: its point estimate has to clear BOTH its own 95% CI and the
    # null-vs-null noise floor of the same statistic. An unresolved estimator
    # contributes only its RESOLUTION LIMIT -- the smallest effect it could
    # have detected -- which is an upper bound on the overhead and is
    # reported as one. Letting an unresolved estimator's noisy point estimate
    # decide the gate would be reporting a number nobody should trust, in
    # either direction: it would fail the gate on noise as readily as it
    # would pass it on noise.
    def _assess(est, floor, label):
        mean, ci, _ = est
        fmean, fci, _ = floor
        limit = max(ci, fmean + fci)         # smallest detectable effect
        return {"label": label, "mean_ns": mean, "ci95_ns": ci,
                "noise_floor_ns": fmean, "noise_floor_ci95_ns": fci,
                "resolution_limit_ns": limit,
                "resolved": abs(mean) > limit,
                # For a resolved estimator this is the measured upper bound;
                # for an unresolved one it is the detection limit, which is
                # still a true upper bound on what the effect can be.
                "upper_bound_ns": (mean + ci) if abs(mean) > limit else limit,
                "pct_of_append_p50": 100.0 * mean / append_p50_null,
                "upper_bound_pct_of_append_p50":
                    100.0 * ((mean + ci) if abs(mean) > limit else limit)
                    / append_p50_null}

    a1 = _assess(e1, f1, "E1 direct")
    a2 = _assess(e2, f2, "E2 throughput")
    a3 = _assess(e3, f3, "E3 cpu-time")
    summary["estimators"] = [a1, a2, a3]

    resolved = [a for a in (a1, a2, a3) if a["resolved"]]
    if resolved:
        # The binding number is the LARGEST upper bound among the estimators
        # that actually resolved the effect -- the most pessimistic thing the
        # rig is able to say, not the most flattering.
        binding = max(resolved, key=lambda a: a["upper_bound_pct_of_append_p50"])
        basis = f"{binding['label']} (resolved)"
    else:
        # Nothing resolved. The tightest resolution limit is then the whole
        # result: "the overhead is below X", stated with the floor that makes
        # X mean something.
        binding = min((a1, a2, a3),
                      key=lambda a: a["upper_bound_pct_of_append_p50"])
        basis = f"{binding['label']} (UNRESOLVED -- reported as an upper bound)"

    upper_pct = binding["upper_bound_pct_of_append_p50"]
    summary["binding_estimator"] = binding["label"]
    summary["binding_basis"] = basis
    summary["upper_bound_pct_of_append_p50"] = upper_pct
    summary["any_resolved"] = bool(resolved)

    # "A small single-digit percentage of append cost" (section 9). 10% is the
    # boundary of "single digit"; the threshold is stated here so the verdict
    # is a computation against a written rule rather than a judgement made
    # after seeing the number.
    if upper_pct < 10.0:
        verdict = "GATE PASSED"
        clause = "inside"
    else:
        verdict = "GATE FAILED"
        clause = "outside"
    why = (f"upper 95% bound of the instrumentation overhead is "
           f"{upper_pct:.2f}% of append p50 ({append_p50_null:.0f} ns), "
           f"{clause} the small-single-digit-percent requirement; basis: "
           f"{basis}")
    if not resolved:
        why += ("; NOTE no estimator resolved the effect above this rig's "
                "noise floor, so this is an upper bound, not a point estimate")
    summary["verdict"] = verdict
    summary["verdict_reason"] = why
    print()
    for a in (a1, a2, a3):
        state = "RESOLVED" if a["resolved"] else "UNRESOLVED"
        print(f"  {a['label']:>14}: {state}  point={a['mean_ns']:+.1f}ns "
              f"resolution limit={a['resolution_limit_ns']:.1f}ns  "
              f"upper bound={a['upper_bound_pct_of_append_p50']:.2f}% of append")
    print()
    print(f"  VERDICT: {verdict} -- {why}")
    return {"summary": summary, "runs": runs}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["close", "cp5"], default="close",
                    help="close = the original rotation-cost sweep; "
                         "cp5 = the instrumentation-overhead gate")
    ap.add_argument("--cp5-records", type=int, default=40000)
    ap.add_argument("--cp5-reps", type=int, default=8)
    ap.add_argument("--cp5-max-segment-records", type=int,
                    default=sg.DEFAULT_MAX_SEGMENT_RECORDS)
    ap.add_argument("--cp5-seed", type=int, default=20260814)
    ap.add_argument("--cp5-markets", type=int, default=8)
    ap.add_argument("--records", type=int, nargs="*",
                    default=[1000, 5000, 13000, 20000])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--commit-to-head", choices=["true", "false", "both"],
                    default="both")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    if args.mode == "cp5":
        out = run_cp5(n=args.cp5_records, reps=args.cp5_reps,
                      max_segment_records=args.cp5_max_segment_records,
                      seed=args.cp5_seed, markets=args.cp5_markets)
        if args.json:
            Path(args.json).write_text(json.dumps(out, indent=2))
            print(f"\nwrote {args.json}")
        # A failed gate is a failed run. It exits non-zero so a wrapper cannot
        # record "the benchmark ran" as "the benchmark passed". A
        # calibration-only run asserts nothing and so cannot pass either.
        verdict = out["summary"]["verdict"]
        if verdict.startswith("CALIBRATION ONLY"):
            return 0
        return 0 if verdict == "GATE PASSED" else 1

    modes = ({"true": [True], "false": [False], "both": [False, True]}
             [args.commit_to_head])
    rows = []
    print(f"{'records':>8} {'cth':>6} {'append ev/s':>12} {'close wall':>11} "
          f"{'close cpu':>10} {'wall ms/1k':>11} {'cpu ms/1k':>10}")
    for n in args.records:
        for cth in modes:
            for _ in range(args.repeat):
                r = one_trial(n=n, commit_to_head=cth)
                rows.append(r)
                print(f"{r['records']:>8} {str(cth):>6} "
                      f"{r['append_ev_per_s']:>12.0f} "
                      f"{r['close_wall_s']:>10.3f}s "
                      f"{r['close_cpu_s']:>9.3f}s "
                      f"{1000 * r['close_wall_s'] / n * 1000:>11.1f} "
                      f"{1000 * r['close_cpu_s'] / n * 1000:>10.1f}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
