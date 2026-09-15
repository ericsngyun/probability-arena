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
from app.microstructure.lifecycle_compatibility import (  # noqa: E402
    TARGET_BIN_REACHABLE, project_target_bin_reachability,
)
from app.microstructure.capture_contract import (  # noqa: E402
    authorize_capture,
)


REST = "https://api.elections.kalshi.com/trade-api/v2"

#: Statuses at which a market can still trade.
#:
#: `open` is a QUERY FILTER, not a status value: `?status=open` returns markets
#: whose own `status` field reads **`active`**. Checking for `"open"` therefore
#: rejected 24 perfectly live markets on the guard's first live firing. Dead
#: states are `closed`, `determined` and `finalized` -- S04's candidates were
#: all `finalized`. Both spellings are accepted so a venue rename cannot
#: silently turn every session into a refusal.
LIVE_STATUSES = frozenset({"active", "open"})


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


def _load_genesis(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text())


def check_capture_authorization(genesis: dict, schedule: Path, events: Path,
                                markets: Path, *, when: str):
    """Executable-content authorization. Repo tip may differ from code_commit."""
    auth = authorize_capture(
        genesis=genesis,
        schedule_path=schedule,
        events_path=events,
        markets_path=markets,
    )
    print(f"  capture authorization ({when})", flush=True)
    print(f"    repo_tip                    {auth.repo_tip[:12]}", flush=True)
    print(f"    authorized_capture_contract {auth.authorized_capture_contract[:12]}",
          flush=True)
    print(f"    capture_code_fingerprint    "
          f"{'MATCH' if auth.capture_code_fingerprint == auth.expected_capture_code_fingerprint else 'DRIFT'}",
          flush=True)
    print(f"    freeze_fingerprint          "
          f"{'MATCH' if auth.freeze_fingerprint == auth.expected_freeze_fingerprint else 'DRIFT'}",
          flush=True)
    print(f"    result                      {auth.result}", flush=True)
    return auth


