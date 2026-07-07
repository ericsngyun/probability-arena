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
| `ENABLE_TENNIS_EXTERNAL_RESEARCH` | Tennis canary collector (TENNIS-001) for **promoted sports_tennis MATCH-WINNER signals with researchable resolution only** | Live source gated by `TENNIS_RESEARCH_PROVIDER` (`template` = honest fallback even when flag on; `espn` = public ESPN tennis API, **payload mapping PENDING validation** — degrades to fallback if the shape differs); `TENNIS_RESEARCH_TIMEOUT_SECONDS=15`, `TENNIS_RESEARCH_MAX_SOURCES=8`, `TENNIS_RESEARCH_COLLECTOR_VERSION=v1`; non-winner/prop/unparseable tickers fall back honestly |
| `ENABLE_TENNIS_EVIDENCE_FORECASTING` | `TennisEvidenceAwareForecaster` for source-backed tennis packets (completeness ≥ `TENNIS_FORECAST_MIN_COMPLETENESS=0.75`) | No external calls; match-winner only in v1; midpoint prior; set/game-margin model weighted by match progress; retirement/walkover resolves near-certain; **tightly capped ±0.20 shift**; missing critical facts cap confidence at 0.50 + high risk; `TENNIS_FORECAST_MAX_CONFIDENCE=0.65`; forecasts are measurement inputs only |

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

OPS-009 minute-level, domain-aware promotion freshness (minutes supersede the
hour knob, which survives as a coarse upper bound —
`min(domain_minutes, hours*60)`): `MARKETOPS_MAX_SIGNAL_AGE_MINUTES=60`,
`MARKETOPS_LIVE_SPORTS_MAX_SIGNAL_AGE_MINUTES=20`,
`MARKETOPS_SOCCER_MAX_SIGNAL_AGE_MINUTES=20`,
`MARKETOPS_BASEBALL_MAX_SIGNAL_AGE_MINUTES=20`,
`MARKETOPS_GENERAL_MAX_SIGNAL_AGE_MINUTES=60`
(`MARKETOPS_CRYPTO_SIGNAL_AGE_MINUTES` is reserved/unused). Promotion is
ordered by a deterministic **measurement-readiness score** (freshness,
source-backed capability, market-type measurability — player props lowest,
signal-type priority, live book quality). The score orders promotion only;
it is never an EV/value/trade quantity.

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

## Frontier evaluation (EVAL-001)

No flags — `frontier-eval-report` (CLI) and `GET /eval/frontier-report` are
always-available read-only evaluation over persisted data. `--save-run`
persists a frontier_eval_runs audit row. Readiness labels gate further
measurement milestones only and never authorize live capital.

## Operational hardening (OPS-007)

`SQLITE_BUSY_TIMEOUT_MS=30000` (SQLite connections wait for write locks
instead of failing; Postgres unaffected), `BACKUP_RETENTION_DAYS=30`,
`BACKUP_DIR=data/backups` (relative paths anchor next to the data directory).

## Crypto risk engine (CRYPTO-002 — risk intelligence, never trade advice)

| Flag | Default | Gates |
|---|---|---|
| `ENABLE_CRYPTO_RISK_ENGINE` | false | Risk engine during crypto scans (heuristics + enabled providers); `crypto-risk-assess` works manually regardless |
| `ENABLE_GOPLUS_RISK` | false | GoPlus Solana Token Security adapter (`GOPLUS_API_KEY` optional, header-only, never printed) |
| `ENABLE_SOLANA_TRACKER_RISK` | false | SolanaTracker risk adapter — full holder set: sniper/insider/bundler/top10 (`SOLANA_TRACKER_API_KEY` optional, header-only, never printed) |
| `ENABLE_BIRDEYE_RISK` | false | **MEME-RISK-003** Birdeye holder + creator/deployer-concentration adapter (`BIRDEYE_API_KEY` optional, header-only `X-API-KEY`, never printed; live payload mapping PENDING validation — degrades to honest absence if the shape differs) |
| `ENABLE_RUGCHECK_RISK` / `ENABLE_HELIUS` | false | **Reserved only** — no adapter exists yet |

Thresholds: `CRYPTO_RISK_MIN_LIQUIDITY_USD=5000`, `CRYPTO_RISK_MAX_TOP_HOLDER_PCT=20`,
`CRYPTO_RISK_MAX_SNIPER_PCT=20`, `CRYPTO_RISK_MAX_INSIDER_PCT=15`,
`CRYPTO_RISK_MAX_BUNDLER_PCT=25`, `CRYPTO_RISK_MAX_CREATOR_PCT=15` (MEME-RISK-003),
`CRYPTO_RISK_MIN_PAIR_AGE_SECONDS=300`, `CRYPTO_RISK_PROVIDER_TIMEOUT_SECONDS=10`,
`CRYPTO_RISK_ENGINE_VERSION=v1`. A risk level is an avoid/flag verdict for review
— never a trade direction. Provider coverage is explicit via
`crypto-provider-health-report` / `meme-risk-coverage-report` (gaps are stated,
not silent).

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

