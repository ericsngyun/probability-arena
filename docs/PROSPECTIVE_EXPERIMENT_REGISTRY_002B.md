# PROSPECTIVE-EXPERIMENT-REGISTRY-002B — result enforcement

**Status:** implemented, reviewed, dark-deployed. **No experiment registered; no
result recorded.**

002A fixed *who is in the cohort*. This fixes *what may be concluded about them*.

---

## 1. The principle

The registry computes the result. The caller supplies an experiment id and,
optionally, prose. It cannot supply a population, a metric value, a sample
count, an end time or a verdict — each is a place where a null result becomes a
positive one, and a registry that accepts them from the person who wants the
answer is a filing cabinet, not a control. A signature test asserts the
parameter list of `evaluate_experiment` stays that way.

## 2. Ordering — the control is the order

```
reconstruct membership   (forecast-time fields ONLY)
→ freeze membership digest
→ attach outcome/score state
→ compute maturity
→ compute metrics
→ derive verdict
→ append an immutable, hash-chained result
```

Outcomes are unreachable until membership is frozen, so a cohort cannot be
shaped by what it would score. A test flips **every** outcome and asserts the
membership digest does not move while the metric does.

## 3. Carried-forward findings from 002A, resolved

| # | finding | resolution |
|---|---|---|
| 1 | A truncated `events.jsonl` prefix remained a valid hash chain | `head.json` pins `event_count` + `terminal_event_hash`, updated in the same operation as every append |
| 2 | `none`-clause NULL asymmetry unresolved | explicit three-valued logic (§5) |
| 3 | `market_ticker in [...]` latent cohort channel | confirmatory experiments need a committed pre-registration universe artifact (§6) |
| 4 | Population reconstruction had no production caller | it is now the first step of every evaluation |
| 5 | Metrics, floors, maturity, baselines, stopping rules unenforced | §7–§10 |
| 6 | No append-only result path | §11 |

## 4. Event-log truncation hardening

A prefix of a valid hash chain is itself a valid chain, so deleting the last
event silently rolled state backwards and let an experiment advance again.
`head.json` records the expected count and terminal hash.

Detected and failing closed: **suffix truncation**, **middle deletion**,
**append without head update**, **missing head**. Zero-event and zero-result
states are explicit and distinguishable from a deleted history.

This is not tamper-**proof** — anyone with write access can rewrite both files.
It is tamper-**evident**, and the evidence lands in a git diff. Overstating that
would be the same error as the prose blocklist.

## 5. NULL semantics

**A predicate matches only when its truth value is explicitly TRUE.**

`forecaster_version not_eq "v2"` against a NULL version is **UNKNOWN, not True**.
Otherwise a rule written to *narrow* a cohort silently widens it — every row with
a missing field joins through the author's own restriction. Including missing
values requires an explicit `not_exists`, which is a declaration rather than an
accident.

- `all` requires TRUE — unproven means **not met**, so the member is excluded.
- `none` vetoes on TRUE only — unproven means **not vetoed**, so the member stays.

The asymmetry is deliberate: `all` is a requirement, `none` is a veto, and
neither acts on an unknown. **De Morgan does not hold across unknowns**:
`none: [x eq "a"]` is not the same population as `all: [x not_eq "a"]` when `x`
can be NULL.

## 6. Identifier-cohort control

Confirmatory experiments may not enumerate `market_ticker` unless the set comes
from a committed pre-registration universe artifact carrying its own digest,
creation time, member count and a **non-result-derived** selection method
(rejected if it mentions performance, best, top, winning, beat, highest, lowest,
Brier or score). Exploratory experiments may — and cannot make a confirmatory
claim. No wildcard, substring, regex or dynamic selection on any identifier.

## 7. Supported metrics

`mean_brier` and `brier_skill_vs_base_rate` as primaries; `base_rate_brier` as
the only baseline; `ece` and `murphy_components` as descriptive secondaries.

