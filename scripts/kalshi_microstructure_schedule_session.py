"""Decide the next tranche session: coverage layer, then anchor layer.

    coverage scheduler  ->  which obligation, and on which future date/slate
    anchor scheduler    ->  which exact occurrence on that slate

The two stay separate on purpose. The coverage layer sees only design
obligations; the anchor layer sees only event timing. Neither can see activity,
price or any prior result, and both are guarded by tests that fail if a
reference appears.

Emits a full audit artifact for the decision, so a scheduling choice is as
inspectable as a capture.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.microstructure import coverage as C            # noqa: E402
from scripts.kalshi_activity_profile_freeze_universe import (  # noqa: E402
    candidates_closing_on)
from scripts.kalshi_microstructure_schedule_anchor import (  # noqa: E402
    SELECTION_RULE_VERSION, covering_intervals, required_start)


def enumerate_slates(days: list[str], *, seconds: int, target_bin: str,
                     now: datetime) -> list[C.SlateOption]:
    """Which eligible series have a FEASIBLE event on each day.

    Feasible means the frozen anchor rule could actually place a session: the
    required start is still in the future and at least one complete 300 s
    interval lands inside the target bin. Only timing is consulted.
    """
    out = []
    for day in days:
        pool = candidates_closing_on(day, series=list(C.ELIGIBLE_SERIES))
        series = set()
        for tk, meta in pool.items():
            ev = datetime.fromisoformat(meta["event_time"].replace("Z", "+00:00"))
            start = required_start(ev, target_bin)
            if start < now + timedelta(seconds=120):
                continue
            if covering_intervals(ev, start, seconds, target_bin) > 0:
                series.add(meta["series"])
        if series:
            out.append(C.SlateOption(day_et=day, series_available=tuple(sorted(series))))
    return out


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True,
                    help="JSON list of completed session records")
    ap.add_argument("--seconds", type=int, default=10_800)
    ap.add_argument("--horizon-days", type=int, default=7)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv[1:])

    records = [C.SessionRecord(**r) for r in json.loads(Path(a.ledger).read_text())]
    state = C.state_from_ledger(records)
    obligation = C.next_obligation(state)
    now = datetime.now(timezone.utc)

    print(f"sessions counted: {state.sessions_completed}")
    print(f"bin remaining   : {state.bin_remaining()}")
    print(f"series used     : {state.series_used}")
    print(f"weekend         : {state.weekend_sessions} done, "
          f"{state.weekend_remaining()} still required")
    print(f"NEXT OBLIGATION : {obligation}")
    if obligation is None:
        print("tranche complete; nothing to schedule")
        return 0

    days = [(now + timedelta(days=k)).strftime("%Y-%m-%d")
            for k in range(0, a.horizon_days + 1)]
    slates = enumerate_slates(days, seconds=a.seconds, target_bin=obligation,
                              now=now)
    print(f"feasible slates : {[(s.day_et, s.series_available) for s in slates]}")

    decision = C.choose_slate(state, slates, target_bin=obligation)
    artifact = {
        "milestone": "MARKET-MICROSTRUCTURE-EDGE-001",
        "phase": "coverage_scheduling_decision",
        "decided_at_utc": now.isoformat(),
        "selection_rule_version": SELECTION_RULE_VERSION,
        "coverage_state": state.to_dict(),
        "feasibility": C.feasibility_report(state),
        "decision": decision,
    }
    Path(a.out).write_text(json.dumps(artifact, indent=2, default=str))
    print()
    print(f"SELECTED SLATE  : {decision.get('selected_day_et')}")
    print(f"PREFERRED SERIES: {decision.get('preferred_series')}")
    print(f"REASON          : {decision.get('reason')}")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
