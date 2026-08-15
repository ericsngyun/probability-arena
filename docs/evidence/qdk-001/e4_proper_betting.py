"""EDGE-DISCOVERY-001 / E4 -- proper-betting decomposition at executable prices.

Implements arXiv 2607.06166 (Theorem 8 / Lemma 9 / Corollary 19) observationally on the
frozen EDGE-DISCOVERY-001 dataset, and reports both

    theory:      Profit = ScoreGap + D_G(q,p) - L_rho(s;q)
    executable:  Net    = Gross@mid - Spread - Fees - PriceImpact(unmeasured, >= 0)

Preregistration: docs/experiments/EDGE-DISCOVERY-001-PREREGISTRATION.md
Pure stdlib on purpose: numpy/scipy are not installed in this repo's venv and no
installs were permitted, matching delta_s_strict.py in the same directory.

Run:  .venv/bin/python docs/evidence/qdk-001/e4_proper_betting.py
"""

from __future__ import annotations

import csv
import json
import math
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "edge_discovery_001_dataset.csv")

CLIP_LO, CLIP_HI = 0.01, 0.99  # preregistration section 1, numerical guards
BLOCK_CONTRACTS = 100  # C in the Kalshi fee formula
TAKER_RATE = 0.07
BOOTSTRAP_ITERS = 5000
RNG_SEED = 20260815

# ----------------------------------------------------------------------------------
# scoring-rule machinery (binary, kept as explicit 2-vectors so the algebra is literal)
# ----------------------------------------------------------------------------------


def G(rule, v):
    if rule == "brier":  # G(v) = sum_k v_k^2
        return v * v + (1.0 - v) * (1.0 - v)
    return v * math.log(v) + (1.0 - v) * math.log(1.0 - v)  # negative entropy


def gradG(rule, v):
    """(d/dYES, d/dNO)."""
    if rule == "brier":
        return 2.0 * v, 2.0 * (1.0 - v)
    return math.log(v) + 1.0, math.log(1.0 - v) + 1.0


def score(rule, v, y):
    """Savage representation S(v,y) = G(v) + gradG(v).(1_y - v). Higher is better."""
    gy, gn = gradG(rule, v)
    return G(rule, v) + gy * (y - v) + gn * ((1.0 - y) - (1.0 - v))


def bregman(rule, a, b):
    """D_G(a,b) = G(a) - G(b) - gradG(b).(a-b) >= 0."""
    gy, gn = gradG(rule, b)
    return G(rule, a) - G(rule, b) - gy * (a - b) - gn * ((1.0 - a) - (1.0 - b))


def net_exposure(rule, p, q):
    """Reduced scalar YES exposure of s_G(p,q) = gradG(p) - gradG(q).

    In a binary market 1_y - q = (y-q, -(y-q)), so s.(1_y - q) = (s_yes - s_no)(y-q);
    only the scalar (s_yes - s_no) survives.
      Brier -> 4(p-q)                 log -> logit(p) - logit(q)
    """
    py, pn = gradG(rule, p)
    qy, qn = gradG(rule, q)
    return (py - qy) - (pn - qn)


# ----------------------------------------------------------------------------------
# fees -- Kalshi schedule effective 2026-07-07, taker only
# ----------------------------------------------------------------------------------


def ceil_cent(dollars):
    return math.ceil(dollars * 100.0 - 1e-9) / 100.0


def taker_fee_cents_per_contract(price_cents, contracts=BLOCK_CONTRACTS, mult=1.0):
    """round_up_to_cent(M * 0.07 * C * P * (1-P)); P = dollar price of the contract
    ACTUALLY BOUGHT, so the fee is per trade at its own price, never a flat rate."""
    P = price_cents / 100.0
    return 100.0 * ceil_cent(mult * TAKER_RATE * contracts * P * (1.0 - P)) / contracts


# ----------------------------------------------------------------------------------
# load
# ----------------------------------------------------------------------------------


