"""Preflight a frozen session, wait for its scheduled start, then launch it.

Re-verifies the frozen artifacts immediately before the socket opens, because
several hours pass between freezing a decision and acting on it. It checks
only mechanical facts -- role, root, schema, capacity inequality, series
restriction, anchor identity, code commit -- and never re-runs the scheduler.
A frozen decision is re-derived only under the preregistered replacement
condition, which is a separate, deliberate act.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.microstructure.panel import (  # noqa: E402
    HARD_STOP_FPS, NEVER_EXCEED_CONCURRENCY, assert_capacity_relationship)
from app.microstructure.rows import (  # noqa: E402
    LABEL_SCHEMA_VERSION, ROW_SCHEMA_VERSION)


REST = "https://api.elections.kalshi.com/trade-api/v2"


def market_status(ticker: str) -> str | None:
    """Read-only GET of one market's lifecycle status."""
    try:
        req = urllib.request.Request(f"{REST}/markets/{ticker}",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return (json.load(r).get("market") or {}).get("status")
    except Exception:
        return None


def fail(msg: str) -> int:
    print(f"PREFLIGHT REFUSED: {msg}", flush=True)
    return 2


def _commit_state(expected: str) -> tuple[bool, str, str]:
    """HEAD must be clean AND exactly the authorised commit."""
    commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        return False, commit, ("working tree is dirty; a confirmation session "
                               f"must run from committed code:\n{dirty[:400]}")
    if not commit.startswith(expected) and not expected.startswith(commit[:len(expected)]):
        return False, commit, (f"HEAD is {commit[:12]}, but this session is "
                               f"authorised for {expected}")
    return True, commit, ""


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--schedule", required=True)
    ap.add_argument("--markets-file", required=True)
    ap.add_argument("--events-file", required=True)
    ap.add_argument("--mode", required=True, choices=["confirmation", "validation"])
    ap.add_argument("--expected-series", required=True)
    ap.add_argument("--expected-anchor", required=True)
    ap.add_argument("--root-base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-events", type=int, default=40_000_000)
    ap.add_argument("--expected-commit", required=True,
                    help="the exact commit authorised to run this session. "
                         "Verified at preflight AND again immediately before "
                         "the socket opens; a drift between the two is a "
                         "refusal, never a silent upgrade.")
    a = ap.parse_args(argv[1:])

    sched = json.loads(Path(a.schedule).read_text())
    events = json.loads(Path(a.events_file).read_text())
    markets = [t for t in Path(a.markets_file).read_text().split() if t]
    seconds = int(sched["session_seconds"])

    print("=== PREFLIGHT ===", flush=True)

    if sched["anchor_occurrence_datetime"] != a.expected_anchor:
        return fail(f"anchor drifted: frozen "
                    f"{sched['anchor_occurrence_datetime']}, expected "
                    f"{a.expected_anchor}")
    print(f"  anchor                {a.expected_anchor}", flush=True)

    series = {v["series"] for v in events.values()}
    if series != {a.expected_series}:
        return fail(f"series restriction violated: {sorted(series)} != "
                    f"{a.expected_series}")
    print(f"  series restriction    {a.expected_series} ({len(markets)} markets)",
          flush=True)

    if set(events) != set(markets):
        return fail("markets.txt and events.json disagree")
    if len(markets) > NEVER_EXCEED_CONCURRENCY:
        return fail(f"{len(markets)} exceeds ceiling {NEVER_EXCEED_CONCURRENCY}")

    try:
        assert_capacity_relationship(a.max_events, HARD_STOP_FPS, seconds)
    except ValueError as exc:
        return fail(str(exc))
    print(f"  capacity              {a.max_events:,} > {HARD_STOP_FPS:,} x "
          f"{seconds:,} = {HARD_STOP_FPS * seconds:,}", flush=True)

    root = Path(a.root_base) / f"session={a.label}"
    if root.exists() and any(root.iterdir()):
        return fail(f"{root} exists and is not empty; a session owns its root")
    print(f"  archive root          fresh: {root}", flush=True)

    ok, commit, why = _commit_state(a.expected_commit)
    if not ok:
        return fail(why)
    print(f"  code commit           {commit[:12]} (clean, pinned)", flush=True)
    print(f"  schema                {ROW_SCHEMA_VERSION} / {LABEL_SCHEMA_VERSION}",
          flush=True)
    print(f"  mode                  {a.mode}", flush=True)

    start = datetime.fromisoformat(
        sched["scheduled_session_start"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    if start < now:
        return fail(f"scheduled start {start.isoformat()} is already past")
    print(f"  scheduled start       {start.isoformat()} "
          f"(in {(start - now).total_seconds() / 3600:.2f} h)", flush=True)
    # LIVENESS. Hours pass between freezing a candidate set and opening the
    # socket, and markets close in between. S04 froze 24 KXMLBHR markets that
    # were open at 05:01Z and captured 27 frames in 16 ms at 01:45Z the next
    # day, because all 24 had closed or resolved. Timing feasibility is not
    # market liveness, and a dead candidate set burns a whole session slot.
    statuses = {t: market_status(t) for t in markets}
    open_now = [t for t, st in statuses.items() if st == "open"]
    unknown = [t for t, st in statuses.items() if st is None]
    print(f"  candidate liveness    {len(open_now)}/{len(markets)} open"
          f"{f', {len(unknown)} unreadable' if unknown else ''}", flush=True)
    if not open_now and not unknown:
        return fail(
            f"every one of the {len(markets)} frozen candidates is closed or "
            f"resolved ({sorted(set(s for s in statuses.values() if s))}). "
            f"Capturing would produce a dead tape and consume a session slot; "
            f"reschedule under the frozen replacement rule instead.")
    print("=== PREFLIGHT PASSED — waiting ===", flush=True)

    while datetime.now(timezone.utc) < start:
        time.sleep(5)

    # RE-VERIFY AFTER THE WAIT. Hours pass between preflight and launch, and
    # the tree can move in that time -- a pull, a merge, a stray edit. The
    # session is authorised for ONE commit, so drift is a refusal rather than
    # a silent upgrade to whatever happens to be checked out now.
    ok, now_commit, why = _commit_state(a.expected_commit)
    if not ok:
        return fail(f"commit drifted between preflight and launch: {why}")
    print(f"  re-verified at launch: {now_commit[:12]}", flush=True)

    cmd = [sys.executable,
           str(REPO / "scripts" / "kalshi_microstructure_capture_runner.py"),
           "--label", a.label, "--mode", a.mode,
           "--markets-file", a.markets_file, "--events-file", a.events_file,
           "--seconds", str(seconds), "--max-events", str(a.max_events),
           "--root-base", a.root_base, "--out", a.out]
    print(f"=== LAUNCHING at {datetime.now(timezone.utc).isoformat()} ===",
          flush=True)
    os.execv(sys.executable, cmd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
