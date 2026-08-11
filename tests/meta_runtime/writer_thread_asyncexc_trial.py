"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1/A7 -- RETIRED.

This script used to bombard the SegmentWriter background writer thread's own
OS thread id with real `ctypes.pythonapi.PyThreadState_SetAsyncExc`
deliveries, as a subprocess trial run under `tests/meta_runtime.
parent_timeout.run_with_parent_timeout` -- a REALISTIC_SIGNAL_FAULT
measurement distinct from `tests/harness_async_accounting/fault_trial.py`'s
main/producer-thread-only `sigint`/`asyncexc` modes, because a real
asynchronous exception can only be targeted at an arbitrary background
thread's OWN OS thread id, and the writer thread was such a thread.

There is no writer thread any more (KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1):
`SegmentWriter.submit` canonicalises, chains and writes one record entirely
on the CALLER's own thread, inside a single lock-held call. The distinction
this script existed to measure -- "the main/producer thread" vs. "the
background writer thread" -- has collapsed, because they are now the same
thread for every `submit()` call. `tests/harness_async_accounting/
fault_trial.py`'s existing main-thread `sigint`/`asyncexc` modes already
exercise the ONLY thread `submit()` now ever runs on, so they cover what this
script used to cover a second, thread-distinct way -- there is no longer a
second thread left to give a second, independent measurement.
"""

from __future__ import annotations
