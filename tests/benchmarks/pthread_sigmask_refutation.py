#!/usr/bin/env python3
"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- the `pthread_sigmask` refutation,
committed as a runnable script.

WHY THIS EXISTS AS A FILE. `_DeferCommitSignals`' module comment in
`app/realtime/segment.py` rejects `signal.pthread_sigmask` as the mechanism
for deferring `SIGINT`/`SIGTERM` across the commitment region, and justifies
that rejection with a MEASUREMENT. The measurement lived only as prose in a
code comment -- the exact pattern this milestone has spent several rounds
eliminating -- so the number could not be checked, could not be regressed,
and (as an independent reviewer found) was wrong: the comment claimed
`300/300` raised inside the masked region for the "bomber started BEFORE the
mask" arm. The direction of the finding is right and load-bearing; the
magnitude was not.

WHAT IT MEASURES. Two arms, differing only in WHEN the bomber thread is
started relative to the main thread's `pthread_sigmask(SIG_BLOCK, ...)`:

  bomber started AFTER the mask   -- `threading` hands a new thread the
                                     CREATING thread's signal mask at
                                     `start()`, so the bomber inherits the
                                     block. Nothing is delivered anywhere.
                                     This arm is THE TRAP: it makes the mask
                                     look like it works.
  bomber started BEFORE the mask  -- the real shape of any collector (a
                                     websocket reader, an asyncio loop, a
                                     metrics thread was already running). The
                                     kernel is free to hand a
                                     PROCESS-DIRECTED signal to that UNMASKED
                                     thread; CPython's C handler trips the
                                     flag from there, and the MAIN thread
                                     raises inside the "masked" region
                                     anyway.

A round counts as `raised_inside` when a `KeyboardInterrupt` is raised on the
main thread between `SIG_BLOCK` and `SIG_SETMASK`-back. The claim under test
is only that the second arm's rate is materially non-zero -- masking the
writer thread cannot stop a signal the kernel may hand to any other thread --
NOT that it is 100%.

USAGE

    python tests/benchmarks/pthread_sigmask_refutation.py
    python tests/benchmarks/pthread_sigmask_refutation.py --rounds 300 --json out.json

Exits non-zero if the "before" arm raises inside ZERO times, i.e. if the
refutation itself stops reproducing on this platform -- that would mean the
comment's design argument needs re-deriving, not quietly trusting.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import sys
import threading
import time

SIGSET = {signal.SIGINT, signal.SIGTERM}

# The Python-level handler is armed only across the two windows this harness
# actually measures. Outside them a delivered SIGINT is counted and dropped,
# so an ordinary between-rounds delivery cannot tear the run down and be
# mistaken for a finding. Arming/disarming does not weaken the measurement:
# what is under test is whether the handler runs AT ALL on the main thread
# while the mask is on, and inside the window it is fully live.
_ARMED = [False]
_SEEN_DISARMED = [0]


def _handler(signum, frame):
    if _ARMED[0]:
        raise KeyboardInterrupt
    _SEEN_DISARMED[0] += 1


def _spin(iterations: int) -> int:
    total = 0
    for i in range(iterations):
        total += i & 7
    return total


def one_round(*, start_bomber_inside: bool, spin: int, send_every_s: float):
    """One masked region. Returns (raised_inside, raised_after, sent)."""
    stop = threading.Event()
    sent = [0]

    def bomber():
        while not stop.is_set():
            os.kill(os.getpid(), signal.SIGINT)
            sent[0] += 1
            if send_every_s:
                time.sleep(send_every_s)

    t = threading.Thread(target=bomber, daemon=True)
    raised_inside = False
    raised_after = False
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    if not start_bomber_inside:
        t.start()
        # Let it get into its loop before the mask goes on, so "started
        # BEFORE" means what it says.
        try:
            time.sleep(0.002)
        except KeyboardInterrupt:
            pass
    try:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, SIGSET)
        _ARMED[0] = True
        if start_bomber_inside:
            t.start()
            try:
                time.sleep(0.002)
            except KeyboardInterrupt:          # cannot happen: masked
                raised_inside = True
        try:
            _spin(spin)
        except KeyboardInterrupt:
            raised_inside = True
        # Stop and JOIN the bomber while still masked, so no signal can be
        # sent after the mask comes off and land outside the accounting.
        stop.set()
        for _ in range(200):
            try:
                t.join(2.0)
                break
            except KeyboardInterrupt:
                raised_inside = True
    finally:
        _ARMED[0] = False
        stop.set()
        # Restoring the mask delivers whatever the kernel queued for us while
        # we were masked. It lands HERE, which is the whole point of a mask
        # and is not a defect -- counted separately as `raised_after`.
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        _ARMED[0] = True
        try:
            _spin(2000)
        except KeyboardInterrupt:
            raised_after = True
        _ARMED[0] = False
        t.join(2.0)
    return raised_inside, raised_after, sent[0]


def arm(*, start_bomber_inside: bool, rounds: int, spin: int,
        send_every_s: float) -> dict:
    inside = after = sent = 0
    for _ in range(rounds):
        try:
            i, a, s = one_round(start_bomber_inside=start_bomber_inside,
                                spin=spin, send_every_s=send_every_s)
        except KeyboardInterrupt:
            # Between rounds, outside any masked region: an ordinary,
            # expected delivery. Not evidence for or against the mask.
            continue
        inside += int(i)
        after += int(a)
        sent += s
    return {
        "arm": "bomber started AFTER the mask" if start_bomber_inside
               else "bomber started BEFORE the mask",
        "start_bomber_inside": start_bomber_inside,
        "rounds": rounds,
        "raised_inside": inside,
        "raised_after": after,
        "signals_sent": sent,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=300)
    p.add_argument("--spin", type=int, default=200_000)
    p.add_argument("--send-every-s", type=float, default=0.0002)
    p.add_argument("--json", default=None)
    args = p.parse_args()

    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        rows = [
            arm(start_bomber_inside=True, rounds=args.rounds, spin=args.spin,
                send_every_s=args.send_every_s),
            arm(start_bomber_inside=False, rounds=args.rounds, spin=args.spin,
                send_every_s=args.send_every_s),
        ]
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)

    print(f"platform: {platform.system().lower()} {platform.release()}  "
          f"python {sys.version.split()[0]}")
    print(f"{'arm':<32} {'rounds':>7} {'raised_inside':>14} "
          f"{'raised_after':>13} {'sent':>8}")
    for r in rows:
        print(f"{r['arm']:<32} {r['rounds']:>7} {r['raised_inside']:>14} "
              f"{r['raised_after']:>13} {r['signals_sent']:>8}")
    if args.json:
        from pathlib import Path
        Path(args.json).write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.json}")

    before = next(r for r in rows if not r["start_bomber_inside"])
    if before["raised_inside"] == 0:
        print("\nREFUTATION DID NOT REPRODUCE: the 'started BEFORE' arm never "
              "raised inside the masked region on this run. The design "
              "argument in segment.py's _DeferCommitSignals comment rests on "
              "this measurement and must be re-derived before it is trusted.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