def check_liveness(markets, *, when, fail):
    """Are the frozen candidates still tradable? Fails closed.

    Timing feasibility is not market liveness. A dead candidate set burns a
    whole session slot and -- worse -- yields a tape that looks like a quiet
    market rather than an absent one.

    `unknown` (unreadable status) is NOT treated as dead: an unreachable
    status endpoint is our failure, not the market's, and refusing on it
    would let a status outage cancel sessions whose markets are fine.
    """
    statuses = {t: market_status(t) for t in markets}
    open_now = [t for t, st in statuses.items() if st in LIVE_STATUSES]
    unknown = [t for t, st in statuses.items() if st is None]
    print(f"  candidate liveness    ({when}) {len(open_now)}/{len(markets)} "
          f"live{f', {len(unknown)} unreadable' if unknown else ''}",
          flush=True)
    if not open_now and not unknown:
        seen = sorted({s for s in statuses.values() if s})
        fail(f"at {when}: every one of the {len(markets)} frozen candidates "
             f"is closed or resolved ({seen}). Capturing would produce a dead "
             f"tape and consume a session slot; reschedule under the frozen "
             f"replacement rule instead.")
        return False
    return True


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
    ap.add_argument("--genesis", required=True,
                    help="frozen session genesis JSON carrying capture_code_"
                         "fingerprint and freeze_fingerprint. Authorization "
                         "binds to those fingerprints, not to HEAD==code_commit.")
    ap.add_argument("--expected-commit", default="",
                    help="deprecated: capture-contract tip label only; "
                         "ignored for the live check when --genesis is present. "
                         "Retained so older runbooks do not break on the flag.")
    ap.add_argument("--validate-only", action="store_true",
                    help="run authorization + mechanical preflight checks and "
                         "exit without waiting or opening a socket")
    a = ap.parse_args(argv[1:])

    sched = json.loads(Path(a.schedule).read_text())
    events = json.loads(Path(a.events_file).read_text())
    markets = [t for t in Path(a.markets_file).read_text().split() if t]
    seconds = int(sched["session_seconds"])
    genesis = _load_genesis(Path(a.genesis))
    if genesis is None:
        return fail(f"genesis not found: {a.genesis}")

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

    auth = check_capture_authorization(
        genesis, Path(a.schedule), Path(a.events_file), Path(a.markets_file),
        when="preflight")
    if not auth.authorized:
        return fail(auth.reason)
    print(f"  schema                {ROW_SCHEMA_VERSION} / {LABEL_SCHEMA_VERSION}",
          flush=True)
    print(f"  mode                  {a.mode}", flush=True)

    start = datetime.fromisoformat(
        sched["scheduled_session_start"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    if start < now and not a.validate_only:
        return fail(f"scheduled start {start.isoformat()} is already past")
    print(f"  scheduled start       {start.isoformat()} "
          f"(in {(start - now).total_seconds() / 3600:.2f} h)", flush=True)

    if a.validate_only:
        # Structural reachability only — no live status GETs required for the
        # authorization/drift boundary test. Sunday arm still checks liveness.
        target_bin = sched.get("scheduled_target_bin") or sched.get("target_bin")
        if target_bin:
            occurrence = datetime.fromisoformat(
                sched["anchor_occurrence_datetime"].replace("Z", "+00:00"))
            code, why = project_target_bin_reachability(
                target_bin=target_bin,
                series=a.expected_series,
                occurrence=occurrence,
                session_start=start,
                session_seconds=seconds,
                now=now,
                candidate_statuses=None,
            )
            print(f"  target-bin reachability {code}", flush=True)
            if code != TARGET_BIN_REACHABLE:
                return fail(f"target-bin reachability {code}: {why}")
        print("=== VALIDATE-ONLY AUTHORIZED (no socket, no wait) ===", flush=True)
        return 0

    if not check_liveness(markets, when="preflight", fail=fail):
        return 1

    # Projected target-bin survival — lifecycle/clock/status only. Refuses a
    # slot that cannot structurally harvest a complete in-bin observation unit.
    target_bin = sched.get("scheduled_target_bin") or sched.get("target_bin")
    if target_bin:
        statuses = {t: market_status(t) for t in markets}
        occurrence = datetime.fromisoformat(
            sched["anchor_occurrence_datetime"].replace("Z", "+00:00"))
        code, why = project_target_bin_reachability(
            target_bin=target_bin,
            series=a.expected_series,
            occurrence=occurrence,
            session_start=start,
            session_seconds=seconds,
            now=now,
            candidate_statuses=statuses,
        )
        print(f"  target-bin reachability {code}", flush=True)
        if code != TARGET_BIN_REACHABLE:
            return fail(f"target-bin reachability {code}: {why}")

    print("=== PREFLIGHT PASSED — waiting ===", flush=True)

    while datetime.now(timezone.utc) < start:
        time.sleep(5)

    # RE-VERIFY AFTER THE WAIT. Hours pass between preflight and launch, and
    # the tree can move in that time -- a pull, a merge, a stray edit. The
    # session is authorised for ONE capture-code fingerprint, so drift is a
    # refusal rather than a silent upgrade to whatever happens to be checked
    # out now. Repo-tip docs commits are fine; collector-byte drift is not.
    auth2 = check_capture_authorization(
        genesis, Path(a.schedule), Path(a.events_file), Path(a.markets_file),
        when="launch")
    if not auth2.authorized:
        return fail(f"capture authorization drifted between preflight and "
                    f"launch: {auth2.reason}")
    if auth2.capture_code_fingerprint != auth.capture_code_fingerprint:
        return fail("capture-code fingerprint changed during the wait")
    print(f"  re-verified at launch: repo_tip={auth2.repo_tip[:12]}", flush=True)

    # RE-CHECK LIVENESS TOO. The preflight check above was written because S04
    # captured 27 frames against 24 dead markets -- and then it was placed
    # BEFORE the wait it describes, so it could not catch its own scenario.
    # S07 proved that: preflight passed at 02:45Z with the candidates live,
    # the script slept 100 minutes, and by launch at 04:25Z all 24 KXMLBHR
    # markets had resolved with the game. The capture ran its full 10,800s,
    # produced 75 frames in the first 5.6 minutes and nothing after, and
    # exited 0. Market drift needs the same treatment as code drift: checked
    # on BOTH sides of the wait, and a refusal rather than a dead tape.
    if not check_liveness(markets, when="launch", fail=fail):
        return 1

    cmd = [sys.executable,
           str(REPO / "scripts" / "kalshi_microstructure_capture_runner.py"),
           "--label", a.label, "--mode", a.mode,
           "--markets-file", a.markets_file, "--events-file", a.events_file,
           "--seconds", str(seconds), "--max-events", str(a.max_events),
           "--root-base", a.root_base, "--out", a.out]
    print(f"=== LAUNCHING at {datetime.now(timezone.utc).isoformat()} ===",
          flush=True)
    os.execv(sys.executable, cmd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
