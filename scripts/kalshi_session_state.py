"""Artifact-backed session state. Process search is diagnostic only.

`pgrep -f <pattern>` over SSH matches its OWN command line, because the pattern
is present in the transmitted command. In this project it has produced a false
"capture still running", a false "DO NOT MERGE", and a false "LAUNCHED" — every
one of them **in the reassuring direction**, which is the dangerous one. A
false "running" makes us believe a capture or a guard exists when it does not.

So state is established from **artifacts the session itself creates**, and a
PID is only ever trusted when it came from a non-self-referential source and is
confirmed with `kill -0`.

    ARMED     arm process alive, no session root yet
    LAUNCHED  session root + genesis exist, capture PID recorded and alive
    RUNNING   as LAUNCHED, and frames are accumulating
    CLOSED    terminal session artifact written

Standalone by design: no repo imports, so it can be run on a host whose
checkout must stay pinned.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ARMED, LAUNCHED, RUNNING, CLOSED, UNKNOWN = (
    "ARMED", "LAUNCHED", "RUNNING", "CLOSED", "UNKNOWN")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True          # exists, owned by someone else


def cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except Exception:
        return ""


def _pids_for(label: str, script_fragment: str) -> list[int]:
    """Resolve PIDs from /proc, never from a pattern on our own command line."""
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        c = cmdline(int(d.name))
        if script_fragment in c and label in c:
            out.append(int(d.name))
    return out


def frame_count(env_dir: Path) -> int:
    n = 0
    for seg in env_dir.glob("**/segment=*"):
        f = seg / "events.jsonl.gz"
        if f.exists():
            n += f.stat().st_size
    return n


def session_state(base: Path, label: str) -> dict:
    root = base / f"session={label}"
    env = root / "env=production"
    out_json = base / f"session-{label.split('-')[1][1:].zfill(2)}.json"

    arm_pids = _pids_for(label, "arm_session")
    cap_pids = _pids_for(label, "capture_p4")
    genesis = env / "archive-genesis.json"
    claim = env / "collection-session.json"

    ev = {
        "label": label,
        "session_root_exists": root.exists(),
        "genesis_exists": genesis.exists(),
        "session_claim_exists": claim.exists(),
        "arm_pids_alive": [p for p in arm_pids if pid_alive(p)],
        "capture_pids_alive": [p for p in cap_pids if pid_alive(p)],
        "archive_bytes": frame_count(env) if env.exists() else 0,
        "terminal_artifact": None,
    }
    for cand in base.glob("session-*.json"):
        try:
            d = json.loads(cand.read_text())
        except Exception:
            continue
        if d.get("label") == label:
            ev["terminal_artifact"] = str(cand)
            ev["dataset_role"] = d.get("dataset_role")
            ev["capture_commit"] = d.get("capture_commit")
            ev["capture_session_status"] = d.get("capture_session_status")

    if ev["terminal_artifact"]:
        state = CLOSED
    elif ev["genesis_exists"] and ev["capture_pids_alive"]:
        state = RUNNING if ev["archive_bytes"] > 0 else LAUNCHED
    elif ev["arm_pids_alive"] and not ev["session_root_exists"]:
        state = ARMED
    else:
        state = UNKNOWN
    ev["state"] = state
    return ev


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--expect-commit", default=None)
    ap.add_argument("--expect-role", default="CONFIRMATION")
    a = ap.parse_args(argv[1:])
    ev = session_state(Path(a.base), a.label)
    print(f"STATE: {ev['state']}")
    for k in ("session_root_exists", "genesis_exists", "session_claim_exists",
              "arm_pids_alive", "capture_pids_alive", "archive_bytes",
              "terminal_artifact", "dataset_role", "capture_commit",
              "capture_session_status"):
        if k in ev:
            print(f"  {k:24} {ev[k]}")
    problems = []
    if a.expect_commit and ev.get("capture_commit") and \
            not ev["capture_commit"].startswith(a.expect_commit):
        problems.append(f"commit {ev['capture_commit'][:12]} != {a.expect_commit}")
    if ev.get("dataset_role") and ev["dataset_role"] != a.expect_role:
        problems.append(f"role {ev['dataset_role']} != {a.expect_role}")
    if problems:
        print("PROBLEMS: " + "; ".join(problems))
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
