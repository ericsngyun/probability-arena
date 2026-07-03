# ADR-004: Calibration before EV / paper trading

**Status:** Accepted (MVP-004C/D)

## Context
EV math is trivial; trustworthy probabilities are not. Computing EV from uncalibrated forecasts creates confident-looking numbers with no evidential basis — the most dangerous artifact an agent system can produce.

## Decision
The template baseline forecaster is deliberately anchored to the market midpoint, so its accumulated Brier/log-loss approximates the market's own calibration. That accumulating dataset (baseline runner, every 4 h) is the bar: no EV design work (MVP-005A) until a challenger forecaster demonstrably beats the baseline on resolved outcomes over a meaningful sample, and no paper trading (MVP-005B) until the EV design is explicitly accepted. Forecast rows carry forecaster identity and calibration tags precisely so these cohort comparisons are possible.

## Consequences
- "Does the model add edge?" becomes an empirical query (`calibration-report`, `by_forecaster`), not an opinion.
- Weeks of patience are a designed-in cost; flipping flags early only accelerates data collection, never stakes.
- If challengers never beat the baseline, the correct outcome is improving models — not lowering the gate.
