"""EDGE-DISCOVERY-001 / E1 — does p - q carry information CONDITIONAL on q?

Preregistration: docs/experiments/EDGE-DISCOVERY-001-PREREGISTRATION.md sec.2

    logit P(Y=1) = alpha + beta_q * z_q + beta_d * d
    z_q = logit(q)          d = logit(p) - logit(q)

Fitted on TRAIN ONLY. HOLDOUT is scored exactly once, at the end.

PASS(E1) iff the two-term model beats MARKET ALONE (raw q) on HOLDOUT on BOTH
Brier and log loss, with event-clustered 95% CIs on the paired differences
excluding zero.

All inference is clustered at the EVENT level (1,482 events), never at ticker.
Primary CI machinery: nonparametric CLUSTER BOOTSTRAP (resample events with
replacement). A cluster-robust sandwich (CR1) is reported alongside the
bootstrap for the train coefficients as an independent cross-check.

READ-ONLY over a frozen in-repo CSV. Authorizes nothing.
"""

from __future__ import annotations

import csv
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "edge_discovery_001_dataset.csv")

CLIP_LO, CLIP_HI = 0.01, 0.99
N_BOOT_COEF = 2000     # preregistered floor is >= 2000
N_BOOT_EVAL = 10000    # paired-difference CIs are cheap (no refit)
SEED = 20260815
Z95 = 1.959963984540054

# E1_SMOKE=1 exercises every code path with TRAIN standing in for HOLDOUT and
# tiny bootstrap counts.  It exists so the machinery can be debugged WITHOUT
# looking at the holdout split, which is scored exactly once, in the real run.
SMOKE = os.environ.get("E1_SMOKE") == "1"
if SMOKE:
    N_BOOT_COEF, N_BOOT_EVAL = 40, 60


# ---------------------------------------------------------------- data

def load():
    rows = []
    with open(DATA, newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append(r)
    return rows


def logit(x):
    return np.log(x / (1.0 - x))


def build(rows):
    """Returns per-split design matrices. Reports clipping and drops."""
    p_raw = np.array([float(r["p"]) for r in rows])
    q_raw = np.array([float(r["q"]) for r in rows])
    y = np.array([float(r["y"]) for r in rows])
    split = np.array([r["split"] for r in rows])
    event = np.array([r["event"] for r in rows])

    n_clip_p = int(((p_raw < CLIP_LO) | (p_raw > CLIP_HI)).sum())
    n_clip_q = int(((q_raw < CLIP_LO) | (q_raw > CLIP_HI)).sum())
    n_clip_either = int(
        ((p_raw < CLIP_LO) | (p_raw > CLIP_HI)
         | (q_raw < CLIP_LO) | (q_raw > CLIP_HI)).sum()
    )
    n_clip_p_lo = int((p_raw < CLIP_LO).sum())
    n_clip_p_hi = int((p_raw > CLIP_HI).sum())
    n_clip_q_lo = int((q_raw < CLIP_LO).sum())
    n_clip_q_hi = int((q_raw > CLIP_HI).sum())

    p = np.clip(p_raw, CLIP_LO, CLIP_HI)
    q = np.clip(q_raw, CLIP_LO, CLIP_HI)

    z_q = logit(q)
    d = logit(p) - logit(q)

    clip = dict(
        n_clip_p=n_clip_p, n_clip_q=n_clip_q, n_clip_either=n_clip_either,
        n_clip_p_lo=n_clip_p_lo, n_clip_p_hi=n_clip_p_hi,
        n_clip_q_lo=n_clip_q_lo, n_clip_q_hi=n_clip_q_hi,
    )
    return dict(p=p, q=q, y=y, split=split, event=event, z_q=z_q, d=d), clip


# ---------------------------------------------------------------- model

def irls(X, y, max_iter=100, tol=1e-10):
    """Plain Newton-Raphson / IRLS logistic fit. Returns (beta, converged)."""
    n, k = X.shape
    beta = np.zeros(k)
    for _ in range(max_iter):
        eta = X @ beta
        eta = np.clip(eta, -35.0, 35.0)
        mu = 1.0 / (1.0 + np.exp(-eta))
        w = np.maximum(mu * (1.0 - mu), 1e-10)
        g = X.T @ (y - mu)
        H = (X * w[:, None]).T @ X
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            return beta, False
        beta = beta + step
        if np.max(np.abs(step)) < tol:
            return beta, True
    return beta, False


def predict(X, beta):
    eta = np.clip(X @ beta, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-eta))


