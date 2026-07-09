# FRONTIER_REVIEW_CONTEXT — Probability Arena current state (2026-07-09, ~06:15 UTC)

Purpose of this document: a self-contained, accurate current-state packet for an
**external frontier model** to give a second opinion on what to build next. All
metrics below are from live CLI reports run on the production host (EVO-X2,
commit `995afa5`, alembic `0024`) at the timestamp above — nothing is projected
or fabricated. Every command listed ran successfully. No secrets are included;
API keys are reported present/absent only.

**Read this first:** Probability Arena is a **read-only market-intelligence and
calibration system**. No trading capability of any kind exists — no EV
calculation, no recommendations, no paper trading, no sizing, no orders, no
wallets/keys/signing/swaps/execution, no autonomy. Every "signal", "score", or
"gap" below is a measurement or a human-review triage label, never advice. Your
review must preserve that boundary: proposals may *stage* capabilities behind
explicit human acceptance, never assume them.

---

## 1. Executive summary

Probability Arena watches **Kalshi** prediction markets (real-time quote
watcher, signal promotion, evidence-backed forecasting, Brier/log-loss
calibration against settled outcomes) plus two parallel read-only lanes: a
**Solana memecoin surveillance + risk-diagnostic lane** and a **Polymarket
observer with cross-venue semantic matching**. The strategy: prove measurable
forecasting edge with calibration data *before* any EV or trading capability is
even designed.

State in one paragraph: the full desk runs unattended on scheduled timers; the
frontier evaluation labels the desk `ready_for_cycle_scoped_edge_automation`
(a measurement label — it authorizes no capital); baseball evidence forecasts
beat the template baseline (paired ΔBrier −0.029, n=91, still `early_signal`);
**gap follow-through is currently negative** (markets move away from our
forecasts on average — see §6, the single most decision-relevant fact in this
document); memecoin review labels do **not** yet materially separate momentum
outcomes (`no_material_separation_recalibrate`); cross-venue matching finds
essentially zero clean comparable markets because the venues list different
market *types*; and storage pressure from raw quote ticks is one gate away from
its mitigation (OPS-014).

Highest-value open decisions: (a) whether the next milestone is OPS-014
(storage), XMARKET-001 (cross-venue), MEME-SHADOW-002 (label recalibration), or
more calibration accumulation; (b) what evidence threshold should unlock a paper
simulator (MVP-005B) given follow-through is currently negative.

## 2. Architecture (backend, all read-only)

- **Kalshi core**: 60s watcher (`market_price_ticks`, informational signals) →
  MarketOps Autopilot (5-min timer: promote/process signals, refresh
  enrichment/research/forecast, sync outcomes, score) → edge precheck
  (probability-gap measurement, forecast − midpoint, validity-gated;
  "EDGE-AUTO" = the cycle-scoped precheck stage inside MarketOps) → frontier
  evaluation (desk-wide quality + conservative readiness labels).
  Forecasters: template baseline + evidence canaries (baseball via MLB Stats
  API, soccer via ESPN; tennis scaffolded, provider mapping unvalidated).
- **Crypto/meme lane**: DexScreener discovery (MEME-NEWS, 10-min timer) →
  attention scores + catalyst events → risk engine (deterministic heuristics +
  providers) → MEME-MAS diagnostic agents (deterministic, no LLM) producing
  `review_priority` → MEME-SHADOW follow-through measurement + multi-objective
  calibration reports.
- **Risk providers**: GoPlus (active, keyless tier) + SolanaTracker Advanced
  (active, key present, budgeted) ; Birdeye **disabled** (creator-dimension gap,
  payload mapping unvalidated); Helius/RugCheck reserved, no adapters.
- **Polymarket/cross-venue lane**: POLY-001 observer (public no-auth Gamma +
  CLOB-read GETs; manual-only) → POLY-COVERAGE-001 targeted/paginated scans →
  POLY-002 deterministic semantic matcher, precision-hardened by
  POLY-PRECISION-001 (side alignment, market-type/threshold/entity/sport gates)
  → XVENUE-OPS-001 recency-aware defaults → XVENUE-OBS-001 window verdicts.
