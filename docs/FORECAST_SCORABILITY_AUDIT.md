# FORECAST-SCORABILITY-AUDIT-001 — read-only forecast scorability diagnostics

Answers whether Probability Arena's forecast inventory can become valid calibration
evidence: a per-forecast scorability state model, a scorability + status funnel,
cohort coverage/representativeness, temporal latency, and a deterministic verdict on
the binding calibration bottleneck.

**Measurement and data-quality analysis only.** It reads each forecast's latest
persisted outcome and latest persisted score and **reuses the deployed scoring
semantics** (`calibration._score_target`, `calibration.latest_score_for`,
`calibration.brier_score`, `outcomes.latest_outcome_for`). It never scores, never
syncs an outcome, never writes, never calls a provider, and changes no
`CalibrationService` behavior. No EV, side, size, order, recommendation, wallet, or
trading output exists by construction. It does **not** rebuild the existing
`calibration-report` performance tables (Brier by cohort) — the new value is the
*scorability funnel, representativeness, latency, and backlog/defect* analysis.

## Scorability state model

Each forecast is classified from its latest outcome + latest score, with the same
outcome-state semantics calibration uses. States (deterministic precedence):

| State | Meaning |
|---|---|
| `scored_current` | settled yes/no AND a scored row whose Brier matches the current outcome |
| `pending_no_outcome` | no outcome row synced yet |
| `pending_market_open` | outcome open, not settled |
| `pending_market_closed_unsettled` | outcome closed but not settled |
| `unscorable_canceled` | outcome canceled |
| `unscorable_unknown` | outcome unknown |
| `unscorable_void_or_missing_winner` | settled but winning side is void/unknown |
| `scorable_score_missing` | settled yes/no but NO score row exists — immediately scorable backlog |
| `scorable_score_stale` | settled yes/no but the latest score is pending/unscorable, or a scored row whose Brier no longer matches (an un-rescored yes↔no flip) |
| `pending_score_stale` | market not settled but the latest score says scored/unscorable |
| `unscorable_score_stale` | outcome canceled/unknown/void but the latest score says scored/pending |
| `state_inconsistent` | unrecognized outcome status / score status / winning side — surfaced, never normalized |

The eight mandatory distinctions (no-outcome vs open; open vs closed-unsettled;
settled yes/no vs void/unknown; no score vs stale score; correctly scored vs
immediately scorable; legitimately pending vs stale pending; legitimately unscorable
vs stale unscorable; internal inconsistency surfaced) are each a distinct state.

**Current-score semantics.** The latest score per forecast is the max-`id` row of the
append-only `forecast_scores` history. A score is *current* only when its
`score_status` equals the expected status from `_score_target(latest_outcome)` **and**,
for a settled market, its stored Brier equals `brier_score(p, y)` recomputed against
the current settled outcome — so an un-rescored yes↔no flip on the in-place outcome row
is caught as `scorable_score_stale`, never counted as evidence.

Six mutually-exclusive **status groups** partition the inventory: `currently_scored`,
`legitimately_pending`, `unscorable`, `scorable_backlog`, `stale_score_backlog`,
`inconsistent`.

## Funnels

Scorability funnel (nested, each step a subset): all forecasts → has local market
metadata → has research packet → has resolution assessment → has local outcome row →
outcome settled yes/no → latest score exists → latest score is current (valid scored
calibration row). Denominators stated explicitly. Pending/unscorable rows are never
counted as scored evidence.

## Cohorts and representativeness

Segmented by domain, forecaster+version, evidence depth, forecast risk,
research-completeness bucket (`missing_packet` / `0.00-0.49` / `0.50-0.74` /
`0.75-0.89` / `0.90-1.00`), research risk, resolution risk, tradeability, and forecast
age. Each cohort reports totals per status group, scorability rate, resolved/scored
sample, and concentration share of the whole scored dataset. A cohort with fewer than
`MIN_COHORT_SCORED` scored rows is labeled `too_thin` and its quality is not asserted.

Representation compares each cohort's share of *all* forecasts vs its share of *scored*
forecasts (delta in percentage points), labeled `too_thin` / `roughly_representative` /
`moderately_over|underrepresented` / `strongly_over|underrepresented`. These are
dataset-composition labels only — never market performance or opportunity.

## Latency

Forecast creation → close, creation → settlement, close → settlement, settlement →
current score, creation → current score. Each reports count / median / p75 / p90 / max
/ missing denominator. Negative or impossible durations are reported as explicit
data-quality findings, never silently clamped. UTC tz-aware; SQLite naive round-trips
are coerced to UTC.

## Verdict

One primary verdict via documented conservative thresholds (module constants), scored
over the *matured-eligible* population (forecasts whose market has already closed):

`HEALTHY_SCORABILITY_PIPELINE` · `FORECAST_INVENTORY_TOO_IMMATURE` ·
`OUTCOME_SYNC_COVERAGE_IS_THE_BLOCKER` · `SETTLEMENT_LATENCY_IS_THE_BLOCKER` ·
`SCORING_BACKLOG_IS_THE_BLOCKER` · `STALE_SCORE_STATE_IS_THE_BLOCKER` ·
`UNSCORABLE_OUTCOME_RATE_IS_THE_BLOCKER` · `SCORED_SAMPLE_IS_NOT_REPRESENTATIVE` ·
`MULTIPLE_SCORABILITY_BLOCKERS` · `INSUFFICIENT_DATA`.

Precedence: below `MIN_TOTAL_FORECASTS` → `INSUFFICIENT_DATA`; if too few markets have
closed → `FORECAST_INVENTORY_TOO_IMMATURE`; else each blocker (stale, backlog,
outcome-sync, settlement, unscorable) is tested at its threshold — ≥2 →
`MULTIPLE_SCORABILITY_BLOCKERS`, exactly one → that verdict; else a healthy scored rate
→ `HEALTHY` (or `SCORED_SAMPLE_IS_NOT_REPRESENTATIVE` if a major cohort is strongly
skewed with adequate scored sample). The verdict may *recommend* one subsequent
measurement milestone but cannot authorize or execute it, and changes no gate, flag,
score, or production behavior.

## CLI

```bash
forecast-scorability-audit-report \
  [--hours N | --since <ISO-UTC>] [--until <ISO-UTC>] \
  [--domain D] [--forecaster F] [--top N] [--format text|json]
```

`--since` overrides `--hours`; `--until` defaults to now; timestamps are UTC; an
invalid window (`since > until`) fails clearly (exit 2); truncation is reported; text
and JSON derive from the same result object. Output carries a measurement-only
disclaimer, `external_calls=0`, and `persisted=false`. The command makes zero provider
calls, performs zero DB writes, triggers no outcome sync or scoring, persists no report
row, and installs no unit.

## Deployment note

Because the CLI wiring touches the frozen `app/cli.py` (re-imported by the live
`marketops-run-once` process), this milestone is **branch-only**: it is committed to
`worktree/forecast-scorability-audit` and available for review, but **must not be merged
to `main` or deployed to EVO-X2 before the 2026-07-23 candidate-readiness checkpoint**.
EVO-X2 stays pinned at `3f742c9` until then.
