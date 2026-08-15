"""delta-S: our source-backed forecasts vs the contemporaneous executable market.

READ-ONLY. NOT a strategy backtest — every forecast was made before its outcome
resolved, so scoring it against the quote that existed at forecast time is a
legitimate prospective-in-origin measurement.

q = (close_bid + close_ask)/200 from MarketPriceTickBucket -> dollars.
delta_S = Brier(market) - Brier(ours);  > 0 means WE BEAT THE MARKET.
"""
import bisect
import math
import os
import sqlite3
import statistics as st

db = os.path.expanduser("~/projects/probability-arena/data/probability_arena.db")
c = sqlite3.connect("file:" + db + "?mode=ro", uri=True)

# 1) buckets by ticker, sorted by time, with an executable two-sided quote
buckets: dict[str, list[tuple[float, float]]] = {}
for tk, bs, cb, ca in c.execute(
    "select market_ticker, julianday(bucket_start), close_bid, close_ask "
    "from market_price_tick_buckets "
    "where close_bid is not null and close_ask is not null"
):
    buckets.setdefault(tk, []).append((bs, (cb + ca) / 200.0))
for v in buckets.values():
    v.sort()
print(f"tickers with two-sided buckets: {len(buckets)}")

# 2) resolved outcomes
outcome = {tk: ws for tk, ws in c.execute(
    "select market_ticker, winning_side from market_outcomes "
    "where winning_side in ('yes','no')")}
print(f"resolved markets: {len(outcome)}")

# 3) forecasts
rows = []
skipped_no_quote = 0
for tk, fn, p, ca in c.execute(
    "select market_ticker, forecaster_name, estimated_probability, "
    "julianday(created_at) from market_forecasts where evidence_depth='source_backed'"
):
    ws = outcome.get(tk)
    if ws is None:
        continue
    arr = buckets.get(tk)
    if not arr:
        skipped_no_quote += 1
        continue
    i = bisect.bisect_left(arr, (ca, -1.0))
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(arr):
            dt = abs(arr[j][0] - ca) * 86400.0
            if best is None or dt < best[0]:
                best = (dt, arr[j][1])
    if best is None or best[0] > 300:
        skipped_no_quote += 1
        continue
    rows.append((tk, fn, float(p), ws, min(max(best[1], 0.0), 1.0)))

print(f"matched (source_backed, resolved, quote within 300s): {len(rows)}"
      f"   [skipped for no nearby quote: {skipped_no_quote}]")
if not rows:
    raise SystemExit(0)

d, bm, bo = [], [], []
by_ticker, by_fc = {}, {}
for tk, fn, p, ws, q in rows:
    y = 1.0 if ws == "yes" else 0.0
    m = (q - y) ** 2
    o = (p - y) ** 2
    d.append(m - o); bm.append(m); bo.append(o)
    by_ticker.setdefault(tk, []).append(m - o)
    by_fc.setdefault(fn, []).append(m - o)

n = len(d)
mean = st.mean(d)
se = st.stdev(d) / math.sqrt(n) if n > 1 else 0.0
print()
print(f"  market Brier = {st.mean(bm):.5f}")
print(f"  our    Brier = {st.mean(bo):.5f}")
print(f"  DELTA-S      = {mean:+.5f}    (>0 = we beat the market)")
print(f"  naive SE={se:.5f}  t={mean/se if se else 0:+.2f}   <-- ANTICONSERVATIVE")

cl = [st.mean(v) for v in by_ticker.values()]
k = len(cl)
cm = st.mean(cl)
cse = st.stdev(cl) / math.sqrt(k) if k > 1 else 0.0
print(f"  clusters (distinct markets) = {k}")
print(f"  CLUSTERED mean={cm:+.5f}  SE={cse:.5f}  t={cm/cse if cse else 0:+.2f}")
print(f"  clustered 95% CI = [{cm - 1.96*cse:+.5f}, {cm + 1.96*cse:+.5f}]")

print("\n--- by forecaster ---")
for fn, v in sorted(by_fc.items(), key=lambda x: -len(x[1])):
    mm = st.mean(v)
    ss = st.stdev(v) / math.sqrt(len(v)) if len(v) > 1 else 0.0
    print(f"  {fn:<22} n={len(v):<6} deltaS={mm:+.5f} SE={ss:.5f} t={mm/ss if ss else 0:+.2f}")
