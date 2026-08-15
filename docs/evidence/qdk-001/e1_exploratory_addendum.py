"""EDGE-DISCOVERY-001 / E1 — EXPLORATORY ADDENDUM.  NOT PREREGISTERED.

Nothing here can change the E1 verdict, which is fixed by
e1_conditional_information.py and its frozen e1_output.txt.  This file exists
for two reasons only:

  1. Descriptive context the writeup needs (how large is d at all? how much
     train/holdout event overlap does the chronological split leave?).

  2. ONE clearly-labelled alternative specification.  The preregistered
     two-term model lets the fit re-estimate alpha and beta_q, so a base-rate
     drift between TRAIN and HOLDOUT can penalise the model for reasons that
     have nothing to do with d.  The OFFSET model removes that confound
     entirely by pinning the market at its raw price:

         logit P(Y=1) = logit(q) + beta_d * d          (one free parameter)

     If d carried conditional information, this model would beat raw q even
     though it is given no freedom to recalibrate the market.  It is the most
     favourable honest test of the agent that can be written down, so a null
     here is stronger evidence of redundancy than the preregistered null.

Exploratory results are reported as exploratory.  They graduate nothing.
"""

from __future__ import annotations

import os

import numpy as np

from e1_conditional_information import (
    CLIP_LO, CLIP_HI, N_BOOT_EVAL, SEED,
    boot_event_draws, brier_vec, build, cluster_index, load, logloss_vec,
)

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    rng = np.random.default_rng(SEED + 1)
    rows = load()
    D, _ = build(rows)
    tr = D["split"] == "train"
    ho = D["split"] == "holdout"

    out = []

    def say(s=""):
        print(s)
        out.append(s)

    say("=" * 74)
    say("E1 EXPLORATORY ADDENDUM — NOT PREREGISTERED, GRADUATES NOTHING")
    say("=" * 74)
    say()

    # ---------------- 1. how big is the disagreement at all?
    d, p, q = D["d"], D["p"], D["q"]
    say("-" * 74)
    say("DISAGREEMENT d = logit(p) - logit(q), and the raw gap p - q")
    say("-" * 74)
    for nm, mask in (("TRAIN", tr), ("HOLDOUT", ho)):
        dd, gap = d[mask], (p - q)[mask]
        qs = np.percentile(np.abs(dd), [50, 75, 90, 99])
        say(f"{nm:8} n={mask.sum():5d}  mean d={dd.mean():+.4f}  "
            f"sd d={dd.std(ddof=1):.4f}")
        say(f"         |d| median {qs[0]:.4f}  p75 {qs[1]:.4f}  "
            f"p90 {qs[2]:.4f}  p99 {qs[3]:.4f}")
        say(f"         |p-q| median {np.median(np.abs(gap)):.4f}  "
            f"mean {np.abs(gap).mean():.4f}  max {np.abs(gap).max():.4f}")
        say(f"         share |p-q| > 0.05 : "
            f"{(np.abs(gap) > 0.05).mean():.3f}   > 0.10 : "
            f"{(np.abs(gap) > 0.10).mean():.3f}")
    say(f"corr(z_q, d) TRAIN = "
        f"{np.corrcoef(D['z_q'][tr], d[tr])[0,1]:+.4f}")
    say()

    # ---------------- 2. chronological split leakage at the EVENT level
    ev_tr, ev_ho = set(D["event"][tr]), set(D["event"][ho])
    both = ev_tr & ev_ho
    ho_rows_in_both = int(np.isin(D["event"][ho], list(both)).sum())
    tr_rows_in_both = int(np.isin(D["event"][tr], list(both)).sum())
    say("-" * 74)
    say("EVENT OVERLAP ACROSS THE CHRONOLOGICAL SPLIT")
    say("-" * 74)
    say(f"events in both splits            {len(both)} of {len(ev_ho)} holdout")
    say(f"holdout rows in a shared event   {ho_rows_in_both} "
        f"({ho_rows_in_both / ho.sum():.3%} of holdout)")
    say(f"train rows in a shared event     {tr_rows_in_both}")
    say()

    # ---------------- 2b. WHAT IS d MADE OF?
    say("-" * 74)
    say("STRUCTURE OF d — is it information, or a recoding of q?  (TRAIN)")
    say("-" * 74)
    zq = D["z_q"][tr]
    zp = zq + d[tr]
    A = np.column_stack([np.ones(tr.sum()), zq])
    c_d = np.linalg.lstsq(A, d[tr], rcond=None)[0]
    c_p = np.linalg.lstsq(A, zp, rcond=None)[0]
    r2 = np.corrcoef(zq, zp)[0, 1] ** 2
    say(f"mean |logit q| = {np.abs(zq).mean():.4f}    "
        f"mean |logit p| = {np.abs(zp).mean():.4f}")
    say(f"OLS  d   ~ a + b*z_q :  a={c_d[0]:+.4f}  b={c_d[1]:+.4f}")
    say(f"OLS  z_p ~ a + b*z_q :  a={c_p[0]:+.4f}  b={c_p[1]:+.4f}   "
        f"R^2={r2:.4f}")
    say(f"share of forecasts on the same side of 0.5 as the market: "
        f"{((zp > 0) == (zq > 0)).mean():.4f}")
    say("b < 1 means p is a SHRUNK copy of q: two thirds of the variance of")
    say("our forecast is explained by the price it is supposed to beat, and")
    say("most of d is mechanical attenuation toward 0.5 rather than a view.")
    say()

    # ---------------- 3. the offset model
    say("-" * 74)
    say("EXPLORATORY ALTERNATIVE — OFFSET MODEL (market pinned at raw price)")
    say("      logit P(Y=1) = logit(q) + beta_d * d      [1 free parameter]")
    say("-" * 74)

    def fit_offset(z_q, dd, y, max_iter=100, tol=1e-12):
        b = 0.0
        for _ in range(max_iter):
            eta = np.clip(z_q + b * dd, -35.0, 35.0)
            mu = 1.0 / (1.0 + np.exp(-eta))
            w = np.maximum(mu * (1 - mu), 1e-10)
            g = float(dd @ (y - mu))
            h = float((dd * dd) @ w)
            if h <= 0:
                return b, False
            step = g / h
            b += step
            if abs(step) < tol:
                return b, True
        return b, False

    b_off, ok = fit_offset(D["z_q"][tr], d[tr], D["y"][tr])
    assert ok, "offset fit failed"

    # event-clustered bootstrap on the TRAIN coefficient
    _, groups_tr = cluster_index(D["event"][tr])
    ztr, dtr, ytr = D["z_q"][tr], d[tr], D["y"][tr]
    bs = []
    for idx in boot_event_draws(groups_tr, rng, 2000):
        bb, o = fit_offset(ztr[idx], dtr[idx], ytr[idx])
        if o:
            bs.append(bb)
    lo, hi = np.percentile(bs, [2.5, 97.5])
    say(f"beta_d (offset, TRAIN) = {b_off:+.5f}   "
        f"event-clustered boot 95% CI [{lo:+.5f}, {hi:+.5f}]   "
        f"{'EXCLUDES 0' if lo * hi > 0 else 'includes 0'}")

    # holdout scoring
    yho = D["y"][ho]
    pred_off = 1.0 / (1.0 + np.exp(-np.clip(
        D["z_q"][ho] + b_off * d[ho], -35.0, 35.0)))
    pred_q = np.clip(q[ho], CLIP_LO, CLIP_HI)

    _, groups_ho = cluster_index(D["event"][ho])
    draws = list(boot_event_draws(groups_ho, rng, N_BOOT_EVAL))

    say()
    say(f"{'model':42} {'Brier':>10} {'log loss':>10}")
    say(f"{'(a) market alone  q':42} {brier_vec(pred_q, yho).mean():10.6f} "
        f"{logloss_vec(pred_q, yho).mean():10.6f}")
    say(f"{'(o) offset  logit(q) + b_d*d':42} "
        f"{brier_vec(pred_off, yho).mean():10.6f} "
        f"{logloss_vec(pred_off, yho).mean():10.6f}")
    say()
    say("paired  (o) offset - (a) market alone   [NEGATIVE favours offset]")
    for nm, fn in (("brier", brier_vec), ("logloss", logloss_vec)):
        diff = fn(pred_off, yho) - fn(pred_q, yho)
        b = np.array([diff[i].mean() for i in draws])
        l, h = np.percentile(b, [2.5, 97.5])
        say(f"    {nm:8} {diff.mean():+.6f}   95% CI [{l:+.6f}, {h:+.6f}]   "
            f"{'EXCLUDES 0' if l * h > 0 else 'includes 0'}")
    say()
    say("=" * 74)
    say("EXPLORATORY. Does not alter the preregistered E1 verdict.")
    say("=" * 74)

    with open(os.path.join(HERE, "e1_exploratory_output.txt"), "w") as fh:
        fh.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
