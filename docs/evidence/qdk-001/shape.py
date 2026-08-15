"""EDGE-DISCOVERY-001 power planning. COUNTS AND DATES ONLY — no outcomes read.

Deliberately does not touch winning_side, so the chronological split and the
minimum-n floors can be declared without seeing any result.
"""
import bisect
import os
import sqlite3
from collections import Counter

db = os.path.expanduser("~/projects/probability-arena/data/probability_arena.db")
c = sqlite3.connect("file:" + db + "?mode=ro", uri=True)

buckets: dict[str, list[float]] = {}
for tk, bend in c.execute(
    "select market_ticker, julianday(bucket_start) + bucket_seconds/86400.0 "
    "from market_price_tick_buckets "
    "where close_bid is not null and close_ask is not null"
):
    buckets.setdefault(tk, []).append(bend)
for v in buckets.values():
    v.sort()

resolved = {tk for (tk,) in c.execute(
    "select market_ticker from market_outcomes where winning_side in ('yes','no')")}

by_week, by_fc = Counter(), Counter()
events, tickers = set(), set()
dates = []
for tk, fn, ca, cad in c.execute(
    "select market_ticker, forecaster_name, julianday(created_at), date(created_at) "
    "from market_forecasts where evidence_depth='source_backed'"
):
    if tk not in resolved:
        continue
    arr = buckets.get(tk)
    if not arr:
        continue
    i = bisect.bisect_left(arr, ca) - 1
    if i < 0 or (ca - arr[i]) * 86400.0 > 900:
        continue
    by_week[cad[:7] + "-w" + str((int(cad[8:10]) - 1) // 7 + 1)] += 1
    by_fc[fn] += 1
    tickers.add(tk)
    # event = ticker minus its final "-<strike>" segment
    events.add(tk.rsplit("-", 1)[0] if "-" in tk else tk)
    dates.append(cad)

print(f"matched observations (no-lookahead, <=900s): {len(dates)}")
print(f"distinct market tickers: {len(tickers)}")
print(f"distinct EVENTS (ticker minus strike): {len(events)}")
print(f"by forecaster: {dict(by_fc)}")
print("\nby week:")
for k in sorted(by_week):
    print(f"  {k}  {by_week[k]}")

dates.sort()
n = len(dates)
for frac in (0.60, 0.65, 0.70):
    idx = int(n * frac)
    print(f"\nsplit at {frac:.0%} -> date {dates[idx]}  "
          f"train={idx}  holdout={n-idx}")
print(f"\ndate range: {dates[0]} -> {dates[-1]}")
