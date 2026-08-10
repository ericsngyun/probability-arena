"""Deterministic asynchronous-exception injection via `sys.settrace`.

Real `KeyboardInterrupt`/async-exc delivery lands "between bytecode
instructions" at a point CPython does not expose by source line — it can
land anywhere. What IS reproducible, and what reviewers used real signals to
demonstrate, is a *class* of landing points: the boundary immediately before
a specific source line runs. `sys.settrace`'s `'line'` event fires exactly
there, in the SAME thread that installs it, for every frame entered after
`settrace` is called (including callees) — so raising an exception from
inside the trace callback is observationally identical to an async exception
that happened to land at that exact boundary, in that call.

This is intentionally a DIFFERENT mechanism from the real-signal trials in
`fault_trial.py`. Two independent methods finding the same class is the
point: the deterministic one gives an exact, replayable minimal case; the
real-signal one proves the class is not an artifact of `sys.settrace`.

Nothing here imports or modifies any file under `app/realtime/`. It only
observes call frames belonging to whatever module the caller points it at.
"""

from __future__ import annotations

import inspect
import re
import sys
import threading
from dataclasses import dataclass, field


class InjectedFault(RuntimeError):
    """Raised at the targeted boundary. Distinguishable from a real bug's
    exception in trial output, but otherwise behaves like any other
    `BaseException` for the purposes of testing an `except BaseException`
    handler — which is deliberate: production's handler is written to catch
    everything, and this must exercise exactly that path."""


@dataclass
class InjectionPoint:
    """One instruction boundary: "the point immediately before `lineno` in
    function `funcname` of `filename` executes, for the `hit_index`-th time
    within one traced call tree."""

    filename: str
    funcname: str
    lineno: int
    hit_index: int = 1
    label: str = ""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        tag = f" ({self.label})" if self.label else ""
        return f"{self.funcname}:{self.lineno}#{self.hit_index}{tag}"


def target(filename: str, funcname: str, lineno: int, hit_index: int = 1,
          label: str = "") -> InjectionPoint:
    return InjectionPoint(filename=filename, funcname=funcname, lineno=lineno,
                          hit_index=hit_index, label=label)


_MARKER_PREFIX = "FAULT-WINDOW:"


def find_marker_line(func, marker: str) -> int:
    """Locate the executable statement immediately following a
    `# FAULT-WINDOW: <marker>` comment inside `func`'s source.

    A SOURCE SCAN, not a hand-maintained line number. The previous remediation
    shifted `segment.py` by +50 lines and broke 9 of these tests, each fixed
    by mechanically re-pointing a literal integer that had no connection to
    what it was supposed to mean. A marker comment lives next to the
    statement it targets and moves WITH it under any amount of code motion
    elsewhere in the file — re-deriving the line number here means a future
    refactor cannot silently point an injector at the wrong statement while
    the test still passes for the wrong reason.

    The marker's comment may span multiple lines (a long explanation is
    exactly what this codebase's commenting style favours); every line from
    the marker to the first non-blank, non-comment line is skipped, and that
    first executable line is the injection target.
    """
    src_lines, first_lineno = inspect.getsourcelines(func)
    marker_re = re.compile(rf"#\s*{re.escape(_MARKER_PREFIX)}\s*"
                           rf"{re.escape(marker)}(?!\S)")
    for i, line in enumerate(src_lines):
        if marker_re.search(line):
            for j in range(i + 1, len(src_lines)):
                stripped = src_lines[j].strip()
                if stripped and not stripped.startswith("#"):
                    return first_lineno + j
            raise ValueError(
                f"marker {marker!r} in {func!r} has no executable statement "
                "after it")
    raise ValueError(f"marker {marker!r} not found in {func!r}'s source")


def target_marker(filename: str, func, marker: str, hit_index: int = 1,
                  label: str = "") -> InjectionPoint:
    """`target()`, but the line number is RE-DERIVED from a source marker
    (see `find_marker_line`) rather than hard-coded. `func` is the actual
    callable (e.g. `sg.SegmentWriter.submit`) so this is exact even if
    `segment.py` grows or shrinks lines anywhere else in the file."""
    lineno = find_marker_line(func, marker)
    return InjectionPoint(filename=filename, funcname=func.__name__,
                          lineno=lineno, hit_index=hit_index,
                          label=label or marker)


