#!/usr/bin/env python3
"""KALSHI-ARCHIVE-CORE-REMEDIATION-003B A3 -- one REAL asynchronous-exception
trial, targeted at the WRITER thread specifically, run as a subprocess.

Not a pytest module -- invoked via `tests/meta_runtime/parent_timeout.
run_with_parent_timeout`, so a wedged trial (a real deadlock -- see
`tests/harness_async_accounting/watchdog.py`'s docstring for the documented
"a sys.settrace fault landing on a `with lock:` `__exit__` can leak the
lock" class, which is a MECHANISM artifact, not evidence about production)
cannot hang the calling suite.

WHY THIS IS A DIFFERENT MEASUREMENT FROM `fault_trial.py`. That module's
`sigint`/`asyncexc` modes both target the MAIN (producer) thread -- real
`SIGINT` can only be delivered there at all, and its own `asyncexc` mode
follows the same convention for a fair comparison. Every writer-thread
window this milestone's A1/A2 harnesses reproduce (`tests/meta_runtime/
independent_accounting.py`, `tests/meta_runtime/queue_gap_locator.py`) is
reached via `sys.settrace` -- a SYNTHETIC_STATE_MACHINE_FAULT, deterministic
and exact, but not evidence of how often a REAL asynchronous exception (the
only delivery mechanism that can target an arbitrary background thread by
OS thread id, via `ctypes.pythonapi.PyThreadState_SetAsyncExc`) actually
lands inside that same narrow window. This script measures that directly:
it bombs the WRITER thread's OWN thread id, not the main thread's.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.realtime import archive_head as ah          # noqa: E402
from app.realtime import canonical as cn             # noqa: E402
from app.realtime import segment as sg               # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
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
    if n > 1:  # pragma: no cover - defensive cleanup
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--segment-id", default="seg")
    ap.add_argument("--n-submits", type=int, default=300)
    ap.add_argument("--n-bombs", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--window-s", type=float, default=0.05)
    args = ap.parse_args()
    try:
        return _run(args)
    except BaseException as e:  # noqa: BLE001 - always report, never crash silently
        print(json.dumps({"top_level_fault": f"{type(e).__name__}: {e}"}))
        return 0


def _run(args) -> int:
    root = Path(args.root)
    ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
    w = sg.SegmentWriter(root, environment=ENV, segment_id=args.segment_id,
                         partition_identity="p", commit_to_head=False)

    rng = random.Random(args.seed)
    fired_at: list = []

    def bomber():
        # `w._thread.ident` may briefly be None before the thread actually
        # starts running Python bytecode; the writer's own constructor
        # already started it synchronously, so this is a formality, not a
        # real race -- but guarded anyway rather than assumed.
        deadline = time.monotonic() + 2.0
        while w._thread.ident is None and time.monotonic() < deadline:
            time.sleep(0.001)
        tid = w._thread.ident
        for _ in range(args.n_bombs):
            time.sleep(rng.uniform(0.0, args.window_s))
            if tid is not None:
                deliver_asyncexc(tid, KeyboardInterrupt)
                fired_at.append(time.monotonic())

    bomb_thread = threading.Thread(target=bomber, name="writer-bomber", daemon=True)
    bomb_thread.start()

    caught = 0
    for i in range(args.n_submits):
        try:
            w.submit(fields(i))
        except BaseException:
            caught += 1

    bomb_thread.join(timeout=1.0)

    pre_close = w.accounting.to_dict()
    writer_thread_died_early = not w._thread.is_alive()
    writer_error = repr(w._writer_error) if w._writer_error is not None else None

    close_ok = True
    close_error = None
    record_count = None
    try:
        manifest = w.close()
        record_count = manifest["record_count"]
    except BaseException as e:
        close_ok = False
        close_error = f"{type(e).__name__}: {e}"

    # "landed" = the async exception provably reached the writer thread and
    # killed it (state INVALID with a writer_error, before close() ever ran
    # its own join) -- NOT merely that `caught` counted something on the
    # producer side (fault_trial.py's own metric, a different thread).
    landed = writer_thread_died_early and writer_error is not None

    result = {
        "n_submits": args.n_submits, "n_bombs": args.n_bombs,
        "seed": args.seed, "n_fired": len(fired_at),
        "caught_in_submit_loop": caught,
        "writer_thread_died_early": writer_thread_died_early,
        "writer_error": writer_error,
        "landed": landed,
        "pre_close_accounting": pre_close,
        "close_ok": close_ok, "close_error": close_error,
        "record_count": record_count,
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
