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
| `ENABLE_SOCCER_EVIDENCE_FORECASTING` | `SoccerEvidenceAwareForecaster` for source-backed soccer packets (completeness ≥ `SOCCER_FORECAST_MIN_COMPLETENESS=0.75`) | No external calls; goal-margin/pace model; red cards reduce confidence, never boost estimates; shootouts capped at 0.50 except team-to-advance; player-goal markets fall back; capped ±0.25 shift; `SOCCER_FORECAST_MAX_CONFIDENCE=0.70`; forecasts are measurement inputs only |

## Operational flags

| Flag | Default | Gates |
|---|---|---|
| `ENABLE_REALTIME_WATCHER` | false (true on EVO-X2 since OPS-003) | `watch-loop` refuses to start without it; `watch-once` always available |
| `ENABLE_WATCHER_RETENTION` | false | Watcher loop prunes at most once/day (never per-iteration) |
| `ENABLE_PIPELINE_RETENTION` | false | Appends a `retention` stage to baseline runs |
| `ENABLE_CRYPTO_SCOUT` | false | Reserved for crypto loop/timer use (none exists in CRYPTO-001); manual `crypto-scan-once` is always allowed |
| `ENABLE_CRYPTO_RISK_PROVIDER` | false | Token risk assessments + risk signals (holder_risk/rug_risk/suspicious_supply_control); provider `CRYPTO_RISK_PROVIDER=mock` is the only CRYPTO-001 implementation |
| `ENABLE_HELIUS` | false | **Reserved only** — no Helius adapter exists in CRYPTO-001 |
| `ENABLE_MARKETOPS_AUTOPILOT` | false | The `marketops-loop` / timer only; `marketops-run-once` is always allowed manually. Read-only coordination — cannot trade, paper trade, calculate EV, or move money |

## MarketOps Autopilot tuning (OPS-006)

`MARKETOPS_PROMOTE_LIMIT=5`, `MARKETOPS_PROCESS_LIMIT=5`,
`MARKETOPS_CRYPTO_SCAN_LIMIT=100`, `MARKETOPS_SYNC_OUTCOME_LIMIT=500`,
`MARKETOPS_SCORE_LIMIT=1000`, `MARKETOPS_MIN_SIGNAL_AGE_SECONDS=30`,
`MARKETOPS_MAX_SIGNAL_AGE_HOURS=24`, `MARKETOPS_INCLUDE_CRYPTO=true`,
`MARKETOPS_INCLUDE_PROBABILITY_MARKETS=true`, `MARKETOPS_FAIL_FAST=false`,
`MARKETOPS_LOOP_INTERVAL_SECONDS=300`,
`MARKETOPS_LOCK_STALE_AFTER_MINUTES=30` (a 'running' cycle older than this is
treated as crashed and no longer blocks new cycles).

## Edge precheck (MVP-005A — probability-gap MEASUREMENT; never advice)

| Flag | Default | Gates |
|---|---|---|
| `ENABLE_EDGE_PRECHECK` | false | `edge-precheck` CLI (without `--force-readonly`), `POST /edge-precheck/run` (without `force_readonly=true`), and the MarketOps stage |
| `MARKETOPS_INCLUDE_EDGE_PRECHECK` | false | The MarketOps stage additionally requires `ENABLE_EDGE_PRECHECK=true` (double-gated) |

Provisional thresholds (design §6): `EDGE_PRECHECK_MIN_ABS_GAP=0.05`,
`EDGE_PRECHECK_MAX_SPREAD_CENTS=10`, `EDGE_PRECHECK_MIN_LIQUIDITY_CENTS=500`,
`EDGE_PRECHECK_MIN_CONFIDENCE=0.60`,
`EDGE_PRECHECK_MAX_FORECAST_AGE_SECONDS=900` (300 for live sports via
`EDGE_PRECHECK_MAX_LIVE_SPORTS_FORECAST_AGE_SECONDS`),
`EDGE_PRECHECK_MAX_MARKET_SNAPSHOT_AGE_SECONDS=120`,
`EDGE_PRECHECK_REQUIRE_SOURCE_BACKED=true`,
`EDGE_PRECHECK_REQUIRE_RESEARCHABLE=true`,
`EDGE_PRECHECK_REQUIRED_PERSISTENCE_SNAPSHOTS=3`,
`EDGE_PRECHECK_DEDUPE_SECONDS=120` (targeted modes skip forecasts measured
within this window), `EDGE_PRECHECK_TARGET_ONLY_SOURCE_BACKED=true`
(window/signal-based targeting selects source-backed forecasts only).
No dollar EV, no advice — `paper_candidate_later` is a review label with zero
attached behavior. Broad `--limit` sweeps are manual diagnostics; targeted
modes (`--latest-marketops-run` etc.) are what automation uses, and the
MarketOps stage is strictly cycle-scoped.

## Operational hardening (OPS-007)

`SQLITE_BUSY_TIMEOUT_MS=30000` (SQLite connections wait for write locks
instead of failing; Postgres unaffected), `BACKUP_RETENTION_DAYS=30`,
`BACKUP_DIR=data/backups` (relative paths anchor next to the data directory).

## Crypto risk engine (CRYPTO-002 — risk intelligence, never trade advice)

| Flag | Default | Gates |
|---|---|---|
| `ENABLE_CRYPTO_RISK_ENGINE` | false | Risk engine during crypto scans (heuristics + enabled providers); `crypto-risk-assess` works manually regardless |
| `ENABLE_GOPLUS_RISK` | false | GoPlus Solana Token Security adapter (`GOPLUS_API_KEY` optional, header-only, never printed) |
| `ENABLE_SOLANA_TRACKER_RISK` | false | SolanaTracker risk adapter (`SOLANA_TRACKER_API_KEY` optional, header-only, never printed) |
| `ENABLE_RUGCHECK_RISK` | false | **Reserved only** — no RugCheck adapter exists in CRYPTO-002 |

Thresholds: `CRYPTO_RISK_MIN_LIQUIDITY_USD=5000`, `CRYPTO_RISK_MAX_TOP_HOLDER_PCT=20`,
`CRYPTO_RISK_MAX_SNIPER_PCT=20`, `CRYPTO_RISK_MAX_INSIDER_PCT=15`,
`CRYPTO_RISK_MAX_BUNDLER_PCT=25`, `CRYPTO_RISK_MIN_PAIR_AGE_SECONDS=300`,
`CRYPTO_RISK_PROVIDER_TIMEOUT_SECONDS=10`, `CRYPTO_RISK_ENGINE_VERSION=v1`.
A risk level is an avoid/flag verdict for review — never a trade direction.

## Crypto Arena tuning (CRYPTO-001 — read-only surveillance; no wallets/swaps/execution)

`CRYPTO_CHAIN=solana`, `CRYPTO_PROVIDER=dexscreener`,
`CRYPTO_WATCHER_POLL_INTERVAL_SECONDS=60`, `CRYPTO_PAIR_LIMIT=100`,
`CRYPTO_MIN_LIQUIDITY_USD=5000`, `CRYPTO_MIN_VOLUME_5M_USD=1000`,
`CRYPTO_SIGNAL_COOLDOWN_SECONDS=900`, `CRYPTO_RETENTION_DAYS=7`
(crypto_price_ticks + crypto_watcher_runs only; tokens/pairs/events/risk/signals
are never pruned).

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
