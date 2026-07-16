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
| `ENABLE_TENNIS_EXTERNAL_RESEARCH` | Tennis canary collector (TENNIS-001) for **promoted sports_tennis MATCH-WINNER signals with researchable resolution only** | Live source gated by `TENNIS_RESEARCH_PROVIDER` (`template` = honest fallback even when flag on; `espn` = public ESPN tennis API — **measured insufficient for our Challenger/ITF universe, 0/176**; `api_tennis` = TENNIS-PROVIDER-001 scaffold, makes **no request unless `TENNIS_PROVIDER_API_KEY` is also set** — key lives on host .env only, never committed/echoed); `TENNIS_RESEARCH_TIMEOUT_SECONDS=15`, `TENNIS_RESEARCH_MAX_SOURCES=8`, `TENNIS_RESEARCH_COLLECTOR_VERSION=v1`; non-winner/prop/unparseable tickers fall back honestly |
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
| `MARKETOPS_INCLUDE_CANDIDATE_READINESS` | false | Gates the isolated, non-blocking, report-only post-crypto-stage candidate-readiness hook (CRYPTO-HORIZON-CANDIDATE-READINESS-001). Off = complete no-op; on = one append-only readiness evaluation per cycle, zero provider calls, cannot fail the cycle. The `crypto-horizon-candidate-readiness-report`/`-history-report` CLIs are always available read-only regardless of this flag |

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
| `ENABLE_SOLANA_TRACKER_RISK` | false | SolanaTracker risk adapter — full holder set from `/tokens/{address}`: **sniper/insider/bundler**/top10 (SOLANA-TRACKER-002: all four parsed from the risk object's `totalPercentage`, one request; `SOLANA_TRACKER_API_KEY` optional, header-only, never printed) |
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

### SolanaTracker request budget (PROVIDER-BUDGET-001 — cost/usage observability, never trading)

Not feature flags — **budget knobs** for the SolanaTracker Advanced plan
(**≈ $58–59/month USD** recurring data-provider OpEx, official ceiling **200,000
requests/month**). Usage is derived read-only from `crypto_token_risk_assessments`
(no new table). `crypto-provider-budget-report` shows plan/limit, requests
today/hour/month, estimated monthly run-rate, remaining daily/monthly budget,
success/error rate, coverage-per-request, and a keep/tune recommendation.

| Setting | Default | Meaning |
|---|---|---|
| `SOLANA_TRACKER_MONTHLY_REQUEST_LIMIT` | 200000 | official plan ceiling (reported; run-rate compared against it) |
| `SOLANA_TRACKER_DAILY_REQUEST_BUDGET` | 5000 | operational per-day target |
| `SOLANA_TRACKER_HOURLY_REQUEST_BUDGET` | 200 | operational per-hour target (reported) |
| `SOLANA_TRACKER_PER_RUN_LOOKUP_LIMIT` | 25 | max SolanaTracker lookups per scan run (hard per-run cap) |
| `SOLANA_TRACKER_CACHE_TTL_HOURS` | 24 | dedupe horizon (run-rate/report context) |
| `SOLANA_TRACKER_WARN_DAILY_REQUESTS` | 4000 | log + report a warning at/above this day total |
| `SOLANA_TRACKER_STOP_DAILY_REQUESTS` | 6000 | at/above this, the engine **skips** optional SolanaTracker lookups for the rest of the day |

The guardrail can only **skip** SolanaTracker (per-run cap or daily STOP) — a
skipped token falls back to GoPlus + heuristics (a fully supported mode). It
never adds calls, never changes **GoPlus/Birdeye** behavior, and carries no EV/
trade/sizing/order/wallet/signing/execution. Defaults are far above current
usage, so under normal load nothing is skipped; the STOP is a cost circuit
breaker. Operational targets: ≤150k/month, ≤5k/day, ≤200/hour, ≤20–30 per
10-minute window.

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

### TENNIS-WATCHER-001 — tennis tick capture (market observation only)

| Flag | Default | Effect |
|---|---|---|
| `ENABLE_TENNIS_TICK_WATCHER` | false | Gates the **scheduled** path only (`tennis-watch-scan-once --scheduled` no-ops while false; no timer artifact is installed by the milestone). Manual bounded `tennis-watch-scan-once` runs and `tennis-watch-report` are always allowed |

Tuning: `TENNIS_TICK_WATCH_LIMIT=200` (bounded targets per pass, match-winner
first). Ticks land in `market_price_ticks` with the existing raw-tick
retention window — no new table, no new retention knob. Observation only: no
signal detection, no forecasts, no trading semantics of any kind.

### TENNIS-GOALSERVE-001 — fallback live-state validation key

| Setting | Default | Effect |
|---|---|---|
| `GOALSERVE_TENNIS_API_KEY` | "" (empty) | Empty = the Goalserve probe makes **no request**. Set only on the host `.env` (never committed). Because Goalserve embeds the key in the URL path, request URLs are never logged/echoed; reports show a masked display URL only |

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

## Memecoin label follow-through (MEME-SHADOW-001 — read-only; no flag)

**No feature flag** — `meme-shadow-report` and `meme-mas-objectives-report` are
always-available read-only calibration reports. **MEME-MAS-003** adds the
multi-objective view (`meme-mas-objectives-report`): momentum_followthrough /
survival_quality / risk_adjusted_movement / review_queue_efficiency /
coverage_quality per review_priority, v1 vs v2 — measurement only, no label
change. It reconstructs MEME-MAS `review_priority` at historical
attention snapshots and measures each token's later trajectory (price/liq/vol at
5m/15m/1h/6h/24h, survival, rug incidence, attention persistence, risk
transition), aggregated by review_priority / sub-score / risk reason /
concentration with a conservative calibration recommendation. Market-movement
MEASUREMENT (like edge follow-through) — NOT PnL/EV/paper/recommendation/sizing/
orders. Compute-on-demand: no table/migration, no external call, no SolanaTracker
budget impact; changes no label and no MarketOps/EDGE-AUTO/MEME-NEWS/SolanaTracker/
Polymarket behavior.