All reuse the deployed canonical implementations — `calibration.brier_score` and
the reliability decomposition — rather than restating the formulas. A test
asserts the module contains no exponentiation, because a second implementation
is a second answer.

**Sign convention.** `mean_brier` is better when lower, so the delta is reported
as `baseline − model`; `brier_skill` is already relative. A positive delta always
means "better than baseline", which is what every decision rule assumes.

## 8. Confidence intervals

`cluster_bootstrap_by_market_v1`, version 1, 2000 samples, **fixed seed
20260806**, α = 0.05.

Clustered by market because forecasts on one market share an outcome; treating
them as independent understates the interval, badly, in a population dominated by
one domain with many forecasts per market. The seed and sample count are
constants: an evaluator who could reroll could keep rolling until an interval
cleared zero.

## 9. Sample floor, maturity and stopping

Closed stopping vocabulary — `fixed_sample_and_end` with `minimum_sample`,
`minimum_matured_fraction`, `not_before`, `not_after`. Prose is never executable.

Below the floor, or with the stopping rule unmet, `supports_hypothesis` and
`does_not_support_hypothesis` are **unreachable**. Pending and unscorable members
are counted and reported, never dropped from the denominator.

## 10. Verdict derivation

Registry-derived, fixed precedence, and every branch above the last returns
**without ever looking at the number**:

1. broken registry/event integrity → evaluation refused
2. material unresolved drift → `invalidated_protocol_deviation`
3. data-quality invalidation → `invalidated_data_quality`
4. stopping rule unmet → `still_collecting`
5. floor unmet at a valid terminal end → `inconclusive_sample_floor`
6. otherwise the registered typed decision rule against the registered metric

Protocol deviations additionally downgrade any favorable verdict.

### The degenerate base rate

A test forced this out: when every member wins, prevalence is 1.0, the base-rate
Brier is 0, and **the baseline is unbeatable**. That is exactly the artifact that
produced soccer's Brier of 0.0033 on 34 members at 2.9% prevalence, reported as a
domain result when it was a property of the sample. It is now an explicit
data-quality invalidation — **at terminal state only**, since mid-collection a
lopsided prevalence is an ordinary small-sample artifact that later observations
will move.

A second thing worth recording because it is easy to get backwards: **a constant
forecast can never beat the base rate**, because the base rate is the optimal
constant. Skill requires discrimination, not confidence.

## 11. Append-only results

`results/<stamp>-<digest>.json`, `result-events.jsonl`, `result-head.json`.
Hash-chained and head-pinned like the event log. A result file is never
overwritten (the writer refuses an existing path), re-evaluation **requires an
explicit reason** and links the prior digest, and the first terminal result is
preserved.

## 12. Drift

Extended with `material_metric_code`, `material_baseline`,
`material_stopping_rule`. Metric-code digests are pinned at registration across
`calibration.py`, `forecast_reliability.py` and `experiment_results.py`. Missing
references are `unknown`, never `none`, and `unknown` cannot produce a favorable
verdict.

## 13. Draft result contracts — still drafts

| experiment | primary | baseline | decision rule | floor | matured |
|---|---|---|---|---|---|
| `baseball-prospective-calibration-stability` | `mean_brier` | `base_rate_brier` | `primary_metric_delta_gt_zero` | 500 | 0.9 |
| `soccer-prospective-reliability` | `brier_skill_vs_base_rate` | `base_rate_brier` | `primary_metric_delta_gt_zero` | 300 | 0.9 |
| `tennis-base-rate-falsification` | `brier_skill_vs_base_rate` | `base_rate_brier` | `primary_metric_delta_lt_zero` | 200 | 0.9 |

Tennis remains a **falsification**: its decision rule is
`primary_metric_delta_lt_zero`, so "supports" means skill is *not* positive. The
negative finding is deliberately not reworded into a promotion objective.

## 14. What this milestone does NOT do

It does not register anything and records no result. Registration is 002C.

## 15. Rollback

Revert the commits. No migration, no schema change, no production write, no
MarketOps change. Nothing is registered, so nothing is orphaned.
