# FEATURE_FLAGS

All defaults live in `app/config.py` / `.env.example`; deployed values in the host `.env`.
Rollout discipline: one flag at a time, per `docs/EVO_X2_RUNBOOK.md`.

## Model / external-data flags (all default **false**)

| Flag | Gates | Notes |
|---|---|---|
| `ENABLE_LLM_RESOLUTION` | `LLMResolutionJudge` for resolution assessments | Falls back to rule-based on any failure; model `RESOLUTION_MODEL_NAME` |
| `ENABLE_EXTERNAL_RESEARCH` | `LLMWebResearchCollector` (Claude + web search) — **global**, currently unused in favor of narrow canaries | Template fallback on failure |
| `ENABLE_BASEBALL_EXTERNAL_RESEARCH` | Baseball canary collector (MLB Stats API) for **promoted sports_baseball signals with researchable resolution only** | Honest template fallback; provenance persisted; `BASEBALL_RESEARCH_*` knobs |
| `ENABLE_SOCCER_EXTERNAL_RESEARCH` | Soccer canary collector for **promoted sports_soccer signals with researchable resolution only** | Live source gated by `SOCCER_RESEARCH_PROVIDER` (`template` = honest fallback even when flag on; `espn` = public ESPN soccer API); `SOCCER_RESEARCH_TIMEOUT_SECONDS=15`, `SOCCER_RESEARCH_MAX_SOURCES=8`, `SOCCER_RESEARCH_COLLECTOR_VERSION=v1` |
| `ENABLE_LLM_FORECASTING` | `LLMForecaster` | Central confidence caps still apply; template fallback |
| `ENABLE_BASEBALL_EVIDENCE_FORECASTING` | `BaseballEvidenceAwareForecaster` for source-backed baseball packets (completeness ≥ `BASEBALL_FORECAST_MIN_COMPLETENESS`) | No external calls; capped ±0.25 shift; `BASEBALL_FORECAST_MAX_CONFIDENCE` |

## Operational flags

| Flag | Default | Gates |
|---|---|---|
| `ENABLE_REALTIME_WATCHER` | false (true on EVO-X2 since OPS-003) | `watch-loop` refuses to start without it; `watch-once` always available |
| `ENABLE_WATCHER_RETENTION` | false | Watcher loop prunes at most once/day (never per-iteration) |
| `ENABLE_PIPELINE_RETENTION` | false | Appends a `retention` stage to baseline runs |

## Watcher tuning

`WATCHER_POLL_INTERVAL_SECONDS=60`, `WATCHER_MARKET_LIMIT=100`,
`WATCHER_PRICE_MOVE_THRESHOLD=0.07`, `WATCHER_MAX_SPREAD=0.15`,
`WATCHER_MIN_LIQUIDITY_PROXY=100`, `WATCHER_SIGNAL_COOLDOWN_SECONDS=900`.

## Baseline runner

`BASELINE_SCAN_LIMIT=500`, `BASELINE_CANDIDATE_LIMIT=20`, `BASELINE_FAIL_FAST=false`,
`BASELINE_SYNC_OUTCOME_LIMIT=200`, `BASELINE_SCORE_LIMIT=1000`.

## Retention windows

`TICK_RETENTION_DAYS=7`, `WATCHER_RUN_RETENTION_DAYS=30`,
`PIPELINE_RUN_RETENTION_DAYS=90`, `SIGNAL_RETENTION_DAYS=0` (keep forever),
`RETENTION_BATCH_SIZE=5000`. Intelligence/calibration tables are never pruned.

## Related non-flag knobs

Eligibility gate thresholds (`REQUIRE_TWO_SIDED_QUOTE`, `MIN_LIQUIDITY`, …),
forecast confidence caps (`TEMPLATE_ONLY_MAX_CONFIDENCE=0.55`,
`SOURCE_BACKED_MAX_CONFIDENCE=0.75`, `MISSING_CRITICAL_INFO_MAX_CONFIDENCE=0.50`),
`MIN_CLARITY_SCORE=0.70` — see `.env.example` for the complete annotated set.
