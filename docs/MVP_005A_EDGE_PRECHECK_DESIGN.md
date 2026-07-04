# MVP-005A — Edge Precheck: Design + Safety Review

**Status:** ACCEPTED + IMPLEMENTED (MVP-005A, 2026-07-04) — see
"Implementation notes" at the end for the deltas between this design and
the shipped implementation. Paper simulation (MVP-005B) remains a separate,
explicitly-gated future milestone.

**Gate evidence (verified 2026-07-04 on EVO-X2 live data):** baseball
champion/challenger paired comparison crossed the required gate —
paired n=36 (≥30), mean_delta_brier=−0.0493 (<0),
mean_delta_log_loss=−0.1525 (<0), wins 12 / losses 3 / ties 21, sample label
`early_signal`. The challenger (`baseball_evidence_v1`) demonstrably beats
the market-anchored baseline on resolved outcomes at an early-signal sample.

## 1. Hard boundary (read this before the design)

The edge precheck is **measurement, not advice**. It compares a calibrated
forecast probability against the market's current price and records the gap
with validity checks. It must never:

- calculate dollar expected value (no payout math, no fees, no PnL units —
  the only quantity is a **probability difference**);
- recommend a trade, a side, a direction, or an action;
- size a position or reference bankroll/stake/portfolio concepts;
- place, simulate, or prepare orders;
- touch wallets, keys, swaps, transactions, or execution of any kind;
- feed any automated downstream action — its outputs are rows a human reads.

`paper_candidate_later` (§5) is a **filing label meaning "if MVP-005B is
someday accepted, this snapshot would have been worth simulating."** It is
not a trade recommendation, not a paper trade, and carries zero behavior.
The safety grep vocabulary (wallet/order/EV/sizing/recommendation terms)
must stay out of the implementation surface exactly as it is today.

## 2. Concept

```
probability_gap = forecast.estimated_probability − market_midpoint
```

- Signed, in probability units (−1..+1). Positive means the forecast is
  above the market price; negative means below. **The sign is recorded, but
  no directional language ("buy YES") may ever be attached to it.**
- `market_midpoint = (yes_bid + yes_ask) / 2 / 100` (dollars 0..1), exactly
  as the watcher already computes it.
- A gap is only *meaningful* when the forecast is source-backed and every
  validity check passes; otherwise the snapshot records why it is invalid.
  Honest invalidation is the core design value: most snapshots should fail.

## 3. Inputs (all existing rows; no new data collection)

| Input | Source | Used for |
|---|---|---|
| Latest **source-backed** forecast | `market_forecasts` (evidence_depth='source_backed'; forecaster identity + `created_at` recorded) | `estimated_probability`, `confidence`, `forecast_risk`, staleness |
| Latest market snapshot / fresh quote | `market_snapshots` (or a fresh read-only quote via the existing adapter) | `yes_bid`, `yes_ask`, midpoint, spread |
| Liquidity proxy | same snapshot (`liquidity`, cents of resting notional) | `invalid_low_liquidity` |
| Latest resolution assessment | `market_resolution_assessments` | `invalid_resolution_risk` (tradeability must be `researchable`) |
| Forecast confidence + risk | forecast row (`confidence`, `forecast_risk`) | `invalid_low_confidence` |
| Signal link (optional) | `opportunity_signals.id` when the forecast came from a promoted-signal refresh | provenance/audit |

## 4. Proposed table (NOT created in MVP-005A)

`edge_precheck_snapshots` — append-only audit rows, one per evaluation:

| Column | Type | Notes |
|---|---|---|
| id | int PK | |
| market_ticker | str, indexed | |
| forecast_id | FK market_forecasts | the forecast evaluated |
| signal_id | FK opportunity_signals, nullable | provenance when signal-driven |
| market_midpoint | float nullable | dollars 0..1 |
| yes_bid / yes_ask | int nullable | cents, as elsewhere |
| spread | int nullable | cents |
| liquidity_proxy | int nullable | cents |
| probability_gap | float nullable | signed; null when midpoint unavailable |
| status | str, indexed | §5 vocabulary |
| invalidation_reasons | JSON list | ALL failed checks, not just the first |
| forecast_age_seconds | int | staleness at evaluation time |
| confidence / forecast_risk / evidence_depth | copied from forecast | denormalized for cohort queries |
| created_at | datetime | |

No EV column, no side, no direction, no size, no notional — by design there
is nowhere to put them.

## 5. Status vocabulary

