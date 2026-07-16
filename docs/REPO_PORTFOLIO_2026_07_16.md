# REPO-PORTFOLIO-001 — safe parallel work during the crypto-readiness measurement (2026-07-16)

Repository-grounded architecture + work-portfolio audit at commit `b877187`. Read-only
planning; **no implementation code was changed**. Determines what Probability Arena can
safely build in parallel while the CRYPTO-HORIZON-CANDIDATE-READINESS-001 measurement
runs on EVO-X2 (activated 2026-07-16T19:56Z; real 7-day checkpoint **2026-07-23 after
19:56 UTC / 12:56 PM PT**; 14-day **2026-07-30**). Synthesis of five disjoint read-only
specialist audits (canon, active-runtime coupling, forecasting, tennis/cross-venue,
code-health).

## 1. Active-experiment freeze manifest

- **Frozen commit:** `b877187` — Mac = origin = EVO-X2. EVO-X2 must stay byte-identical
  here until the 7-day checkpoint. Alembic **0027** (no migration).
- **Active carrier:** `infra/systemd/user/probability-arena-marketops.{service,timer}` →
  `python -m app.cli marketops-run-once`, `EnvironmentFile=.env`. Each firing is a **fresh
  oneshot process that re-imports code from disk**, so any on-disk change to a module the
  oneshot imports ships into the next cycle. Cadence ~6 min (measured 364 s median gap).
- **Active flag:** `MARKETOPS_INCLUDE_CANDIDATE_READINESS=true` (EVO `.env` only, not
  committed). Do not change except to disable on a safety failure.
- **Live measurement health (as of this audit):** 9 records (cycles 3097–3105), all
  `expired`, all `external_calls=0`, distinct cycles, **0 readiness errors**, cohorts
  1–6 unchanged. Append-only sink `~/crypto-horizon-readiness/readiness.jsonl` (single
  logical writer, guarded by the marketops `_active_run` gate).
- **FROZEN code surface** (imported/executed by the oneshot — do NOT touch pre-07-23):
  `app/cli.py`, `app/config.py`, `app/models.py`, `app/db.py`, `app/schemas.py`,
  `app/__init__.py`, `app/adapters/{kalshi,dexscreener}.py`; `app/services/`
  `marketops.py`, `signal_workflow.py`, `research.py`, `outcomes.py`, `calibration.py`,
  `champion_challenger.py`, `watcher.py`, `eligibility.py`, `enrichment.py`,
  `resolution.py`, `retention.py`, `forecasting.py`, `edge_precheck.py`,
  `provider_budget.py`, `crypto_scout.py`, `crypto_provider_policy.py`, `crypto_risk.py`,
  `crypto_risk_engine.py`, `crypto_horizon_readiness.py`, `crypto_horizon_feasibility.py`,
  `crypto_horizon.py`, `crypto_horizon_orchestrator.py`, `crypto_horizon_schedule.py`,
  `crypto_tape.py`, and the domain forecasters (`soccer_*`, `baseball_*`, `tennis_*`);
  `alembic/versions/*`; the marketops systemd unit; the shared SQLite DB; the readiness
  JSONL + observation dir.
- **Highest blast radius:** `app/cli.py` (6,779 LOC) is the process entry — a
  `SyntaxError`/`ImportError` anywhere bricks every cycle. `app/config.py` (a removed/
  renamed/no-default field fails settings load). `app/models.py` (52-table shared `Base`;
  a model↔DB divergence throws in every stage). Treat all three as Tier-3.
- **Not on the oneshot path** (safely editable off-EVO): `app/canon.py` (imported lazily
  only by the `agent-context` handler at `app/cli.py:5429`, never by
  `marketops-run-once`); `app/main.py`/`app/routers/*`; and the other live-unit services
  (backup, watcher, tick_aggregation, meme_news, retention, baseline, polymarket,
  cross_venue, tennis_*, edge_* analysis) — but those feed **other** EVO units, so they
  are branch-only until the freeze lifts.
- **Deploy rule during the freeze:** only pure `docs/*.md` may be pulled to EVO-X2.
  Anything importable (even inert `canon.py`) merges to `main` but its EVO deploy batches
  at the 2026-07-23 re-sync, keeping the measurement host's executable code byte-frozen.

## 2. Capability inventory