- **Storage lane (OPS-011/012/013)**: db-growth observability; raw ticks rolled
  into 5-minute OHLC/spread/liquidity buckets (`market_price_tick_buckets`,
  ~85:1 smaller by bytes), per-sub-window commits (seconds of SQLite lock hold),
  hourly gated timer, audit spine, readiness gates for a staged raw-retention
  reduction.
- Stack: Python/FastAPI/SQLAlchemy/Alembic, SQLite on host, systemd user units.
  LLM paths exist but are **off** (`ENABLE_LLM_*`=false); runtime has zero model
  spend.

## 3. Services / timers (live status at capture)

| Unit | Cadence | Status |
|---|---|---|
| probability-arena-watcher.service | continuous 60s | active (running), up 4 days |
| probability-arena-marketops.timer | 5 min | active; run #1264 `ok` |
| probability-arena-baseline.timer | 4 h | active (next 08:04 UTC) |
| probability-arena-retention.timer | daily | active (next 00:07 UTC) |
| probability-arena-meme-news.timer | 10 min | active; run #402 `ok`, 0 errors in window |
| probability-arena-edge-observation.timer | daily 15:01 | active |
| probability-arena-tick-aggregation.timer | hourly | active + **flag-enabled** since 2026-07-09 01:35 UTC; 5 clean scheduled runs |
| provider-roll-001-t24h.timer | one-shot | completed 2026-07-08 21:00 UTC (SolanaTracker T+24h check) |

(The host also runs unrelated units — `arena-daily`, `launchpadlib` — it is a
shared box; ours are only the `probability-arena-*` units.)

## 4. Feature flags (host `.env`, secrets redacted)

| Flag | Value |
|---|---|
| ENABLE_EDGE_PRECHECK | **true** |
| MARKETOPS_INCLUDE_EDGE_PRECHECK | **true** (cycle-scoped stage live since EDGE-AUTO-001) |
| ENABLE_MARKETOPS_AUTOPILOT | **true** |
| ENABLE_MEME_NEWS_SCOUT | **true** |
| ENABLE_CRYPTO_RISK_ENGINE / ENABLE_GOPLUS_RISK / ENABLE_SOLANA_TRACKER_RISK | **true** (SolanaTracker key present; value never printed) |
| ENABLE_BIRDEYE_RISK | **false** (absent from .env → default false) |
| ENABLE_POLYMARKET_SCOUT | **false** (absent → default false; no Polymarket timer exists; manual scans only) |
| ENABLE_TICK_AGGREGATION_TIMER | **true** (flipped 2026-07-09 ~01:35 UTC) |
| TICK_RETENTION_DAYS | **3** (unchanged; reduction gated behind OPS-014) |
| ENABLE_BASEBALL/SOCCER_EXTERNAL_RESEARCH + EVIDENCE_FORECASTING | true (canaries) |
| ENABLE_LLM_RESOLUTION / ENABLE_LLM_FORECASTING / ENABLE_EXTERNAL_RESEARCH | false |
| SolanaTracker budget caps | monthly 200,000 · daily 5,000 · hourly 200 · per-run 15 · warn 4,000/day · stop 6,000/day · cache TTL 24h |

## 5. DB / storage state

- **DB 2,728.14 MiB** — above the 1,536 MiB warning tier, below the 3,072 MiB
  critical tier. Backups: 7 files / 1,177 MiB.
- Largest tables: `market_price_ticks` **659,100 rows / 1,821.47 MiB (~67% of
  file)**; market_snapshots 94.6 MiB; crypto_token_discovery_events 65.2 MiB;
  **market_price_tick_buckets 98,674 rows / 21.98 MiB**.
- Tick inflow ~8,550/hour ≈ **567 MiB/day**; projected steady-state under the
  current 3d window ≈ **1,701 MiB** of raw ticks.
- Aggregation (OPS-012/013): 5-min OHLC buckets, **~85:1 byte compression**;
  coverage last-48h **1.0 (healthy)**; 5 clean scheduled cycles, 0
  failed/oversized windows, 0 retries; scheduled `max_commit_ms` = 327 / 3,294 /
  15,798 (one busy-window wait outlier, neighbors sub-second–3s) / 357 / 2,451.
