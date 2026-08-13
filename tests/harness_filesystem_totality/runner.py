"""Parent-side driver: run one matrix cell in a subprocess, killable from here.

This is the piece that makes the methodology honest. Every previous review
round asserted hang-freedom with an in-process deadline (a thread with a
timer, or `signal.alarm`) sitting in the SAME interpreter as the blocking
`open()` it was trying to bound -- which cannot work, and additionally
`archive_head.py::_read_json`'s `except Exception` swallows an in-process
`TimeoutError` from `signal.alarm` and reports a clean `ArchiveHeadError`,
turning a hang into a reported PASS. Here the timeout is enforced by
`subprocess.run(..., timeout=...)` in THIS process, against a CHILD process
that is stuck; on expiry Python sends SIGKILL to the child and there is no
code path in the child that can intercept that.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_RUNNER_SCRIPT = Path(__file__).with_name("entrypoint_runner.py")

DEFAULT_TIMEOUT_S = 2.0

# One dial for how dangerous a cell is allowed to be. `/dev/zero` reproduction
# is real but macOS has no working RLIMIT_AS (`setrlimit` on RLIMIT_AS fails
# with EINVAL/"current limit exceeds maximum limit" on every macOS host this
# was checked against, and shell `ulimit -v` fails identically), so there is
# no in-process way to cap the child's memory growth. This timeout is kept
# very small specifically for that shape so a kill happens before growth
# becomes a host-level problem.
DEV_ZERO_TIMEOUT_S = 0.2


def run_cell(entrypoint: str, *, root, environment: str = "demo",
            segment_dir=None, source=None, dest=None,
            timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Run one entry point against one archive root in a subprocess.

    Returns a dict with one of:
      {"classification": "RETURNED", "outcome": ..., "detail": ...}
      {"classification": "RAISED", "exception_type": ..., "exception_message": ...}
      {"classification": "TIMEOUT", "timeout_s": ...}
      {"classification": "CRASHED", "returncode": ..., "stderr": ...}
      {"classification": "PROTOCOL_VIOLATION", "stdout": ..., "stderr": ...}
    """
    args = {"entrypoint": entrypoint, "root": str(root),
            "environment": environment}
    if segment_dir is not None:
        args["segment_dir"] = str(segment_dir)
    if source is not None:
        args["source"] = str(source)
    if dest is not None:
        args["dest"] = str(dest)

    cmd = [sys.executable, str(_RUNNER_SCRIPT), json.dumps(args)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"classification": "TIMEOUT", "timeout_s": timeout_s,
                "entrypoint": entrypoint}

    if proc.returncode != 0:
        return {"classification": "CRASHED", "returncode": proc.returncode,
                "stderr": proc.stderr[-4000:], "stdout": proc.stdout[-2000:],
                "entrypoint": entrypoint}

    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if len(lines) != 1:
        return {"classification": "PROTOCOL_VIOLATION",
                "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:],
                "entrypoint": entrypoint}
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError:
        return {"classification": "PROTOCOL_VIOLATION",
                "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:],
                "entrypoint": entrypoint}

    if payload.get("raised"):
        return {"classification": "RAISED",
                "exception_type": payload.get("exception_type"),
                "exception_module": payload.get("exception_module"),
                "exception_message": payload.get("exception_message"),
                "entrypoint": entrypoint}
    return {"classification": "RETURNED", "outcome": payload.get("outcome"),
            "detail": payload.get("detail"), "entrypoint": entrypoint}


def run_script(script: str, *, timeout_s: float) -> dict:
    """Run an arbitrary standalone python -c script under the same kill-guard.

    Used by the discrimination tests to run deliberately-broken or
    deliberately-hanging stand-ins that are not part of production.
    """
    cmd = [sys.executable, "-c", script]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"classification": "TIMEOUT", "timeout_s": timeout_s}
    if proc.returncode != 0:
        return {"classification": "CRASHED", "returncode": proc.returncode,
                "stderr": proc.stderr[-4000:]}
    return {"classification": "RETURNED", "stdout": proc.stdout.strip()}
