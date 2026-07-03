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
| OPS-005 | (this) | Project canon + agent operating framework |

## Immediate next steps

1. Deploy OPS-004 → MVP-004F to EVO-X2 (host is on `eeb799d`; follow `docs/EVO_X2_RUNBOOK.md`).
2. Roll out the two baseball canary flags per the documented sequence; let live games flow through promote → process.
3. Accumulate resolved outcomes; watch `calibration-report` `by_forecaster` cohorts (`template_baseline` vs `baseball_evidence`).

## Gated future steps (in order; each requires explicit acceptance)

- **MVP-004G — champion/challenger**: systematic side-by-side forecaster comparison on identical inputs; promotion criteria defined by calibration, not vibes.
- **MVP-005A — EV precheck**: *design + safety review only.* Requires calibration evidence that a challenger beats the market baseline over a meaningful resolved sample. No trading surface.
- **MVP-005B — paper simulator**: gated on MVP-005A acceptance. Simulation only; still no orders.
- **CRYPTO-001 — read-only crypto scout**: separate track; same read-only doctrine as the Kalshi scanner.
- **Wallet milestones**: later only, behind dedicated custody/security review — see `docs/SAFETY_BOUNDARIES.md` and ADR-002.