def load():
    rows = []
    n_clip_p = n_clip_q = 0
    with open(DATA) as fh:
        for r in csv.DictReader(fh):
            p_raw, q_raw = float(r["p"]), float(r["q"])
            p = min(max(p_raw, CLIP_LO), CLIP_HI)
            q = min(max(q_raw, CLIP_LO), CLIP_HI)
            n_clip_p += p != p_raw
            n_clip_q += q != q_raw
            rows.append(
                {
                    "event": r["event"],
                    "split": r["split"],
                    "p": p,
                    "q": q,
                    "bid": float(r["yes_bid_c"]),
                    "ask": float(r["yes_ask_c"]),
                    "y": float(r["y"]),
                }
            )
    return rows, n_clip_p, n_clip_q


# ----------------------------------------------------------------------------------
# trade construction -- executable prices only
# ----------------------------------------------------------------------------------


def build_trade(row, use_abstain_band=True, block=BLOCK_CONTRACTS):
    """One row -> at most one taker trade, held to settlement.

      BUY YES pays  yes_ask_c
      BUY NO  pays  100 - yes_bid_c    (NO ask = 100 - YES bid; NO bid = 100 - YES ask)

    Direction:
      abstain band (Corollary 19, primary): YES iff p > ask/100, NO iff p < bid/100.
      naive (sensitivity):                  YES iff p > q,       NO iff p < q.
    """
    p, q, bid, ask, y = row["p"], row["q"], row["bid"], row["ask"], row["y"]
    crossed = ask < bid  # quote artifact, see deviation D-3
    if use_abstain_band:
        side = 1 if p > ask / 100.0 else (-1 if p < bid / 100.0 else 0)
    else:
        side = 1 if p > q else (-1 if p < q else 0)
    if crossed:
        side = 0
    entry = ask if side > 0 else (100.0 - bid)
    if side != 0 and not (0.0 < entry < 100.0):
        side = 0  # a contract can only be bought strictly inside (0, 100)
    if side == 0:
        return {"traded": False, "crossed": crossed, "side": 0}
    payoff = 100.0 * y if side > 0 else 100.0 * (1.0 - y)
    mid_price = 100.0 * q if side > 0 else 100.0 * (1.0 - q)
    fee = taker_fee_cents_per_contract(entry, contracts=block)
    return {
        "traded": True,
        "crossed": crossed,
        "side": side,
        "entry": entry,
        "gross_settle": payoff - entry,  # gross P&L per contract, executable price
        "gross_mid": payoff - mid_price,  # frictionless P&L at the midpoint
        "spread": entry - mid_price,  # >= 0 for an uncrossed book
        "fee": fee,
        "net": payoff - entry - fee,
    }


def weight(row, tr, strategy, lam=None):
    """Relative weight. s_G is defined only up to a positive scalar (Theorem 13), so
    weights are normalised downstream to mean 1 contract per trade; no positive scalar
    can change any sign."""
    if strategy in ("brier", "log"):
        return abs(net_exposure(strategy, row["p"], row["q"]))
    if strategy.startswith("kelly"):
        a = tr["entry"] / 100.0
        belief = row["p"] if tr["side"] > 0 else 1.0 - row["p"]
        return max(0.0, lam * (belief - a) / (1.0 - a))
    raise ValueError(strategy)


# ----------------------------------------------------------------------------------
# clustered bootstrap -- resample EVENTS with replacement
# ----------------------------------------------------------------------------------


def cluster_bootstrap(events, values, weights=None, iters=BOOTSTRAP_ITERS, seed=RNG_SEED):
    """Nonparametric cluster bootstrap at the EVENT level. With weights this is the
    ratio estimator sum(w*x)/sum(w), recomputed inside every resample so the random
    denominator is handled correctly."""
    if not values:
        return float("nan"), float("nan"), float("nan")
    if weights is None:
        weights = [1.0] * len(values)
    sv, sw = {}, {}
    for e, v, w in zip(events, values, weights):
        sv[e] = sv.get(e, 0.0) + v * w
        sw[e] = sw.get(e, 0.0) + w
    keys = list(sv)
    a = [sv[k] for k in keys]
    b = [sw[k] for k in keys]
    K = len(keys)
    point = sum(a) / sum(b) if sum(b) else float("nan")
    rng = random.Random(seed)
    idx_pool = range(K)
    stats = []
    for _ in range(iters):
        draw = rng.choices(idx_pool, k=K)
        num = 0.0
        den = 0.0
        for i in draw:
            num += a[i]
            den += b[i]
        if den:
            stats.append(num / den)
    stats.sort()
    lo = stats[int(0.025 * len(stats))]
    hi = stats[min(len(stats) - 1, int(0.975 * len(stats)))]
    return point, lo, hi


