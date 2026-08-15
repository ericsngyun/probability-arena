"""EDGE-DISCOVERY-001 frozen analysis extract. READ-ONLY (mode=ro).

Produces ONE dataset that all four preregistered experiments share, so E1-E4
provably run on identical data and EVO's bucket table is scanned once.

Specification is docs/experiments/EDGE-DISCOVERY-001-PREREGISTRATION.md, frozen
at main 16abe02 BEFORE this ran.
"""
import bisect
import csv
import os
import sqlite3

DB = os.path.expanduser("~/projects/probability-arena/data/probability_arena.db")
OUT = "/tmp/edge_discovery_001_dataset.csv"
SPLIT = "2026-08-02"          # frozen in the preregistration
MATCH_MAX_S = 900.0           # bucket must END within 900s BEFORE the forecast
HORIZONS = [("5m", 300), ("15m", 900), ("30m", 1800),
            ("1h", 3600), ("3h", 10800), ("6h", 21600)]

c = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)

# --- buckets per ticker, keyed by bucket END time (julian days) ----------------
bk: dict[str, list[tuple]] = {}
for tk, bend, cb, ca, sp, liq, cm in c.execute(
    "select market_ticker, julianday(bucket_start) + bucket_seconds/86400.0, "
    "close_bid, close_ask, spread_avg, liquidity_avg, close_mid "
    "from market_price_tick_buckets "
    "where close_bid is not null and close_ask is not null"
):
    bk.setdefault(tk, []).append((bend, cb, ca, sp, liq, cm))
for v in bk.values():
    v.sort()
print(f"tickers with two-sided buckets: {len(bk)}")

outcome = {}
for tk, ws, ct in c.execute(
    "select market_ticker, winning_side, julianday(close_time) "
    "from market_outcomes where winning_side in ('yes','no')"
):
    outcome[tk] = (ws, ct)
print(f"resolved markets: {len(outcome)}")


def at_or_before(arr, t, max_s):
    """Latest bucket whose END is <= t, within max_s seconds. No lookahead."""
    i = bisect.bisect_right(arr, (t, float("inf"),)) - 1
    if i < 0:
        return None
    if (t - arr[i][0]) * 86400.0 > max_s:
        return None
    return arr[i]


def at_or_after(arr, t, tol_s):
    """Earliest bucket whose END is >= t, within tol_s seconds. For horizons."""
    i = bisect.bisect_left(arr, (t, -1.0))
    if i >= len(arr):
        return None
    if (arr[i][0] - t) * 86400.0 > tol_s:
        return None
    return arr[i]


rows = []
for fid, tk, fn, p, cj, ciso in c.execute(
    "select id, market_ticker, forecaster_name, estimated_probability, "
    "julianday(created_at), created_at from market_forecasts "
    "where evidence_depth='source_backed'"
):
    o = outcome.get(tk)
    if o is None:
        continue
    arr = bk.get(tk)
    if not arr:
        continue
    b = at_or_before(arr, cj, MATCH_MAX_S)
    if b is None:
        continue
    _, cb, ca, sp, liq, _cm = b
    q = (cb + ca) / 200.0
    ws, close_j = o
    rec = {
        "forecast_id": fid,
        "market_ticker": tk,
        "event": tk.rsplit("-", 1)[0] if "-" in tk else tk,
        "forecaster": fn,
        "created_at": ciso,
        "split": "train" if ciso[:10] < SPLIT else "holdout",
        "p": round(float(p), 6),
        "q": round(q, 6),
        "yes_bid_c": cb,
        "yes_ask_c": ca,
        "spread_avg": sp if sp is not None else "",
        "liquidity_avg": liq if liq is not None else "",
        "y": 1 if ws == "yes" else 0,
        "hours_to_close": (round((close_j - cj) * 24.0, 4)
                           if close_j is not None else ""),
    }
    # E2 horizons: market probability at t+h, half-horizon tolerance
    for name, secs in HORIZONS:
        nb = at_or_after(arr, cj + secs / 86400.0, secs / 2.0)
        rec[f"q_{name}"] = round((nb[1] + nb[2]) / 200.0, 6) if nb else ""
    rows.append(rec)

print(f"matched observations: {len(rows)}")
print(f"  train:   {sum(1 for r in rows if r['split']=='train')}")
print(f"  holdout: {sum(1 for r in rows if r['split']=='holdout')}")
print(f"  events:  {len({r['event'] for r in rows})}")
for name, _ in HORIZONS:
    n = sum(1 for r in rows if r[f"q_{name}"] != "")
    print(f"  q_{name:<4} present: {n} ({100*n/max(len(rows),1):.1f}%)")

with open(OUT, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
print(f"\nwrote {OUT}")