class ChainInjector:
    """Generalizes `LineBoundaryInjector` to a SEQUENCE of boundaries.

    CPython DISABLES the active trace function the moment it raises (this
    was verified empirically, not assumed: `sys.gettrace()` is `None`
    immediately after a local trace function raises). That means a chain of
    length >= 2 where BOTH faults are delivered by this injector can only
    ever fire its first element — the second point is silently never
    reached because tracing is gone by the time execution gets there.

    So a `ChainInjector` of length >= 2 is only meaningful when every
    element after the first is expected NOT to fire from this mechanism —
    i.e. as a diagnostic/negative-control tool, not as how window (d)'s
    double fault is actually produced. Window (d) instead pairs a
    `poison_once`-delivered first fault (an ordinary Python exception, which
    does not touch `sys.settrace` and so does not disarm tracing) with a
    `LineBoundaryInjector` for the second — see `poison_once` below and its
    use in the test file.

    `LineBoundaryInjector` is a `ChainInjector` of length one, which is
    unaffected by this limitation.
    """

    def __init__(self, points, exc_factory=InjectedFault):
        self.points = list(points)
        self.exc_factory = exc_factory
        self._idx = 0
        self._hits: dict = {}
        self._prev_trace = None

    @property
    def fired_count(self) -> int:
        return self._idx

    def _local_trace(self, frame, event, arg):
        if event != "line" or self._idx >= len(self.points):
            return self._local_trace
        point = self.points[self._idx]
        code = frame.f_code
        if (code.co_filename == point.filename
                and code.co_name == point.funcname
                and frame.f_lineno == point.lineno):
            key = self._idx
            self._hits[key] = self._hits.get(key, 0) + 1
            if self._hits[key] == point.hit_index:
                self._idx += 1
                raise self.exc_factory(
                    f"injected (chain step {key}) at boundary {point}")
        return self._local_trace

    def _global_trace(self, frame, event, arg):
        if event == "call":
            return self._local_trace
        return None

    def __enter__(self):
        self._prev_trace = sys.gettrace()
        sys.settrace(self._global_trace)
        threading.settrace(self._global_trace)
        return self

    def __exit__(self, exc_type, exc, tb):
        sys.settrace(self._prev_trace)
        threading.settrace(self._prev_trace if self._prev_trace else lambda *a: None)
        return False


class LineBoundaryInjector(ChainInjector):
    def __init__(self, point: InjectionPoint, exc_factory=InjectedFault):
        super().__init__([point], exc_factory=exc_factory)
        self.point = point

    @property
    def hits(self) -> int:
        return self._hits.get(0, 0)

    @property
    def fired(self) -> bool:
        return self._idx >= 1


class _PoisonOnce:
    """Context manager: replace `getattr(obj, name)` with a callable that
    raises `exc_factory()` the `hit_index`-th time it is called, then
    restores the original — otherwise delegating to it. This is an ordinary
    Python-level monkeypatch, not `sys.settrace`-based, which matters for
    window (d): a fault delivered this way does NOT disable tracing, so it
    can be safely combined with a `LineBoundaryInjector` for a SECOND fault
    later in the same call (see the `ChainInjector` docstring for why the
    reverse — two trace-raised faults — does not work).

    This only ever patches an object this test constructed (a `SegmentWriter`
    instance's own bound `_queue`/similar), never anything in `app/`'s
    module-level namespace, and always restores on exit.
    """

    def __init__(self, obj, name: str, exc_factory=InjectedFault, hit_index: int = 1):
        self.obj = obj
        self.name = name
        self.exc_factory = exc_factory
        self.hit_index = hit_index
        self._hits = 0
        self._original = None

    def __enter__(self):
        self._original = getattr(self.obj, self.name)

        def wrapper(*args, **kwargs):
            self._hits += 1
            if self._hits == self.hit_index:
                raise self.exc_factory(
                    f"poisoned {self.name} (call #{self._hits})")
            return self._original(*args, **kwargs)

        setattr(self.obj, self.name, wrapper)
        return self

    def __exit__(self, exc_type, exc, tb):
        setattr(self.obj, self.name, self._original)
        return False


def poison_once(obj, name: str, exc_factory=InjectedFault, hit_index: int = 1):
    return _PoisonOnce(obj, name, exc_factory=exc_factory, hit_index=hit_index)