# ----------------------------------------------------------------------------------
# evaluation
# ----------------------------------------------------------------------------------

STRATEGIES = [
    ("brier", "proper Brier transform", None),
    ("log", "proper log transform", None),
    ("kelly025", "fractional Kelly lambda=0.25", 0.25),
    ("kelly050", "fractional Kelly lambda=0.50", 0.50),
    ("no_trade", "no-trade baseline", None),
]

ZERO_ROW = {
    "n_trades": 0,
    "n_events": 0,
    "gross_mid": 0.0,
    "spread": 0.0,
    "fee": 0.0,
    "gross_settle": 0.0,
    "net": 0.0,
    "net_lo": 0.0,
    "net_hi": 0.0,
    "net_w": 0.0,
    "net_w_lo": 0.0,
    "net_w_hi": 0.0,
    "total_net_dollars": 0.0,
    "score_gap": 0.0,
    "divergence": 0.0,
    "theory_profit": 0.0,
    "win_rate": float("nan"),
}


def evaluate(rows, split, use_abstain_band=True, block=BLOCK_CONTRACTS, iters=BOOTSTRAP_ITERS):
    sub = [r for r in rows if r["split"] == split]
    trades = [build_trade(r, use_abstain_band, block) for r in sub]
    n_crossed = sum(1 for t in trades if t["crossed"])
    sel = [(r, t) for r, t in zip(sub, trades) if t["traded"]]
    out = []
    for name, label, lam in STRATEGIES:
        if name == "no_trade":
            out.append(dict(ZERO_ROW, strategy=name, label=label))
            continue
        rule = name if name in ("brier", "log") else "brier"
        ev = [r["event"] for r, _ in sel]
        net = [t["net"] for _, t in sel]
        ws = [weight(r, t, name, lam) for r, t in sel]
        mw = sum(ws) / len(ws) if ws and sum(ws) > 0 else 1.0
        ws = [w / mw for w in ws]

        sg, dv, th = [], [], []
        for r, _t in sel:
            p, q, y = r["p"], r["q"], r["y"]
            g = score(rule, p, y) - score(rule, q, y)
            d = bregman(rule, q, p)
            t_ = net_exposure(rule, p, q) * (y - q)
            assert abs(t_ - (g + d)) < 1e-9, "Lemma 9 identity violated"
            sg.append(g)
            dv.append(d)
            th.append(t_)

        n = len(sel)
        pt, lo, hi = cluster_bootstrap(ev, net, iters=iters)
        ptw, low, hiw = cluster_bootstrap(ev, net, weights=ws, iters=iters)
        out.append(
            {
                "strategy": name,
                "label": label,
                "n_trades": n,
                "n_events": len(set(ev)),
                "gross_mid": sum(t["gross_mid"] for _, t in sel) / n,
                "spread": sum(t["spread"] for _, t in sel) / n,
                "fee": sum(t["fee"] for _, t in sel) / n,
                "gross_settle": sum(t["gross_settle"] for _, t in sel) / n,
                "net": pt,
                "net_lo": lo,
                "net_hi": hi,
                "net_w": ptw,
                "net_w_lo": low,
                "net_w_hi": hiw,
                "total_net_dollars": sum(net) * block / 100.0,
                "score_gap": sum(sg) / n,
                "divergence": sum(dv) / n,
                "theory_profit": sum(th) / n,
                "win_rate": sum(1 for x in net if x > 0) / n,
                "mean_entry_cents": sum(t["entry"] for _, t in sel) / n,
                "pct_yes_side": sum(1 for _, t in sel if t["side"] > 0) / n,
                "weight_share_top_decile": (
                    sum(sorted(ws, reverse=True)[: max(1, n // 10)]) / sum(ws)
                ),
            }
        )
    return out, {"n_rows": len(sub), "n_crossed": n_crossed, "n_traded": len(sel)}


def kelly_absolute(rows, split, bankroll=1000.0):
    """Fractional Kelly at a real, stated scale: lambda*f of a fixed $1,000 per trade,
    non-compounding. The only place lambda is NOT cancelled by normalisation."""
    sub = [r for r in rows if r["split"] == split]
    res = {}
    for lam in (0.25, 0.50):
        tot_net = tot_notional = tot_contracts = 0.0
        for r in sub:
            t = build_trade(r, True, BLOCK_CONTRACTS)
            if not t["traded"]:
                continue
            a = t["entry"] / 100.0
            belief = r["p"] if t["side"] > 0 else 1.0 - r["p"]
            f = max(0.0, lam * (belief - a) / (1.0 - a))
            c = math.floor(f * bankroll / a)
            if c <= 0:
                continue
            fee_c = 100.0 * ceil_cent(TAKER_RATE * c * a * (1 - a)) / c
            payoff = 100.0 * r["y"] if t["side"] > 0 else 100.0 * (1.0 - r["y"])
            tot_net += (payoff - t["entry"] - fee_c) * c / 100.0
            tot_notional += t["entry"] * c / 100.0
            tot_contracts += c
        res[str(lam)] = {
            "total_net_dollars": tot_net,
            "total_notional_dollars": tot_notional,
            "contracts": tot_contracts,
            "roi_pct": 100.0 * tot_net / tot_notional if tot_notional else float("nan"),
        }
    return res


def main():
    rows, clip_p, clip_q = load()
    report = {
        "n_rows": len(rows),
        "n_events": len(set(r["event"] for r in rows)),
        "clip_p": clip_p,
        "clip_q": clip_q,
        "block_contracts": BLOCK_CONTRACTS,
        "bootstrap_iters": BOOTSTRAP_ITERS,
        "rng_seed": RNG_SEED,
    }
    for split in ("train", "holdout"):
        for band, key in ((True, "abstain"), (False, "naive")):
            res, meta = evaluate(rows, split, use_abstain_band=band)
            report[f"{split}_{key}"] = res
            report[f"{split}_{key}_meta"] = meta
        res1, _ = evaluate(rows, split, use_abstain_band=True, block=1, iters=2000)
        report[f"{split}_abstain_C1"] = res1
        report[f"{split}_kelly_absolute"] = kelly_absolute(rows, split)

    with open(os.path.join(HERE, "e4_results.json"), "w") as fh:
        json.dump(report, fh, indent=2)

    for split in ("train", "holdout"):
        for key in ("abstain", "naive", "abstain_C1"):
            m = report.get(f"{split}_{key}_meta")
            print(f"\n=== {split.upper()} / {key} (C={BLOCK_CONTRACTS if key!='abstain_C1' else 1}) {m or ''}")
            print(
                f"{'strategy':<10}{'n':>6}{'ev':>6}{'gross@mid':>11}{'spread':>9}"
                f"{'fee':>7}{'grossExe':>10}{'net':>9}{'95% CI':>22}{'netW':>9}{'95% CI(W)':>22}"
            )
            for r in report[f"{split}_{key}"]:
                print(
                    f"{r['strategy']:<10}{r['n_trades']:>6}{r['n_events']:>6}"
                    f"{r['gross_mid']:>11.4f}{-r['spread']:>9.4f}{-r['fee']:>7.4f}"
                    f"{r['gross_settle']:>10.4f}{r['net']:>9.4f}"
                    f"   [{r['net_lo']:>7.4f},{r['net_hi']:>8.4f}]"
                    f"{r['net_w']:>9.4f}   [{r['net_w_lo']:>7.4f},{r['net_w_hi']:>8.4f}]"
                )
        print(f"--- {split} theory terms (per trade, score units)")
        for r in report[f"{split}_abstain"]:
            print(
                f"    {r['strategy']:<10} scoregap={r['score_gap']:+.5f} "
                f"divergence={r['divergence']:+.5f} sum={r['theory_profit']:+.5f} "
                f"winrate={r['win_rate']:.4f} meanEntry={r.get('mean_entry_cents', 0):.2f} "
                f"pctYES={r.get('pct_yes_side', 0):.3f} topDecileW={r.get('weight_share_top_decile', 0):.3f}"
            )
        print(f"--- {split} kelly absolute ($1,000/trade, non-compounding)")
        print("   ", json.dumps(report[f"{split}_kelly_absolute"]))


if __name__ == "__main__":
    main()