| Lane | Services | Commands (CLI) | Tables | Providers | Flags | Scheduled/live? | Evidence | Blocker | Safe to change now? |
|---|---|---|---|---|---|---|---|---|---|
| Kalshi baseline | scanner, eligibility, enrichment, resolution | run-baseline | markets/events/series | Kalshi (read GET) | — | baseline.timer 4h | live | — | FROZEN (branch-only) |
| Watcher + signals | watcher, signal_workflow, ranking | watch-loop, signals-* | market_price_ticks, opportunity_signals | Kalshi | ENABLE_REALTIME_WATCHER | watcher.service | live | — | FROZEN |
| Baseball | baseball_research/forecasting | baseball-* | research/forecasts | LLM (flag) | ENABLE_BASEBALL_* | canary | thin | flags off | FROZEN |
| Soccer | soccer_research/forecasting | soccer-* | " | LLM (flag) | ENABLE_SOCCER_* | canary | thin | flags off | FROZEN |
| Tennis | tennis_watcher/tape/goalserve/providers | tennis-* | tennis_tape_* (0025), market ticks | API-Tennis(dead)/Goalserve(pending) | goalserve_tennis_api_key(empty) | manual | score side EMPTY | Goalserve key | BLOCKED_BY_PROVIDER |
| Edge measurement | edge_precheck, edge_cost, edge_cohort, edge_followthrough | edge-* | edge_precheck_snapshots | — | ENABLE_EDGE_PRECHECK / MARKETOPS_INCLUDE_EDGE_PRECHECK | in MarketOps (dbl-gated) | shipped; all candidates RETIRED/cost-killed | policies retired | FROZEN (precheck) |
| Frontier eval | frontier_eval | frontier-eval-report | frontier_eval_runs | — | — | on demand | live | — | branch-only |
| Crypto discovery+risk | crypto_scout, crypto_provider_policy, crypto_risk(_engine) | crypto-scan-once, crypto-risk-* | crypto_* | Dex/GoPlus/ST/Birdeye (gated) | ENABLE_CRYPTO_* | in MarketOps | live (fail-closed) | — | FROZEN |
| Meme/news + MEME-MAS | meme_news, meme_scout, meme_mas, meme_shadow | meme-* | meme_* | — | — | meme-news.timer 10m | live | — | branch-only |
| Lifecycle tape | crypto_tape, crypto_retrospect, crypto_coverage | crypto-tape-*, crypto-retrospect-* | crypto lifecycle/tape (0026) | — | — | on demand | live | — | FROZEN (tape on readiness path) |
| Horizon obs/orch/readiness | crypto_horizon(_feasibility/_orchestrator/_schedule/_readiness) | crypto-horizon-* (incl. candidate-readiness-report/-history) | crypto_horizon_* (0027) | Dex (obs only) | MARKETOPS_INCLUDE_CANDIDATE_READINESS | **ACTIVE readiness hook** | measuring | discovery lag | **FROZEN (active experiment)** |
| Polymarket | polymarket, polymarket_coverage | polymarket-* | polymarket_* (0022) | Polymarket (manual GET) | ENABLE_POLYMARKET_SCOUT | manual | thin | — | branch-only |
| Cross-venue | cross_venue, xvenue_observation | cross-venue-*, xvenue-* | cross_venue_* | — (persisted rows only) | — | manual | ~0–1 clean comparables | supply | branch-only |
| Storage/retention/agg | retention, tick_aggregation, db_growth | prune-retention, aggregate-market-ticks | tick buckets (0023/0024) | — | ENABLE_*_RETENTION, ENABLE_TICK_AGGREGATION_TIMER | retention.timer daily, tick-agg.timer 1h | live | — | branch-only |
| Backup/ops | backup | backup-db | — | — | — | backup.timer daily | live | — | branch-only |

## 3. Canon-drift findings (Agent A)

