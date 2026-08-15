"""EDGE-DISCOVERY-001 / E4 -- independent numpy cross-check of the headline numbers.

Deliberately a SEPARATE code path from e4_proper_betting.py (vectorised, different
bootstrap RNG, Bregman/score terms written out in closed form rather than via the generic
Savage representation) so that agreement is evidence and not a shared bug.

Run:  /usr/local/bin/python3 docs/evidence/qdk-001/e4_crosscheck_numpy.py
"""

import csv
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
rows = list(csv.DictReader(open(os.path.join(HERE, "edge_discovery_001_dataset.csv"))))

split = np.array([r["split"] for r in rows])
event = np.array([r["event"] for r in rows])
p = np.clip(np.array([float(r["p"]) for r in rows]), 0.01, 0.99)
q = np.clip(np.array([float(r["q"]) for r in rows]), 0.01, 0.99)
bid = np.array([float(r["yes_bid_c"]) for r in rows])
ask = np.array([float(r["yes_ask_c"]) for r in rows])
y = np.array([float(r["y"]) for r in rows])

for which in ("train", "holdout"):
    m = (split == which) & (ask >= bid)  # crossed quotes excluded, deviation D-3
    P, Q, B, A, Y, E = p[m], q[m], bid[m], ask[m], y[m], event[m]

    yes = P > A / 100.0  # Corollary 19 abstain band
    no = P < B / 100.0
    tr = yes | no

    entry = np.where(yes, A, 100.0 - B)[tr]
    payoff = np.where(yes, 100.0 * Y, 100.0 * (1.0 - Y))[tr]
    midpx = np.where(yes, 100.0 * Q, 100.0 * (1.0 - Q))[tr]
    d = entry / 100.0
    fee = 100.0 * np.ceil(0.07 * 100.0 * d * (1 - d) * 100 - 1e-9) / 100.0 / 100.0
    net = payoff - entry - fee

    # closed-form Brier terms: S(v,y) = 1 - 2(v-y)^2 ; D(q,p) = 2(q-p)^2
    pp, qq, yy = P[tr], Q[tr], Y[tr]
    sg = (1 - 2 * (pp - yy) ** 2) - (1 - 2 * (qq - yy) ** 2)
    dv = 2 * (qq - pp) ** 2

    ev = E[tr]
    uniq, inv = np.unique(ev, return_inverse=True)
    sums = np.bincount(inv, weights=net, minlength=len(uniq))
    cnts = np.bincount(inv, minlength=len(uniq))
    rng = np.random.default_rng(7)
    draw = rng.integers(0, len(uniq), size=(5000, len(uniq)))
    stats = sums[draw].sum(1) / cnts[draw].sum(1)
    lo, hi = np.percentile(stats, [2.5, 97.5])

    print(
        f"{which:>8}  n={tr.sum():>5} ev={len(uniq):>4} "
        f"gross@mid={(payoff-midpx).mean():+.4f} spread={-(entry-midpx).mean():+.4f} "
        f"fee={-fee.mean():+.4f} NET={net.mean():+.4f} CI=[{lo:+.4f},{hi:+.4f}] "
        f"| scoregap={sg.mean():+.5f} div={dv.mean():+.5f} sum={(sg+dv).mean():+.5f}"
    )
