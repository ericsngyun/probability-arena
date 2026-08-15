"""EDGE-DISCOVERY-001 / E2 — does the forecast LEAD the market?

Preregistered in docs/experiments/EDGE-DISCOVERY-001-PREREGISTRATION.md §3.
Deviations/refinements logged in §8 (D-3, D-4) BEFORE any number below was read.

Primary statistic per horizon h:   A_h  = E[ sign(d) * dq_h ],   d = p - q
Secondary (magnitude weighted):    Bs_h = E[ d * dq_h ] / E[ |d| ]
Cost floor, PER OBSERVATION:       floor_i = half_spread_i + taker_fee_i
Net excess (the PASS statistic):   N_h  = E[ s * sign(d) * dq_h - floor ]
                                   with s = +-1 the data-chosen direction (D-4)

All uncertainty is EVENT-clustered (1,482 events, never ticker). Method:
cluster bootstrap resampling EVENTS with replacement, B = 10,000 replicates,
percentile intervals, two-sided bootstrap p-values. Every statistic is a ratio
of two cluster-additive sums, so a replicate is computed exactly from resampled
per-event sums without materialising rows.

Multiple testing: Holm-Bonferroni, family = ALL SIX horizons (D-2 keeps the
underpowered 3h/6h in the family deliberately), alpha = 0.05.

Pure standard library on purpose: numpy/scipy are absent from this repo's venv
and installing was not authorised. This changes no part of the specification --
drawing E events with replacement is exactly the multinomial draw of D-3(6).

Run:  .venv/bin/python docs/evidence/qdk-001/e2_leadlag.py
"""

import csv
import json
import math
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "edge_discovery_001_dataset.csv")
OUT_JSON = os.path.join(HERE, "e2_leadlag_results.json")

HORIZONS = ["5m", "15m", "30m", "1h", "3h", "6h"]
B = 10000
ALPHA = 0.05
SEED = 20260815

# number of accumulator columns carried through the bootstrap
# 0 a=sign(d)*dq | 1 d*dq | 2 |d| | 3 floor_mid | 4 floor_adv
# 5 sign(d)*resid | 6 count
NCOL = 7


# ---------------------------------------------------------------- cost floor

def taker_fee(price):
    """Kalshi taker fee, verified primary schedule effective 2026-07-07:

        fee = ceil_to_cent( M * 0.07 * C * P * (1-P) ),  M=1, C=1

    P is the contract price in DOLLARS. A $1-notional contract makes the fee in
    dollars equal to the fee in probability units, so no conversion is needed.
    ceil_to_cent forces the fee to at least $0.01 whenever 0 < P < 1.
    """
    raw = 0.07 * price * (1.0 - price)
    return math.ceil(round(raw * 100.0, 9)) / 100.0


def sgn(x):
    return (x > 0) - (x < 0)


# ------------------------------------------------------------------- loading

def load():
    with open(CSV_PATH, newline="") as fh:
        rows = list(csv.DictReader(fh))

    events = {}
    recs = []
    for r in rows:
        e = r["event"]
        if e not in events:
            events[e] = len(events)
        p = float(r["p"])
        q = float(r["q"])
        bid = float(r["yes_bid_c"])
        ask = float(r["yes_ask_c"])
        d = p - q
        rec = {
            "ev": events[e],
            "split": r["split"],
            "p": p, "q": q, "d": d, "sd": sgn(d),
            "bid": bid, "ask": ask,
            "half_spread": (ask - bid) / 200.0,
            "fee_mid": taker_fee(q),
            "fee_yes": taker_fee(ask / 100.0),          # buy YES at the ask
            "fee_no": taker_fee((100.0 - bid) / 100.0),  # buy NO at 1-bid
            "fwd": {h: (float(r[f"q_{h}"]) if r[f"q_{h}"] != "" else None)
                    for h in HORIZONS},
        }
        recs.append(rec)
    return recs, len(events)


# ------------------------------------------------------- cluster bootstrap

def percentile(sorted_vals, pct):
    """Linear-interpolation percentile on an already-sorted list."""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    k = (n - 1) * pct
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def ci(sorted_vals, level):
    lo = (1.0 - level) / 2.0
    return (percentile(sorted_vals, lo), percentile(sorted_vals, 1.0 - lo))


def boot_p(sorted_vals):
    """Two-sided bootstrap p: the smallest alpha whose percentile CI excludes 0."""
    n = len(sorted_vals)
    le = sum(1 for v in sorted_vals if v <= 0.0)
    ge = n - sum(1 for v in sorted_vals if v < 0.0)
    return min(1.0, 2.0 * min((1.0 + le) / (n + 1.0), (1.0 + ge) / (n + 1.0)))