| # | Finding | Evidence | Severity |
|---|---|---|---|
| 1 | PROJECT_CANON cites Alembic **0021**; actual head **0027** (6 revs stale; self-contradicts its own table list) | `docs/PROJECT_CANON.md:52` vs `alembic/versions/0027_*`, `PROJECT_CANON.md:74-78` | material |
| 2 | canon.py ALLOWED_CAPABILITIES omits SHARED-CANDIDATE-FEASIBILITY-001 + CANDIDATE-READINESS-001 | `app/canon.py:18-49` | material |
| 3 | CAPABILITY_MATRIX has no row for feasibility or readiness lanes | `docs/CAPABILITY_MATRIX.md` (:23,:26-27) | material |
| 4 | KEY_FEATURE_FLAGS omits `MARKETOPS_INCLUDE_CANDIDATE_READINESS` (now live) | `app/canon.py:92-108` vs `docs/FEATURE_FLAGS.md:62` | material |
| 5 | NEXT_MILESTONES still lists MVP-005B / CRYPTO-003 paper simulators; ignores EDGE-RETIRE-001 + COST-MODEL-001 verdicts and the crypto-horizon frontier | `app/canon.py:70-78` vs `docs/ROADMAP.md:56,65,93-100` | material |
| 6 | EXPECTED_SERVICES_EVO_X2 omits the MarketOps timer/service that runs the active hook | `app/canon.py:64-68` | material |
| 7 | CURRENT_PHASE doesn't mention horizon/readiness lane (canon lags its own AGENTS.md) | `app/canon.py:9-16` vs `AGENTS.md:11` | minor |
| 8 | PROJECT_CANON "Key tables (15+…)" miscounts and omits 0025 tennis_tape tables | `docs/PROJECT_CANON.md:61` | minor |

**Verdict:** REPO-CANON-SYNC-001 (docs + `app/canon.py` constants only; zero runtime/flag/
migration change) is justified and should be the **first implementation milestone** — it
degrades the exact artifact agents load first (`agent-context` → `canon.py`) and violates
the repo's own "update canon when a milestone lands" doctrine. `canon.py` is NOT on the
marketops runtime path (§1), so the edits are runtime-safe.

## 4. Ranked milestone portfolio

Scores 1–5 (higher is better; effort/coupling/deploy-risk phrased so 5 = low-effort /
low-coupling / low-risk). No financial-EV terminology used.

| Milestone | Res | Ops | Data | LowEffort | LowCoupling | LowRisk | Falsifiable | ≤Jul23 | **Class** |
|---|---|---|---|---|---|---|---|---|---|
| **REPO-CANON-SYNC-001** | 2 | 5 | 5 | 5 | 5 | 5 | 4 | 5 | **MERGE_AND_DEPLOY_NOW** |
| **FORECAST-SCORABILITY-AUDIT-001** | 4 | 5 | 5 | 4 | 2 | 3 | 5 | 4 | **BUILD_ON_BRANCH_NO_DEPLOY** |
| **FORECAST-RELIABILITY-DECOMP-001** (rename of FORECAST-QUALITY-ATTRIBUTION-001) | 5 | 4 | 3 | 3 | 2 | 3 | 5 | 3 | **BUILD_ON_BRANCH_NO_DEPLOY** |
| **RUNTIME-UTIL-001** (net-new shared clock/lock module) | 2 | 4 | 5 | 4 | 4 | 4 | 3 | 4 | **BUILD_ON_BRANCH_NO_DEPLOY** |
| **SQLITE-WRITER-TOPOLOGY-001** | 1 | 5 | 5 | 2 | 1 | 1 | 3 | 3 | **DESIGN_ONLY** |
| **CLI-DECOMPOSITION-DESIGN-001** | 1 | 4 | 5 | 2 | 1 | 1 | 2 | 3 | **DESIGN_ONLY** |
| **MARKET-REFERENCE-SKILL-001** | 5 | 3 | 2 | 3 | 2 | 2 | 4 | 2 | **DESIGN_ONLY** (retired-edge adjacency → human sign-off) |
| **XVENUE-MATCH-QUALITY-001** | 3 | 2 | 2 | 3 | 2 | 3 | 3 | 3 | **BUILD_ON_BRANCH_NO_DEPLOY** (thin; scope to multi-run/golden-set) |
| **TENNIS-MICROSTRUCTURE-001** | 4 | 2 | 1 | 3 | 2 | 3 | 2 | 1 | **BLOCKED_BY_PROVIDER** (Goalserve key) |
| **CRYPTO-DISCOVERY-FRESHNESS-001** | 5 | 4 | 4 | 2 | 1 | 1 | 4 | 1 | **BLOCKED_BY_ACTIVE_MEASUREMENT** (changing discovery contaminates the measurement) |