## Targeted game-level market scans (SCANNER-002/OPS-010 — coverage only, never advice)

| Flag | Default | Gates |
|---|---|---|
| `ENABLE_TARGETED_MARKET_SCANS` | **true** | Per-series supplement to the generic scan (same read-only GET /markets, filtered by `series_ticker`); `false` restores the exact pre-SCANNER-002 scan |

Knobs: `TARGETED_MARKET_SERIES=KXWCGAME,KXWCTOTAL,KXWCSPREAD,KXMLBGAME,KXMLBTOTAL,KXMLBSPREAD`
(comma-separated; unknown series simply return empty), `TARGETED_MARKET_SCAN_LIMIT_PER_SERIES=250`,
`TARGETED_MARKET_SCAN_ACTIVE_ONLY=true` (status=open), `TARGETED_MARKET_SCAN_DEDUP=true`
(drop targeted tickers already in the generic page). Per-series failures (incl. bounded-429
exhaustion) are recorded in scan output and never fail the scan. Targeted markets pass the
same eligibility gate and ranking as everything else — nothing is forced into candidates.

`WATCHER_SUPPORTED_UNIVERSE_LIMIT=50` bounds the watcher's supported-universe supplement:
game-level baseball/soccer markets (spread/total/winner/advance) from the latest scan with a
two-sided quote and an unexpired close, included even at score 0 so live-window volume can be
observed as it appears. Player props never enter via the supplement. `0` disables it.
This is scanner/watcher **coverage**: it calculates no EV, recommends no trades, does no paper
trading, sizes no positions, places no orders, and touches no wallets/keys/swaps/execution.

## Meme/news + domain scout (MEME-NEWS-001 — read-only discovery/scouting; no trade/EV/orders/wallets)

| Flag | Default | Effect |
|---|---|---|
| `ENABLE_MEME_SCOUT` | false | Reserved for a future meme-scout loop/timer; manual `meme-scan-once` / `meme-scout-report` / `catalyst-report` are always allowed |
| `ENABLE_DOMAIN_SCOUT` | false | Reserved for a future domain-scout loop/timer; manual `domain-scout-report` is always allowed |

Tuning: `MEME_SCOUT_LIMIT=30` (max tokens scored per pass), `MEME_SCOUT_VERSION=v1`,
`DOMAIN_SCOUT_VERSION=v1`. The meme scout reuses the crypto-lane DexScreener GETs
and `CRYPTO_CHAIN=solana`. `attention_score` is an interest/velocity signal for
human review — never a buy/trade/EV/alpha score. No new authenticated sources
are added; rss/x/discord/telegram catalyst sources remain unconfigured placeholders.

### MEME-NEWS-002 — scheduled discovery lane

| Flag | Default | Effect |
|---|---|---|
| `ENABLE_MEME_NEWS_SCOUT` | false | Gates the **scheduled** path only (`meme-news-run-once --scheduled` / the systemd timer no-op while false). Manual `meme-news-run-once` and all reports are always allowed |
| `MEME_NEWS_SEVERE_RISK_ALERT` | true | Emit a local `severe_risk` notable-event row for severe/high-risk tokens (avoid/flag verdict — never a trade direction) |

Tuning: `MEME_NEWS_SCOUT_INTERVAL_SECONDS=300` (informational; the systemd timer
governs cadence at 10 min), `MEME_NEWS_MAX_PROFILES_PER_RUN=30`,
`MEME_NEWS_MAX_BOOSTS_PER_RUN=30`, `MEME_NEWS_RETENTION_DAYS=14`,
`MEME_NEWS_ATTENTION_ALERT_THRESHOLD=0.6`, `MEME_NEWS_ATTENTION_JUMP_THRESHOLD=0.15`.
Retention prunes `meme_scout_runs` / `meme_attention_snapshots` /
`meme_catalyst_events` after `MEME_NEWS_RETENTION_DAYS` to bound the always-on
lane (**documented** — the report/alerts use recent windows; the domain-scout
inventory tables are NOT pruned). Alerts are local, derived, informational — no
push notifications, no recommendations. Read-only discovery only.

## Polymarket market-data observer (POLY-001 — read-only second venue; no EV/orders/wallets)