def cluster_sandwich(X, y, beta, cluster_ids):
    """CR1 cluster-robust covariance for a logistic MLE."""
    n, k = X.shape
    mu = predict(X, beta)
    w = np.maximum(mu * (1.0 - mu), 1e-10)
    bread = np.linalg.inv((X * w[:, None]).T @ X)
    resid = (y - mu)[:, None] * X
    uniq, inv = np.unique(cluster_ids, return_inverse=True)
    G = len(uniq)
    sums = np.zeros((G, k))
    np.add.at(sums, inv, resid)
    meat = sums.T @ sums
    c = (G / (G - 1.0)) * ((n - 1.0) / (n - k))
    return c * (bread @ meat @ bread), G


# ---------------------------------------------------------------- scoring

def brier_vec(pred, y):
    return (pred - y) ** 2


def logloss_vec(pred, y):
    pr = np.clip(pred, 1e-12, 1 - 1e-12)
    return -(y * np.log(pr) + (1 - y) * np.log(1 - pr))


# ---------------------------------------------------------------- bootstrap

def cluster_index(event_ids):
    """Map events -> list of row indices, as an object array of int arrays."""
    order = np.argsort(event_ids, kind="stable")
    ev_sorted = event_ids[order]
    uniq, starts = np.unique(ev_sorted, return_index=True)
    groups = np.split(order, starts[1:])
    return uniq, groups


def boot_event_draws(groups, rng, n_iter):
    """Yield row-index arrays for n_iter cluster-bootstrap resamples."""
    G = len(groups)
    for _ in range(n_iter):
        pick = rng.integers(0, G, size=G)
        yield np.concatenate([groups[j] for j in pick])


def pct_ci(samples):
    s = np.asarray(samples)
    s = s[np.isfinite(s)]
    return float(np.percentile(s, 2.5)), float(np.percentile(s, 97.5)), len(s)


# ---------------------------------------------------------------- main

