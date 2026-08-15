"""EDGE-DISCOVERY-001 / E3 — preregistered conditional slices of delta-S.

Specification: docs/experiments/EDGE-DISCOVERY-001-PREREGISTRATION.md sections 1,
4 and 8 (deviation D-3, which fixes every bucket boundary and was committed
BEFORE this file was written).

  delta_S = S(q, y) - S(p, y),  S = Brier.   delta_S > 0 means OUR FORECAST BEAT
  THE MARKET in that cell.

Guards, none of which are optional:
  * floors   n >= 200 observations AND >= 50 events, required in BOTH splits
  * cluster  every CI and p-value is an EVENT-level cluster bootstrap, 4000
             iterations, events resampled with replacement
  * two-stage discover on TRAIN, confirm on HOLDOUT; nothing is promoted on the
             data that discovered it
  * BH       Benjamini-Hochberg FDR at q=0.10 pooled over ALL evaluable cells in
             the whole family, not per slice; FCR-adjusted intervals for the
             selected set

READ-ONLY. Reads one frozen CSV, writes one markdown table. Authorizes nothing.
Run:  /usr/local/bin/python3 docs/evidence/qdk-001/e3_slices.py
"""
from __future__ import annotations

import csv
import os
from collections import Counter

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "edge_discovery_001_dataset.csv")
OUT = os.path.join(HERE, "e3_slices_output.txt")

N_BOOT = 4000
SEED = 20260815
Q_FDR = 0.10
FLOOR_OBS = 200
FLOOR_EVENTS = 50

# ---------------------------------------------------------------- load -------
rows = list(csv.DictReader(open(DATA)))
p = np.array([float(r["p"]) for r in rows])
q = np.array([float(r["q"]) for r in rows])
y = np.array([int(r["y"]) for r in rows], dtype=float)
spread = np.array([float(r["spread_avg"]) for r in rows])
liq = np.array([float(r["liquidity_avg"]) for r in rows])
hours = np.array([float(r["hours_to_close"]) for r in rows])
forecaster = np.array([r["forecaster"] for r in rows])
ticker = np.array([r["market_ticker"] for r in rows])
is_train = np.array([r["split"] == "train" for r in rows])

events = np.array([r["event"] for r in rows])
ev_codes, ev_index = np.unique(events, return_inverse=True)

# delta_S per observation: market Brier minus our Brier
dS = (q - y) ** 2 - (p - y) ** 2

# league from the market_ticker series prefix (D-3 item 2)
PREFIX_TO_LEAGUE = {"KXMLB": "MLB", "KXWC": "WC", "KXMLS": "MLS"}


def league_of(tk: str) -> str:
    head = tk.split("-")[0]
    for pre, lg in PREFIX_TO_LEAGUE.items():
        if head.startswith(pre):
            return lg
    return "UNMAPPED"


league = np.array([league_of(t) for t in ticker])

# ------------------------------------------------------- slice definitions ---
# Cut points come from TRAIN ONLY and are applied unchanged to HOLDOUT.


def train_edges(x: np.ndarray, n_bins: int) -> np.ndarray:
    """Interior quantile cut points from the TRAIN rows only, duplicates
    collapsed (heavy ties, e.g. spread_avg == 1.0, legitimately merge bins)."""
    probs = np.arange(1, n_bins) / n_bins
    return np.unique(np.quantile(x[is_train], probs))


def quantile_cells(x: np.ndarray, n_bins: int, label: str, unit: str = "") -> list:
    """Bins are LEFT-CLOSED, RIGHT-OPEN: searchsorted(side='right') counts the
    edges <= x, so idx == b means edges[b-1] <= x < edges[b]. The labels below
    state that convention exactly — an earlier revision printed (lo, hi] here,
    which mislabelled the same partition (e.g. the 4 TRAIN rows at q == 0.82 sit
    in the top decile, not below it). Labels corrected; no bin membership, and
    therefore no statistic, changed."""
    edges = train_edges(x, n_bins)
    idx = np.searchsorted(edges, x, side="right")
    out = []
    for b in range(len(edges) + 1):
        lo = "-inf" if b == 0 else f"{edges[b - 1]:.6g}"
        hi = "+inf" if b == len(edges) else f"{edges[b]:.6g}"
        out.append((f"{label} [{lo}, {hi}){unit}", idx == b))
    return out


