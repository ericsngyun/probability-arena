"""Independent numpy cross-check of the E2 point estimates.

Deliberately written from the preregistration text, NOT by importing anything
from e2_leadlag.py, so an arithmetic bug in the pure-stdlib implementation would
show up as a disagreement here. Bootstrap CIs are not re-derived; this checks
the point estimates, the per-observation cost floors and the coverage counts.

Run with /usr/local/bin/python3 (numpy 2.1.3).
"""
import csv
import json
import sys

import numpy as np

CSV_PATH = ("<LOCAL_HOME>/code-stuff/probability-arena/.claude/worktrees/"
            "agent-a1f91dc901d704820/docs/evidence/qdk-001/"
            "edge_discovery_001_dataset.csv")
JSON_PATH = ("<LOCAL_HOME>/code-stuff/probability-arena/.claude/worktrees/"
             "agent-a1f91dc901d704820/docs/evidence/qdk-001/"
             "e2_leadlag_results.json")
HORIZONS = ["5m", "15m", "30m", "1h", "3h", "6h"]

rows = list(csv.DictReader(open(CSV_PATH, newline="")))
p = np.array([float(r["p"]) for r in rows])
q = np.array([float(r["q"]) for r in rows])
bid = np.array([float(r["yes_bid_c"]) for r in rows])
ask = np.array([float(r["yes_ask_c"]) for r in rows])
split = np.array([r["split"] for r in rows])
events = np.array([r["event"] for r in rows])

d = p - q
sd = np.sign(d)
half_spread = (ask - bid) / 200.0


def fee(price):
    return np.ceil(np.round(0.07 * price * (1 - price) * 100, 9)) / 100.0


fee_mid = fee(q)
fee_yes = fee(ask / 100.0)
fee_no = fee((100.0 - bid) / 100.0)

ref = json.load(open(JSON_PATH))
print(f"rows={len(rows)} events={len(set(events))} "
      f"train={(split=='train').sum()} holdout={(split=='holdout').sum()}")
print(f"d==0: {(sd==0).sum()}   mean|d|={np.abs(d).mean():.5f}\n")

hdr = "{:<5} {:>6} {:>10} {:>10} {:>9} {:>10} {:>10} {:>9}"
print(hdr.format("hor", "n", "primary", "ref", "d", "floor_adv", "ref", "d"))
worst = 0.0
for h in HORIZONS:
    fwd = np.array([float(r[f"q_{h}"]) if r[f"q_{h}"] != "" else np.nan
                    for r in rows])
    m = ~np.isnan(fwd)
    dq = (fwd - q)[m]
    a = sd[m] * dq
    prim = a.mean()
    s = 1.0 if prim >= 0 else -1.0
    trade = s * sd[m]
    fee_adv = np.where(trade >= 0, fee_yes[m], fee_no[m])
    f_adv = (half_spread[m] + fee_adv).mean()
    sec = np.sum(d[m] * dq) / np.sum(np.abs(d[m]))
    net = s * prim - f_adv

    R = ref["pooled"][h]
    e1 = abs(prim - R["primary"])
    e2 = abs(f_adv - R["floor_adv"])
    e3 = abs(sec - R["secondary"])
    e4 = abs(net - R["net_adv"])
    worst = max(worst, e1, e2, e3, e4)
    print(hdr.format(h, int(m.sum()), f"{prim:+.6f}", f"{R['primary']:+.6f}",
                     f"{e1:.2e}", f"{f_adv:.6f}", f"{R['floor_adv']:.6f}",
                     f"{e2:.2e}"))
    assert int(m.sum()) == R["n"], (h, m.sum(), R["n"])
    assert len(set(events[m])) == R["n_events"]
    assert s == R["direction_sign"]

print(f"\nworst absolute disagreement across all checked quantities: {worst:.3e}")
print("PASS" if worst < 1e-9 else "MISMATCH")
