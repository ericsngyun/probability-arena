"""EDGE-DISCOVERY-001 / E2 — does the forecast LEAD the market?

Preregistered in docs/experiments/EDGE-DISCOVERY-001-PREREGISTRATION.md §3.
Deviations/refinements logged in §8 (D-3, D-4) BEFORE any number below was read.

Primary statistic per horizon h:   A_h = E[ sign(d) * dq_h ],  d = p - q
Secondary (magnitude weighted):    Bs_h = E[ d * dq_h ] / E[ |d| ]
Cost floor, PER OBSERVATION:       floor_i = half_spread_i + taker_fee_i
Net excess (the PASS statistic):   N_h = E[ s * sign(d) * dq_h - floor ]
                                   with s = +-1 the data-chosen direction (D-4)

All uncertainty is EVENT-clustered (1,482 events, never ticker). Method:
cluster bootstrap resampling EVENTS with replacement, B = 10,000 replicates.
Every statistic here is a ratio of two cluster-additive sums, so a replicate is
computed exactly from multinomial event counts (no row materialisation).

Multiple testing: Holm-Bonferroni, family = ALL SIX horizons (D-2 keeps the
underpowered 3h/6h in the family deliberately), alpha = 0.05.

Run:  .venv/bin/python docs/evidence/qdk-001/e2_leadlag.py
"""

import csv
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "edge_discovery_001_dataset.csv")
OUT_JSON = os.path.join(HERE, "e2_leadlag_results.json")

HORIZONS = ["5m", "15m", "30m", "1h", "3h", "6h"]
B = 10000
ALPHA = 0.05
SEED = 20260815

# ---------------------------------------------------------------- cost floor


def taker_fee_dollars(price):
    """Kalshi taker fee, verified primary schedule effective 2026-07-07:

        fee = ceil_to_cent( M * 0.07 * C * P * (1-P) ),  M=1, C=1

    P is the contract price in DOLLARS. A $1-notional contract means the fee in
    dollars equals the fee in probability units, so no conversion is needed.
    ceil_to_cent makes the fee at least $0.01 whenever 0 < P < 1.
    """
    raw = 0.07 * price * (1.0 - price)
    return np.ceil(np.round(raw * 100.0, 9)) / 100.0