| Status | Meaning |
|---|---|
| `no_gap` | all checks passed, but abs(gap) < threshold — market and forecast agree |
| `watchlist` | all checks passed and abs(gap) ≥ threshold — worth *watching* whether the gap closes toward the forecast (calibration insight) |
| `invalid_wide_spread` | spread above threshold: midpoint is not a trustworthy price |
| `invalid_low_liquidity` | liquidity proxy below threshold: quote is not real depth |
| `invalid_low_confidence` | forecast confidence below threshold (caps already limit this) |
| `invalid_stale_forecast` | forecast older than threshold: the game state has moved on |
| `invalid_resolution_risk` | latest resolution assessment is not `researchable` |
| `paper_candidate_later` | `watchlist` + persistence criteria (§6) — a filing label for a *possible future* MVP-005B review. **Not a recommendation; no behavior attaches.** |

Multiple failures record every reason in `invalidation_reasons`; `status`
carries the first failure in the order listed above (deterministic).

## 6. Provisional thresholds (PROVISIONAL — to be tuned against
edge_precheck_snapshots data itself before anything downstream exists)

| Threshold | Provisional value | Rationale |
|---|---|---|
| min abs(probability_gap) | 0.05 | below a nickel of probability, noise dominates |
| max spread | 10¢ | tighter than the watcher's 15¢ band; midpoint quality matters more here |
| min liquidity proxy | 500¢ | 5× the watcher's floor; thin books produce fake midpoints |
| min confidence | 0.60 | above the template ceiling (0.55) — only evidence-backed confidence qualifies |
| max forecast age | 900 s | one watcher cooldown window; live-game forecasts stale fast |
| resolution | `researchable` only | unchanged from research/forecast gates |
| `paper_candidate_later` promotion | gap persists across ≥3 consecutive snapshots ≥5 min apart with all checks passing | single-snapshot gaps are usually latency artifacts |

## 7. Read-only surfaces (design)

- CLI `edge-precheck --limit N` (evaluate latest source-backed forecasts),
  `edge-precheck-report` (status counts, gap distribution, invalidation
  reason frequencies, watchlist list).
- API `GET /edge-precheck/snapshots`, `GET /edge-precheck/report`.
- MarketOps integration (later, behind `ENABLE_EDGE_PRECHECK=false`): one
  precheck pass per cycle after forecasts refresh; an informational
  `watchlist_gap_persisting` alert type. No autopilot behavior may branch on
  gap sign or size.

## 8. Tests required before any implementation merges

- gap math: signed direction, null midpoint → null gap + honest status
- every invalidation status triggers on its threshold boundary, and
  `invalidation_reasons` collects ALL failures
- deterministic status precedence order
- `no_gap` vs `watchlist` boundary at the gap threshold
- `paper_candidate_later` requires persistence (single snapshot never
  qualifies) and never fires when any check fails
- stale-forecast and non-researchable paths
- API serialization excludes nothing sensitive but contains no
  EV/side/size fields (schema-shape test)
- safety grep stays clean; a dedicated test asserts the module contains no
  banned vocabulary as identifiers
- migration up/down; existing suites untouched

## 9. Explicit non-goals (restating the boundary as scope)

No dollar EV. No fees/payout modeling. No Kelly or any sizing math. No
trade/paper-trade objects. No order concepts. No wallet/key/swap/signing
surface. No autonomous consumption of the outputs. MVP-005B (paper
simulator) remains a separately-gated milestone that requires explicit
acceptance of this design first, then its own review.

## 10. Acceptance checklist for this design

- [ ] Human review of §5 statuses and §6 provisional thresholds
- [ ] Agreement that `paper_candidate_later` semantics are label-only
- [ ] Agreement on the deterministic invalidation precedence
- [ ] Sign-off recorded in ROADMAP before an implementation milestone is cut

---

## Implementation notes (MVP-005A, implemented)

- Statuses extended from this design per the implementation spec: added
  `invalid_stale_market_snapshot` and `invalid_not_source_backed` as their
  own statuses (the design had folded them into other checks); precedence
  as implemented: resolution → not_source_backed → stale_forecast →
  stale_market_snapshot → low_confidence → wide_spread → low_liquidity →
  no_gap → watchlist → paper_candidate_later.
- `market_snapshot_id` references `market_price_ticks` (the watcher's 60s
  quote stream) — much fresher than the 4h scanner snapshots and the right
  price source for the 120s snapshot-age threshold.
- Live-sports forecasts (domains sports_*) use the tighter
  `EDGE_PRECHECK_MAX_LIVE_SPORTS_FORECAST_AGE_SECONDS=300`.
- Persistence: 1 + streak of immediately-prior watchlist/candidate
  snapshots for the same (ticker, forecaster) with the same gap sign; any
  other row (including any invalid measurement) breaks the streak.
- §10 checklist accepted 2026-07-04 (implementation authorized by the
  MVP-005A implementation directive).
