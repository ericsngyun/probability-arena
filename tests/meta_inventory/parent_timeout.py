"""A3 — a minimal, LOCAL parent-enforced wall-clock timeout helper.

DUPLICATION NOTE, read before consolidating: the milestone brief states a
reusable helper is planned at `tests/meta_runtime/parent_timeout.py`, owned
by a separate lane. As of this module's writing that path does not exist
(`tests/meta_runtime/` is not present in this worktree at all). Rather than
block A3 on another lane's unmerged work, this is a small, self-contained
stand-in with the SAME core property
(`tests/harness_filesystem_totality/runner.py` already established the
pattern, and this module deliberately mirrors its reasoning rather than
importing it, to keep `tests/meta_inventory/` free of a dependency on
`tests/harness_filesystem_totality/` internals beyond the read-only fixture
reuse noted elsewhere):

    the timeout must be enforced by a PARENT process against a CHILD
    process, never by an in-process signal/thread timer racing the same
    interpreter that might be blocked inside a syscall.

When `tests/meta_runtime/parent_timeout.py` lands, this module should be
DELETED and its two call sites (`argument_matrix.py`,
`test_kalshi_meta_inventory_001.py`) repointed at the shared one -- this is
flagged here, not resolved, because resolving it means depending on a file
this lane does not own and has not seen.
"""

from __future__ import annotations

import subprocess
import sys


def run_with_parent_timeout(cmd: list, *, timeout_s: float) -> dict:
    """Run `cmd` as a subprocess; SIGKILL it from here if it outlives
    `timeout_s`. Returns a dict always -- never raises `TimeoutExpired`
    upward, since a caller building an argument-shape matrix needs "this
    cell hung" to be a DATA POINT, not an uncaught exception that aborts
    every remaining cell.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"classification": "TIMEOUT", "timeout_s": timeout_s}
    if proc.returncode != 0:
        return {"classification": "CRASHED", "returncode": proc.returncode,
                "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]}
    return {"classification": "EXITED_CLEAN", "stdout": proc.stdout,
            "stderr": proc.stderr[-2000:]}


def run_python_with_parent_timeout(script_path: str, arg: str, *,
                                   timeout_s: float) -> dict:
    return run_with_parent_timeout([sys.executable, script_path, arg],
                                   timeout_s=timeout_s)
