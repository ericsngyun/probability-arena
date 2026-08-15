#!/usr/bin/env python3
"""One real-fault trial, run as a subprocess.

Not a pytest module — invoked via `subprocess.run([sys.executable, __file__,
...], timeout=...)` from `test_kalshi_async_accounting_harness_001.py`, so a
wedged trial (a real deadlock, not a settrace artifact — see `watchdog.py`
for why those two are different) cannot hang the suite, and a hang is
distinguishable from a returned result: the parent sees `TimeoutExpired`
rather than a JSON line.

Two independent real-fault mechanisms, selected by `--mode`:

  sigint    a bomber thread sends real `SIGINT` to this process a small
            number of times while a submitter thread calls `submit()` in a
            loop. This is what both reviewers used.
  asyncexc  a bomber thread calls `ctypes.pythonapi.PyThreadState_SetAsyncExc`
            against the submitting thread's id instead of using a real
            signal — a second, independent delivery mechanism for the same
            fault class.

`--target segment` drives the real `app.realtime.segment.SegmentWriter`.
`--target shim:<name>` drives one of `reference_shim.VARIANTS` instead, so
the SAME real-fault campaign can be pointed at the good control and the
four broken variants — required deliverable (a)/(b)/(c) of the harness spec.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from app.realtime import archive_head as ah          # noqa: E402
from app.realtime import canonical as cn             # noqa: E402
from app.realtime import segment as sg               # noqa: E402
from harness_async_accounting import reference_shim as rs  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
ENV = "demo"


def fields(i: int) -> dict:
    return {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": "KXA", "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {"price_dollars": "0.5100", "side": "no"},
        "normalized_event": {"raw_price_units": 5100},
    }


def deliver_asyncexc(tid: int, exc_type) -> None:
    ctypes.pythonapi.PyThreadState_SetAsyncExc.argtypes = (
        ctypes.c_ulong, ctypes.py_object)
    n = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(tid), ctypes.py_object(exc_type))
    if n > 1:  # pragma: no cover - defensive cleanup, should not happen
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)


def run_segment(root: Path, seg_id: str, n_submits: int, progress: list) -> tuple:
    ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
    w = sg.SegmentWriter(root, environment=ENV, segment_id=seg_id,
                         partition_identity="p", commit_to_head=False)

    def submit_loop():
        caught = 0
        for i in range(n_submits):
            # A8-SIGINT-CLASSIFY-002: publish loop PROGRESS so the bomber can
            # gate on it instead of on a wall clock. A single store into a
            # preallocated list slot is one bytecode under the GIL; it costs
            # nothing measurable and cannot itself be torn.
            progress[0] = i
            try:
                w.submit(fields(i))
            except BaseException:
                caught += 1
        return caught

    return w, submit_loop, w.accounting


def run_shim(variant: str, n_submits: int, progress: list) -> tuple:
    cls = rs.VARIANTS[variant]
    s = cls()

    def submit_loop():
        caught = 0
        for i in range(n_submits):
            progress[0] = i
            try:
                s.submit(i)
            except BaseException:
                caught += 1
        return caught

    return s, submit_loop, s.accounting


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=False)
    ap.add_argument("--target", default="segment",
                    help="'segment' or 'shim:<good|bad_a|bad_b|bad_c|bad_d>'")
    ap.add_argument("--segment-id", default="seg")
    ap.add_argument("--n-submits", type=int, default=20_000)
    ap.add_argument("--n-interrupts", type=int, default=2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--mode", choices=["sigint", "asyncexc"], default="sigint")
    ap.add_argument("--window-s", type=float, default=0.6,
                    help="RETAINED FOR CLI COMPATIBILITY, NO LONGER SCHEDULES "
                    "ANYTHING. Interrupts used to be scheduled by "
                    "`time.sleep(rng.uniform(0, window_s))`, i.e. by a wall "
                    "clock that knew nothing about how far the submit loop "
                    "had actually got. See A8-SIGINT-CLASSIFY-002 and "
                    "`bomber()` below for why that was replaced by "
                    "progress-gated delivery.")
    args = ap.parse_args()
    try:
        return _run(args)
    except BaseException as e:  # noqa: BLE001 - last-resort: never crash silently
        # A fault landing OUTSIDE everything this script otherwise guards
        # (argument parsing already done, but before/around thread setup)
        # still gets a JSON line instead of an opaque non-zero exit + a
        # KeyboardInterrupt traceback the parent has to special-case.
        print(json.dumps({"target": args.target, "mode": args.mode,
                          "top_level_fault": f"{type(e).__name__}: {e}"}))
        return 0


def _run(args) -> int:
    rng = random.Random(args.seed)
    caught_holder = {}

    progress = [-1]          # last submit index the loop has STARTED
    loop_done = threading.Event()

    if args.target == "segment":
        root = Path(args.root)
        obj, submit_loop, accounting = run_segment(root, args.segment_id,
                                                    args.n_submits, progress)
    elif args.target.startswith("shim:"):
        obj, submit_loop, accounting = run_shim(args.target.split(":", 1)[1],
                                                 args.n_submits, progress)
    else:
        raise SystemExit(f"unknown --target {args.target!r}")

    # CRITICAL: Python delivers a real signal's handler ONLY on the main
    # thread, regardless of which OS thread the kernel notified — so the
    # submissions being bombed must run on THIS (main) thread, not a spawned
    # one. `PyThreadState_SetAsyncExc` can target any thread, but is aimed
    # at the main thread too here so the two mechanisms are measured on the
    # same footing. An earlier version of this script ran submissions on a
    # background thread and bombed the main thread instead — every trial
    # silently missed, because a signal delivered while the main thread sat
    # in `Thread.join()` raised there, not inside any `submit()` call. That
    # failure mode is itself worth recording: it is exactly the kind of
    # "the fault landed somewhere uninteresting" false negative a harness
    # has to rule out, not just the true negatives.
    main_tid = threading.get_ident()
    fired_at = []

    # A8-SIGINT-CLASSIFY-002: PROGRESS-GATED DELIVERY, NOT WALL-CLOCK.
    #
    # The bomber used to `time.sleep(rng.uniform(0, window_s))`. That schedule
    # is a pure function of a wall clock and knows nothing about how far the
    # submit loop has actually got, so whether an interrupt landed in the
    # region under test at all was a RACE between the bomber's sleeps and the
    # loop's duration. Measured on idle hardware: the loop ran 2.325 s and the
    # latest bomb fired at 1.142 s -- ~1.18 s of unearned margin. Erode that
    # margin (machine load, a faster loop, a wider window) and interrupts
    # start landing in `close()` instead, where they abort the un-deferred
    # read-only reconciliation and the trial legitimately does not publish.
    # Measured calibration curve, idle, varying ONLY window_s:
    #     0.6 -> 5/5 published   0.9 -> 6/6   1.2 -> 5/6   1.8 -> 4/6
    # -- publication falling off exactly as the bomb window crosses the loop's
    # 2.325 s duration. That is a property of the harness's clock, not of the
    # writer, and it is what made this test flaky in BOTH directions.
    #
    # Each interrupt is now pinned to a SUBMIT INDEX drawn from the same
    # seeded `rng`, and the bomber waits for the loop to actually reach it.
    # This is strictly stronger than the wall clock it replaces: it also
    # closes the false negative the comment above warns about -- previously an
    # unknown fraction of bombs missed `submit()` entirely and the trial had
    # no way to say so, which is precisely how a fault campaign silently
    # stops testing anything. `bombs_missed` is now reported, so a miss is a
    # visible datum rather than an invisible pass.
    #
    # Targets are confined to [5%, 85%] of the loop. The 15% tail is RUNWAY:
    # `os.kill` only sets a flag, and CPython runs the handler at the main
    # thread's next bytecode-boundary check, so the handler lands a few
    # bytecodes after the target -- and, if it lands inside `submit()`'s
    # deferred commitment region, at that region's exit. 3,000 remaining
    # iterations is many orders of magnitude more runway than either needs,
    # and it is denominated in LOOP PROGRESS rather than seconds, so machine
    # load stretches the runway and the delivery equally. That is the whole
    # point: the margin can no longer be eroded by load.
    lo = max(1, int(0.05 * args.n_submits))
    hi = max(lo + 1, int(0.85 * args.n_submits))
    bomb_targets = sorted(rng.randrange(lo, hi) for _ in range(args.n_interrupts))
    fired_at_index = []
    bombs_missed = 0

    def bomber():
        nonlocal bombs_missed
        for target in bomb_targets:
            while progress[0] < target:
                if loop_done.is_set():
                    # The loop finished before reaching this target. Under
                    # progress gating this should be unreachable (targets are
                    # capped at 85% of the loop); record it rather than fire
                    # a bomb into `close()` and call the result a finding.
                    bombs_missed += 1
                    return
                time.sleep(0.0002)
            at = progress[0]
            if args.mode == "sigint":
                os.kill(os.getpid(), signal.SIGINT)
            else:
                deliver_asyncexc(main_tid, KeyboardInterrupt)
            fired_at.append(time.monotonic())
            fired_at_index.append(at)

    bomb_thread = threading.Thread(target=bomber, name="bomber", daemon=True)
    bomb_thread.start()

    # A stray fault landing AFTER the submission loop (e.g. while this
    # process is joining the bomber thread, or inside `close()` itself) is a
    # real and reportable outcome — an operator's Ctrl-C during shutdown,
    # not during ingestion — but it must not be allowed to crash this
    # trial's own bookkeeping the way an uncaught `KeyboardInterrupt`
    # terminates the whole process. Everything from here on is wrapped so
    # such a fault is CLASSIFIED (`late_fault`) instead of losing the trial.
    late_fault = None
    try:
        caught_holder["caught"] = submit_loop()
    finally:
        # Release any bomber still waiting on a target the loop will now never
        # reach, so it records a miss instead of stalling the trial. In the
        # `finally` so an interrupt escaping the loop cannot strand it.
        loop_done.set()
    pre_close = accounting.to_dict()

    close_ok = True
    close_error = None
    record_count = None
    try:
        if args.target == "segment":
            try:
                manifest = obj.close()
                record_count = manifest["record_count"]
            except BaseException as e:
                close_ok = False
                close_error = f"{type(e).__name__}: {e}"
        else:
            obj.drain_all()
            record_count = accounting.written
            close_ok = accounting.clean()
            if not close_ok:
                close_error = f"not clean: {accounting.to_dict()}"
    except BaseException as e:
        # A fault landed in THIS script's own bookkeeping around close(),
        # not inside the code under test. Record it as such rather than
        # letting it look like a `close()` failure.
        late_fault = f"{type(e).__name__} outside close(): {e}"

    try:
        # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1: the docstring above always
        # claimed this join was covered ("while this process is joining the
        # bomber thread... is wrapped"), but the code never actually wrapped
        # it -- a latent gap that stayed invisible while `close()` itself
        # took several seconds (the old queue-based writer's admission-seal
        # wait), which gave every bomb thread time to fire well before this
        # line was ever reached. The synchronous writer's `close()` returns
        # almost immediately, so a bomb can now genuinely still be pending
        # when this line runs; without this `try`, that interrupt propagated
        # as an uncaught `KeyboardInterrupt` and crashed the whole trial
        # script instead of being classified.
        bomb_thread.join(timeout=0.5)  # best-effort; it is a daemon thread
    except BaseException as e:
        late_fault = late_fault or f"{type(e).__name__} joining bomber: {e}"

    # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8: STRUCTURED OUTCOME FIELDS.
    # The parent test used to have nothing to assert on except `close_ok`
    # (which it did not assert) and a substring search of `close_error`
    # (which could not match anything `app/` actually emits). These four
    # fields are what the properties are actually ABOUT: how many records
    # the writer BOOKED, how many are physically READABLE, and what state
    # the writer ended in. `on_disk_records > written` is the identity
    # violation -- durable evidence the writer does not know it has, which
    # means `_ordinal`/`_prev_digest` did not advance and the chain is
    # broken from that point on.
    on_disk_records = None
    state = None
    post_close = None
    try:
        if args.target == "segment":
            post_close = accounting.to_dict()
            state = obj.state.value
            on_disk_records = len(sg.read_segment_records(obj.events_path))
    except BaseException as e:                 # noqa: BLE001 - diagnostic only
        late_fault = late_fault or f"{type(e).__name__} reading back: {e}"

    result = {
        "target": args.target,
        "mode": args.mode,
        "n_submits": args.n_submits,
        "n_interrupts": args.n_interrupts,
        "seed": args.seed,
        "n_fired": len(fired_at),
        # A8-SIGINT-CLASSIFY-002 diagnostics: which submit indices each
        # interrupt was AIMED at (deterministic in `--seed`) and where the
        # loop actually was when it was delivered. A future failure is then
        # diagnosable from the trial's own output -- "did the bomb land in the
        # region under test at all?" is answerable without re-deriving the
        # timing study that produced this design.
        "bomb_targets": bomb_targets,
        "fired_at_index": fired_at_index,
        "bombs_missed": bombs_missed,
        "caught_in_loop": caught_holder.get("caught"),
        "late_fault": late_fault,
        "pre_close_accounting": pre_close,
        "post_close_accounting": post_close,
        "close_ok": close_ok,
        "close_error": close_error,
        "record_count": record_count,
        "on_disk_records": on_disk_records,
        "state": state,
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
