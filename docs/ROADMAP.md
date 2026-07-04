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
| MVP-005A-design | (this) | Edge-precheck design + safety review (`docs/MVP_005A_EDGE_PRECHECK_DESIGN.md`) — gate crossed at paired n=36, d_brier=−0.049, d_log_loss=−0.152 (early_signal). Design only: probability gaps + validity checks; no EV, no recommendations, no sizing, no simulation |

## Immediate next steps

1. Deploy SOCCER-001 to EVO-X2 dark; roll out per the README sequence (flag → provider) while keeping both baseball canaries running on live games.
2. Accumulate resolved outcomes; read `champion-challenger-report --domain sports_baseball` weekly — the sample-size label must reach at least `early_signal`/`useful_sample` with negative paired deltas before MVP-005A is considered.

## Gated future steps (in order; each requires explicit acceptance)

- **MVP-005A — edge precheck implementation**: gated on explicit human acceptance of `docs/MVP_005A_EDGE_PRECHECK_DESIGN.md` (checklist in §10). Probability-gap measurement + validity checks only — no dollar EV, no recommendations, no sizing.
- **MVP-005B — paper simulator**: gated on MVP-005A implementation + its own acceptance. Simulation only; still no orders.
- **CRYPTO-003 — crypto paper simulator**: gated like MVP-005B; simulation only, no orders, no wallets; requires CRYPTO-002 risk data to mature first.
- **WALLET-001 — policy-controlled transaction proposal gateway**: *much later*; proposals only — no signing, no private keys, behind a dedicated custody/security review — see `docs/SAFETY_BOUNDARIES.md` and ADR-002.
