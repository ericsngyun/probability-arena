"""External wall-clock timeout enforcement for termination tests.

An in-process deadline (signal.alarm, threading.Timer, etc.) cannot prove a
hang: it can only prove that ONE thread stopped responding, and Python's GIL
plus C-level loops (e.g. an unbounded `for v in value` walking a hostile
__iter__) can defeat it entirely, or leave the interpreter in a state later
tests inherit. The only trustworthy signal is a PARENT process watching a
CHILD process it can kill outright.

Every scenario below is a small, self-contained Python script (a string) run
with `python -c`. This keeps each scenario immune to pickling limits on
hostile classes (closures, dynamically-defined __iter__/__eq__/__hash__
overrides are not picklable, which rules out multiprocessing.Process with a
target function) and keeps the parent's classification unambiguous:

    REFUSED     the child exited 0 having printed a REASON/REFUSED line
                 (or non-zero having printed a controlled diagnostic)
    COMPLETED   the child exited 0 having printed a RESULT line -- it
                 terminated, successfully or not, within the deadline
    CRASHED     the child exited non-zero without a controlled line -- an
                 uncaught exception escaped somewhere unexpected
    TIMEOUT     the parent killed the child after `timeout_s` -- the child
                 did NOT terminate. This is the only classification that may
                 legitimately be produced by a hang.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass


@dataclass
class SubprocessVerdict:
    classification: str          # REFUSED | COMPLETED | CRASHED | TIMEOUT
    duration_s: float
    returncode: int | None
    stdout: str
    stderr: str

    @property
    def timed_out(self) -> bool:
        return self.classification == "TIMEOUT"


_PREAMBLE = """
import sys, os
sys.path.insert(0, {repo_root!r})
"""


def run_script(body: str, *, timeout_s: float, repo_root: str) -> SubprocessVerdict:
    """Run `body` as a standalone script in a fresh interpreter.

    The child is expected to print exactly one of:
        "REFUSED:<text>"   -- it detected the hazard and declined, on purpose
        "RESULT:<text>"    -- it ran to completion (successfully or not)
    to stdout as its LAST line. Anything else on exit is CRASHED.
    """
    code = _PREAMBLE.format(repo_root=repo_root) + body
    start = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout_s)
        duration = time.monotonic() - start
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        duration = time.monotonic() - start
        return SubprocessVerdict("TIMEOUT", duration, None, out or "", err or "")

    last_line = ""
    for line in reversed(out.splitlines()):
        if line.strip():
            last_line = line.strip()
            break
    if proc.returncode == 0 and last_line.startswith("REFUSED:"):
        return SubprocessVerdict("REFUSED", duration, proc.returncode, out, err)
    if proc.returncode == 0 and last_line.startswith("RESULT:"):
        return SubprocessVerdict("COMPLETED", duration, proc.returncode, out, err)
    return SubprocessVerdict("CRASHED", duration, proc.returncode, out, err)