- **OPS-014 (raw retention 3d → staged reduction) readiness: `not_ready`** —
  the only failing gate is `coverage_72h = 0.7534 < 0.98` (pre-aggregation
  Jul-6 hours still inside the 72h view; they age out ~2026-07-09 22:00 UTC).
  Cycles gate passes (5 ≥ 5); no run errors; raw feed fresh. **Next retention
  decision:** re-check after ~22:00 UTC; if `ready_to_stage`, propose OPS-014 as
  design-only with a staged **3d → 2d** first step (internal rule: never
  straight to 24h). Nothing reduces automatically.
- prune-retention dry-run: only raw ticks currently eligible (53,400 rows past
  the 3d window — normal); buckets (90d), aggregation runs (30d), and all other
  lanes 0 eligible; intelligence/calibration tables are never pruned.

## 6. Prediction-market core (Kalshi)

- **MarketOps health**: 1,264 total runs; last run `ok`; duration p50 40.1s /
  p90 57.2s / p99 79.0s (60s threshold: p90 passes). 1 open warning
  (crypto_signal_spike: 25 signals in one scan) + routine info alerts.
- **Frontier readiness label**: `ready_for_cycle_scoped_edge_automation`
  (highest label currently attainable; gates measurement milestones only). The
  report's "recommended next action" line still suggests enabling
  MARKETOPS_INCLUDE_EDGE_PRECHECK — **that flag is already true**; the
  recommender text lags the deployed state (cosmetic, known).
- **Edge precheck (24h)**: 217 snapshots; 114 `watchlist`, 73 `no_gap`, 30
  invalid by status (invalidation reasons observed: wide spread 25, stale
  snapshot 5, low liquidity 3; `invalid_explainable_rate` 1.0);
  valid-measurement rate 0.862; mean |gap| 0.111; `paper_candidate_later` = 0;
  persistence mostly 1 snapshot (203 of 217).
- **Gap follow-through (24h window; market movement, not PnL) — the honest
  headline**: moved-toward-forecast rate **0.33 @5m → 0.24 @60m** (below the
  0.5 coin-flip line) and mean gap closure **negative at every horizon**
  (−0.17 @5m to −0.78 @60m). In this window, when our forecast disagreed with
  the market, the market on average moved *further away*. Either forecasts are
  slow/wrong on these markets (all `sports_baseball` in this window), gaps are
  measured at moments of transient dislocation that mean-revert against us, or
  the 114-row watchlist sample is too thin — but as measured, **there is no
  demonstrated follow-through edge right now.** This is the central fact for
  any paper-trading discussion.
- **Champion/challenger**: baseball evidence vs template — paired n=91, ΔBrier
  **−0.029**, Δlog-loss −0.075 (challenger better), label `early_signal`
  (needs n≥100 for `useful_sample`). Soccer: paired n=1, `insufficient_sample`.
- **Why paper/live trading is blocked**: MVP-005B requires explicit human
  acceptance on top of MVP-005A evidence. Missing evidence, concretely:
  (1) paired calibration n ≥ 100 with deltas holding negative; (2) *positive*
  gap follow-through — currently negative; (3) candidate-quality gaps (zero
  `paper_candidate_later` rows so far); (4) persistence >1 snapshot on
  meaningful gaps. Live/autonomous trading is not on the roadmap at all.

## 7. Crypto / memecoin lane

- **MEME-NEWS**: 133 runs/24h, 0 errors; 375 new tokens, 9,626 catalysts;
  attention p50 0.40 / p90 0.50; 2 severe-risk tokens flagged (avoid/review).
- **SolanaTracker Advanced**: **≈ $58–59/month** — the project's only paid
  provider. Usage: 960 requests today / 5,708 this month / 3,630 rolling-24h;
  est. monthly run-rate ~108,900 vs 200,000 limit; success rate 0.892;
  recommendation **KEEP** (well within budget). GoPlus is keyless/free.