def categorical_cells(x: np.ndarray, label_fmt: str) -> list:
    return [(label_fmt.format(v), x == v)
            for v, _ in Counter(x.tolist()).most_common()]


absd = np.abs(p - q)
SLICES: list[tuple[str, list]] = [
    ("1. forecaster", categorical_cells(forecaster, "{}")),
    ("2. sport / league", categorical_cells(league, "{}")),
    ("3. time-to-resolution", quantile_cells(hours, 5, "hours_to_close", "h")),
    ("4. market-probability decile", quantile_cells(q, 10, "q")),
    ("5. |p-q| disagreement", quantile_cells(absd, 5, "|p-q|")),
    ("6. spread", quantile_cells(spread, 5, "spread_avg", "c")),
    ("7. depth / liquidity", quantile_cells(liq, 5, "liquidity_avg")),
    ("8. favourite vs underdog", [("favourite (q > 0.5)", q > 0.5),
                                  ("underdog (q <= 0.5)", q <= 0.5)]),
    ("9. forecast direction", [("p > q (above market)", p > q),
                               ("p < q (below market)", p < q),
                               ("p == q (exact tie)", p == q)]),
    # 10. resolution-clarity tier -> NOT RUN, see preregistration D-3 item 10.
]

# --------------------------------------------------- cluster bootstrap -------
# One set of event resamples per split, SHARED by every cell, so all cells in a
# split are bootstrapped against identical event draws.


def make_boot(split_mask: np.ndarray, seed: int):
    idx_rows = np.flatnonzero(split_mask)
    ev_local = ev_index[idx_rows]
    uniq, inv = np.unique(ev_local, return_inverse=True)
    # rows grouped by event, as a flat array plus offsets
    order = np.argsort(inv, kind="stable")
    rows_sorted = idx_rows[order]
    counts = np.bincount(inv, minlength=len(uniq))
    offsets = np.concatenate([[0], np.cumsum(counts)])
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(uniq), size=(N_BOOT, len(uniq)))
    resamples = []
    for b in range(N_BOOT):
        d = draws[b]
        take = np.concatenate([rows_sorted[offsets[e]:offsets[e + 1]] for e in d])
        resamples.append(take)
    return resamples


print("building event-cluster bootstrap resamples ...", flush=True)
BOOT = {
    "train": make_boot(is_train, SEED),
    "holdout": make_boot(~is_train, SEED + 1),
}


def cell_stats(mask: np.ndarray, split: str):
    """Point estimate, percentile CI bounds function, and bootstrap p-value for
    one cell, using the shared event resamples of that split."""
    split_mask = is_train if split == "train" else ~is_train
    sel = mask & split_mask
    n_obs = int(sel.sum())
    n_ev = int(len(np.unique(ev_index[sel]))) if n_obs else 0
    if n_obs == 0:
        return dict(n_obs=0, n_events=0, est=float("nan"), boot=None)
    est = float(dS[sel].mean())
    in_cell = np.zeros(len(dS), dtype=bool)
    in_cell[np.flatnonzero(sel)] = True
    reps = np.empty(N_BOOT)
    for b, take in enumerate(BOOT[split]):
        m = in_cell[take]
        reps[b] = dS[take][m].mean() if m.any() else np.nan
    reps = reps[~np.isnan(reps)]
    return dict(n_obs=n_obs, n_events=n_ev, est=est, boot=reps)


def boot_p(est: float, reps: np.ndarray) -> float:
    """Two-sided bootstrap p-value by recentring the resample distribution on
    the null (theta* - theta_hat), then asking how often it reaches |theta_hat|."""
    if reps is None or len(reps) == 0:
        return float("nan")
    null = reps - est
    hits = int((np.abs(null) >= abs(est)).sum())
    return (hits + 1) / (len(null) + 1)


def pct_ci(reps: np.ndarray, level: float) -> tuple[float, float]:
    a = (1.0 - level) / 2.0
    return (float(np.quantile(reps, a)), float(np.quantile(reps, 1 - a)))


# ------------------------------------------------------------- evaluate ------
cells = []
for slice_name, defs in SLICES:
    for cell_name, mask in defs:
        tr = cell_stats(mask, "train")
        ho = cell_stats(mask, "holdout")
        evaluable = (tr["n_obs"] >= FLOOR_OBS and tr["n_events"] >= FLOOR_EVENTS
                     and ho["n_obs"] >= FLOOR_OBS and ho["n_events"] >= FLOOR_EVENTS)
        cells.append(dict(slice=slice_name, cell=cell_name, tr=tr, ho=ho,
                          evaluable=evaluable))

