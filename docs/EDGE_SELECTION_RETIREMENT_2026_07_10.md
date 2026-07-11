# EDGE-RETIRE-001 — Retirement of the EDGE-SELECTION-001 candidate policies

**Status: RETIRED (all six candidates), per prereg §6, on out-of-sample
evidence.** Lock: 2026-07-09T19:00:00Z. Decisive validation window measured:
2026-07-11 07:15 UTC (n=297 pure post-lock rows). This document is the
experiment-registry record — documentation only. It changes no live gate,
forecast, promotion, flag, MarketOps/EDGE-AUTO behavior, and it authorizes
nothing. **MVP-005B remains blocked.**

## 1. What was tested

Six candidate row-selection policies (plus baseline and a negative control),
frozen in `docs/EDGE_SELECTION_PREREG_2026_07_09.md` BEFORE any window that
could validate them. They were the best performers of an 18-policy search on
the discovery windows (EDGE-FILTER-001), after TRIGGER-TIMING-001 established
that selection — not measurement timing — was the plausible mechanism.

## 2. The verdict (discovery vs out-of-sample, 60m toward / mean closure)

| Policy | Discovery (in-sample, 48h 2026-07-09) | Validation (out-of-sample, n=297) | Protocol status |
|---|---|---|---|
| require_gap_follows_move_totals_only (primary) | 0.539 / **+0.42** | **0.286 / −1.22** | failing_gates → **RETIRED** |
| require_gap_follows_move_exclude_spreads | 0.459 / +0.26 | 0.261 / −1.52 | failing_gates → **RETIRED** |
| gap_follows_move_and_high_liquidity | 0.483 / +0.35 | 0.220 / −1.74 | failing_gates → **RETIRED** |
| gap_follows_move_and_tight_spread | 0.404 / +0.24 | 0.232 / −1.38 | failing_gates → **RETIRED** |
| totals_only (`total_only`) | 0.380 / −0.11 | 0.349 / −0.06 | failing_gates → **RETIRED** |
| exclude_spreads (`exclude_spread_markets`) | 0.335 / −0.22 | 0.337 / −0.25 | failing_gates → **RETIRED** |
| baseline_all_watchlist | 0.285 / −0.33 | 0.349 / −0.06 | reference |
| spread_only (negative control) | 0.204 / −0.51 | **0.375 / +0.33** | control_consistent (see §4) |

## 3. Cost-adjusted confirmation

COST-MODEL-001 independently killed the same cohorts BEFORE the validation
failure: at conservative friction (half-spread + round-trip Kalshi fee +
executable touch), every positive-frictionless cohort was `cost_killed`
(`cohorts_positive_after_costs: NONE` on both 24h and 48h windows,
2026-07-10). The out-of-sample window agrees: all pre-registered cohorts
remain `cost_killed`. So the candidates failed on BOTH axes — direction
(out-of-sample toward/closure) and economics (friction).

## 4. The negative-control inversion

`spread_only` — pre-registered as the ADVERSE control (discovery toward
0.204, closure −0.51) — posted toward 0.375 / closure **+0.33** on the
validation window, outperforming every candidate. Under the prereg's letter
it remains `control_consistent` (toward < 0.50), but the inversion is the
loudest overfitting signal in the record: cohort performance across windows
is dominated by regime noise, not by the policies' hypothesized mechanism.
Per protocol, a non-adverse control also means candidate results in such
windows deserve suspicion in BOTH directions — including any future window
where a retired policy happens to look good again.

## 5. Conclusion

**The policy search overfit.** One fleeting 48h window printed
`blocked: False` (2026-07-09 ~17:45 UTC) and regressed within hours; the
locked out-of-sample protocol then falsified all six candidates on their
first substantial validation window. The apparent frictionless shadow edge
was selection noise amplified by an 18-policy search, and it was
additionally uneconomic at realistic friction.

## 6. Standing rules from this retirement

1. **No retired policy is eligible for any live gate, paper-trading
   discussion, or MVP-005B step.** MVP-005B remains blocked.
2. **Resurrection requires a NEW pre-registration document with a NEW lock**
   and fresh out-of-sample windows — prior data cannot be reused, and a
   retired policy looking good on some future window is NOT evidence without
   a new lock (see §4).
3. Any successor hypothesis should be **mechanism-first, not search-first**:
   pre-registered from a causal story (e.g. the tennis score-to-market
   latency thesis under measurement in the TENNIS-* lane), with cost-adjusted
   gates (COST-MODEL-001) included from day one.
4. The daily edge-observation snapshot continues to record the retired
   policies' out-of-sample behavior for the registry — observation only.
