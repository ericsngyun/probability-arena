# QDK-001 — measured ΔS: our forecasts vs the contemporaneous market

**Measured 2026-08-15 on EVO against production data. READ-ONLY.**

This is the number the whole QDK-001 research programme was circling, and it
was computable all along — see "why this was thought impossible" below.

## Result (STRICT, no lookahead — the headline)

Market quote required to **end strictly before** the forecast was made.

```
matched (source_backed, resolved, quote ending within 900s BEFORE forecast): 10,285
market Brier = 0.17276
our    Brier = 0.18975
ΔS           = -0.01700          (>0 would mean we beat the market)
clustered over 5,819 distinct markets:  mean -0.01747  SE 0.00118  t = -14.80
clustered 95% CI = [-0.01978, -0.01515]

baseball_evidence  n=10,053  ΔS=-0.01625  t=-15.12
soccer_evidence    n=   232  ΔS=-0.04925  t=-3.72
```

**The market beats our forecasts.** Both forecasters, decisively, with event
clustering and no lookahead.

A permissive variant (nearest bucket within ±300s, allowing the bucket's close
stamp to post-date the forecast) gives ΔS = −0.03182, t = −23.25. Lookahead
therefore accounts for ~45% of that gap; the sign and significance do not
depend on it.

## Why it matters

`g(f*) = KL(p‖q)` — maximum Kelly growth is exactly the log-score advantage
over the market price. Ours is **negative**, so Kelly betting these forecasts
loses money systematically **before any fees**, and Kalshi's taker fee peaks at
1.75¢/contract on top of that.

The repo's headline skill numbers (baseball +0.2286, soccer +0.2434) are
`brier_skill_vs_base_rate` (`app/services/forecast_reliability.py:234`). Beating
the base rate is not evidence of tradable edge; the market beats the base rate
by far more than we do.

## Why this was thought impossible, and was not

`MarketForecastRecord` has no market-price column, and the market-anchored
`template_baseline` rows pair with **zero** source-backed forecasts (verified
PAIRED = 0). Both facts are real. The missed asset is
**`MarketPriceTickBucket`** (`app/models.py:319`), which carries
`open_bid/close_bid/open_ask/close_ask` in 300 s buckets and **survived the
OPS-014 raw-tick pruning**: 1,615,867 buckets (1,425,660 two-sided) spanning
2026-07-06 → present, covering **89.1%** of source-backed forecasts within
±300 s. No schema change and no live tape writer were required.

## Honest limits

- Brier, not log score. The Kelly identity is stated in log score; Brier is also
  proper and the **sign** is what carries, but this is not literally KL(p‖q).
- Domain is sports (baseball/soccer) — the one domain the conditional-calibration
  literature reports as already well calibrated, i.e. the hardest to beat. This
  is **not** evidence that no edge exists in any domain.
- `q` is the bid/ask **midpoint**. A taker pays bid or ask, so real trading is
  worse than this comparison.
- Clusters are `market_ticker`. A single game spans several markets, so even the
  clustered SE is likely anticonservative. The margin (t ≈ −15) is large enough
  that this does not change the conclusion.
- Not a strategy backtest: every forecast was genuinely made before its outcome
  resolved, so this is prospective-in-origin and does not hit the
  LLM-contamination objection in `QDK-001-evaluation-methodology.md`.

## Reproduce

`delta_s_strict.py` (headline) and `delta_s.py` (permissive), read-only,
`mode=ro`, run against the production DB on EVO.
