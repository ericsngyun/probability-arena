# ADR-005: Source-backed baseball canary before general LLM forecasting

**Status:** Accepted (MVP-004E/F)

## Context
Two paths existed for making forecasts smarter than the midpoint: enable the general LLM forecaster everywhere, or build a narrow, verifiable canary first. General LLM enablement is expensive, hard to attribute (was the edge from evidence or eloquence?), and hits every domain at once.

## Decision
Start with the narrowest defensible slice: baseball, because Kalshi MLB tickers resolve deterministically to official structured data (MLB Stats API — live score/inning/outs/lineups/weather, free, read-only, no key). MVP-004E turns template packets into source-backed packets with persisted provenance; MVP-004F consumes them with a deterministic, capped, fully-explained model. Both sit behind their own flags, fall back honestly, and are tagged (`baseball_evidence_v1`, `market_type_*`) so calibration can attribute results. General LLM forecasting stays off until this canary's calibration story is read.

## Consequences
- Evidence quality and forecasting quality are separately measurable (collector metrics vs forecaster cohorts).
- The pattern (official structured source → evidence extraction → capped deterministic model) is the template for the next domains (tennis/soccer have equivalent official feeds) before any general LLM rollout.
- LLM forecasting, when it comes, must beat not just the market baseline but this cheap deterministic canary.
