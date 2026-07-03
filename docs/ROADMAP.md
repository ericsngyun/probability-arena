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
| MVP-004G | (this) | Champion/challenger comparison (paired + cohort, sample-size gated) |

## Immediate next steps

1. Deploy MVP-004G to EVO-X2; keep both baseball canaries running on live games.
2. Accumulate resolved outcomes; read `champion-challenger-report --domain sports_baseball` weekly — the sample-size label must reach at least `early_signal`/`useful_sample` with negative paired deltas before MVP-005A is considered.

## Gated future steps (in order; each requires explicit acceptance)

- **MVP-005A — EV precheck**: *design + safety review only.* Requires champion/challenger evidence (paired, adequate sample) that a challenger beats the market baseline. No trading surface.
- **MVP-005B — paper simulator**: gated on MVP-005A acceptance. Simulation only; still no orders.
- **CRYPTO-001 — read-only crypto scout**: separate track; same read-only doctrine as the Kalshi scanner.
- **Wallet milestones**: later only, behind dedicated custody/security review — see `docs/SAFETY_BOUNDARIES.md` and ADR-002.
