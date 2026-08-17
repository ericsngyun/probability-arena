"""KALSHI-DEMO-TRAFFIC-CAPACITY-001 — the frozen decision rule, applied.

    N_4h = 14,400 x sum_i lambda_hat_i          (lambda in frames/second)

    REACHABLE    conservative LOWER BOUND >= 125,000
    BORDERLINE   point estimate >= 100,000 but lower bound < 125,000
    UNREACHABLE  point estimate < 100,000

The rule is frozen in the preregistration and is not adjusted here.

**The interval, and why it is built this way.** The preregistration requires a
stated interval on the SUM with its method named, and warns that per-market
rates are not independent within an event. They are not: the pool holds two
KXPGA markets from the same tournament and the plateau markets move together
if a single simulated market maker drives them, so a per-market Poisson
interval combined under independence would understate the variance of the sum
by whatever the cross-market correlation is.

So nothing is ever combined across markets. The unit of resampling is the
**observation bin total** — frames from ALL pool markets in one bin, already
summed — which carries whatever cross-market dependence exists inside it
without needing to model it. Serial dependence between bins (a burst spanning
several bins) is carried by resampling **contiguous blocks** of bins rather
than single bins: a circular moving-block bootstrap, which is the standard
interval for the mean of a dependent stationary series and assumes neither
Poisson arrivals nor independent markets.

**What the interval does NOT cover, stated up front.** It bounds sampling
variability WITHIN the observed window. It cannot bound variation BETWEEN a
short window and a four-hour session — diurnal flow, an event starting or
ending, a venue maintenance window. A short observation cannot produce a
four-hour guarantee, and calling the bootstrap bound one would be exactly the
false confidence this milestone exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

SECONDS_IN_4H = 14_400
REACHABLE_LOWER_BOUND = 125_000
BORDERLINE_POINT = 100_000

# Frames the venue sends once per subscription generation, not continuously.
# They are real archived frames and are reported, but a rate extrapolated from
# them would multiply a one-off handshake cost by 14,400.
ONE_OFF_TYPES = ("subscribed", "orderbook_snapshot", "error", "ok")


def block_bootstrap_lower_bound(counts, *, bin_seconds, block_bins, draws,
                                alpha, seed):
    """Circular moving-block bootstrap on the per-bin totals. One-sided lower.

    Returns (lower_bound_rate, point_rate, bootstrap_percentiles).
    """
    n = len(counts)
    if n == 0:
        return 0.0, 0.0, {}
    point = sum(counts) / (n * bin_seconds)
    if n < 2 or block_bins >= n:
        return point, point, {}
    rng = random.Random(seed)
    blocks_needed = math.ceil(n / block_bins)
    means = []
    for _ in range(draws):
        total = 0
        drawn = 0
        for _ in range(blocks_needed):
            start = rng.randrange(n)
            for offset in range(block_bins):
                total += counts[(start + offset) % n]
                drawn += 1
        means.append(total / (drawn * bin_seconds))
    means.sort()

    def pct(p):
        index = min(len(means) - 1, max(0, int(round(p * (len(means) - 1)))))
        return means[index]

    return pct(alpha), point, {
        "p01": pct(0.01), "p05": pct(0.05), "p50": pct(0.50),
        "p95": pct(0.95), "p99": pct(0.99),
    }


def analyse(probe: dict, *, block_seconds, draws, alpha, seed) -> dict:
    bin_seconds = probe["config"]["bin_seconds"]
    bins = probe["bins"]
    complete = [b for b in bins if b["complete"]]
    dropped_incomplete = len(bins) - len(complete)

    def bin_total(row, *, exclude_one_off: bool) -> int:
        if not exclude_one_off:
            return row["total"]
        return sum(n for t, n in row["by_type"].items()
                   if t not in ONE_OFF_TYPES)

    arms = {}
    for name, exclude in (("all_archived_frames", False),
                          ("continuous_frames_only", True)):
        counts = [bin_total(b, exclude_one_off=exclude) for b in complete]
        block_bins = max(1, int(round(block_seconds / bin_seconds)))
        lower, point, percentiles = block_bootstrap_lower_bound(
            counts, bin_seconds=bin_seconds, block_bins=block_bins,
            draws=draws, alpha=alpha, seed=seed)
        arms[name] = {
            "bins_used": len(counts),
            "bin_seconds": bin_seconds,
            "observed_seconds": len(counts) * bin_seconds,
            "frames_observed": sum(counts),
            "lambda_sum_point_per_second": point,
            "lambda_sum_lower_bound_per_second": lower,
            "N_4h_point": point * SECONDS_IN_4H,
            "N_4h_lower_bound": lower * SECONDS_IN_4H,
            "bootstrap_percentiles_of_lambda_sum": percentiles,
        }

    # Per-market rates, on the same complete bins and the same exclusion.
    per_market_counts: dict[str, int] = {}
    for row in complete:
        one_off = sum(n for t, n in row["by_type"].items() if t in ONE_OFF_TYPES)
        for ticker, n in row["by_ticker"].items():
            per_market_counts[ticker] = per_market_counts.get(ticker, 0) + n
        # `by_ticker` and `by_type` are two views of the same frames, so the
        # one-off count is reported beside them rather than subtracted from a
        # ticker it cannot be attributed to bin by bin.
        per_market_counts["__one_off_frames_in_window__"] = (
            per_market_counts.get("__one_off_frames_in_window__", 0) + one_off)

    observed_seconds = len(complete) * bin_seconds
    pool = probe["config"]["market_tickers"]
    per_market = {}
    for ticker in pool:
        n = per_market_counts.get(ticker, 0)
        per_market[ticker] = {
            "frames_in_window": n,
            "lambda_hat_per_second": (n / observed_seconds
                                      if observed_seconds else 0.0),
            "frames_per_4h_if_this_rate_held": (
                n / observed_seconds * SECONDS_IN_4H if observed_seconds else 0.0),
        }
    unexpected = {k: v for k, v in per_market_counts.items()
                  if k not in pool and not k.startswith("__")}

    primary = arms["all_archived_frames"]
    if primary["N_4h_lower_bound"] >= REACHABLE_LOWER_BOUND:
        verdict = "REACHABLE"
    elif primary["N_4h_point"] >= BORDERLINE_POINT:
        verdict = "BORDERLINE"
    else:
        verdict = "UNREACHABLE"

    return {
        "milestone": "KALSHI-DEMO-TRAFFIC-CAPACITY-001",
        "run_label": probe.get("run_label"),
        "environment": probe.get("environment"),
        "decision_rule": {
            "formula": "N_4h = 14,400 x sum_i lambda_hat_i",
            "REACHABLE": f"conservative lower bound >= {REACHABLE_LOWER_BOUND:,}",
            "BORDERLINE": f"point estimate >= {BORDERLINE_POINT:,} "
                          f"but lower bound < {REACHABLE_LOWER_BOUND:,}",
            "UNREACHABLE": f"point estimate < {BORDERLINE_POINT:,}",
            "frozen_in": "docs/experiments/"
                         "KALSHI-DEMO-TRAFFIC-CAPACITY-001-PREREGISTRATION.md",
        },
        "interval_method": {
            "name": "circular moving-block bootstrap on per-bin pool totals",
            "one_sided_lower_percentile": alpha,
            "block_seconds": block_seconds,
            "draws": draws,
            "seed": seed,
            "why_not_poisson": (
                "per-market rates are not independent within an event, so a "
                "sum of independent per-market Poisson intervals would "
                "understate the variance of the sum. Resampling the POOL TOTAL "
                "per bin carries the cross-market dependence without modelling "
                "it; resampling contiguous blocks carries serial dependence."),
            "what_it_does_not_cover": (
                "variation BETWEEN this window and a four-hour session. This is "
                "a within-window sampling interval, not a four-hour guarantee."),
        },
        "incomplete_bins_dropped": dropped_incomplete,
        "arms": arms,
        "per_market": per_market,
        "frames_attributed_to_non_pool_tickers": unexpected,
        "one_off_frames_in_window": per_market_counts.get(
            "__one_off_frames_in_window__", 0),
        "session_result": probe.get("session_result"),
        "verdict": verdict,
        "verdict_computed_on": "all_archived_frames",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("probe_json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--block-seconds", type=float, default=60.0)
    parser.add_argument("--draws", type=int, default=20_000)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=20260817)
    args = parser.parse_args()

    probe = json.loads(Path(args.probe_json).read_text())
    result = analyse(probe, block_seconds=args.block_seconds, draws=args.draws,
                     alpha=args.alpha, seed=args.seed)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "run_label": result["run_label"],
        "verdict": result["verdict"],
        "arms": {k: {kk: v[kk] for kk in (
            "frames_observed", "observed_seconds",
            "lambda_sum_point_per_second",
            "lambda_sum_lower_bound_per_second",
            "N_4h_point", "N_4h_lower_bound")}
            for k, v in result["arms"].items()},
    }, indent=2))


if __name__ == "__main__":
    main()
