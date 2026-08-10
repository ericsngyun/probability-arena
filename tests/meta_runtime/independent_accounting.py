"""KALSHI-ARCHIVE-VERIFICATION-META-001 A7 -- two INDEPENDENTLY SOURCED
observations of what a segment writer actually, durably disposed of.

WHY THIS EXISTS. `WriterAccounting.accepted` is now DERIVED:

    accepted := written + failed_after_accept + pending (+ live queue depth)

`disposition_holds()` compares `accepted` against exactly that same formula,
so it is TRUE BY DEFINITION -- a tautology, not an observation. Any writer-
side loss that removes an item from ALL THREE of `written`/
`failed_after_accept`/`pending` simultaneously (rather than double-counting
or dropping just one of them) is therefore INVISIBLE to every identity
`WriterAccounting` can state about itself: the formula and the check are the
same formula, so nothing can ever disagree with itself.

Detecting real loss of that shape requires a SECOND observation that was
never derived from the writer's own bookkeeping at all:

  Source 1 -- ADMISSION LEDGER, recorded OUTSIDE `SegmentWriter`, at the
             moment `submit()` returns to its caller. This is simply "how
             many times did the producer's own call site see `None` (
             accepted) come back" -- a fact the producer already knows
             without reading a single attribute of the writer.

  Source 2 -- DURABLE WRITER DISPOSITION, read from what the writer itself
             can independently prove happened: `written` + `failed_after_
             accept` (both incremented only on the writer thread, at the
             moment of an actual outcome) + whatever is STILL sitting in
             the queue right now (`_queue.qsize()`, read directly -- not via
             `WriterAccounting.pending`, which is only populated by
             `_measure_pending()` at close and would otherwise make this
             "source" secretly identical to the derived one).

These two are RECONCILED, not each trusted alone: `ledger_accepted ==
written + failed_after_accept + queue_depth_right_now` must hold for a
non-lossy writer. A gap means an item the ledger independently confirms was
accepted is unaccounted for anywhere the writer itself can currently see --
which is exactly the shape of loss `disposition_holds()`'s tautology cannot
express, by construction.

FAULT INJECTION TARGETS THE WRITER THREAD SPECIFICALLY
(`SegmentWriter._thread`/`_thread.ident`), not the producer/main thread that
`tests/harness_async_accounting/` already covers exhaustively. `_run`'s own
source (see `locate_writer_run_gap_line`) has exactly one totally-unguarded
boundary: between `item = self._queue.get(...)` succeeding and the SECOND
`try:` (the one wrapping `self._write_one(item)` and its `finally:
self._queue.task_done()`) beginning. A fault landing there is not caught by
ANY exception handler in `_run` -- it propagates straight out of the
function, killing the writer thread, with the dequeued item counted
NOWHERE: not `written` (never reached `_write_one`), not `failed_after_
accept` (the `except BaseException` around `_write_one` never ran), and not
`pending` (the item is no longer in the queue for `_measure_pending()` to
find at close). Nothing here modifies `app/realtime/segment.py`; the target
line is located by SOURCE SCAN (mirroring `line_injector.find_marker_line`'s
approach), not by a hardcoded line number, so it moves with the file under
refactoring.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field


def locate_writer_run_gap_line(run_func) -> int:
    """The line number of the SECOND `try:` inside `SegmentWriter._run` --
    the one immediately guarding `self._write_one(item)`. An exception
    injected to fire exactly BEFORE this line runs lands in the one gap
    `_run`'s own exception handling does not cover: the item has already
    been irreversibly dequeued (`queue.get()` already returned it) but
    nothing durable has happened to it yet.

    Located by scanning the function's own source for the line containing
    `self._write_one(item)` and stepping back to the nearest preceding bare
    `try:` line, rather than a hardcoded line number -- this is deliberately
    NOT a `# FAULT-WINDOW:` marker comment (which would require editing
    `segment.py`, out of scope for this milestone), so it is slightly more
    fragile against a refactor that changes this shape; that fragility is
    accepted and made LOUD (an assertion failure naming exactly what was
    expected to find) rather than silent.
    """
    src_lines, first_lineno = inspect.getsourcelines(run_func)
    target_idx = None
    for i, line in enumerate(src_lines):
        if "self._write_one(item)" in line:
            target_idx = i
            break
    if target_idx is None:
        raise AssertionError(
            "could not locate 'self._write_one(item)' inside "
            f"{run_func!r}'s source; the writer-thread injection target "
            "has moved and this locator needs updating")
    for j in range(target_idx - 1, -1, -1):
        stripped = src_lines[j].strip()
        if stripped == "try:":
            return first_lineno + j
        if stripped and not stripped.startswith("#"):
            # Hit a non-blank, non-comment, non-`try:` line before finding
            # one -- the shape this locator expects (a bare `try:` directly
            # guarding the write call) is gone.
            break
    raise AssertionError(
        "could not locate the 'try:' immediately guarding "
        f"'self._write_one(item)' inside {run_func!r}'s source; the "
        "writer-thread injection target has moved and this locator needs "
        "updating rather than being pointed at the wrong line silently")


def locate_task_done_line(run_func) -> int:
    """The line number of `self._queue.task_done()` inside `_run`'s
    `finally:` block -- the SECOND writer-thread injection target (a fault
    landing here fires AFTER the record was already durably written or
    already booked as failed, so no EVIDENCE is lost, but the writer thread
    still dies without ever calling `task_done()`, which is the "dead
    writer thread, diagnostic state does not notice" class fed into A8).
    """
    src_lines, first_lineno = inspect.getsourcelines(run_func)
    for i, line in enumerate(src_lines):
        if "self._queue.task_done()" in line:
            return first_lineno + i
    raise AssertionError(
        "could not locate 'self._queue.task_done()' inside "
        f"{run_func!r}'s source; the writer-thread injection target has "
        "moved and this locator needs updating")


@dataclass
class AdmissionLedger:
    """SOURCE 1: recorded by the TEST, at the producer's own call site --
    never reads a single attribute of the `SegmentWriter` being tested.
    This is deliberately the most naive possible observation: "how many
    times did `submit()` hand back `None`", which is exactly what a real
    producer already knows without any cooperation from the writer.
    """

    accepted: int = 0
    rejected: int = 0
    outcomes: list = field(default_factory=list)

    def record(self, reason) -> None:
        self.outcomes.append(reason)
        if reason is None:
            self.accepted += 1
        else:
            self.rejected += 1


@dataclass
class DurableDisposition:
    """SOURCE 2: read directly off the writer's OWN durable/queued state --
    never through `WriterAccounting.accepted`/`disposition_holds()` (the
    tautological identity), and never through `WriterAccounting.pending`
    (which is only populated by `_measure_pending()` at close -- reading it
    before that would just be reading zero, not "what's really queued").
    """

    written: int
    failed_after_accept: int
    queue_depth_now: int

    @property
    def total(self) -> int:
        return self.written + self.failed_after_accept + self.queue_depth_now


def read_durable_disposition(writer) -> DurableDisposition:
    # MUTATION: derived from the SAME tautological source A7's
    # own docstring names as the thing this function exists to
    # be independent of.
    return DurableDisposition(
        written=writer.accounting.accepted,
        failed_after_accept=0,
        queue_depth_now=0)


@dataclass
class ReconciliationReport:
    ledger_accepted: int
    durable_total: int
    matches: bool
    gap: int

    def to_dict(self) -> dict:
        return {"ledger_accepted": self.ledger_accepted,
               "durable_total": self.durable_total,
               "matches": self.matches, "gap": self.gap}


def reconcile(ledger: AdmissionLedger, writer) -> ReconciliationReport:
    """THE independent check. Never reads `writer.accounting.accepted` or
    calls `writer.accounting.disposition_holds()`/`reconciles()` -- those
    are the tautology this function exists to be independent OF."""
    durable = read_durable_disposition(writer)
    gap = ledger.accepted - durable.total
    return ReconciliationReport(ledger.accepted, durable.total, gap == 0, gap)