## Memecoin diagnostic (MEME-MAS-001 — read-only; no flag)

**No feature flag** — `meme-mas-report` / `meme-mas-assess` /
`meme-mas-calibration-report` are always-available read-only diagnostic reports
(like `crypto-provider-budget-report`). Deterministic agents recompute sub-scores
on demand from persisted meme/risk rows into a `review_priority` (low/monitor/
elevated_review/high_review/reject_risk) plus `momentum_quality`/`structure_quality`/
`coverage_quality`. **MEME-MAS-002** recalibrated the scorer (profile `v2` default;
`v1` preserved for the before/after `meme-mas-calibration-report`): risk-aware,
heavier penalties for missing coverage + concentration, and a **gated high_review**
(clean structure + non-missing coverage + no concentration flags). No LLM, no external call, no
new provider, no table/migration, no SolanaTracker budget impact. `review_priority`
is NOT a trade recommendation/EV/sizing/order/buy/sell/bet; `reject_risk` is an
avoid/flag verdict for review. Changes no MarketOps/EDGE-AUTO/MEME-NEWS/
SolanaTracker/Polymarket behavior.

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
to Kalshi shipped in POLY-002 (comparability verdicts + measured probability-point
differences for human review; no arb/EV/trade-candidate labels exist). Read-only
market-data observation only.

## Polymarket coverage expansion (POLY-COVERAGE-001 — read-only; no EV/arb/orders/wallets)

No new feature flag. `ENABLE_POLYMARKET_SCOUT` **remains false** and still gates
only a future scheduled path — no timer is installed and every manual scan and
report stays allowed.

Tuning (all bounded; the adapter enforces hard ceilings regardless of the value):

| Setting | Default | Effect |
|---|---|---|
| `POLYMARKET_PAGE_SIZE` | 100 | catalog rows per page (Gamma caps a page at 100) |
| `POLYMARKET_MAX_PAGES` | 5 | catalog pages per scan (hard ceiling 20) |
| `POLYMARKET_SEARCH_LIMIT_PER_TYPE` | 20 | `/public-search` rows per page |
| `POLYMARKET_SEARCH_MAX_PAGES` | 3 | pages per search query (hard ceiling 5) |
| `POLYMARKET_MAX_TARGETED_QUERIES` | 6 | Kalshi-derived queries per scan |

`polymarket-scan-once` gains `--limit`, `--orderbook-limit`, `--category TAG_ID`,
`--active-only`/`--no-active-only`, `--include-closed`, `--query TEXT` (repeatable),
`--targeted`, `--end-date-min`, `--end-date-max`. `polymarket-coverage-report
[--top --kalshi-limit]` prints a per-domain/per-market-type SUPPLY census.

`--targeted` derives search queries deterministically from already-persisted
Kalshi ACTIVE market titles/ticker series (**no LLM**, no external taxonomy, no
new/paid provider). Targeted queries claim the market budget first and each gets
a fair share, so one high-yield topic cannot starve the others; skipped queries
and truncation are logged, never silent. Public/no-auth GETs only. Coverage
expansion widens WHICH markets are observed — it identifies no arbitrage, computes
no EV, recommends no trade, sizes nothing, places no order, uses no
wallets/keys/signing/swaps/execution, and forces no match.

## Cross-venue matcher precision (POLY-PRECISION-001 — read-only; no EV/arb/orders/wallets)