evaluable = [c for c in cells if c["evaluable"]]
m = len(evaluable)
for c in evaluable:
    c["p_train"] = boot_p(c["tr"]["est"], c["tr"]["boot"])
    c["p_holdout"] = boot_p(c["ho"]["est"], c["ho"]["boot"])


def bh_select(pvals: list[float], qlevel: float) -> tuple[set[int], list[float]]:
    """Benjamini-Hochberg: returns selected indices and adjusted p-values."""
    k = len(pvals)
    order = sorted(range(k), key=lambda i: pvals[i])
    adj = [0.0] * k
    running = 1.0
    for rank in range(k, 0, -1):
        i = order[rank - 1]
        running = min(running, pvals[i] * k / rank)
        adj[i] = running
    sel = {i for i in range(k) if adj[i] <= qlevel}
    return sel, adj


sel_tr, adj_tr = bh_select([c["p_train"] for c in evaluable], Q_FDR)
sel_ho, adj_ho = bh_select([c["p_holdout"] for c in evaluable], Q_FDR)
for i, c in enumerate(evaluable):
    c["adj_train"], c["adj_holdout"] = adj_tr[i], adj_ho[i]
    c["bh_train"], c["bh_holdout"] = i in sel_tr, i in sel_ho

# FCR-adjusted interval level for the BH-selected HOLDOUT set (Benjamini &
# Yekutieli 2005). Unselected cells get a plain 95% percentile interval, which
# is reported as uncorrected and carries no inferential weight.
R = len(sel_ho)
fcr_level = 1.0 - (R * Q_FDR / m) if (R and m) else 0.95
for i, c in enumerate(evaluable):
    lvl = fcr_level if c["bh_holdout"] else 0.95
    c["ci_train"] = pct_ci(c["tr"]["boot"], 0.95)
    c["ci_holdout"] = pct_ci(c["ho"]["boot"], lvl)
    c["ci_holdout_level"] = lvl

# PROMOTION: TRAIN delta_S > 0 (discovery) AND HOLDOUT delta_S > 0 with a
# BH-selected, FCR-adjusted interval strictly above zero (confirmation).
promoted = [c for c in evaluable
            if c["tr"]["est"] > 0 and c["ho"]["est"] > 0
            and c["bh_holdout"] and c["ci_holdout"][0] > 0]

# ---------------------------------------------------------------- report -----
L: list[str] = []


def w(s=""):
    L.append(s)
    print(s, flush=True)


w("EDGE-DISCOVERY-001 / E3 — preregistered conditional slices")
w(f"rows={len(rows)}  train={int(is_train.sum())}  holdout={int((~is_train).sum())}  "
  f"events={len(ev_codes)}")
w(f"overall delta_S = {dS.mean():+.5f}   (train {dS[is_train].mean():+.5f} / "
  f"holdout {dS[~is_train].mean():+.5f})")
w(f"bootstrap: {N_BOOT} event-cluster resamples per split, seed {SEED}")
w(f"floors: n>={FLOOR_OBS} obs AND >={FLOOR_EVENTS} events, in BOTH splits")
w(f"cells defined={len(cells)}  evaluable (BH denominator m)={m}  "
  f"underpowered={len(cells) - m}")
w()
hdr = (f"{'slice':<30}{'cell':<40}{'n_tr':>6}{'ev_tr':>6}{'n_ho':>6}{'ev_ho':>6}"
       f"{'dS_train':>11}{'CI_train':>22}{'dS_hold':>11}{'CI_hold':>22}"
       f"{'p_ho':>8}{'adj_ho':>8}  verdict")