| Flag | Default | Effect |
|---|---|---|
| `ENABLE_POLYMARKET_SCOUT` | false | Reserves loop/timer use (none installed in POLY-001); manual `polymarket-scan-once` and all reports are always allowed. A `--scheduled` run no-ops while false |

Tuning: `POLYMARKET_MARKET_LIMIT=50` (max markets fetched/persisted per scan),
`POLYMARKET_ORDERBOOK_LIMIT=20` (max token order books fetched per scan),
`POLYMARKET_TIMEOUT_SECONDS=15`, `POLYMARKET_RETENTION_DAYS=14`,
`POLYMARKET_PROVIDER_VERSION=v1`. Public/no-auth GETs only — the Gamma market
catalog (`gamma-api.polymarket.com/markets`) + the CLOB read-only order book
(`clob.polymarket.com/book`). No API key, wallet, or signing is used or
required; the authenticated CLOB trading endpoints are deliberately not
implemented. `polymarket-scan-once`/`polymarket-report`/`polymarket-domain-report`.
Retention prunes `polymarket_markets` / `polymarket_orderbook_snapshots` /
`polymarket_scout_runs` after `POLYMARKET_RETENTION_DAYS` (**documented** — the
`polymarket_domain_inventory_snapshots` coverage table is NOT pruned). Prices and
order books are informational quotes for human review — never EV, a
recommendation, an instruction, or a trade trigger. Cross-venue semantic linking
to Kalshi is a documented POLY-002 placeholder only (no arb/EV/trade-candidate
labels exist). Read-only market-data observation only.

## Retention windows

`TICK_RETENTION_DAYS=7`, `WATCHER_RUN_RETENTION_DAYS=30`,
`PIPELINE_RUN_RETENTION_DAYS=90`, `SIGNAL_RETENTION_DAYS=0` (keep forever),
`CRYPTO_RETENTION_DAYS=7` (crypto_price_ticks + crypto_watcher_runs only),
`RETENTION_BATCH_SIZE=5000`. Intelligence/calibration tables are never pruned
(`edge_precheck_snapshots` is audit history — kept forever). `market_price_ticks`
is the dominant growth driver; `prune-retention --dry-run` prints an OPS-011
per-table projection (window, total, eligible, remaining, oldest/newest ticks).

## OPS-011 — DB growth & alert calibration (ops/observability only)

Advisory operational alerts, not trading logic. Static thresholds were raised
after SCANNER-002 expanded the watcher/tick universe (the old 512 MiB / 150
signals-per-hour tripped on normal live-slate volume). Both alert types now
have configurable warning **and** critical tiers:

| Setting | Default | Meaning |
|---|---|---|
| `DB_GROWTH_WARNING_MB` | 1536 | `db_growth_warning` alert (severity warning) at/above this file size |
| `DB_GROWTH_CRITICAL_MB` | 3072 | same alert at severity critical (disk-pressure risk) |
| `DB_GROWTH_WARNING_DAILY_MB` | 1024 | observability/proposed — surfaced by `db-growth-report` as an estimated raw-tick MiB/day rate |
| `DB_GROWTH_WINDOW_HOURS` | 24 | observability/proposed — rate window |
| `MARKETOPS_SIGNAL_FLOOD_WARNING_PER_HOUR` | 400 | `too_many_signals` at severity warning (busy live slates are ~200/h — normal) |
| `MARKETOPS_SIGNAL_FLOOD_CRITICAL_PER_HOUR` | 800 | same alert at severity critical (watcher looping / mis-dedup) |

`db-growth-report` (read-only CLI) reports DB size, per-table row counts + est
MiB (via SQLite `dbstat` when compiled in), largest tables, tick age buckets,
ticks-by-domain, edge-precheck/crypto row growth, backups, retention windows,
and these thresholds. Rate-based *alerting* (vs the current absolute-size gates)
is documented as future work — see `docs/ROADMAP.md`. OPS-011 changes no
forecasting, edge, or promotion logic and adds no EV/paper-trading/sizing/
orders/wallets/swaps/execution.

## Related non-flag knobs

Eligibility gate thresholds (`REQUIRE_TWO_SIDED_QUOTE`, `MIN_LIQUIDITY`, …),
forecast confidence caps (`TEMPLATE_ONLY_MAX_CONFIDENCE=0.55`,
`SOURCE_BACKED_MAX_CONFIDENCE=0.75`, `MISSING_CRITICAL_INFO_MAX_CONFIDENCE=0.50`),
`MIN_CLARITY_SCORE=0.70` — see `.env.example` for the complete annotated set.