Note: FORECAST-QUALITY-ATTRIBUTION-001 as literally worded is ~80% already shipped in
`calibration.summary()` (`app/services/calibration.py:180-197`) — do not rebuild it; the
net-new value is the reliability curve + Murphy/Brier decomposition + base-rate skill +
time trend (renamed FORECAST-RELIABILITY-DECOMP-001).

## 5. Top-five path-level execution plans

Every path below verified to exist (existing) or verified absent (net-new).

### A. REPO-CANON-SYNC-001 — MERGE_AND_DEPLOY_NOW
- **Objective:** correct the 8 canon-drift findings; document the two newest shipped
  capabilities + the active flag; re-point NEXT_MILESTONES at the real frontier.
- **Changes:** `app/canon.py` (CURRENT_PHASE, ALLOWED_CAPABILITIES, KEY_FEATURE_FLAGS,
  EXPECTED_SERVICES_EVO_X2, NEXT_MILESTONES), `docs/PROJECT_CANON.md`,
  `docs/CAPABILITY_MATRIX.md`, `docs/SAFETY_BOUNDARIES.md`, `docs/ROADMAP.md`.
- **Untouched:** everything on the FROZEN runtime surface (§1); no service, no flag value,
  no migration. `canon.py` is lazily imported only by `agent-context` — zero marketops
  effect.
- **cli.py?** No. **config.py?** No. **models/migrations?** No. **External calls:** none.
  **Persistence:** none.
- **Tests:** run any docs/canon-honesty guard if present; full suite must stay green
  (canon.py imports must still succeed).
- **Safety audit:** AGENTS.md safety grep; confirm no capability wording implies EV/trading.
- **Deploy:** merge to `main` immediately; EVO deploy batches at the 2026-07-23 re-sync
  (canon.py is inert, but batching preserves the byte-freeze).
- **Rollback:** `git revert`. **Commits:** `REPO-CANON-SYNC-001: sync canon + capability docs`.
- **Worktree:** `worktree/canon-sync` (owns `app/canon.py` + the four docs).

### B. FORECAST-SCORABILITY-AUDIT-001 — BUILD_ON_BRANCH_NO_DEPLOY
- **Objective:** measure the forecast→score funnel denominator (fraction of
  `market_forecasts` that ever reach `scored` vs pending/unscorable/no-outcome), decomposed
  by domain / forecaster / evidence-depth / age. Nothing measures this today; `calibration.
  summary()` aggregates only scored rows.
- **New files:** `app/services/forecast_scorability.py`, `tests/test_forecast_scorability.py`
  (both verified absent). Optional additive schema in `app/schemas.py`.
- **cli.py?** Yes — additive only (new subparser + dispatch branch + async handler,
  mirroring `calibration_report`). Because `app/cli.py` is FROZEN, this stays branch/Mac-only
  and is **not deployed to EVO before 07-23**. **config.py?** No. **models/migrations?** No.
- **External calls:** none. **Persistence:** none (recomputable, per champion/challenger).
- **Reads:** `market_forecasts`, `forecast_scores` (`latest_score_for`), `market_outcomes`.
- **Tests:** funnel buckets mutually exclusive and sum to `count(market_forecasts)`; per-
  cohort counts reconcile to the global funnel; status classification fixtures; zero
  external calls; safety grep.
- **Deploy:** merge to `main`; dark-deploy to EVO only after the 07-23 re-sync. **Rollback:**
  revert. **Commits:** impl+tests, then `docs: document forecast scorability audit`.
- **Worktree:** `worktree/forecast-attribution` (shared with C to serialize the one
  `app/cli.py` edit and avoid a frozen-file merge conflict).

### C. FORECAST-RELIABILITY-DECOMP-001 — BUILD_ON_BRANCH_NO_DEPLOY
- **Objective:** reliability curve (binned predicted vs observed), Murphy/Brier decomposition
  (reliability−resolution+uncertainty), base-rate (climatology) skill score, and time-bucketed
  calibration trend. All four are verified-absent computations (grep for reliability/murphy/
  skill returns nothing).
- **New files:** `app/services/forecast_reliability.py`, `tests/test_forecast_reliability.py`.
  Optional additive `app/schemas.py` dataclasses.