**No feature flag and no new setting.** The precision fixes are unconditional matcher
behavior; `cross-venue-match-once` / `cross-venue-report` / `cross-venue-candidates`
are unchanged in shape. No timer, no migration, no external call.

Behavioral thresholds live in `app/services/cross_venue.py`:
`LARGE_OBSERVED_DIFFERENCE=0.35` and `HIGH_SEMANTIC_CONFIDENCE=0.85` gate the
`large_observed_difference_requires_review` REVIEW annotation — a suspicion that the
MATCH is wrong, never an opportunity, edge, arbitrage, or action; a large gap alone
never rejects a pair. A Polymarket midpoint and any `observed_difference` exist only
when the outcome side has been aligned to the Kalshi YES proposition; otherwise both
are absent and the pair is annotated `outcome_side_uncertain` / `midpoint_side_uncertain`
and left `unresolved_semantic_match`. It identifies no arbitrage, computes no EV,
recommends no trade, paper trades nothing, sizes nothing, places no orders, and uses no
wallets/private keys/signing/swaps/execution.

## Cross-venue sample selection (XVENUE-OPS-001 — read-only usability; no EV/arb/orders/wallets)

**No feature flag and no new setting.** `cross-venue-match-once` now loads Kalshi
markets **most-recently-seen first** (`last_seen_at DESC`, was rowid/oldest-first)
and gains sample-scoping CLI options: `--recent-hours N` (drops markets not seen
in the window), `--domain`, `--market-type`, alongside the existing
`--kalshi-limit` / `--polymarket-limit`. Bounded defaults were raised to be
representative without magic limits: **`--kalshi-limit 4000`** (was 1500),
**`--polymarket-limit 500`** (was 200). Every run prints a transient
sample-composition report (rows loaded/considered, per-domain + per-market-type
breakdown, stale/no-snapshot counts, domain overlap, low-overlap note). These
change only WHICH persisted rows are considered — the matcher, labels, gates, and
midpoint/side alignment are unchanged. No timer, migration, endpoint, or external
call; no EV/arbitrage/opportunity/sizing/orders/wallets/keys/signing/swaps/execution.

## Tick aggregation (OPS-012 — storage plumbing; no EV/trade/orders/wallets)

**No feature flag.** Aggregation is manual-only (`aggregate-market-ticks`); no timer is
installed and nothing schedules it. Tuning:

| Setting | Default | Effect |
|---|---|---|
| `TICK_AGGREGATION_BUCKET_SECONDS` | 300 | bucket interval (must divide 3600) |
| `TICK_AGGREGATION_MAX_ROWS` | 200000 | raw rows read per pass (truncation reported, never silent) |
| `TICK_BUCKET_RETENTION_DAYS` | 90 | aggregated buckets' own retention window |

**Raw tick retention (`TICK_RETENTION_DAYS`) is UNCHANGED by OPS-012.** The
`tick-aggregation-report` stages — but never enacts — the future option of reducing raw
retention toward 24-48h once bucket coverage is proven healthy. Aggregation never deletes
raw ticks; buckets are telemetry summaries, never trading signals; no
EV/recommendations/sizing/orders/wallets/keys/signing/swaps/execution.

### OPS-013 hardening + gated timer

| Flag / Setting | Default | Effect |
|---|---|---|
| `ENABLE_TICK_AGGREGATION_TIMER` | **false** | Gates ONLY the `--scheduled` path (the timer unit no-ops while false; manual runs always allowed) |
| `TICK_AGGREGATION_SUBWINDOW_HOURS` | 1 | commit after each sub-window (seconds of SQLite lock hold) |
| `TICK_AGGREGATION_BUSY_RETRIES` | 3 | bounded apply+commit retries on a locked DB (the unit re-applies its work per attempt) |
| `TICK_AGGREGATION_BUSY_RETRY_SECONDS` | 2.0 | sleep between retries |
| `TICK_AGGREGATION_MAX_ROWS_PER_SUBWINDOW` | 100000 | runaway guard — an oversized window is skipped LOUDLY |
| `TICK_AGGREGATION_SCHEDULED_HOURS` | 12 | window per scheduled cycle (hourly timer; overlap-by-design so cycles self-heal) |

Timer artifacts `infra/systemd/user/probability-arena-tick-aggregation.{timer,service}`
are **NOT auto-installed** — two-step rollout like meme-news: install dark, then flip the
flag. Failed/oversized sub-windows are recorded on the `tick_aggregation_runs` audit row
and exit nonzero — never silent; idempotent reruns repair them. The readiness section of
`tick-aggregation-report` (coverage_72h >= 0.98, >= 5 clean scheduled cycles, no recent
errors, raw feed fresh) is evidence for a FUTURE raw-retention milestone and enacts
nothing.

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