def load():
    rows = []
    with open(CSV_PATH, newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append(r)
    return rows


def build():
    rows = load()
    n = len(rows)
    p = np.array([float(r["p"]) for r in rows])
    q = np.array([float(r["q"]) for r in rows])
    bid = np.array([float(r["yes_bid_c"]) for r in rows])
    ask = np.array([float(r["yes_ask_c"]) for r in rows])
    split = np.array([r["split"] for r in rows])
    ev_names = [r["event"] for r in rows]
    uniq = sorted(set(ev_names))
    ev_index = {e: i for i, e in enumerate(uniq)}
    ev = np.array([ev_index[e] for e in ev_names])

    d = p - q
    sd = np.sign(d)

    # half-spread in probability units, from the quoted cents
    half_spread = (ask - bid) / 2.0 / 100.0

    fwd = {}
    for h in HORIZONS:
        col = f"q_{h}"
        vals = np.full(n, np.nan)
        for i, r in enumerate(rows):
            v = r[col]
            if v != "":
                vals[i] = float(v)
        fwd[h] = vals

    return dict(
        n=n, p=p, q=q, d=d, sd=sd, bid=bid, ask=ask, split=split, ev=ev,
        n_events=len(uniq), half_spread=half_spread, fwd=fwd,
    )


def floors(D, direction_sign):
    """Per-observation cost floor, two variants.

    mid  : fee priced at the mid probability q  (primary; the half-spread term
           already carries the mid->executable crossing cost, so pricing the fee
           at the mid is the internally consistent decomposition)
    adv  : fee priced at the executable price actually paid for the direction
           implied by `direction_sign * sign(d)` -- buy YES at the ask, or buy
           NO at (100 - bid)/100. Reported as the adverse-bound sensitivity per
           preregistration §5 ("any assumption is declared as an adverse bound").
    """
    fee_mid = taker_fee_dollars(D["q"])
    f_mid = D["half_spread"] + fee_mid

    trade = direction_sign * D["sd"]          # +1 long YES, -1 long NO, 0 none
    p_yes = D["ask"] / 100.0
    p_no = (100.0 - D["bid"]) / 100.0
    p_exec = np.where(trade >= 0, p_yes, p_no)
    fee_adv = taker_fee_dollars(p_exec)
    f_adv = D["half_spread"] + fee_adv
    return f_mid, f_adv


# ------------------------------------------------------- cluster bootstrap


def event_sums(ev_local, cols):
    """cols: (n_rows, K). Returns (n_events_local, K) event-wise sums."""
    e = np.unique(ev_local)
    remap = np.searchsorted(e, ev_local)
    E = len(e)
    out = np.zeros((E, cols.shape[1]))
    for k in range(cols.shape[1]):
        out[:, k] = np.bincount(remap, weights=cols[:, k], minlength=E)
    return out


def boot_replicates(esums, rng, B=B):
    """B x K matrix of resampled cluster sums (events drawn with replacement)."""
    E = esums.shape[0]
    counts = rng.multinomial(E, np.full(E, 1.0 / E), size=B).astype(np.float64)
    return counts @ esums


def pctl_ci(reps, level):
    lo = (1.0 - level) / 2.0 * 100.0
    return float(np.percentile(reps, lo)), float(np.percentile(reps, 100.0 - lo))


def boot_p(reps):
    """Two-sided bootstrap p: smallest alpha whose percentile CI excludes 0."""
    n = len(reps)
    lo = (1.0 + np.sum(reps <= 0.0)) / (n + 1.0)
    hi = (1.0 + np.sum(reps >= 0.0)) / (n + 1.0)
    return float(min(1.0, 2.0 * min(lo, hi)))


def holm(pvals, alpha=ALPHA):
    """Returns (reject[], holm_level[]) -- holm_level is the CI level at which
    each hypothesis is judged, 1 - alpha/(m-k+1) at its rank k."""
    m = len(pvals)
    order = np.argsort(pvals)
    reject = np.zeros(m, dtype=bool)
    level = np.zeros(m)
    still = True
    for k, idx in enumerate(order, start=1):
        thr = alpha / (m - k + 1)
        level[idx] = 1.0 - thr
        if still and pvals[idx] <= thr:
            reject[idx] = True
        else:
            still = False
    return reject, level


# ------------------------------------------------------------- statistics


def analyse(D, h, mask, rng, direction_sign=None, resid=None, label=""):
    """All statistics for one horizon x split. Every one is a ratio of two
    cluster-additive sums, so the bootstrap is exact from event counts."""
    dq_all = D["fwd"][h] - D["q"]
    m = mask & ~np.isnan(dq_all)
    n = int(m.sum())
    if n == 0:
        return None
    dq = dq_all[m]
    sd = D["sd"][m]
    d = D["d"][m]
    ev = D["ev"][m]

    a_i = sd * dq                                   # primary integrand
    if direction_sign is None:
        direction_sign = 1.0 if float(np.mean(a_i)) >= 0 else -1.0
    f_mid_all, f_adv_all = floors(D, direction_sign)
    f_mid, f_adv = f_mid_all[m], f_adv_all[m]

    net_mid_i = direction_sign * a_i - f_mid
    net_adv_i = direction_sign * a_i - f_adv
    net_rt_i = direction_sign * a_i - 2.0 * f_adv   # round-trip sensitivity

    cols = [a_i, d * dq, np.abs(d), f_mid, f_adv,
            net_mid_i, net_adv_i, net_rt_i, np.ones(n)]
    if resid is not None:
        r = resid[m]
        cols.append(sd * r)
    M = np.column_stack(cols)

    es = event_sums(ev, M)
    reps = boot_replicates(es, rng)
    cnt = reps[:, 8]

    def ratio(k, den_k=8):
        return reps[:, k] / reps[:, den_k]

    res = {
        "horizon": h, "label": label, "n": n,
        "n_events": int(len(np.unique(ev))),
        "coverage": n / int(mask.sum()),
        "direction_sign": float(direction_sign),
        "n_zero_d": int(np.sum(sd == 0)),
    }

    # primary
    a_reps = ratio(0)
    res["primary"] = float(np.mean(a_i))
    res["primary_reps"] = a_reps
    res["primary_ci95"] = pctl_ci(a_reps, 0.95)
    res["primary_p"] = boot_p(a_reps)

    # secondary, magnitude weighted
    s_reps = reps[:, 1] / reps[:, 2]
    res["secondary"] = float(np.sum(d * dq) / np.sum(np.abs(d)))
    res["secondary_ci95"] = pctl_ci(s_reps, 0.95)
    res["secondary_p"] = boot_p(s_reps)

    # cost floors
    res["floor_mid"] = float(np.mean(f_mid))
    res["floor_adv"] = float(np.mean(f_adv))
    res["half_spread_mean"] = float(np.mean(D["half_spread"][m]))

    # net excess over the per-observation floor
    for key, k in (("net_mid", 5), ("net_adv", 6), ("net_roundtrip", 7)):
        rr = ratio(k)
        res[key] = float(np.mean(M[:, k]))
        res[key + "_reps"] = rr
        res[key + "_ci95"] = pctl_ci(rr, 0.95)
        res[key + "_p"] = boot_p(rr)

    if resid is not None:
        rr = ratio(9)
        res["primary_resid"] = float(np.mean(M[:, 9]))
        res["primary_resid_ci95"] = pctl_ci(rr, 0.95)
        res["primary_resid_p"] = boot_p(rr)
        res["primary_resid_reps"] = rr

    return res


# --------------------------------------------- mean-reversion confound (exploratory)


def q_residualiser(D, h, fit_mask, n_bins=20):
    """Ehat[dq_h | q] as an equal-count binned mean of q, FIT ON TRAIN ONLY.
    Returns a residual array dq - Ehat[dq|q] (nan where dq is absent)."""
    dq = D["fwd"][h] - D["q"]
    fm = fit_mask & ~np.isnan(dq)
    if fm.sum() < n_bins * 10:
        n_bins = max(2, int(fm.sum() // 10))
    qs = D["q"][fm]
    edges = np.unique(np.quantile(qs, np.linspace(0, 1, n_bins + 1)))
    edges[0], edges[-1] = -np.inf, np.inf
    idx_fit = np.digitize(qs, edges[1:-1])
    means = np.zeros(len(edges) - 1)
    for b in range(len(edges) - 1):
        sel = idx_fit == b
        means[b] = dq[fm][sel].mean() if sel.sum() > 0 else 0.0
    idx_all = np.digitize(D["q"], edges[1:-1])
    return dq - means[idx_all], edges, means


def main():
    rng = np.random.default_rng(SEED)
    D = build()
    is_train = D["split"] == "train"
    is_hold = D["split"] == "holdout"
    allm = np.ones(D["n"], dtype=bool)

    print(f"rows={D['n']}  events={D['n_events']}  "
          f"train={int(is_train.sum())}  holdout={int(is_hold.sum())}")
    print(f"d == 0 exactly: {int(np.sum(D['sd'] == 0))}")
    print(f"mean |d| = {np.mean(np.abs(D['d'])):.5f}")

    out = {"pooled": {}, "train": {}, "holdout": {}, "exploratory": {}}

    # ---- pooled: the preregistered criterion is on the FULL matched set
    pooled = {}
    for h in HORIZONS:
        resid, edges, means = q_residualiser(D, h, is_train)
        pooled[h] = analyse(D, h, allm, rng, resid=resid, label="pooled")
    pv = np.array([pooled[h]["primary_p"] for h in HORIZONS])
    rej, lev = holm(pv)
    net_pv = np.array([pooled[h]["net_adv_p"] for h in HORIZONS])
    rej_n, lev_n = holm(net_pv)
    for i, h in enumerate(HORIZONS):
        r = pooled[h]
        r["holm_reject_primary"] = bool(rej[i])
        r["holm_level"] = float(lev[i])
        r["primary_ci_holm"] = pctl_ci(r["primary_reps"], lev[i])
        r["holm_reject_net"] = bool(rej_n[i])
        r["holm_level_net"] = float(lev_n[i])
        r["net_adv_ci_holm"] = pctl_ci(r["net_adv_reps"], lev_n[i])
        r["net_mid_ci_holm"] = pctl_ci(r["net_mid_reps"], lev_n[i])

    # train / holdout, direction of the net statistic fixed from TRAIN
    tr, ho = {}, {}
    for h in HORIZONS:
        resid, _, _ = q_residualiser(D, h, is_train)
        tr[h] = analyse(D, h, is_train, rng, resid=resid, label="train")
        ds = tr[h]["direction_sign"] if tr[h] else None
        ho[h] = analyse(D, h, is_hold, rng, direction_sign=ds,
                        resid=resid, label="holdout")

    # ---- exploratory: is dq predictable from q alone?
    expl = {}
    for h in HORIZONS:
        dq = D["fwd"][h] - D["q"]
        m = ~np.isnan(dq)
        if m.sum() == 0:
            continue
        toward_mid = np.sign(0.5 - D["q"][m])
        e = {}
        e["n"] = int(m.sum())
        # mean reversion strength: E[dq * sign(0.5-q)] > 0 means q drifts to .5
        rev_i = dq[m] * toward_mid
        es = event_sums(D["ev"][m],
                        np.column_stack([rev_i, np.ones(m.sum())]))
        rr = boot_replicates(es, rng)
        v = rr[:, 0] / rr[:, 1]
        e["reversion_stat"] = float(np.mean(rev_i))
        e["reversion_ci95"] = pctl_ci(v, 0.95)
        e["reversion_p"] = boot_p(v)
        # does the model disagree TOWARD the mid?
        agree = (D["sd"][m] == toward_mid) & (D["sd"][m] != 0)
        nz = D["sd"][m] != 0
        e["frac_sign_d_toward_mid"] = float(agree.sum() / max(nz.sum(), 1))
        e["corr_q_dq"] = float(np.corrcoef(D["q"][m], dq[m])[0, 1])
        e["primary_raw"] = pooled[h]["primary"]
        e["primary_resid"] = pooled[h].get("primary_resid")
        e["primary_resid_ci95"] = pooled[h].get("primary_resid_ci95")
        e["primary_resid_p"] = pooled[h].get("primary_resid_p")
        expl[h] = e

    def strip(dd):
        return {h: {k: v for k, v in r.items() if not k.endswith("_reps")}
                for h, r in dd.items() if r}

    out["pooled"] = strip(pooled)
    out["train"] = strip(tr)
    out["holdout"] = strip(ho)
    out["exploratory"] = expl
    out["meta"] = {
        "B": B, "alpha": ALPHA, "seed": SEED, "n_rows": D["n"],
        "n_events": D["n_events"],
        "n_train": int(is_train.sum()), "n_holdout": int(is_hold.sum()),
    }

    with open(OUT_JSON, "w") as fh:
        json.dump(out, fh, indent=1, default=float)

    # -------------------------------------------------- console report
    def show(name, dd, holm_keys=False):
        print(f"\n=== {name} ===")
        hdr = ("hor      n   evts   cov%   E[sgn(d)dq]        95% CI"
               "        p     floor_adv   net_adv")
        print(hdr)
        for h in HORIZONS:
            r = dd.get(h)
            if not r:
                print(f"{h:<4}  ABSENT")
                continue
            lo, hi = r["primary_ci95"]
            print(f"{h:<4} {r['n']:>6} {r['n_events']:>6} "
                  f"{100*r['coverage']:>5.1f}  {r['primary']:>+.5f}  "
                  f"[{lo:>+.5f},{hi:>+.5f}] {r['primary_p']:>7.4f}  "
                  f"{r['floor_adv']:>.5f}  {r['net_adv']:>+.5f}")

    show("POOLED (preregistered criterion set)", out["pooled"])
    print("\nHolm (family = all 6 horizons, alpha=0.05) on the PRIMARY statistic:")
    for h in HORIZONS:
        r = out["pooled"][h]
        lo, hi = r["primary_ci_holm"]
        print(f"  {h:<4} p={r['primary_p']:.4f} holm_level={r['holm_level']:.5f} "
              f"CI=[{lo:+.5f},{hi:+.5f}] reject={r['holm_reject_primary']}")
    print("\nHolm on the NET-OF-FLOOR statistic (the PASS statistic):")
    for h in HORIZONS:
        r = out["pooled"][h]
        lo, hi = r["net_adv_ci_holm"]
        print(f"  {h:<4} net_adv={r['net_adv']:+.5f} p={r['net_adv_p']:.4f} "
              f"CI=[{lo:+.5f},{hi:+.5f}] reject={r['holm_reject_net']} "
              f"| net_rt={r['net_roundtrip']:+.5f}")
    print("\nSecondary (magnitude weighted E[d*dq]/E[|d|]), pooled:")
    for h in HORIZONS:
        r = out["pooled"][h]
        lo, hi = r["secondary_ci95"]
        print(f"  {h:<4} {r['secondary']:+.5f} [{lo:+.5f},{hi:+.5f}] "
              f"p={r['secondary_p']:.4f}")

    show("TRAIN", out["train"])
    show("HOLDOUT (direction fixed from TRAIN)", out["holdout"])

    print("\n=== EXPLORATORY: mean reversion in q alone ===")
    print("hor    E[dq*sgn(.5-q)]        95% CI          p   corr(q,dq)  "
          "P(sgn d toward mid)  raw->resid")
    for h in HORIZONS:
        e = expl.get(h)
        if not e:
            continue
        lo, hi = e["reversion_ci95"]
        rl, rh = e["primary_resid_ci95"]
        print(f"{h:<4} {e['reversion_stat']:>+.5f}  [{lo:+.5f},{hi:+.5f}] "
              f"{e['reversion_p']:>7.4f}  {e['corr_q_dq']:>+.4f}   "
              f"{e['frac_sign_d_toward_mid']:>6.3f}   "
              f"{e['primary_raw']:+.5f} -> {e['primary_resid']:+.5f} "
              f"[{rl:+.5f},{rh:+.5f}] p={e['primary_resid_p']:.4f}")

    print(f"\nwrote {OUT_JSON}")


if __name__ == "__main__":
    main()