- **Provider coverage** (observed over recent assessments): authority 100%;
  top10_holder / sniper / insider / bundler ≈ 50% (SolanaTracker-covered
  lookups only — the per-run cap of 15 means only a subset of tokens get
  provider lookups); **creator 0% — the known gap** (only Birdeye covers it and
  Birdeye is off pending payload validation). Recent observed rug/honeypot
  dimension rate 0% in the last 44 assessments (GoPlus covers these; observed
  window artifact — historical rug_flag data exists and drives findings below).
- **MEME-MAS v2** (24h): 375 tokens → low 11 / monitor 131 / elevated_review
  142 / high_review 89 / reject_risk 2; 46 tokens missing provider coverage.
- **MEME-SHADOW / objectives findings (24h, 3,429 anchors)** — the honest read:
  overall `calibration_recommendation` = **`no_material_separation_recalibrate`**
  — v2 `review_priority` does **not** materially separate 1h momentum outcomes
  (momentum-positive rate ~0.31–0.35 across all tiers). What *does* work:
  survival/risk separation — `elevated_review` survival 0.993 (v2 improved vs
  v1's 0.955), `reject_risk` catches rug incidence 0.49 (avoid-flag working),
  `rug_flag` reason → 0.93 rug incidence, `suspicious_volume` → 0.35, missing
  provider coverage predicts worse outcomes (survival 0.875 vs 0.965,
  price_1h −25% vs −3.7%). Queue efficiency: high_review holds 31% of the queue
  with only 1.08× momentum lift. **Conclusion: the lane is genuinely
  diagnostic/review-only — its labels are risk/survival triage, not momentum
  prediction, and its own measurement says so.**

## 8. Polymarket / cross-venue lane

- **POLY-001 observer**: manual-only (no timer, flag false). Latest scan:
  targeted mode, 400 markets / 100 order books / 0 errors, queries
  `[mlb, tennis, world cup, election]` derived deterministically from persisted
  Kalshi tickers/titles (no LLM). 450 market rows persisted, 14d retention.
- **POLY-002 matcher + POLY-PRECISION-001**: deterministic semantic matching
  with side-alignment (a Polymarket midpoint exists only when its outcome side
  is aligned to Kalshi's YES; `outcome_prices[0]` is a team name in ~26% of
  markets), market-type/threshold/entity/sport gates, and a
  large-difference review flag. Live A/B removed all false positives
  (comparable 9 → 2 on identical data; measured-gap p50 0.39 → 0.125).
- **XVENUE-OPS-001**: no-arg `cross-venue-match-once` is now recency-aware and
  representative (was 0 candidates on stale rowid order; now 389).
- **Latest verdict (XVENUE-OBS-001, current)**: **`overlap_no_clean_comparable`**
  — 389 candidates, comparable total 1 / clean 0 / flagged 1 (an ATP set-winner
  vs match-market pairing, gap 0.51, correctly flagged), side-uncertain 5.
  Dominant mismatch reasons: resolution_gap_days 351, entity_mismatch 176,
  outcome_type_mismatch 171, market_type_mismatch 87. Structural cause: Kalshi
  lists game-level props (winners/totals/spreads/props); Polymarket lists
  tournament futures + some game moneylines — **the venues rarely list the same
  proposition**. Best expected window: World Cup semifinals (Jul 9–11) game-
  winner ↔ game-winner, per `docs/XVENUE_OBSERVATION_RUNBOOK.md`.
- **Before any XMARKET-001** (whatever deeper cross-market observation would
  be): need ≥1 window with `clean_comparable_present`, repeated across slates,
  plus side-alignment holding on game markets (Kalshi encodes Yes-side in
  ticker suffixes — some pairs are structurally unalignable from titles).

## 9. Safety boundaries (hard, phase-independent)

**Forbidden — no implementation surface may exist, including "disabled"
placeholders** (docs/SAFETY_BOUNDARIES.md; enforced by canonical grep + AST
audits + the frontier safety scanner, currently `safety_ok=True`, 66 files):

EV calculation · trade recommendations · paper trading · portfolio sizing ·
order placement · wallets/private keys · swaps · transaction signing ·
execution/live trading · autonomous trading.

All external interaction is read-only GETs. Provider keys are header-only and
never printed. Every score/label in the system is measurement or human-review
triage with explicit "not advice" disclaimers in code and output. Unlocking any
forbidden capability requires its own explicitly-accepted milestone (MVP-005B
paper simulator is the first candidate and remains blocked on evidence).

## 10. Open decisions — questions for the reviewing model

1. **Next milestone**: OPS-014 (staged raw-retention reduction; unblocks
   ~storage headroom), XMARKET-001 (deeper cross-venue observation; currently
   supply-starved), MEME-SHADOW-002 (recalibrate review_priority toward the
   survival/risk axis it demonstrably predicts), or simply more calibration
   accumulation (baseball paired n 91 → 100+)? What ordering maximizes learning
   per unit of work?
2. **Are the evidence gates appropriately conservative?** e.g. OPS-014 needs
   coverage_72h ≥ 0.98 + 5 clean scheduled cycles; MVP-005B needs paired n ≥ 100
   *and* human acceptance. Too strict, too loose, or missing dimensions
   (e.g. should follow-through polarity be an explicit gate)?
3. **What metrics should justify a paper simulator?** Given gap follow-through
   is currently *negative*, what would you require: positive follow-through at
   which horizon/rate, what minimum n, what persistence distribution, what
   spread/liquidity validity profile? Should negative follow-through be treated
   as a signal to *invert* the interpretation (market leads forecast) — i.e. is
   the current gap measurement asking the wrong question?
4. **How should a future paper simulator be structured** so it cannot
   contaminate the read-only lanes — separate DB/schema, separate process,
   separate repo, event-sourced replay from persisted snapshots? What
   boundaries make "no execution ever escapes the sandbox" provable?
5. **SolanaTracker budget**: at ~$58–59/month, ~109k/200k run-rate, 89% success,
   and 50% observed coverage on holder dimensions (per-run cap 15) — keep as
   is, raise the per-run cap for higher coverage, or cut spend given the lane
   is diagnostic-only and its momentum value is unproven?
6. **Path to actual profitability while preserving safety**: given (a) baseball
   calibration edge is real but small, (b) follow-through is negative, (c) the
   memecoin lane predicts survival not momentum, (d) cross-venue has no clean
   comparables yet — which lane, if any, has a credible route to justified
   paper simulation, and what would you cut as not pulling its weight?

## 11. Our current internal view (for contrast with your review)

- Keep the tick-aggregation timer on; **do not** reduce raw retention until the
  OPS-014 gate passes (re-check after 2026-07-09 ~22:00 UTC), then propose
  OPS-014 as design-only, staged 3d → 2d first.
- Run the XVENUE observation runbook during the World Cup semifinal windows
  (Jul 9–11) — the best near-term chance of a first clean comparable.
- Continue MEME-MAS v2 outcome accumulation; treat MEME-SHADOW-002
  (recalibrating review_priority toward survival/risk, away from implied
  momentum) as the likely next meme-lane milestone given
  `no_material_separation_recalibrate`.
- Keep accumulating baseball paired-calibration outcomes toward n≥100; treat
  negative follow-through as a finding to investigate (measurement timing,
  market-type mix, persistence gating), not to rationalize away.
- No paper trading, no execution, no autonomy — unchanged. Any step toward
  MVP-005B requires the evidence in §6 plus explicit human acceptance.

---
*Capture provenance: all figures from `agent-context`, `marketops-report`,
`frontier-eval-report --hours 24 --include-crypto --include-safety`,
`db-growth-report`, `tick-aggregation-report`, `prune-retention --dry-run`,
`crypto-provider-budget-report`, `crypto-provider-health-report`,
`meme-news-report`, `meme-mas-report`, `meme-shadow-report --lookback-hours 24`,
`meme-mas-objectives-report --lookback-hours 24`, `polymarket-report`,
`cross-venue-report`, `xvenue-observation-report --top 20`, and
`systemctl --user list-timers/status`, run on EVO-X2 at 2026-07-09 ~05:45–06:15
UTC against commit `995afa5`. All commands succeeded.*
