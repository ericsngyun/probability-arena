# PROJECT_CANON — Probability Arena system reference

Last updated: OPS-005 (post MVP-004F). Update alongside `app/canon.py` when milestones land.

## System overview

A read-only Kalshi market-intelligence system that measures its own forecasting quality. Everything external is a GET (Kalshi trade API, MLB Stats API); everything written goes to our own database. The template forecaster is midpoint-anchored (≈ the market's own calibration) and serves as the baseline every smarter forecaster must beat on Brier/log-loss before higher-stakes capabilities are considered.

## Current architecture

```
FastAPI app (app/main.py) — read-only API over the same services the CLI uses
CLI (app/cli.py)          — one command per operation; every command owns its session
Services (app/services/)  — scanner, eligibility, enrichment, resolution, research,
                            baseball_research, forecasting, baseball_forecasting,
                            outcomes, calibration, watcher, signal_workflow,
                            pipeline (baseline runner), retention
Adapter (app/adapters/kalshi.py) — list/detail/event/series/by-tickers GETs,
                            legacy + dollars/fp payload shapes, outcome parsing
DB: SQLAlchemy + Alembic (rev 0013) — SQLite on EVO-X2, Postgres-ready (JSONB variants)
```

## Pipeline stages (baseline runner order)

scan → *(eligibility gate inside scan)* → enrich_details → assess_resolution → collect_research → forecast → sync_outcomes → score_forecasts → calibration_report *(+ optional retention stage)*

Parallel to that: watcher (60s ticks + signals) → promote-signals → process-promoted-signals (fresh enrichment/assessment/packet/forecast per signal).

## Key tables (15 + alembic_version)

| Table | Role |
|---|---|
| markets, market_snapshots, orderbook_snapshots, scanner_runs | scan universe + quotes + audit |
| market_eligibility_assessments | deterministic gate audit |
| market_detail_enrichments | detail/event/series metadata + settlement sources |
| market_resolution_assessments | clarity/tradeability verdicts |
| market_research_packets | evidence packets (collector identity, facts, sources, gaps) |
| market_forecasts | probability forecasts (forecaster identity, reasoning, tags) |
| market_outcomes, forecast_scores | settlement truth + Brier/log-loss (append-only) |
| pipeline_runs, pipeline_stage_runs | baseline runner audit + overlap lock |
| market_price_ticks, opportunity_signals, watcher_runs | watcher telemetry + signal workflow |

## Current services / collectors / forecasters / judges

- Judges: `RuleBasedResolutionJudge` (default), `MockResolutionJudge`, `LLMResolutionJudge` (flag).
- Collectors: `TemplateResearchCollector` (default), `MockResearchCollector`, `LLMWebResearchCollector` (flag), `BaseballExternalResearchCollector` (canary flag; MLB Stats API).
- Forecasters: `TemplateBaselineForecaster` (default; midpoint prior), `MockForecaster`, `LLMForecaster` (flag), `BaseballEvidenceAwareForecaster` (canary flag; consumes source-backed packets, capped ±0.25 shift).
- Central guarantees regardless of provider: evidence-depth recomputation, confidence caps (template_only 0.55 / source_backed 0.75 / critical-missing 0.50), avoid→high-risk forcing.

## Feature flags

See `docs/FEATURE_FLAGS.md`. All model/external flags default **false**; deployed EVO-X2 values live in its `.env` (watcher enabled there since OPS-003).

## Latest accepted milestones

MVP-001…004F and OPS-001…004 — full list with commits in `docs/ROADMAP.md`. Tests at OPS-005: 312+ passing, 2 gated live tests skipped by default.

## Current known limitations

- Template forecasts carry no independent edge by construction (midpoint prior).
- Baseball evidence model v1 is naive (league-average pace, no simulations, assumed ticker-line semantics — stated in every forecast's skeptic notes).
- Market-type support: totals/spreads/game-winner only; player props fall back to template.
- Calibration cohorts are still small; resolved-outcome sample accumulates via the 4h baseline timer.
- EVO-X2 deployment lags main when milestones haven't been rolled out yet — always check the runbook/host before assuming.
- SQLite on EVO-X2 (deliberate); Postgres migration path documented in the deployment report.
