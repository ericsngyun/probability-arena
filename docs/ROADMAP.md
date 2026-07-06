# ROADMAP

## Completed milestones

| Milestone | Commit | Summary |
|---|---|---|
| MVP-001 | `4fdb0ee` | Read-only Kalshi scanner, ranking, API, Compose, tests |
| MVP-002 | `4d1f28a` | Alembic, scanner audit fields, CLI, live tests |
| MVP-003A | `b3ccce2` | Eligibility gate; fixed live payload drift (dollars/fp), MVE filter |
| MVP-003B | `2236323` | Resolution assessments (rule-based judge; LLM behind flag) |
| MVP-003C | `5bcdf48` | Detail enrichment (settlement sources); clarity 0.75→1.00 live |
| MVP-004A | `41b90fd` | Research packets (domains, template collector) |
| MVP-004B | `90f2b62` | Forecast engine, evidence-depth confidence caps |
| MVP-004C | `56f3ab3` | Outcome sync + Brier/log-loss calibration |
| MVP-004D | `0cd62b0` | Scheduled baseline runner + pipeline audit + overlap lock |
| OPS-001 (deploy) | `a562e05` | EVO-X2 deployment: venv + SQLite + user timer |
| OPS-002 | `109d385` | Real-time watcher, price ticks, opportunity signals |
| OPS-003 | `27a4501` / `eeb799d` | Retention/pruning, db-stats, watcher+retention deployed to EVO-X2 |
| OPS-004 | `f9fda96` | Signal promotion + signal-triggered intelligence refresh |
| MVP-004E | `9b46911` | Baseball external research canary (MLB Stats API, source-backed packets) |
| MVP-004F | `20d4fda` | Evidence-aware baseball forecaster (capped non-midpoint forecasts) |
| OPS-005 | `c35e704` | Project canon + agent operating framework; deployed to EVO-X2 with baseball canaries enabled (`71dab1d`) |
| MVP-004G | `918b9de` | Champion/challenger comparison (paired + cohort, sample-size gated) |
| SOCCER-001 | `e1d3b7b` | Soccer/World Cup external research canary (provider-gated, source-backed packets); rolled out to EVO-X2 (`f76baaa`) |
| CRYPTO-001 | `9d72237` | Crypto Arena: read-only Solana memecoin discovery + risk surveillance (DEX Screener, 7 tables, 9 deterministic signal types, CLI/API reports); deployed dark to EVO-X2 (`7606ca6`) |
| OPS-006 | `b0dd1d6` | MarketOps Autopilot: read-only 24/7 coordination (auto-promote/process, crypto scan, sync/score, champion/challenger snapshot, local DB alerts); live on EVO-X2 with 5-min timer (`28b3476`) |
| CRYPTO-002 | `6450194` | Crypto risk engine: heuristics + optional GoPlus/SolanaTracker providers, composite risk scores/levels, activated risk signals, risk reports (read-only risk intelligence — never trade advice); live on EVO-X2 GoPlus-backed (`ad79fde`; SolanaTracker needs an API key — `e2d8ae9`) |
| OPS-007 | `a1d4ff6` | Operational hardening: MarketOps overlap guard (skipped/already_running + stale-lock recovery), SQLite busy timeout, DB backup/verify/retention CLI + optional daily timer; deployed + validated live (`19370c2`) |
| MVP-005A-design | `cd6760a` | Edge-precheck design + safety review (`docs/MVP_005A_EDGE_PRECHECK_DESIGN.md`) — gate crossed at paired n=36, d_brier=−0.049, d_log_loss=−0.152 (early_signal) |
| MVP-005A | `1bd134a` | Edge precheck implementation: probability-gap measurement (10 statuses, deterministic precedence, persistence counting), edge_precheck_snapshots audit rows, CLI/API, double-gated MarketOps stage; live on EVO-X2 for manual measurement (`fa0ac34`) |
| MVP-005A.1 | `5324046` | Targeted edge-precheck modes: explicit forecast ids, MarketOps-cycle scoping, recent-refreshed-signals; dedupe window; MarketOps stage now strictly cycle-scoped (broad sweeps stay manual-diagnostic) |
| SOCCER-002 | `2d2cf10` | Soccer evidence-aware forecaster (goal-margin/pace model, red-card + penalty handling, capped ±0.25 shift) — makes soccer forecasts measurable by edge-precheck |
| EVAL-001 | `57e8369` | Frontier evaluation harness: 8 quality sections (signal/forecast/edge/follow-through/microstructure/crypto/latency/safety) + conservative readiness scorecard; evaluation only — readiness labels never authorize live capital |
| OPS-009 | `7746ef9` | Promotion quality: minute-level domain-aware freshness windows + measurement-readiness scoring (market-type/book-quality/source-backed priority; player props deprioritized); promotion stats in run summaries |
| SCANNER-002 / OPS-010 | `00e169b` | Targeted game-level market scan coverage: per-series read-only fetches (`KXWCGAME`/`KXWCTOTAL`/`KXWCSPREAD`/`KXMLBGAME`/`KXMLBTOTAL`/`KXMLBSPREAD`) supplement the generic scan (deduped, partial-failure tolerant); watcher supported-universe supplement (game-level baseball/soccer with two-sided quotes, bounded, never props). Motivated by `docs/VALIDATION_PAR_FRA_2026_07_04.md` |
| MVP-005A.2 / EDGE-AUTO-001 | `134e401` (deploy) | Enabled cycle-scoped edge-precheck inside MarketOps (`MARKETOPS_INCLUDE_EDGE_PRECHECK=true`) after readiness crossed `ready_for_cycle_scoped_edge_automation`; measurement automation only |
| OPS-011 | `dd99146` (deploy) | DB growth/retention observability + alert calibration: `db-growth-report` CLI, retention dry-run detail, configurable warning/critical tiers for DB-growth (1536/3072 MiB) and signal-flood (400/800 per hour) alerts. Ops-only; no alpha/edge/trading change |
| EDGE-ANALYSIS-001 | `d20ca56` (deploy `447e7ae`) | Edge cohort analysis: `edge-cohort-report --hours N` slices watchlist/`paper_candidate_later` snapshots into 10 cohort dimensions (market type/domain/gap sign/abs-gap/confidence/signal type/liquidity/spread/game phase/persistence) and measures per-cohort gap follow-through, labelling each `too_thin`/`promising`/`neutral`/`weak`/`exclude_candidate`; reports whether MVP-005B-design remains blocked. Analysis/reporting only — no advice, no PnL, no flag/logic change |
| MEME-NEWS-001 | (this) | Read-only meme/news + domain-expansion scout: **Part A** `meme-scan-once`/`meme-scout-report` — DexScreener token-genesis + boost velocity → per-token `attention_score` (freshness/liquidity/volume growth/boost velocity/metadata/social − risk penalty × provider confidence); **Part B** `catalyst-report` — generic source-agnostic `meme_catalyst_events` (dexscreener now; rss/x/discord/telegram placeholders); **Part C** `domain-scout-report` — probability-market inventory grouped by domain/series with `canary_priority` for weather/tennis/basketball/golf/esports/… . 5 audit tables (migration 0019). Discovery/scoring/inventory only — `attention_score` is an interest signal, not a buy/trade/EV score; adds no forecaster; changes no promotion/edge/forecast logic; no EV/paper/sizing/orders/wallets/keys/swaps/signing/execution |
| EDGE-POLICY-001 | `debdfda` (deploy `bbc761d`) | Edge shadow-policy analysis: `edge-policy-report --hours N` simulates 13 candidate cohort filters over existing watchlist/`paper_candidate_later` rows (included/invalid counts, per-horizon follow-through, settlement-conditioned forecast-vs-market Brier on resolved outcomes, `too_thin`/`worse_than_baseline`/`neutral`/`promising_shadow`/`reject_policy` labels) + a decision/gate readout. Read-only shadow analysis — re-slices existing rows only; no live gating, flag, forecaster, edge, or service change; no EV/PnL/trade/recommendation; MVP-005B stays blocked unless a policy clearly clears the gate |

