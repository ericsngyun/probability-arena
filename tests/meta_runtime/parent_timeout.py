"""KALSHI-ARCHIVE-VERIFICATION-META-001 A4 -- parent-enforced timeout as the
default execution mode for anything that MIGHT hang.

WHY THIS EXISTS, STATED EXACTLY: `archive_head._read_json` used to be
reachable through a call path where an in-process `signal.alarm`-based
deadline could be delivered as an ordinary `SIGALRM` -> Python signal handler
-> raised exception INSIDE the very function being tested for hanging, and
that function's own `except Exception` (a broad, deliberately defensive
catch written for a completely different purpose -- reporting a corrupt
artifact as a verdict, not surviving a deadline) swallowed the alarm and
returned a CLEAN verdict. The deadline fired, the function "finished", and
the test that trusted `signal.alarm` reported PASS on code that would, on a
real hang (a FIFO with no writer, a symlink to /dev/zero, an unbounded
generator), have blocked the calling thread/process forever in production.

A deadline enforced BY the thing it is measuring is not a deadline; it is
another code path through the thing it is measuring, and it inherits every
bug the thing it is measuring has (including "catches everything and
returns a verdict").

THE FIX: measure from OUTSIDE the process. The code under test runs in a
freshly spawned CHILD process, in its own process group (`start_new_session`
on POSIX), and the PARENT holds the only deadline. A parent that kills its
child with SIGKILL cannot be defeated by anything the child's Python code
does -- no exception handler, no `except BaseException`, no signal handler
installed in the child can intercept or ignore SIGKILL. If the child spawned
threads that themselves ignore `SIGTERM` (a Python thread with the GIL held
by a tight non-cooperating loop is exactly this: `Thread.join(timeout=...)`
in the child would never see progress), the parent does not ask nicely: it
sends `SIGKILL` to the whole process GROUP (`os.killpg`), which the kernel
delivers unconditionally to every process in it -- including any further
subprocess a hung child itself launched. Nothing under test is trusted to
cooperate with its own death.

Four possible verdicts, mirroring (and generalising) the existing bilateral
REFUSED/COMPLETED/CRASHED/TIMEOUT convention already used by
`tests/harness_encoder_fidelity/subprocess_timeout.py` for the encoder
harness -- this module is the general-purpose version of the same idea,
built specifically to prove the NEGATIVE direction too (a fast, terminating
control must be COMPLETED, never TIMEOUT_FAIL) and to prove `killpg`
actually reaches a grandchild process, not merely the immediate child.

    COMPLETED     the child exited (any returncode) within the deadline.
    TIMEOUT_FAIL  the parent's deadline elapsed before the child exited; the
                  parent killed it (SIGKILL, process-group-wide) and this is
                  the ONLY verdict a genuine hang may ever produce. Never
                  "the suite hangs" -- the test harness always gets control
                  back.
    CRASHED       the child raised at the OS/interpreter level in a way this
                  module could not even start measuring (Popen itself
                  failed). Distinguished from COMPLETED with a non-zero
                  returncode, which IS still COMPLETED -- a python script
                  under test that exits non-zero terminated, it just failed;
                  that is not a timeout.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

# Generous relative to any real deterministic reproduction in this package
# (all of which are designed to terminate in well under a second when
# correct) and small enough that a genuine hang is caught promptly rather
# than dominating suite runtime. Callers may override per-call.
DEFAULT_TIMEOUT_S = 3.0

# How long the parent waits for the killed process (and its group) to
# actually be reaped after SIGKILL before giving up on collecting its
# stdout/stderr. SIGKILL cannot be blocked or ignored by anything running
# in userspace, so this is bounded by kernel scheduling latency, not by
# anything the child's Python code can influence -- if this ever fires it is
# telling us something about the HOST, not about the code under test.
REAP_GRACE_S = 5.0

_PREAMBLE = "import sys\nsys.path.insert(0, {repo_root!r})\n"


@dataclass
class TimeoutVerdict:
    classification: str            # COMPLETED | TIMEOUT_FAIL | CRASHED
    returncode: int | None
    duration_s: float
    stdout: str
    stderr: str
    killed_via: str | None = None  # None | "SIGKILL" | "SIGKILL+killpg"
    pid: int | None = None

    @property
    def timed_out(self) -> bool:
        return self.classification == "TIMEOUT_FAIL"

    @property
    def completed(self) -> bool:
        return self.classification == "COMPLETED"


def _kill_process_group(proc: subprocess.Popen) -> str:
    """SIGKILL the whole process group. Falls back to SIGKILL on just the
    child if the group cannot be resolved (e.g. the child already exited in
    the race between the timeout firing and this call, or the platform does
    not support process groups (`start_new_session` is POSIX-only)).

    SIGKILL specifically -- never SIGTERM alone -- because SIGTERM is a
    request a Python thread that is spinning inside a non-cooperating loop
    (the exact "ignores SIGTERM" case this module exists to defeat) never
    gets a chance to honour: the interpreter only checks for a pending
    signal between bytecode instructions in the thread that owns the GIL,
    and a hung C-level call or a GIL-held tight loop can starve that check
    indefinitely. SIGKILL bypasses userspace entirely.
    """
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
        return "SIGKILL+killpg"
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()             # SIGKILL on POSIX, TerminateProcess on Windows
    except (ProcessLookupError, OSError):
        pass
    return "SIGKILL"


def run_with_parent_timeout(script: str, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                            repo_root: str | None = None,
                            env: dict | None = None) -> TimeoutVerdict:
    """Run `script` (a standalone Python program, as source text) as a child
    process, with the DEADLINE HELD AND ENFORCED BY THIS PARENT PROCESS --
    never by anything inside `script`.

    `script` must not itself install `signal.alarm`/a watchdog thread/any
    other in-process deadline as its OWN termination guarantee; the whole
    point of this helper is that the code under test needs none, because
    nothing it does -- however it hangs, whatever it catches -- can prevent
    the parent from killing it.
    """
    code = script if repo_root is None else _PREAMBLE.format(repo_root=repo_root) + script
    popen_kwargs: dict = {}
    if hasattr(os, "setsid"):
        # New session == new process group, so `os.killpg` reaches every
        # descendant the child spawns, not merely the immediate PID.
        popen_kwargs["start_new_session"] = True
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, **popen_kwargs)
    except OSError as exc:
        return TimeoutVerdict("CRASHED", None, time.monotonic() - start,
                              "", repr(exc))

    try:
        out, err = proc.communicate(timeout=timeout_s)
        duration = time.monotonic() - start
        return TimeoutVerdict("COMPLETED", proc.returncode, duration,
                              out or "", err or "", None, proc.pid)
    except subprocess.TimeoutExpired:
        killed_via = _kill_process_group(proc)
        try:
            out, err = proc.communicate(timeout=REAP_GRACE_S)
        except subprocess.TimeoutExpired:
            # SIGKILL cannot be blocked; if the OS still has not reaped it
            # within REAP_GRACE_S the parent gives up WAITING for output but
            # still returns control to the caller -- a suite must never hang
            # waiting on a process it already killed.
            out, err = "", ""
        duration = time.monotonic() - start
        return TimeoutVerdict("TIMEOUT_FAIL", proc.returncode, duration,
                              out or "", err or "", killed_via, proc.pid)


def run_callable_source(body: str, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                        repo_root: str | None = None) -> TimeoutVerdict:
    """Convenience alias with a name that reads better at call sites that
    are running a self-contained snippet ("the code under test") rather
    than a file. Identical behaviour to `run_with_parent_timeout`."""
    return run_with_parent_timeout(body, timeout_s=timeout_s, repo_root=repo_root)
