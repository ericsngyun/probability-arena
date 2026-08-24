"""Pick the next qualifying session anchor for a target TTE bin. Deterministic.

Scheduling only. This tool reads market **timing** and nothing else — no price,
no spread, no volume, no activity, no model output. It cannot express a
preference for a busier-looking game, because it never sees activity.

The rule (capture plan Addendum 1 + Addendum 2):

  * a session's start is chosen so that a complete 300 s post-warmup panel
    interval lies wholly inside the target bin;
  * the anchor is the EARLIEST occurrence time whose required start is still in
    the future;
  * the candidate set is markets sharing that occurrence time first, then the
    next occurrence times, ordered `(occurrence_datetime ASC, ticker ASC)` and
    truncated to the concurrency ceiling.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.microstructure.panel import (  # noqa: E402
    NEVER_EXCEED_CONCURRENCY, TTE_APPROACHING, TTE_FAR, TTE_LATE_RESOLUTION,
    TTE_LIVE_EVENT, TTE_NEAR_EVENT, WARMUP_S, tte_bin,
)
from scripts.kalshi_activity_profile_freeze_universe import (  # noqa: E402
    candidates_closing_on)

SELECTION_RULE_VERSION = "capture-plan-addendum-1+2 / edge-amendment-4"
SERIES = ("KXMLBGAME", "KXMLBTOTAL", "KXMLBHR", "KXATPMATCH", "KXWTAMATCH",
          "KXWNBATOTAL", "KXWNBAGAME", "KXNFLGAME")

#: TTE the FIRST research tick should land on, per bin. Chosen at each bin's
#: upper edge so the session descends through the whole stratum, maximising
#: the number of complete intervals that fall wholly inside it.
#: `far` is unbounded above, so its first tick is placed high enough that the
#: WHOLE session stays above the 21,600 edge; anchoring it just above the edge
#: would descend out of the stratum within one interval.
FIRST_TICK_TTE_S = {
    TTE_FAR: 21_600 + 10_800,
    TTE_APPROACHING: 21_600,
    TTE_NEAR_EVENT: 7_200,
    TTE_LIVE_EVENT: 900,
    TTE_LATE_RESOLUTION: -600,
}

MIN_LEAD_S = 120   # do not schedule a start we cannot actually reach


def required_start(event: datetime, target_bin: str) -> datetime:
    """Session start that puts the first post-warmup tick at the bin's edge."""
    return event - timedelta(seconds=FIRST_TICK_TTE_S[target_bin] + WARMUP_S)


def covering_intervals(event: datetime, start: datetime, seconds: int,
                       target_bin: str) -> int:
    """Complete 300 s intervals falling wholly inside THE TARGET BIN.

    Both endpoints must be in `target_bin` -- not merely in the same bin as
    each other. Testing `tte_bin(a) == tte_bin(b)` counts intervals that sit
    wholly inside some *other* stratum, which made a 900 s-wide `live_event`
    target report 34 covering intervals for a 3 h session.
    """
    n, t = 0, start + timedelta(seconds=WARMUP_S)
    while t + timedelta(seconds=300) <= start + timedelta(seconds=seconds):
        a = (event - t).total_seconds()
        if tte_bin(a) == target_bin and tte_bin(a - 300) == target_bin:
            n += 1
        t += timedelta(seconds=300)
    return n


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-bin", required=True, choices=sorted(FIRST_TICK_TTE_S))
    ap.add_argument("--seconds", type=int, default=10_800)
    ap.add_argument("--days", default="", help="ET days to enumerate, comma-separated")
    ap.add_argument("--series", default="",
                    help="restrict to these series, comma-separated. Supplied "
                         "by the COVERAGE layer to discharge a diversity "
                         "obligation; it is a design quantity, never an "
                         "activity signal, and this module still cannot see "
                         "activity for the series it is handed.")
    ap.add_argument("--out-prefix", default="/tmp/anchor")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv[1:])

    now = datetime.now(timezone.utc)
    days = ([d for d in a.days.split(",") if d]
            or [(now + timedelta(days=k)).strftime("%Y-%m-%d") for k in (0, 1, 2)])

    allowed = tuple(x for x in a.series.split(",") if x) or SERIES
    unknown = set(allowed) - set(SERIES)
    if unknown:
        print(f"REFUSED: unknown series {sorted(unknown)}"); return 2
    pool = {}
    for d in days:
        pool.update(candidates_closing_on(d, series=list(allowed)))
    if not pool:
        print("REFUSED: no open markets enumerated"); return 2

    def ev(t):
        return datetime.fromisoformat(pool[t]["event_time"].replace("Z", "+00:00"))

    # Every distinct occurrence time whose required start is still reachable,
    # earliest first. No activity input, so no way to prefer a busy slate.
    times = sorted({ev(t) for t in pool})
    feasible = [e for e in times
                if required_start(e, a.target_bin) >= now + timedelta(seconds=MIN_LEAD_S)
                and covering_intervals(e, required_start(e, a.target_bin), a.seconds,
                                       a.target_bin) > 0]
    print(f"now={now.isoformat()}  target_bin={a.target_bin}")
    print(f"pool={len(pool)} markets, {len(times)} distinct occurrence times, "
          f"{len(feasible)} feasible anchors")
    if not feasible:
        print("NO FEASIBLE ANCHOR in the enumerated days -- widen --days and retry")
        return 3

    anchor = feasible[0]
    start = required_start(anchor, a.target_bin)
    # anchor's own slate first, then later slates, each ticker-ascending
    chosen = sorted(pool, key=lambda t: (ev(t) != anchor, ev(t), t))[:NEVER_EXCEED_CONCURRENCY]

    record = {
        "scheduled_target_bin": a.target_bin,
        "anchor_event_id": anchor.isoformat(),
        "anchor_occurrence_datetime": anchor.isoformat(),
        "scheduled_session_start": start.isoformat(),
        "session_seconds": a.seconds,
        "selection_rule_version": SELECTION_RULE_VERSION,
        "series_restriction": list(allowed),
        "candidate_count_at_freeze": len(chosen),
        "pool_size_at_freeze": len(pool),
        "distinct_occurrence_times": len(times),
        "feasible_anchors": len(feasible),
        "projected_covering_intervals": covering_intervals(anchor, start, a.seconds,
                                                          a.target_bin),
        "replacement_reason": None,
        "frozen_at_utc": now.isoformat(),
        "markets": chosen,
        "on_anchor_slate": sum(1 for t in chosen if ev(t) == anchor),
    }
    print(f"ANCHOR  {anchor.isoformat()}")
    print(f"START   {start.isoformat()}   (in {(start-now).total_seconds()/3600:.2f} h)")
    print(f"candidates {len(chosen)} ({record['on_anchor_slate']} on the anchor slate)")
    print(f"projected covering intervals in {a.target_bin}: "
          f"{record['projected_covering_intervals']}")
    if a.dry_run:
        print("DRY RUN -- nothing frozen")
        return 0
    Path(f"{a.out_prefix}_events.json").write_text(json.dumps(
        {t: {"series": pool[t]["series"],
             "occurrence_datetime": pool[t]["event_time"]} for t in chosen}, indent=1))
    Path(f"{a.out_prefix}_markets.txt").write_text("\n".join(chosen))
    Path(f"{a.out_prefix}_schedule.json").write_text(json.dumps(record, indent=2))
    print(f"froze {a.out_prefix}_{{events.json,markets.txt,schedule.json}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
