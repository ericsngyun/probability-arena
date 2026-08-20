"""PROD-ACTIVITY-PROFILE-001 — freeze one day's observation universe.

Amendment 2. Read-only. The rule is written in the preregistration BEFORE this
script runs, and this script implements exactly that rule and nothing else:

1. candidates = open markets whose `close_time` falls on the profile day (ET);
2. discovery = one global `ticker` pass; a candidate needs >= 1 ticker frame;
3. selection = the 40 with the most ticker frames, ties by ticker ascending;
4. positive control = a 41st market closing the same day with ZERO ticker
   frames, first by ticker ascending. It must FAIL the s4 criteria or the run
   is void.

`ticker` is a CANDIDATE-DISCOVERY heuristic and nothing more (s5). It is
unsequenced with unknowable completeness, so it selects what we WATCH and never
what qualifies: s4's criteria are evaluated exclusively from order-book
evidence, by a different tool, after all six windows.

No volume, open-interest, liquidity or top-of-book field is read anywhere here.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REST = "https://api.elections.kalshi.com/trade-api/v2"
ET = ZoneInfo("America/New_York")
UNIVERSE_SIZE = 40


def _get(path: str) -> dict:
    req = urllib.request.Request(REST + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def candidates_closing_on(day_et: str, *, series: list[str]) -> dict:
    """Open markets whose close_time falls on `day_et` (YYYY-MM-DD, ET).

    Series are enumerated explicitly rather than paged blindly: the venue
    reports ~12,000 open markets dominated by one non-orderbook series, and
    paging the whole book to find sports markets is both slow and fragile.
    The series list is an input, recorded in the output, not a hidden constant.
    """
    start = datetime.fromisoformat(day_et).replace(tzinfo=ET)
    end = start + timedelta(days=1)
    out = {}
    for s in series:
        cursor, pages = None, 0
        while pages < 10:
            q = f"/markets?status=open&limit=1000&series_ticker={s}"
            if cursor:
                q += f"&cursor={cursor}"
            d = _get(q)
            for m in d.get("markets", []):
                ct = m.get("close_time")
                if not ct:
                    continue
                when = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                if start <= when.astimezone(ET) < end:
                    out[m["ticker"]] = {"close_time": ct, "series": s}
            cursor = d.get("cursor")
            pages += 1
            if not cursor or not d.get("markets"):
                break
    return out


def select(candidates: dict, ticker_counts: Counter) -> dict:
    """The rule, applied. Deterministic and telemetry-blind beyond ticker counts."""
    live = {t: ticker_counts.get(t, 0) for t in candidates}
    active = sorted(((c, t) for t, c in live.items() if c > 0),
                    key=lambda ct: (-ct[0], ct[1]))
    silent = sorted(t for t, c in live.items() if c == 0)

    universe = [t for _c, t in active[:UNIVERSE_SIZE]]
    control = silent[0] if silent else None

    return {
        "universe": universe,
        "universe_size": len(universe),
        "positive_control": control,
        "control_note": ("s7 anti-vacuity: this market emitted ZERO ticker "
                         "frames during discovery and MUST FAIL the s4 "
                         "criteria. If it passes, the measurement is broken "
                         "and the run is void."),
        "candidates_total": len(candidates),
        "candidates_with_ticker_activity": len(active),
        "candidates_silent": len(silent),
        "ticker_frames_selected": {t: live[t] for t in universe},
        "shortfall": max(0, UNIVERSE_SIZE - len(universe)),
    }


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="profile day, YYYY-MM-DD (ET)")
    ap.add_argument("--series", default="KXMLBGAME,KXMLBTOTAL,KXMLBHR,"
                                        "KXATPMATCH,KXWTAMATCH,KXWNBATOTAL,"
                                        "KXWNBAGAME,KXNFLGAME")
    ap.add_argument("--ticker-counts", required=True,
                    help="JSON {ticker: frames} from the discovery pass")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv[1:])

    series = [s for s in args.series.split(",") if s]
    cands = candidates_closing_on(args.day, series=series)
    counts = Counter(json.loads(Path(args.ticker_counts).read_text()))
    result = select(cands, counts)
    result.update({
        "milestone": "PROD-ACTIVITY-PROFILE-001",
        "phase": "freeze_universe",
        "profile_day_et": args.day,
        "series_enumerated": series,
        "rule": "Amendment 2 s3: open markets closing this day; >=1 ticker "
                "frame in discovery; top 40 by ticker frames, ties by ticker "
                "ascending; 41st zero-frame market as positive control",
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "external_calls_are_read_only": True,
    })
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True))

    print(f"  profile day (ET)        {args.day}")
    print(f"  candidates closing      {result['candidates_total']}")
    print(f"  with ticker activity    {result['candidates_with_ticker_activity']}")
    print(f"  silent                  {result['candidates_silent']}")
    print(f"  UNIVERSE                {result['universe_size']}")
    print(f"  positive control        {result['positive_control']}")
    if result["shortfall"]:
        print(f"  SHORTFALL               {result['shortfall']} — fewer than "
              f"{UNIVERSE_SIZE} active candidates exist; reported, not "
              f"back-filled from silent markets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