def bootstrap(event_sums, rng, derive, b=B):
    """event_sums: list of NCOL-tuples, one per event present in this subset.
    derive(cols) -> dict of scalar statistics. Returns {name: sorted replicates}.
    """
    E = len(event_sums)
    out = None
    for _ in range(b):
        sel = rng.choices(event_sums, k=E)
        c0 = c1 = c2 = c3 = c4 = c5 = c6 = 0.0
        for t in sel:
            c0 += t[0]; c1 += t[1]; c2 += t[2]; c3 += t[3]
            c4 += t[4]; c5 += t[5]; c6 += t[6]
        vals = derive((c0, c1, c2, c3, c4, c5, c6))
        if out is None:
            out = {k: [] for k in vals}
        for k, v in vals.items():
            out[k].append(v)
    return {k: sorted(v) for k, v in out.items()}


def holm(pvals, alpha=ALPHA):
    """Holm-Bonferroni. Returns (reject list, per-hypothesis CI level list).
    The CI level for the hypothesis at rank k is 1 - alpha/(m-k+1)."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    reject = [False] * m
    level = [0.0] * m
    still = True
    for k, idx in enumerate(order, start=1):
        thr = alpha / (m - k + 1)
        level[idx] = 1.0 - thr
        if still and pvals[idx] <= thr:
            reject[idx] = True
        else:
            still = False
    return reject, level


# --------------------------------------------- mean-reversion residualiser

def fit_q_residualiser(recs, h, fit_pred, n_bins=20):
    """Ehat[dq_h | q] as an equal-count binned mean over q, FIT ON TRAIN ONLY.
    Returns a function q -> Ehat[dq|q], plus the bin edges and means."""
    pts = [(r["q"], r["fwd"][h] - r["q"]) for r in recs
           if fit_pred(r) and r["fwd"][h] is not None]
    if len(pts) < 20:
        return (lambda q: 0.0), [], []
    nb = min(n_bins, max(2, len(pts) // 25))
    pts.sort(key=lambda t: t[0])
    qs = [t[0] for t in pts]
    edges = []
    for i in range(1, nb):
        edges.append(qs[int(len(qs) * i / nb)])
    edges = sorted(set(edges))
    means = [0.0] * (len(edges) + 1)
    sums = [0.0] * (len(edges) + 1)
    cnts = [0] * (len(edges) + 1)
    import bisect as _b
    for qv, dv in pts:
        j = _b.bisect_right(edges, qv)
        sums[j] += dv
        cnts[j] += 1
    for j in range(len(means)):
        means[j] = sums[j] / cnts[j] if cnts[j] else 0.0

    def f(qv, _e=edges, _m=means):
        return _m[_b.bisect_right(_e, qv)]

    return f, edges, means


# ------------------------------------------------------------- one analysis

def analyse(recs, h, pred, rng, resid_fn, direction_sign=None, label=""):
    sub = [r for r in recs if pred(r)]
    denom = len(sub)
    sub = [r for r in sub if r["fwd"][h] is not None]
    n = len(sub)
    if n == 0:
        return None

    # point estimate of the primary statistic fixes the trade direction (D-4)
    a_vals = [r["sd"] * (r["fwd"][h] - r["q"]) for r in sub]
    prim = sum(a_vals) / n
    if direction_sign is None:
        direction_sign = 1.0 if prim >= 0 else -1.0

    # accumulate per-event sums
    ev_sums = {}
    n_zero = 0
    for r, a in zip(sub, a_vals):
        dq = r["fwd"][h] - r["q"]
        if r["sd"] == 0:
            n_zero += 1
        trade = direction_sign * r["sd"]
        fee_adv = r["fee_yes"] if trade >= 0 else r["fee_no"]
        f_mid = r["half_spread"] + r["fee_mid"]
        f_adv = r["half_spread"] + fee_adv
        rv = r["sd"] * (dq - resid_fn(r["q"]))
        t = ev_sums.get(r["ev"])
        row = (a, r["d"] * dq, abs(r["d"]), f_mid, f_adv, rv, 1.0)
        if t is None:
            ev_sums[r["ev"]] = list(row)
        else:
            for i in range(NCOL):
                t[i] += row[i]
    es = [tuple(v) for v in ev_sums.values()]

    ds = direction_sign

    def derive(c):
        cnt = c[6]
        return {
            "primary": c[0] / cnt,
            "secondary": (c[1] / c[2]) if c[2] > 0 else 0.0,
            "net_mid": (ds * c[0] - c[3]) / cnt,
            "net_adv": (ds * c[0] - c[4]) / cnt,
            "net_rt": (ds * c[0] - 2.0 * c[4]) / cnt,
            "primary_resid": c[5] / cnt,
        }

    reps = bootstrap(es, rng, derive)

    tot = [0.0] * NCOL
    for t in es:
        for i in range(NCOL):
            tot[i] += t[i]
    pt = derive(tuple(tot))

    res = {
        "horizon": h, "label": label, "n": n, "n_events": len(es),
        "coverage": n / denom if denom else 0.0,
        "direction_sign": ds, "n_zero_d": n_zero,
        "floor_mid": tot[3] / n, "floor_adv": tot[4] / n,
        "half_spread_mean": sum(r["half_spread"] for r in sub) / n,
        "fee_mid_mean": sum(r["fee_mid"] for r in sub) / n,
        "mean_abs_d": tot[2] / n,
    }
    for k in ("primary", "secondary", "net_mid", "net_adv", "net_rt",
              "primary_resid"):
        res[k] = pt[k]
        res[k + "_ci95"] = ci(reps[k], 0.95)
        res[k + "_p"] = boot_p(reps[k])
    res["_reps"] = reps
    return res


# --------------------------------------------------------------------- main

def main():
    rng = random.Random(SEED)
    recs, n_events = load()
    n = len(recs)
    tr = lambda r: r["split"] == "train"
    ho = lambda r: r["split"] == "holdout"
    allp = lambda r: True

    n_tr = sum(1 for r in recs if tr(r))
    n_ho = n - n_tr
    print(f"rows={n}  events={n_events}  train={n_tr}  holdout={n_ho}")
    print(f"d == 0 exactly: {sum(1 for r in recs if r['sd'] == 0)}")
    print(f"mean |d| = {sum(abs(r['d']) for r in recs)/n:.5f}")
    print(f"B={B}  alpha={ALPHA}  seed={SEED}\n")

    resid_fns = {}
    for h in HORIZONS:
        resid_fns[h], _, _ = fit_q_residualiser(recs, h, tr)

    pooled, train, hold = {}, {}, {}
    for h in HORIZONS:
        print(f"  bootstrapping {h} ...", flush=True)
        pooled[h] = analyse(recs, h, allp, rng, resid_fns[h], label="pooled")
        train[h] = analyse(recs, h, tr, rng, resid_fns[h], label="train")
        ds = train[h]["direction_sign"] if train[h] else None
        hold[h] = analyse(recs, h, ho, rng, resid_fns[h],
                          direction_sign=ds, label="holdout")

    # Holm on the pooled set -- the preregistered criterion is the full matched set
    pv = [pooled[h]["primary_p"] for h in HORIZONS]
    rej, lev = holm(pv)
    npv = [pooled[h]["net_adv_p"] for h in HORIZONS]
    rej_n, lev_n = holm(npv)
    for i, h in enumerate(HORIZONS):
        r = pooled[h]
        r["holm_reject_primary"] = rej[i]
        r["holm_level"] = lev[i]
        r["primary_ci_holm"] = ci(r["_reps"]["primary"], lev[i])
        r["holm_reject_net"] = rej_n[i]
        r["holm_level_net"] = lev_n[i]
        r["net_adv_ci_holm"] = ci(r["_reps"]["net_adv"], lev_n[i])
        r["net_mid_ci_holm"] = ci(r["_reps"]["net_mid"], lev_n[i])
        r["PASS"] = bool(rej_n[i] and r["net_adv_ci_holm"][0] > 0.0)

    # ---- exploratory: is dq predictable from q alone?
    expl = {}
    for h in HORIZONS:
        sub = [r for r in recs if r["fwd"][h] is not None]
        if not sub:
            continue
        ev_sums = {}
        nz = agree = 0
        for r in sub:
            dq = r["fwd"][h] - r["q"]
            toward = sgn(0.5 - r["q"])
            if r["sd"] != 0:
                nz += 1
                if r["sd"] == toward:
                    agree += 1
            t = ev_sums.setdefault(r["ev"], [0.0, 0.0])
            t[0] += dq * toward
            t[1] += 1.0
        es = [(v[0], 0, 0, 0, 0, 0, v[1]) for v in ev_sums.values()]
        reps = bootstrap(es, rng, lambda c: {"rev": c[0] / c[6]})
        tot0 = sum(v[0] for v in ev_sums.values())
        totn = sum(v[1] for v in ev_sums.values())
        # correlation of q with dq
        qs = [r["q"] for r in sub]
        dqs = [r["fwd"][h] - r["q"] for r in sub]
        mq = sum(qs) / len(qs)
        md = sum(dqs) / len(dqs)
        cov = sum((a - mq) * (b - md) for a, b in zip(qs, dqs))
        vq = sum((a - mq) ** 2 for a in qs)
        vd = sum((b - md) ** 2 for b in dqs)
        corr = cov / math.sqrt(vq * vd) if vq > 0 and vd > 0 else 0.0
        expl[h] = {
            "n": len(sub),
            "reversion_stat": tot0 / totn,
            "reversion_ci95": ci(reps["rev"], 0.95),
            "reversion_p": boot_p(reps["rev"]),
            "corr_q_dq": corr,
            "frac_sign_d_toward_mid": agree / nz if nz else 0.0,
            "primary_raw": pooled[h]["primary"],
            "primary_resid": pooled[h]["primary_resid"],
            "primary_resid_ci95": pooled[h]["primary_resid_ci95"],
            "primary_resid_p": pooled[h]["primary_resid_p"],
        }

    def strip(dd):
        return {h: {k: v for k, v in r.items() if k != "_reps"}
                for h, r in dd.items() if r}

    out = {
        "meta": {"B": B, "alpha": ALPHA, "seed": SEED, "n_rows": n,
                 "n_events": n_events, "n_train": n_tr, "n_holdout": n_ho},
        "pooled": strip(pooled), "train": strip(train),
        "holdout": strip(hold), "exploratory": expl,
    }
    with open(OUT_JSON, "w") as fh:
        json.dump(out, fh, indent=1)

    # ------------------------------------------------------ console report
    def show(name, dd):
        print(f"\n=== {name} ===")
        print("hor       n   evts   cov%   E[sgn(d)dq]          95% CI"
              "          p    floor_adv    net_adv")
        for h in HORIZONS:
            r = dd.get(h)
            if not r:
                print(f"{h:<4}  ABSENT")
                continue
            lo, hi = r["primary_ci95"]
            print(f"{h:<4} {r['n']:>7} {r['n_events']:>6} "
                  f"{100*r['coverage']:>5.1f}   {r['primary']:>+.5f}   "
                  f"[{lo:>+.5f},{hi:>+.5f}] {r['primary_p']:>7.4f}   "
                  f"{r['floor_adv']:>.5f}   {r['net_adv']:>+.5f}")

    show("POOLED (preregistered criterion set)", out["pooled"])

    print("\nHolm (family = all 6 horizons, alpha=0.05) on the PRIMARY statistic:")
    for h in HORIZONS:
        r = out["pooled"][h]
        lo, hi = r["primary_ci_holm"]
        print(f"  {h:<4} p={r['primary_p']:.4f} lvl={r['holm_level']:.5f} "
              f"CI=[{lo:+.5f},{hi:+.5f}] reject={r['holm_reject_primary']}")

    print("\nHolm on the NET-OF-FLOOR statistic (the PASS statistic):")
    for h in HORIZONS:
        r = out["pooled"][h]
        lo, hi = r["net_adv_ci_holm"]
        print(f"  {h:<4} net_adv={r['net_adv']:+.5f} p={r['net_adv_p']:.4f} "
              f"CI=[{lo:+.5f},{hi:+.5f}] reject={r['holm_reject_net']} "
              f"PASS={r['PASS']} | net_roundtrip={r['net_rt']:+.5f}")

    print("\nSecondary (magnitude weighted E[d*dq]/E[|d|]), pooled:")
    for h in HORIZONS:
        r = out["pooled"][h]
        lo, hi = r["secondary_ci95"]
        print(f"  {h:<4} {r['secondary']:+.5f} [{lo:+.5f},{hi:+.5f}] "
              f"p={r['secondary_p']:.4f}")

    show("TRAIN", out["train"])
    show("HOLDOUT (direction fixed from TRAIN)", out["holdout"])

    print("\n=== EXPLORATORY: mean reversion in q alone ===")
    print("hor  E[dq*sgn(.5-q)]         95% CI          p  corr(q,dq) "
          " P(sgn d->mid)   raw -> residualised")
    for h in HORIZONS:
        e = expl.get(h)
        if not e:
            continue
        lo, hi = e["reversion_ci95"]
        rl, rh = e["primary_resid_ci95"]
        print(f"{h:<4} {e['reversion_stat']:>+.5f}  [{lo:+.5f},{hi:+.5f}] "
              f"{e['reversion_p']:>7.4f}  {e['corr_q_dq']:>+.4f}  "
              f"{e['frac_sign_d_toward_mid']:>7.3f}   "
              f"{e['primary_raw']:+.5f} -> {e['primary_resid']:+.5f} "
              f"[{rl:+.5f},{rh:+.5f}] p={e['primary_resid_p']:.4f}")

    print(f"\nwrote {OUT_JSON}")


if __name__ == "__main__":
    main()