- **cli.py?** Yes, additive only (branch-only, no pre-07-23 EVO deploy). **config.py?** No.
  **models/migrations?** No. **External calls:** none. **Persistence:** none.
- **Reads:** same population as `calibration.summary` (`forecast_scores` latest-per-forecast,
  `market_forecasts`, `market_outcomes`).
- **Tests:** algebraic identity `brier == reliability − resolution + uncertainty` (1e-9);
  perfectly-calibrated fixture → reliability≈0, skill≥0; per-bin integer `n`, no div-by-zero;
  aggregate Brier equals `calibration.summary().overall.mean_brier` (cross-check); time buckets
  partition resolved rows exactly; mandatory `sample_label` so thin bins self-flag; safety grep.
- **Deploy/rollback/commits/worktree:** as B (same worktree).

### D. RUNTIME-UTIL-001 — BUILD_ON_BRANCH_NO_DEPLOY (net-new module only)
- **Objective:** create one shared `_now()`/`_aware()` and one canonical DB lock-retry
  primitive, ending the 35× `_now` / 14× `_aware` duplication and the 3 divergent (2-vs-3
  attempt) lock-retry loops.
- **New files:** `app/util/__init__.py`, `app/util/time.py`, `app/util/dblock.py`,
  `tests/test_util_time_001.py` (all verified absent — `app/util` does not exist).
- **Scope split (critical):** creating the module + tests is a net-new isolated module (safe).
  **Retrofitting the 35 frozen call-sites** (marketops.py, crypto_tape.py, crypto_horizon_*,
  etc.) EDITS FROZEN files → that retrofit is **DEVELOP_BRANCH_ONLY, deferred past 07-23**.
  This milestone ships only the new module + tests now; retrofit is a separate post-freeze PR.
- **cli.py/config/models/migrations?** None. **External calls:** none. **Persistence:** none.
- **Tests:** `now()` tz-aware UTC; `aware()` idempotent + naive→UTC; lock-retry honors budget
  and re-raises non-lock errors; parity with the existing `crypto_tape._is_db_locked` behavior.
- **Deploy:** merge new module to `main`; no EVO effect (nothing imports it yet). **Rollback:**
  delete module. **Worktree:** `worktree/runtime-util` (owns `app/util/*`).

### E. SQLITE-WRITER-TOPOLOGY-001 — DESIGN_ONLY
- **Objective:** a written topology audit + proposal for serializing the EVO SQLite writers
  (marketops 5-min, watcher continuous, tick-agg 1h, meme-news 10-min, baseline 4h, retention/
  backup daily, armed horizon jobs, ad-hoc CLI) that today rely only on a 30 s busy-timeout +
  three inconsistent retry loops, with **no WAL mode set anywhere**.
- **Deliverable:** `docs/design/SQLITE_WRITER_TOPOLOGY_2026_07.md` (net-new doc). Enumerate
  every writer; evaluate WAL (interacts with the daily `backup.service` snapshot → Tier-3),
  a single shared retry lease, and cadence de-alignment. **No code, no runtime change.**
- **Evidence files (read-only):** `app/db.py:22-36`, `app/config.py:240`, `crypto_tape.py:922`,
  `crypto_horizon_orchestrator.py:54`, `tick_aggregation.py:373`, `infra/systemd/user/*.timer`.
- **cli/config/models/migrations/providers/persistence:** none (design doc only).
- **Deploy:** docs-only → merge to `main`, safe to sync EVO. **Worktree:** `worktree/cli-design`
  (shares the design lane with CLI-DECOMPOSITION-DESIGN-001; both are docs-only).

## 6. Worktree strategy (disjoint path ownership)

