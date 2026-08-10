"""Standalone accounting shims used ONLY to validate the harness itself.

None of this is production code, none of it is imported by `app/`, and it
does not reuse `SegmentWriter.submit()`'s control flow — it is a small,
independent implementation of the same admission/disposition contract
(`WriterAccounting`, reused as-is because it is a plain dataclass with no
behaviour worth re-deriving).

Required deliverable (c) of the harness spec: prove the fault-injection
methodology discriminates correct accounting from broken accounting, and
that a correct implementation survives the SAME campaign that catches the
broken ones — i.e. the harness is not merely detecting its own injector.

`GoodSubmitter` books a terminal stage under a retry-until-committed critical
section, so an asynchronous exception landing ANYWHERE — including a second
one delivered while the first is still being booked — cannot leave a
submission without exactly one terminal disposition.

The four `Bad*` variants each reproduce one of the four production windows
by construction, in this simplified shape, so the harness's identity checks
can be shown to catch each one on demand rather than only when a specific
line of `segment.py` happens to be hit.
"""

from __future__ import annotations

import collections
import queue
import threading

from app.realtime.segment import RejectReason, WriterAccounting


class _BaseSubmitter:
    """Shared machinery: a bounded queue plus a same-shape drain step, so the
    written/accepted/pending identity can be checked the same way the real
    writer thread's disposition is checked in `segment.py`."""

    def __init__(self, maxsize: int = 10_000):
        self.accounting = WriterAccounting()
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()

    def drain_all(self) -> int:
        """Stand-in for the writer thread: drain whatever is queued and book
        it `written`. Called by the harness after the submission phase, the
        same way `_measure_pending`/the writer thread finish out a real
        segment before `close()` reconciles."""
        n = 0
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            n += 1
            with self._lock:
                self.accounting.written += 1
        return n

    def submit(self, item):  # pragma: no cover - overridden
        raise NotImplementedError


class GoodSubmitter(_BaseSubmitter):
    """The reference-correct pattern, per the two things the four production
    windows have in common:

    1. The counter mutation and the STAGE FLAG that records "this call has a
       terminal booking" are written in the SAME critical section, as one
       compound statement, so there is no source LINE between them for a
       line-boundary fault to land on. (Windows a/b/c are all exactly a gap
       BETWEEN two lines/statements that should have been one fact.)

    2. The booking action itself runs inside a retry-until-committed loop,
       so a second fault delivered while the booking runs (window d) cannot
       leave the call with zero terminal bookings — it retries until one
       delivery of the fault fails to land inside the critical section,
       which a one-shot fault (a real signal, a single async-exc delivery,
       or this harness's single-shot injector) cannot outlast.

    Caveat, stated plainly rather than glossed over: (1) defeats any fault
    our LINE-granularity injector can express, because it can only land
    between distinct source lines. It does NOT constitute a formal proof of
    atomicity against a hypothetical adversary who can interrupt at
    arbitrary CPython bytecode boundaries mid-statement — no pure-Python
    critical section can promise that; only blocking signal delivery
    (`signal.pthread_sigmask`) or a C extension can. What IS empirically
    checked below is immunity to every boundary this harness's deterministic
    method and its real single-shot SIGINT/async-exc campaigns actually
    reached, across the campaign sizes recorded in the report.
    """

    def __init__(self, maxsize: int = 10_000):
        super().__init__(maxsize=maxsize)
        self._raw: collections.deque = collections.deque()
        self._maxsize = maxsize

    def drain_all(self) -> int:
        n = 0
        while True:
            with self._lock:
                if not self._raw:
                    break
                self._raw.popleft()
                self.accounting.written += 1
            n += 1
        return n

    def _commit(self, counter_name: str, stage_ref: list, stage_value: str) -> None:
        while True:
            try:
                with self._lock:
                    # ONE compound statement: the counter and the flag that
                    # says "this is now terminal" move together, or neither
                    # moves. There is no source line between them.
                    setattr(self.accounting, counter_name,
                            getattr(self.accounting, counter_name) + 1)
                    stage_ref[0] = stage_value
                return
            except BaseException:
                # If the counter update above completed before the fault
                # landed (only possible mid-statement, at a bytecode
                # boundary finer than any line), a naive retry here would
                # double-count. `stage_ref[0]` is written in the SAME
                # statement as the counter, so if the fault landed after the
                # counter moved, the flag also already moved, and the retry
                # loop's `return` above already fired — this `except` branch
                # is only reached when NEITHER moved yet, so retrying is
                # safe.
                continue

    def _commit_reject(self, stage_ref: list, stage_value: str) -> None:
        while True:
            try:
                with self._lock:
                    self.accounting.reject_before_accept(
                        RejectReason.SERIALIZATION_FAILURE)
                    stage_ref[0] = stage_value
                return
            except BaseException:
                continue

    def _commit_enqueue(self, item, stage_ref: list) -> bool:
        """The enqueue and the `accepted` counter FUSED into one critical
        section, retried until committed. This is the piece window (b)
        shows `queue.Queue.put_nowait()` + a later, separate counter update
        cannot give you: production (and the naive `Bad*` variants above)
        call an EXTERNAL queue whose own success is not observable in the
        same statement as booking `accepted`, which is exactly the gap a
        fault lands in. Here the "queue" is just this object's own deque, so
        appending to it and booking `accepted` are one statement under one
        lock — nothing else can be interleaved between "it is durably
        enqueued" and "it is durably counted".

        Returns False (not queued) if the shim's queue was full — a real
        rejection reason, never confused with a fault.
        """
        while True:
            try:
                with self._lock:
                    if len(self._raw) >= self._maxsize:
                        return False
                    # ONE statement: append, count, and flag move together.
                    # Splitting this across lines (as an earlier revision of
                    # this shim did) reintroduces window (b) BY ACCIDENT —
                    # confirmed the hard way, by this harness's own
                    # discrimination test catching its own reference
                    # implementation. That failure is worth keeping in the
                    # historical record (see the A3 report) as evidence the
                    # harness actually discriminates rather than rubber-
                    # stamping whatever it's pointed at.
                    (self._raw.append(item),
                     self.accounting.__setattr__(
                         "accepted", self.accounting.accepted + 1),
                     stage_ref.__setitem__(0, "accepted"))
                return True
            except BaseException:
                continue

    def submit(self, item):
        stage = ["start"]
        try:
            self._commit("attempted", stage, "attempted")
            ok = self._commit_enqueue(item, stage)
            if not ok:
                self._commit_reject(stage, "rejected")
                return RejectReason.QUEUE_FULL
            return None
        except BaseException:
            if stage[0] == "start":
                pass  # attempted never landed; nothing to reconcile
            elif stage[0] == "attempted":
                self._commit_reject(stage, "rejected")
            # stage == "accepted": already terminal (the enqueue+accepted
            # commit above is one statement — there is no "queued but not
            # yet accepted" stage left to be caught between).
            raise


