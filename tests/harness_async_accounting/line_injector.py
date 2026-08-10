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