| Worktree | Milestones | Owns (no overlap) | main? | EVO before 07-23? |
|---|---|---|---|---|
| `worktree/canon-sync` | REPO-CANON-SYNC-001 | `app/canon.py`, `docs/PROJECT_CANON.md`, `docs/CAPABILITY_MATRIX.md`, `docs/SAFETY_BOUNDARIES.md`, `docs/ROADMAP.md` | merge now | deploy batched at 07-23 (inert, but preserves freeze) |
| `worktree/forecast-attribution` | FORECAST-SCORABILITY-AUDIT-001 + FORECAST-RELIABILITY-DECOMP-001 | `app/services/forecast_scorability.py`, `app/services/forecast_reliability.py`, `tests/test_forecast_*`, the additive `app/cli.py` + `app/schemas.py` edits | merge now | **NO** (touches frozen cli.py) — Mac/branch only |
| `worktree/runtime-util` | RUNTIME-UTIL-001 (new module only) | `app/util/*`, `tests/test_util_*` | merge now | NO (retrofit deferred) |
| `worktree/cli-design` | SQLITE-WRITER-TOPOLOGY-001 + CLI-DECOMPOSITION-DESIGN-001 | `docs/design/*.md` (net-new) | merge now (docs) | safe (docs-only) |
| `worktree/tennis-microstructure` | TENNIS-MICROSTRUCTURE-001 | (deferred) | — | blocked (Goalserve key) |
| `worktree/xvenue-quality` | XVENUE-MATCH-QUALITY-001 | `app/services/xvenue_match_quality.py`, tests, additive cli.py | PR, hold | NO (thin data + frozen cli.py) |

The **only** shared frozen file any active lane touches is `app/cli.py` (forecast + xvenue).
Confine all `app/cli.py` edits to `worktree/forecast-attribution`, serialize them, and never
merge them onto EVO before 07-23. Every other lane owns disjoint net-new paths.

## 7. Recommended sequence

### Now → 2026-07-23 (freeze active)
- **Implement + review + merge to `main`:** REPO-CANON-SYNC-001 (docs+canon); the design docs
  (SQLITE-WRITER-TOPOLOGY-001, CLI-DECOMPOSITION-DESIGN-001); RUNTIME-UTIL-001 (new module).
- **Build on branch, merge to `main`, Mac-validate only (NO EVO deploy):**
  FORECAST-SCORABILITY-AUDIT-001, then FORECAST-RELIABILITY-DECOMP-001.
- **Deploy to EVO-X2:** only pure `docs/*.md` (this portfolio; the canon-sync docs if desired).
  EVO executable code stays at `b877187`. Full test suite green on `main` each merge.
- **Do NOT:** touch any FROZEN file on EVO, add a migration, add a CLI subcommand *to EVO*,
  build CRYPTO-DISCOVERY-FRESHNESS-001 (it changes the discovery being measured), or start
  any lane that needs a provider call or the Goalserve key.

### At the 2026-07-23 checkpoint
- Run the real 7-day readiness analysis (state distribution, catch rate, evaluator errors,
  JSONL growth). If healthy and continuing: **re-sync EVO-X2 to `main`** (one clean pull),
  then dark-deploy the merged forecast reports and canon-sync. Measurement findings reshape
  the backlog: a persistent all-`expired`/near-zero-catch signal strengthens the case for
  CRYPTO-DISCOVERY-FRESHNESS-001; a caught ready moment triggers a separate CANARY-004
  authorization request.

### After 2026-07-23 (and the 2026-07-30 14-day verdict)
- Reconsider the frozen refactors: RUNTIME-UTIL-001 retrofit of the 35 call-sites; the
  CLI-DECOMPOSITION implementation; SQLITE WAL/serialization (staged, Tier-3, backup-aware).
- Decide the crypto path per the 14-day evidence: proceed to CANARY-004 with a naturally
  observed pair · continue measurement · implement CRYPTO-DISCOVERY-FRESHNESS-001. Do not end
  the 14-day measurement early unless the checkpoint evidence supports it.

## 8. Recommended first implementation milestone

**REPO-CANON-SYNC-001.** It is the only high-value candidate with zero coupling to the frozen
runtime (docs + inert `canon.py` constants), trivially completable before 07-23, falsifiable
against a docs-honesty check, and it repairs the exact artifact every agent loads first — a
6-revision Alembic understatement, two undocumented shipped capabilities, the now-active
`MARKETOPS_INCLUDE_CANDIDATE_READINESS` flag, a missing live service, and a NEXT_MILESTONES
list still pointing at retired/cost-killed paper-simulator work. Sequence FORECAST-SCORABILITY-
AUDIT-001 next (highest immediate research/ops value, useful at any sample size), then
FORECAST-RELIABILITY-DECOMP-001 — both branch-only until the 07-23 re-sync.