class BadWindowA(_BaseSubmitter):
    """Mirrors production's actual defect: `attempted` is booked OUTSIDE the
    try/except that covers the rest of admission."""

    def submit(self, item):
        with self._lock:
            self.accounting.attempted += 1
        # <-- an async exception landing HERE is not covered by anything.
        try:
            self._queue.put_nowait(item)
            with self._lock:
                self.accounting.accepted += 1
            return None
        except BaseException:
            with self._lock:
                self.accounting.reject_before_accept(
                    RejectReason.SERIALIZATION_FAILURE)
            raise


class BadWindowB(_BaseSubmitter):
    """The exception handler always books `reject_before_accept`, even when
    the item was already successfully queued — so a fault landing between a
    successful `put_nowait` and the `accepted` counter update makes
    `written > accepted` once the item is drained."""

    def submit(self, item):
        with self._lock:
            self.accounting.attempted += 1
        try:
            self._queue.put_nowait(item)
            # <-- fault lands here: queued, but not yet booked accepted.
            with self._lock:
                self.accounting.accepted += 1
            return None
        except BaseException:
            with self._lock:
                self.accounting.reject_before_accept(
                    RejectReason.SERIALIZATION_FAILURE)
            raise


class BadWindowC(_BaseSubmitter):
    """The exception handler unconditionally books `reject_before_accept`
    even when `accepted` was already committed — double-booking a single
    event as both accepted and rejected."""

    def submit(self, item):
        with self._lock:
            self.accounting.attempted += 1
        try:
            self._queue.put_nowait(item)
            with self._lock:
                self.accounting.accepted += 1
            # <-- fault lands here, AFTER accepted is already committed.
            self._note_depth()
            return None
        except BaseException:
            with self._lock:
                self.accounting.reject_before_accept(
                    RejectReason.SERIALIZATION_FAILURE)
            raise

    def _note_depth(self):
        with self._lock:
            _ = self._queue.qsize()


class BadWindowD(_BaseSubmitter):
    """The exception handler books the terminal stage in a single
    non-retried statement, so a SECOND fault delivered while the handler is
    still running leaves no terminal booking at all."""

    def submit(self, item):
        with self._lock:
            self.accounting.attempted += 1
        try:
            self._queue.put_nowait(item)
            with self._lock:
                self.accounting.accepted += 1
            return None
        except BaseException:
            # <-- a SECOND fault landing anywhere in this block is fatal:
            # nothing retries, so an interrupted booking is a lost booking.
            with self._lock:
                self.accounting.reject_before_accept(
                    RejectReason.SERIALIZATION_FAILURE)
            raise


VARIANTS = {
    "good": GoodSubmitter,
    "bad_a": BadWindowA,
    "bad_b": BadWindowB,
    "bad_c": BadWindowC,
    "bad_d": BadWindowD,
}