def main():
    rng = np.random.default_rng(SEED)
    rows = load()
    D, clip = build(rows)

    n_total = len(rows)
    tr = D["split"] == "train"
    ho = D["split"] == "holdout"
    if SMOKE:
        ho = tr.copy()   # never touches the real holdout
    n_tr, n_ho = int(tr.sum()), int(ho.sum())
    n_other = n_total - n_tr - n_ho

    out = []

    def say(s=""):
        print(s)
        out.append(s)

    say("=" * 74)
    say("EDGE-DISCOVERY-001 / E1 — conditional information of p given q")
    say("=" * 74)
    say(f"rows total                     {n_total}")
    say(f"  TRAIN                        {n_tr}")
    say(f"  HOLDOUT                      {n_ho}")
    say(f"  neither split (dropped)      {n_other}")
    say(f"events (all)                   {len(set(D['event']))}")
    say(f"events TRAIN                   {len(set(D['event'][tr]))}")
    say(f"events HOLDOUT                 {len(set(D['event'][ho]))}")
    say(f"events in BOTH splits          "
        f"{len(set(D['event'][tr]) & set(D['event'][ho]))}")
    say(f"tickers (all)                  "
        f"{len({r['market_ticker'] for r in rows})}")
    say()
    say("CLIPPING to [%.2f, %.2f] before any logit:" % (CLIP_LO, CLIP_HI))
    say(f"  p clipped                    {clip['n_clip_p']}  "
        f"(low {clip['n_clip_p_lo']}, high {clip['n_clip_p_hi']})")
    say(f"  q clipped                    {clip['n_clip_q']}  "
        f"(low {clip['n_clip_q_lo']}, high {clip['n_clip_q_hi']})")
    say(f"  rows with either clipped     {clip['n_clip_either']}")
    say(f"  rows dropped for any reason  0")
    say()
    say(f"base rate Y=1  TRAIN {D['y'][tr].mean():.5f}   "
        f"HOLDOUT {D['y'][ho].mean():.5f}")
    say()

    # ---- design matrices
    ones = np.ones(n_total)
    X2_all = np.column_stack([ones, D["z_q"], D["d"]])     # two-term
    X1_all = np.column_stack([ones, D["z_q"]])             # market-only

    Xtr2, Xtr1, ytr = X2_all[tr], X1_all[tr], D["y"][tr]
    Xho2, Xho1, yho = X2_all[ho], X1_all[ho], D["y"][ho]
    ev_tr, ev_ho = D["event"][tr], D["event"][ho]

    # ---- fits (TRAIN ONLY)
    b2, ok2 = irls(Xtr2, ytr)
    b1, ok1 = irls(Xtr1, ytr)
    assert ok2 and ok1, "IRLS failed to converge on TRAIN"

    V2, G2 = cluster_sandwich(Xtr2, ytr, b2, ev_tr)
    V1, G1 = cluster_sandwich(Xtr1, ytr, b1, ev_tr)
    se2, se1 = np.sqrt(np.diag(V2)), np.sqrt(np.diag(V1))

    # ---- cluster bootstrap for coefficients (refit each draw)
    uniq_tr, groups_tr = cluster_index(ev_tr)
    boot2 = np.full((N_BOOT_COEF, 3), np.nan)
    boot1 = np.full((N_BOOT_COEF, 2), np.nan)
    fails = 0
    for i, idx in enumerate(boot_event_draws(groups_tr, rng, N_BOOT_COEF)):
        bb2, o2 = irls(Xtr2[idx], ytr[idx])
        bb1, o1 = irls(Xtr1[idx], ytr[idx])
        if o2:
            boot2[i] = bb2
        if o1:
            boot1[i] = bb1
        if not (o2 and o1):
            fails += 1

    say("-" * 74)
    say("TRAIN-FITTED COEFFICIENTS  (n_train = %d, event clusters = %d)"
        % (n_tr, G2))
    say("-" * 74)
    say("two-term model:  logit P(Y=1) = a + b_q * z_q + b_d * d")
    names2 = ["alpha", "beta_q", "beta_d"]
    say(f"{'term':8} {'est':>10} {'boot 95% CI':>24} "
        f"{'CR1 SE':>9} {'CR1 95% CI':>24}")
    for j, nm in enumerate(names2):
        lo, hi, _ = pct_ci(boot2[:, j])
        clo, chi = b2[j] - Z95 * se2[j], b2[j] + Z95 * se2[j]
        say(f"{nm:8} {b2[j]:10.5f} [{lo:9.5f}, {hi:9.5f}] "
            f"{se2[j]:9.5f} [{clo:9.5f}, {chi:9.5f}]")
    say()
    say("market-only model:  logit P(Y=1) = a + b_q * z_q")
    names1 = ["alpha", "beta_q"]
    for j, nm in enumerate(names1):
        lo, hi, _ = pct_ci(boot1[:, j])
        clo, chi = b1[j] - Z95 * se1[j], b1[j] + Z95 * se1[j]
        say(f"{nm:8} {b1[j]:10.5f} [{lo:9.5f}, {hi:9.5f}] "
            f"{se1[j]:9.5f} [{clo:9.5f}, {chi:9.5f}]")
    say()
    say(f"bootstrap draws with a non-converged refit: {fails} / {N_BOOT_COEF}")
    # is the market itself miscalibrated?  test beta_q vs 1 (two-term & 1-term)
    lo, hi, _ = pct_ci(boot2[:, 1] - 1.0)
    say(f"two-term  beta_q - 1  = {b2[1]-1:+.5f}  boot 95% CI "
        f"[{lo:+.5f}, {hi:+.5f}]  -> "
        f"{'EXCLUDES 0' if lo*hi > 0 else 'includes 0'}")
    lo, hi, _ = pct_ci(boot1[:, 1] - 1.0)
    say(f"market-only beta_q-1  = {b1[1]-1:+.5f}  boot 95% CI "
        f"[{lo:+.5f}, {hi:+.5f}]  -> "
        f"{'EXCLUDES 0' if lo*hi > 0 else 'includes 0'}")
    # in-sample LR statistic for d (descriptive; CIs above are the inference)
    ll2 = -logloss_vec(predict(Xtr2, b2), ytr).sum()
    ll1 = -logloss_vec(predict(Xtr1, b1), ytr).sum()
    say(f"TRAIN log-lik: two-term {ll2:.3f}  market-only {ll1:.3f}  "
        f"LR chi2(1) = {2*(ll2-ll1):.4f}  (iid-naive, descriptive only)")
    say()

    # ---- HOLDOUT: touched exactly once, here
    preds = {
        "a_market_q":     np.clip(D["q"][ho], CLIP_LO, CLIP_HI),
        "b_two_term":     predict(Xho2, b2),
        "c_our_p":        np.clip(D["p"][ho], CLIP_LO, CLIP_HI),
        "d_market_recal": predict(Xho1, b1),
    }
    labels = {
        "a_market_q":     "(a) market alone  q",
        "b_two_term":     "(b) two-term model  a+b_q*z_q+b_d*d",
        "c_our_p":        "(c) our forecast alone  p",
        "d_market_recal": "(x) market recalibrated  a+b_q*z_q",
    }

    say("-" * 74)
    say("HOLDOUT EVALUATION  (n_holdout = %d, event clusters = %d)"
        % (n_ho, len(set(ev_ho))))
    say("-" * 74)
    say(f"{'model':42} {'Brier':>10} {'log loss':>10}")
    scores = {}
    for k in ["a_market_q", "b_two_term", "c_our_p", "d_market_recal"]:
        bs = brier_vec(preds[k], yho)
        ls = logloss_vec(preds[k], yho)
        scores[k] = (bs, ls)
        say(f"{labels[k]:42} {bs.mean():10.6f} {ls.mean():10.6f}")
    say()

    # ---- paired differences, event-clustered bootstrap
    uniq_ho, groups_ho = cluster_index(ev_ho)
    draws = [idx for idx in boot_event_draws(groups_ho, rng, N_BOOT_EVAL)]

    def paired(k_new, k_ref):
        """mean(new - ref); NEGATIVE favours k_new (lower score is better)."""
        res = {}
        for mi, metric in enumerate(["brier", "logloss"]):
            diff = scores[k_new][mi] - scores[k_ref][mi]
            point = float(diff.mean())
            bs = np.array([diff[idx].mean() for idx in draws])
            lo, hi = float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))
            res[metric] = (point, lo, hi, lo * hi > 0)
        return res

    comparisons = [
        ("b_two_term", "a_market_q",
         "PRIMARY  (b) two-term  -  (a) market alone"),
        ("b_two_term", "c_our_p",
         "         (b) two-term  -  (c) our p alone"),
        ("c_our_p", "a_market_q",
         "         (c) our p     -  (a) market alone   [known: p is worse]"),
        ("d_market_recal", "a_market_q",
         "DECOMP-1 (x) market recalibrated - (a) raw market  [MARKET effect]"),
        ("b_two_term", "d_market_recal",
         "DECOMP-2 (b) two-term - (x) market recalibrated  [AGENT effect]"),
    ]

    say("-" * 74)
    say("PAIRED DIFFERENCES ON HOLDOUT — event-clustered bootstrap")
    say("(%d resamples of %d holdout events; NEGATIVE favours the first model)"
        % (N_BOOT_EVAL, len(uniq_ho)))
    say("-" * 74)
    results = {}
    for k_new, k_ref, lab in comparisons:
        r = paired(k_new, k_ref)
        results[(k_new, k_ref)] = r
        say(lab)
        for metric in ["brier", "logloss"]:
            pt, lo, hi, excl = r[metric]
            verdict = "EXCLUDES 0" if excl else "includes 0"
            say(f"    {metric:8} {pt:+.6f}   95% CI [{lo:+.6f}, {hi:+.6f}]   "
                f"{verdict}")
        say()

    # ---- verdict
    pb = results[("b_two_term", "a_market_q")]["brier"]
    pl = results[("b_two_term", "a_market_q")]["logloss"]
    beats_brier = pb[0] < 0 and pb[3]
    beats_ll = pl[0] < 0 and pl[3]
    passed = beats_brier and beats_ll

    say("=" * 74)
    say("PREREGISTERED VERDICT")
    say("=" * 74)
    say(f"  beats market alone on Brier   with CI excluding 0 : {beats_brier}")
    say(f"  beats market alone on logloss with CI excluding 0 : {beats_ll}")
    say(f"  E1 : {'PASS' if passed else 'FAIL'}")
    lo, hi, _ = pct_ci(boot2[:, 2])
    say(f"  beta_d = {b2[2]:+.5f}  event-clustered boot 95% CI "
        f"[{lo:+.5f}, {hi:+.5f}]")
    say("=" * 74)

    if SMOKE:
        say("\n*** SMOKE RUN — train stood in for holdout; NOT A RESULT ***")
        return 0
    with open(os.path.join(HERE, "e1_output.txt"), "w") as fh:
        fh.write("\n".join(out) + "\n")
    return 0 if True else 1


if __name__ == "__main__":
    sys.exit(main())
