"""PROD-ACTIVITY-PROFILE-001 — run one profile day, in order, with the gate.

Sequential BY DESIGN. s6 requires that a 3,500 f/s breach halt the set, and
independent cron entries cannot do that: each would fire regardless of what the
previous window measured. This driver runs discovery, freezes that day's
universe, then runs the three slots in order and REFUSES to start a later window
once the gate has been breached.

Read-only throughout. Every window gets its own immutable archive root.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
ET = ZoneInfo("America/New_York")
SLOTS = {"A": "10:00", "B": "14:00", "C": "20:00"}
DISCOVERY_MINUTES = 5


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def sleep_until(when_utc: datetime):
    while True:
        delta = (when_utc - datetime.now(timezone.utc)).total_seconds()
        if delta <= 0:
            return
        log(f"waiting {delta/60:.1f} min until {when_utc.isoformat()}")
        time.sleep(min(delta, 900))


def slot_time_utc(day_et: str, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return (datetime.fromisoformat(day_et)
            .replace(hour=h, minute=m, tzinfo=ET).astimezone(timezone.utc))


def count_ticker_frames(env_dir: Path) -> Counter:
    c = Counter()
    for d in sorted(env_dir.glob("**/segment=*")):
        f = d / "events.jsonl.gz"
        if not f.exists():
            continue
        with gzip.open(f, "rt") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("message_type") == "ticker" and r.get("market_ticker"):
                    c[r["market_ticker"]] += 1
    return c


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="profile day, YYYY-MM-DD (ET)")
    ap.add_argument("--base", required=True)
    ap.add_argument("--window-seconds", type=int, default=1500)
    ap.add_argument("--series", default="KXMLBGAME,KXMLBTOTAL,KXMLBHR,"
                                        "KXATPMATCH,KXWTAMATCH,KXWNBATOTAL,"
                                        "KXWNBAGAME,KXNFLGAME")
    a = ap.parse_args(argv[1:])
    base = Path(a.base)
    base.mkdir(parents=True, exist_ok=True)
    day_dir = base / f"day={a.day}"
    day_dir.mkdir(parents=True, exist_ok=True)

    from scripts.kalshi_activity_profile_freeze_universe import (
        candidates_closing_on)

    # --- candidates -----------------------------------------------------
    log(f"enumerating candidates whose EVENT occurs on {a.day} (ET)")
    series = [s for s in a.series.split(",") if s]
    cands = candidates_closing_on(a.day, series=series)
    log(f"{len(cands)} candidates")
    cand_file = day_dir / "candidates.txt"
    cand_file.write_text("\n".join(sorted(cands)))
    (day_dir / "candidates.json").write_text(json.dumps(cands, indent=1,
                                                        sort_keys=True))
    if not cands:
        log("REFUSED: zero candidates. Not running a day with no universe.")
        return 2

    # --- discovery ------------------------------------------------------
    disc_at = slot_time_utc(a.day, SLOTS["A"]) - timedelta(minutes=15)
    sleep_until(disc_at)
    log(f"discovery: {DISCOVERY_MINUTES} min ticker pass over {len(cands)} markets")
    disc_root = day_dir / "discovery"
    rc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "kalshi_activity_profile_window.py"),
         "--label", f"PAP001-discovery-{a.day}", "--day", a.day,
         "--slot", "DISCOVERY", "--tickers-file", str(cand_file),
         "--channels", "ticker", "--seconds", str(DISCOVERY_MINUTES * 60),
         "--root-base", str(day_dir / "_discovery"),
         "--out", str(day_dir / "discovery-window.json")]).returncode
    disc_env = day_dir / "_discovery" / f"day={a.day}" / "slot=DISCOVERY" / "env=production"
    counts = count_ticker_frames(disc_env) if disc_env.exists() else Counter()
    (day_dir / "ticker-counts.json").write_text(json.dumps(dict(counts),
                                                           indent=1, sort_keys=True))
    log(f"discovery: {sum(counts.values())} ticker frames across "
        f"{len(counts)} markets (rc={rc})")

    # --- freeze the universe --------------------------------------------
    uni_out = day_dir / "universe.json"
    subprocess.run(
        [sys.executable,
         str(REPO / "scripts" / "kalshi_activity_profile_freeze_universe.py"),
         "--day", a.day, "--series", a.series,
         "--ticker-counts", str(day_dir / "ticker-counts.json"),
         "--out", str(uni_out)], check=False)
    uni = json.loads(uni_out.read_text())
    if not uni["universe"]:
        log("REFUSED: discovery found no active market. Universe empty; "
            "the day is reported as such rather than back-filled.")
        return 2
    watch = list(uni["universe"])
    if uni["positive_control"]:
        watch.append(uni["positive_control"])      # s7 anti-vacuity arm
    uni_file = day_dir / "universe.txt"
    uni_file.write_text("\n".join(watch))
    log(f"universe frozen: {len(uni['universe'])} markets + "
        f"control {uni['positive_control']}")

    # --- the three windows ----------------------------------------------
    for slot, hhmm in SLOTS.items():
        sleep_until(slot_time_utc(a.day, hhmm))
        log(f"slot {slot} ({hhmm} ET) starting")
        out = day_dir / f"window-{slot}.json"
        subprocess.run(
            [sys.executable,
             str(REPO / "scripts" / "kalshi_activity_profile_window.py"),
             "--label", f"PAP001-{a.day}-{slot}", "--day", a.day,
             "--slot", slot, "--tickers-file", str(uni_file),
             "--seconds", str(a.window_seconds),
             "--root-base", str(day_dir / "_windows"), "--out", str(out)],
            check=False)
        res = json.loads(out.read_text())
        hs = res["hard_stop"]
        log(f"slot {slot}: {res['validity']} | peak_1s_sliding="
            f"{hs['observed']} | gate={'BREACHED' if hs['breached'] else 'clear'}")
        if hs["breached"]:
            log("HALTING THE SET — s6: no later window may run on a breached "
                "configuration. Halve the universe and restart all six.")
            (day_dir / "HALTED.json").write_text(json.dumps(res, indent=2,
                                                            default=str))
            return 3

    log(f"day {a.day} complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
