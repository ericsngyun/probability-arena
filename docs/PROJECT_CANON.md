# PROJECT_CANON — Probability Arena system reference

Last updated: OPS-005 (post MVP-004F). Update alongside `app/canon.py` when milestones land.

## System overview

A read-only Kalshi market-intelligence system that measures its own forecasting quality. Everything external is a GET (Kalshi trade API, MLB Stats API); everything written goes to our own database. The template forecaster is midpoint-anchored (≈ the market's own calibration) and serves as the baseline every smarter forecaster must beat on Brier/log-loss before higher-stakes capabilities are considered.

## Current architecture

```
FastAPI app (app/main.py) — read-only API over the same services the CLI uses
CLI (app/cli.py)          — one command per operation; every command owns its session
Services (app/services/)  — scanner, eligibility, enrichment, resolution, research,
                            baseball_research, soccer_research, forecasting,
                            baseball_forecasting, soccer_forecasting, outcomes,
                            calibration, watcher,
                            signal_workflow, pipeline (baseline runner), retention,
                            crypto_scout + crypto_risk + crypto_risk_engine
                            (Crypto Arena, read-only; risk = avoid/flag verdicts),
                            polymarket (POLY-001: read-only SECOND-venue market-data
                            observer — catalog + order books + domain inventory),
                            cross_venue (POLY-002: read-only Kalshi<->Polymarket
                            semantic matching + observed-difference measurement,
                            never EV/arbitrage/trade),
                            provider_budget (PROVIDER-BUDGET-001: SolanaTracker request
                            accounting + budget guardrails, read-only observability),
                            meme_mas (MEME-MAS-001: read-only multi-agent memecoin
                            DIAGNOSTIC scoring → review_priority, never a trade signal),
                            meme_shadow (MEME-SHADOW-001: read-only follow-through /
                            calibration of review_priority labels — market-movement
                            measurement, not PnL),
                            marketops (Autopilot: read-only coordination + alerts),
                            edge_precheck (MVP-005A: gap measurement, never advice),
                            frontier_eval (EVAL-001: desk-wide evaluation + readiness),
                            db_growth (OPS-011: read-only storage/retention observability),
                            tick_aggregation (OPS-012: raw ticks -> OHLC bucket
                            summaries — storage plumbing, never trading signals)
Adapters (app/adapters/) — kalshi.py (list/detail/event/series/by-tickers/by-series GETs,
                            legacy + dollars/fp payload shapes, outcome parsing, bounded
                            429 retries), dexscreener.py (crypto, read-only), polymarket.py
                            (POLY-001: public/no-auth Gamma catalog + CLOB read-only books)
DB: SQLAlchemy + Alembic (rev 0021) — SQLite on EVO-X2, Postgres-ready (JSONB variants)
```

## Pipeline stages (baseline runner order)

scan *(generic first-N + targeted supported series, deduped — SCANNER-002)* → *(eligibility gate inside scan)* → enrich_details → assess_resolution → collect_research → forecast → sync_outcomes → score_forecasts → calibration_report *(+ optional retention stage)*

Parallel to that: watcher (60s ticks + signals; universe = top-scored candidates of the latest scan **plus a bounded supported-universe supplement** — game-level baseball/soccer markets with two-sided quotes, even at score 0, never props — SCANNER-002) → promote-signals → process-promoted-signals (fresh enrichment/assessment/packet/forecast per signal).

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
| market_price_tick_buckets | OPS-012 aggregated tick summaries (OHLC/spread/liquidity per fixed interval) — storage telemetry, never a trading signal |
| crypto_tokens, crypto_pairs, crypto_token_discovery_events, crypto_token_risk_assessments, crypto_price_ticks, crypto_opportunity_signals, crypto_watcher_runs | Crypto Arena read-only surveillance (CRYPTO-001) |
| polymarket_markets, polymarket_orderbook_snapshots, polymarket_scout_runs, polymarket_domain_inventory_snapshots | Polymarket read-only market-data observer (POLY-001, second venue) |
| cross_venue_observation_runs, cross_venue_market_candidates | Kalshi<->Polymarket read-only cross-venue observation (POLY-002; measurement, never EV/arbitrage) |
| marketops_runs, marketops_alerts | MarketOps Autopilot coordination audit + local alerts (OPS-006) |
| edge_precheck_snapshots | probability-gap measurement audit (MVP-005A; no EV/side/size fields) |
| frontier_eval_runs | persisted evaluation runs (EVAL-001; evaluation audit only) |

## Current services / collectors / forecasters / judges

- Judges: `RuleBasedResolutionJudge` (default), `MockResolutionJudge`, `LLMResolutionJudge` (flag).
- Collectors: `TemplateResearchCollector` (default), `MockResearchCollector`, `LLMWebResearchCollector` (flag), `BaseballExternalResearchCollector` (canary flag; MLB Stats API), `SoccerExternalResearchCollector` (canary flag + provider; ESPN soccer API), `TennisExternalResearchCollector` (TENNIS-001 canary flag + provider; ESPN tennis API pending validation, template fallback default).
- Forecasters: `TemplateBaselineForecaster` (default; midpoint prior), `MockForecaster`, `LLMForecaster` (flag), `BaseballEvidenceAwareForecaster` (canary flag; capped ±0.25 shift), `SoccerEvidenceAwareForecaster` (SOCCER-002 canary flag; goal-margin/pace model, red-card/penalty-aware, capped ±0.25 shift), `TennisEvidenceAwareForecaster` (TENNIS-001 canary flag; match-winner only, set-margin model, tightly-capped ±0.20 shift, conf cap 0.65).
- Central guarantees regardless of provider: evidence-depth recomputation, confidence caps (template_only 0.55 / source_backed 0.75 / critical-missing 0.50), avoid→high-risk forcing.

## Feature flags

See `docs/FEATURE_FLAGS.md`. All model/external flags default **false**; deployed EVO-X2 values live in its `.env` (watcher enabled there since OPS-003).

## Latest accepted milestones

MVP-001…005A.1, OPS-001…007, OPS-009, SOCCER-001…002, CRYPTO-001…002, EVAL-001, and SCANNER-002/OPS-010 — full list with commits in `docs/ROADMAP.md`. Tests at SCANNER-002: 647 passing, 2 gated live tests skipped by default.

## Current known limitations

- Template forecasts carry no independent edge by construction (midpoint prior).
- Baseball evidence model v1 is naive (league-average pace, no simulations, assumed ticker-line semantics — stated in every forecast's skeptic notes).
- Market-type support: totals/spreads/game-winner only; player props fall back to template.
- Calibration cohorts are still small; resolved-outcome sample accumulates via the 4h baseline timer.
- Generic scan order is the API's default paging; targeted series scans (SCANNER-002) cover supported game-level families, but unsupported domains can still be crowded out of the first page.
- EVO-X2 deployment lags main when milestones haven't been rolled out yet — always check the runbook/host before assuming.
- SQLite on EVO-X2 (deliberate); Postgres migration path documented in the deployment report.
- `market_price_ticks` is the dominant growth driver (SCANNER-002's 150-ticker universe); `db-growth-report` measures it and OPS-011 calibrated the DB-growth/signal-flood alerts to the larger steady state. Tick aggregation (hourly OHLC + shorter raw retention) is a documented future milestone in `docs/ROADMAP.md`.
