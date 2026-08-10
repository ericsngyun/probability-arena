"""A wall-clock guard for in-process fault-injection trials.

Discovered empirically while building this harness (see the A3 report): a
`sys.settrace`-raised fault landing exactly on the source line that
corresponds to a `with lock:` block's implicit `__exit__` call can prevent
that `__exit__` from ever running, leaking the lock — a deadlock that is an
artifact of the injection MECHANISM, not evidence of a real bug in the code
under test. Any in-process campaign that walks many injection points can hit
this, and a hung trial must not be allowed to hang the whole suite, nor be
silently indistinguishable from a trial that legitimately finished.

`deadline()` uses `signal.alarm`, which — unlike a background-thread
watchdog — can actually interrupt a blocked `Lock.acquire()` in the main
thread, which is exactly the failure mode being guarded against. This is the
in-process analogue of the subprocess + external wall-clock timeout used for
the real-signal (SIGINT / async-exc) campaigns in `fault_trial.py`; the
mechanism differs because those campaigns already run in a subprocess the
parent can simply kill, whereas single deterministic trials are cheap enough
that spawning a subprocess per trial would dominate the harness's runtime.
"""

from __future__ import annotations

import contextlib
import signal


class TrialTimedOut(RuntimeError):
    """A trial did not return within its deadline. Distinguishable from a
    real identity violation: the accounting was never observed at all."""


@contextlib.contextmanager
def deadline(seconds: float):
    if not hasattr(signal, "SIGALRM"):  # pragma: no cover - POSIX only
        yield
        return

    def _on_alarm(signum, frame):
        raise TrialTimedOut(f"trial exceeded {seconds}s deadline")

    previous = signal.signal(signal.SIGALRM, _on_alarm)
    # itimer gives sub-second resolution; plain alarm() only takes whole
    # seconds, which is too coarse for a fast deterministic trial budget.
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
