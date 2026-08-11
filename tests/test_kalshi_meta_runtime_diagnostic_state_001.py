"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1/A7 -- diagnostic state vs
canonical state, RETIRED.

This file inventoried every queue-era variable that could be either
AUTHORITATIVE (agrees with durable/filesystem state by construction) or
merely DIAGNOSTIC (a live, in-memory belief that could desynchronise from
what actually happened), and reproduced the worst instance: `_inflight`
never reaching zero again after a fault landed in its decrement, permanently
bricking the partition -- `close()` failing identically forever, the
`writer.lock` flock never released, and `verify_archive` reporting VALID
with empty reasons over a segment that was simply invisible to it.

`_inflight`, `_admission`, `_sealed`, `_seal_admissions`, the background
writer thread, and the queue it drained do not exist any more (KALSHI-
ARCHIVE-REPLAY-INTEGRITY-001 A1). Re-deriving the same inventory against the
new architecture is short enough to state directly, rather than needing a
360-line harness:

  `self.state` (SegmentWriter)
      CAN BLOCK CLOSE: N/A in the old sense -- `close()` now cannot be
      permanently blocked by a producer at all. `submit()` and `close()`
      are mutually exclusive on `self._lock` (see `SegmentWriter.submit`'s
      docstring): a `close()` call either runs to completion once it
      acquires the lock, or -- if a `submit()` raised while holding it --
      observes `self.state is SegmentState.INVALID` and raises a typed
      `SegmentError` that still releases the flock (`_release_lock()` runs
      on every exception path out of `_close_locked`). There is no
      "waiting forever for a counter that nothing decrements" state left to
      reach, because there is no counter separate from `self.state` itself
      for a fault to desynchronise from.
      AUTHORITATIVE: yes, by construction -- it is read and written under
      the SAME lock every `submit()`/`close()` call takes.

  `WriterAccounting.{attempted, rejected_before_accept, written,
  failed_after_accept}`
      Unchanged in kind from before: `attempted`/`rejected_before_accept`
      stay DIAGNOSTIC (excluded from `clean()`/`close()`'s gate, see
      `admission_holds()`'s docstring); `written`/`failed_after_accept`
      stay the AUTHORITATIVE pair `clean()` gates on. What changed is
      `pending`, which is now ALWAYS 0 (a read-only property, not a
      mutable field -- see `WriterAccounting.pending`'s docstring) rather
      than something `_measure_pending()` had to reconcile from a queue at
      close: there is no asynchronous gap left for an accepted-but-
      undelivered item to sit in.

  writer-thread liveness
      RETIRED outright: there is no writer thread. A write failure is
      caught and reflected in `self.state`/`self.healthy` synchronously,
      in the SAME call that experienced it (proven in
      `tests/test_kalshi_meta_runtime_independent_accounting_001.py::
      TestSubmitFailureIsImmediatelyVisible`), not discovered later by
      polling a thread's liveness.

  queue counters (`_queue.qsize()`, `queue_high_water`)
      RETIRED outright: there is no queue.

The worst-case failure mode this file used to reproduce (a diagnostic
counter permanently bricking `close()` while durable evidence sits
unreachable) has no synchronous-era analogue to reproduce: `self.state`
IS the lock-protected fact `close()` acts on, not a second, separately-
maintained belief about it.
"""

from __future__ import annotations