## Immediate next steps

1. Roll out SCANNER-002/OPS-010 on EVO-X2 and validate during the next live game-level window (POR–ESP Jul 6 / ARG–EGY Jul 7 / MLB evenings): game-level markets in the scan, watcher ticking them, signals → `soccer_evidence`/`baseball_evidence` forecasts at ≥0.60 confidence → first valid watchlist rows.
2. Run targeted `edge-precheck --latest-marketops-run` sessions during those windows; on sane watchlist behavior, consider `MARKETOPS_INCLUDE_EDGE_PRECHECK=true`.
3. Keep accumulating champion/challenger pairs toward `useful_sample` (n≥100) for both `baseball_evidence_v1` and (as data arrives) `soccer_evidence_v1` cohorts.
4. Consider the remaining CAN–MAR promotion-tuning ideas (harder player-prop deprioritization; KXWCAST/KXWCSOA classification) once game-level supply exists to compare against.

## Gated future steps (in order; each requires explicit acceptance)

- **MVP-005B — paper simulator**: gated on accumulated edge-precheck measurement data (watchlist/paper_candidate_later precision over time) + its own explicit acceptance. Simulation only; still no orders.
- **CRYPTO-003 — crypto paper simulator**: gated like MVP-005B; simulation only, no orders, no wallets; requires CRYPTO-002 risk data to mature first.
- **OPS-012 — tick aggregation (proposed)**: `market_price_ticks` is the dominant storage growth source. Future milestone: roll raw ticks into hourly OHLC/spread/liquidity aggregates, retain raw ticks shorter (e.g. 3d) and aggregates longer, and move DB-growth alerting from absolute-size gates to a rate-based (MiB/day over a window) signal. Read-only/ops; no alpha change. Build only when small and explicitly safe.
- **WALLET-001 — policy-controlled transaction proposal gateway**: *much later*; proposals only — no signing, no private keys, behind a dedicated custody/security review — see `docs/SAFETY_BOUNDARIES.md` and ADR-002.