w(hdr)
w("-" * len(hdr))
for c in cells:
    tr, ho = c["tr"], c["ho"]
    if not c["evaluable"]:
        w(f"{c['slice']:<30}{c['cell']:<40}{tr['n_obs']:>6}{tr['n_events']:>6}"
          f"{ho['n_obs']:>6}{ho['n_events']:>6}"
          f"{'':>11}{'':>22}{'':>11}{'':>22}{'':>8}{'':>8}  underpowered")
        continue
    cit, cih = c["ci_train"], c["ci_holdout"]
    if c in promoted:
        verdict = "PROMOTED"
    elif c["bh_holdout"]:
        verdict = ("BH-signif, delta_S<0 (market better)" if ho["est"] < 0
                   else "BH-signif holdout, failed discovery")
    else:
        verdict = "no effect"
    w(f"{c['slice']:<30}{c['cell']:<40}{tr['n_obs']:>6}{tr['n_events']:>6}"
      f"{ho['n_obs']:>6}{ho['n_events']:>6}"
      f"{tr['est']:>+11.5f}{f'[{cit[0]:+.5f},{cit[1]:+.5f}]':>22}"
      f"{ho['est']:>+11.5f}{f'[{cih[0]:+.5f},{cih[1]:+.5f}]':>22}"
      f"{c['p_holdout']:>8.4f}{c['adj_holdout']:>8.4f}  {verdict}")
w()
w(f"BH q={Q_FDR} selections: TRAIN {len(sel_tr)}/{m}, HOLDOUT {R}/{m}")
w(f"FCR-adjusted interval level for the selected HOLDOUT set: {fcr_level:.4f}")
pos_tr = [c for c in evaluable if c["tr"]["est"] > 0]
pos_ho = [c for c in evaluable if c["ho"]["est"] > 0]
w(f"evaluable cells with delta_S > 0: TRAIN {len(pos_tr)}/{m}, HOLDOUT {len(pos_ho)}/{m}")
both = [c for c in evaluable if c["tr"]["est"] > 0 and c["ho"]["est"] > 0]
w(f"evaluable cells positive in BOTH splits: {len(both)}"
  + (" -> " + ", ".join(c["cell"] for c in both) if both else ""))
w(f"promoted cells: {len(promoted)}")
w()
w("E3 VERDICT: " + ("PASS" if promoted else "FAIL — no cell survives"))

# Regression-to-the-mean diagnostic: does a cell's TRAIN rank predict its
# HOLDOUT delta_S at all? This is the EDGE-SELECTION failure mode, quantified.
if m >= 3:
    a = np.array([c["tr"]["est"] for c in evaluable])
    b = np.array([c["ho"]["est"] for c in evaluable])
    r = float(np.corrcoef(a, b)[0, 1])
    w()
    w(f"TRAIN-vs-HOLDOUT correlation of cell delta_S across the {m} evaluable "
      f"cells: r = {r:+.3f}")
    top = max(evaluable, key=lambda c: c["tr"]["est"])
    w(f"best TRAIN cell: {top['cell']} ({top['tr']['est']:+.5f}) -> "
      f"holdout {top['ho']['est']:+.5f}")

open(OUT, "w").write("\n".join(L) + "\n")
print(f"\nwrote {OUT}")

# Markdown cell table, emitted rather than transcribed so the result document
# cannot drift from the computation.
MD = os.path.join(HERE, "e3_cell_table.md")
T = ["| slice | cell | n_obs (tr/ho) | n_events (tr/ho) | TRAIN ΔS [95% CI] | "
     "HOLDOUT ΔS [CI] | BH adj p (ho) | verdict |",
     "|---|---|---|---|---|---|---|---|"]
def esc(s: str) -> str:
    """Cell labels contain '|' (the |p-q| slice); escape it or the table breaks."""
    return s.replace("|", "\\|")


for c in cells:
    tr, ho = c["tr"], c["ho"]
    nobs = f"{tr['n_obs']}/{ho['n_obs']}"
    nev = f"{tr['n_events']}/{ho['n_events']}"
    sl, cl = esc(c["slice"]), esc(c["cell"])
    if not c["evaluable"]:
        T.append(f"| {sl} | {cl} | {nobs} | {nev} | — | — | — | "
                 f"**underpowered** |")
        continue
    cit, cih = c["ci_train"], c["ci_holdout"]
    if any(c is x for x in promoted):
        verdict = "**PROMOTED**"
    elif c["bh_holdout"]:
        verdict = "market better (BH-signif, ΔS<0)"
    else:
        verdict = "no effect"
    T.append(
        f"| {sl} | {cl} | {nobs} | {nev} | "
        f"{tr['est']:+.5f} [{cit[0]:+.5f}, {cit[1]:+.5f}] | "
        f"{ho['est']:+.5f} [{cih[0]:+.5f}, {cih[1]:+.5f}] | "
        f"{c['adj_holdout']:.4f} | {verdict} |")
open(MD, "w").write("\n".join(T) + "\n")
print(f"wrote {MD}")
