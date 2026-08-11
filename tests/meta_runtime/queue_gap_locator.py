"""KALSHI-ARCHIVE-CORE-REMEDIATION-003B A2 -- the queue-get/disposition
gap, located SEMANTICALLY rather than by "nearest preceding bare `try:`".

WHY THIS EXISTS. `tests/meta_runtime/independent_accounting.py`'s
`locate_writer_run_gap_line` finds the line containing
`self._write_one(item)` inside `SegmentWriter._run` and walks BACKWARDS to
the nearest preceding line whose stripped text is exactly `try:`. Before
defect F's fix, that walk-back landed on the ONE bare `try:` directly
guarding the write call -- correct, because at that time NOTHING sat
between `queue.get()` returning and that `try:`.

Defect F's own fix inserted a NEW statement in between:

    item = self._queue.get(timeout=0.05)   # <- irreversible removal
    ...
    with self._lock:                       # <- NEW, defect F's own counter
        self.accounting.dequeued += 1
    try:                                    # <- the locator's target
        self._write_one(item)

The locator's nearest-`try:` walk-back still finds the SAME `try:` line --
but that line is now ONE STATEMENT AFTER the counter defect F added, not
immediately after `queue.get()`. The locator's own docstring claims it
finds "the one gap `_run`'s own exception handling does not cover: the item
has already been irreversibly dequeued... but nothing durable has happened
to it yet" -- but `self.accounting.dequeued += 1` is neither "nothing" nor
covered by an exception handler at that boundary; it is durable
bookkeeping that already ran by the time the locator's target line fires.
The locator's SEMANTICS are therefore false today: it no longer finds the
window it claims to, it finds a narrower, ALREADY-COUNTED window one
statement later.

THE REAL, STILL-OPEN GAP is between `queue.get()` returning and
`self.accounting.dequeued += 1` executing -- genuinely unguarded by ANY
`try`/`except` in `_run` (the only `try` that could cover it,
`try: item = self._queue.get(...)`, ends the instant `.get()` returns
successfully; the `with self._lock:` block that increments `dequeued`
begins as a SIBLING statement, not nested inside that try). A fault landing
there is invisible to `written`, to `failed_after_accept`, AND to
`dequeued` -- so `WriterAccounting.dequeue_disposition_gap()` (defect F's
own second-sourced check, which `close()` gates publication on) sees NO
disagreement: `dequeued` never moved for the lost item, so it never
disagrees with `written + failed_after_accept` either. This is a REAL,
CURRENTLY-INVISIBLE gap, strictly wider than what defect F's own
independent counter can see.

THIS MODULE locates it SEMANTICALLY: by scanning `_run`'s source text for
the statement that increments `self.accounting.dequeued`, and returning
THAT line directly -- not by walking back from `_write_one(item)` to a
nearby `try:`, and not by a hard-coded absolute line number. It moves with
the file exactly the way `find_marker_line`
(`tests/harness_async_accounting/line_injector.py`) does for a
`# FAULT-WINDOW:` marker, except there is no marker comment here (adding
one would mean editing `segment.py`, out of scope for this milestone) --
the semantic anchor is the counter's own statement text, which cannot
silently drift the way a relative "nearest try:" walk can.
"""

from __future__ import annotations

import inspect


def locate_predequeue_gap_line(run_func) -> int:
    """The line number of `self.accounting.dequeued += 1` inside
    `SegmentWriter._run`. Injecting a fault to fire immediately BEFORE this
    line lands in the TRUE unguarded window: `queue.get()` has already
    returned (the item is irreversibly out of the queue) but NOTHING --
    not `dequeued`, not `written`, not `failed_after_accept`, not
    `pending` (the item already left the queue `_measure_pending` would
    otherwise find it in) -- has recorded that fact anywhere.
    """
    src_lines, first_lineno = inspect.getsourcelines(run_func)
    for i, line in enumerate(src_lines):
        stripped = line.strip()
        if (stripped.startswith("self.accounting.dequeued")
                and "+=" in stripped):
            return first_lineno + i
    raise AssertionError(
        "could not locate 'self.accounting.dequeued += 1' inside "
        f"{run_func!r}'s source; the pre-dequeue-gap injection target has "
        "moved and this locator needs updating rather than being pointed "
        "at the wrong line silently")


def nearest_try_before_write_one(run_func) -> int:
    """Restates `independent_accounting.locate_writer_run_gap_line`'s OWN
    walk-back algorithm, standalone, so this module's tests can prove --
    independently of that module -- that its result now differs from
    `locate_predequeue_gap_line`'s, which is the concrete, source-level
    evidence that its docstring's stated semantics ("the item has already
    been irreversibly dequeued... but nothing durable has happened to it
    yet") no longer describes the line it finds.
    """
    src_lines, first_lineno = inspect.getsourcelines(run_func)
    target_idx = None
    for i, line in enumerate(src_lines):
        if "self._write_one(item)" in line:
            target_idx = i
            break
    if target_idx is None:
        raise AssertionError("could not locate 'self._write_one(item)'")
    for j in range(target_idx - 1, -1, -1):
        stripped = src_lines[j].strip()
        if stripped == "try:":
            return first_lineno + j
        if stripped and not stripped.startswith("#"):
            break
    raise AssertionError("could not locate the guarding 'try:'")
