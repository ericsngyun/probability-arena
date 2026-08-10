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


def run_segment(root: Path, seg_id: str, n_submits: int) -> tuple:
    ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
    w = sg.SegmentWriter(root, environment=ENV, segment_id=seg_id,
                         partition_identity="p", commit_to_head=False)

    def submit_loop():
        caught = 0
        for i in range(n_submits):
            try:
                w.submit(fields(i))
            except BaseException:
                caught += 1
        return caught

    return w, submit_loop, w.accounting


def run_shim(variant: str, n_submits: int) -> tuple:
    cls = rs.VARIANTS[variant]
    s = cls()

    def submit_loop():
        caught = 0
        for i in range(n_submits):
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
                    help="each interrupt's delay is drawn uniformly from "
                    "[0, window-s]; calibrated so a --n-submits campaign's "
                    "wall-clock duration comfortably covers the window a "
                    "background bomber thread needs to actually get "
                    "scheduled and land inside the submit loop (measured: a "
                    "tight sub-millisecond submit loop starves a daemon "
                    "bomber thread of a scheduling slot entirely)")
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

    if args.target == "segment":
        root = Path(args.root)
        obj, submit_loop, accounting = run_segment(root, args.segment_id,
                                                    args.n_submits)
    elif args.target.startswith("shim:"):
        obj, submit_loop, accounting = run_shim(args.target.split(":", 1)[1],
                                                 args.n_submits)
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

    def bomber():
        for _ in range(args.n_interrupts):
            time.sleep(rng.uniform(0.0, args.window_s))
            if args.mode == "sigint":
                os.kill(os.getpid(), signal.SIGINT)
            else:
                deliver_asyncexc(main_tid, KeyboardInterrupt)
            fired_at.append(time.monotonic())

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
    caught_holder["caught"] = submit_loop()
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

    bomb_thread.join(timeout=0.5)  # best-effort; it is a daemon thread

    result = {
        "target": args.target,
        "mode": args.mode,
        "n_submits": args.n_submits,
        "n_interrupts": args.n_interrupts,
        "seed": args.seed,
        "n_fired": len(fired_at),
        "caught_in_loop": caught_holder.get("caught"),
        "late_fault": late_fault,
        "pre_close_accounting": pre_close,
        "close_ok": close_ok,
        "close_error": close_error,
        "record_count": record_count,
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
