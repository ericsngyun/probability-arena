# Post-drain outcome and reliability baseline — 2026-08-05

**The backlog is fully drained.** Outcome coverage is 100%, every forecast has a
current score, and the scored sample is — for the first time — statistically
indistinguishable from the forecast population it is drawn from.

That last point is the milestone. Before this, forecast quality was being
measured on 903 forecasts selected by an accident of alphabetical and id
ordering. It is now measured on 12,889 with every domain represented within
0.21 percentage points of its true share.

---

## 1. Timeline

| | |
|---|---|
| repair dark-deployed | 2026-08-04, `a82d6c8` |
| activated | 2026-08-05T01:01:56Z, `MARKETOPS_SCORE_LIMIT=100` |
| first active cycle | 7730, 01:07:03Z |
| latest cycle at checkpoint | 7850, 13:02:03Z |
| elapsed | **11.92 h** |
| checkpoint floor | 13:00Z — observed, not pre-empted |

## 2. State verification (Gate 1)

Mac = origin = EVO-X2 = **`c711429`**, Alembic **0027**, no tracked drift.
`ENABLE_OUTCOME_SYNC_COVERAGE_REPAIR=true`, `MARKETOPS_SCORE_LIMIT=100`,
`MARKETOPS_SYNC_OUTCOME_LIMIT=100`. No failed units. Backup **healthy**
(`backup-20260805T013926Z`). Host free 93.99 GB.

| | at activation | now |
|---|---:|---:|
| database bytes | 4,550,623,232 | **4,550,623,232** |
| page count | 1,110,992 | 1,110,992 |
| freelist pages | 312,994 | **290,440** |
| lock events (lifetime) | 4 | **4** |

~22,554 freelist pages (~92 MB) absorbed 12 hours of sustained writing with
**zero file growth and zero new lock events**.

## 3. Drain-window integrity (Gate 2)

| | |
|---|---:|
| completed cycles | **121** |
| successful | **121** |
| failed | **0** |
| cycles with stage errors | **0** |
| manually triggered cycles | **0** |
| median duration | 42,807 ms |
| p95 duration | 67,576 ms |
| max duration | 79,023 ms |
| outcome provider calls | 12,100 |
| provider failures | **0** |
| score candidates processed | 11,100 |
| → scored | 9,911 |
| → pending_outcome | 1,189 |
| → unscorable | 0 |
| → **skipped** | **0** |
| lock events / retry exhaustion | 0 / 0 |
| backup, readiness, anchor-feed errors | 0 |

**`skipped = 0` across all 121 cycles** is the cleanest possible statement that
the selector is doing real work. The last pre-activation cycle skipped 1,000 of
1,000.

p95 and max duration (67.6 s / 79.0 s) sit above the pre-activation band of
40–64 s but well inside the 120 s stop threshold. The rise is consistent with
scoring 100 forecasts per cycle instead of zero.

## 4. Outcome coverage (Gate 3)

| | before | after |
|---|---:|---:|
| all forecasts | 12,759 | 13,077 |
| markets closed | 11,661 | 12,083 |
| matured eligible | 11,617 | 12,015 |
| outcome row present | 1,770 | **12,015** |
| settled yes/no | 1,684 | **12,015** |
| **matured coverage** | **14.5%** | **100.0%** |
| missing outcome | 9,933 | **0** |
| `sync_never_attempted` | 9,847 | **0** |
| `local_outcome_stale` | 86 | **0** |
| canceled / void / winner missing / ambiguous | 0 | **0** |
| conflicts / mapping failures / provider failures | 0 | **0** |
| permanently unscorable | 0 | **0** |
| verdict | `OUTCOME_SYNC_SELECTION_IS_THE_BLOCKER` | **`COVERAGE_HEALTHY`** |

Coverage is 100% in **every** domain and **every** close-age bucket:

| domain | matured | usable | coverage |
|---|---:|---:|---:|
| sports_baseball | 10,148 | 10,148 | 100.0% |
| sports_soccer | 1,439 | 1,439 | 100.0% |
| general | 290 | 290 | 100.0% |
| sports_tennis | 108 | 108 | 100.0% |
| politics | 30 | 30 | 100.0% |

| close age | matured | usable | coverage |
|---|---:|---:|---:|
| <2 d | 1,039 | 1,039 | 100.0% |
| 2–7 d | 2,361 | 2,361 | 100.0% |
| 7–30 d | 8,521 | 8,521 | 100.0% |
| >30 d | 94 | 94 | 100.0% |

The missing-reason taxonomy is **empty**. There is no remaining population that
is draining, unresolved, unscorable, mapping-blocked, provider-blocked or
starved — the classification has nothing left to classify.

## 5. Scoring drain (Gate 4) — **FULLY_DRAINED**

| | before | after |
|---|---:|---:|
| distinct scored forecasts | 1,000 | **12,945** |
| min / max scored forecast id | 1 / 1,000 | 1 / **12,945** |
| max forecast id | 12,759 | 12,945 |
| forecasts with **no** score row | 11,759 | **0** |
| scored_current | 903 | **12,889** |
| stale scores | 0 | **0** |
| inconsistent | 0 | **0** |
| duplicate current scores | 0 | **0** |
| canceled/void incorrectly scored | 0 | **0** |
| conflicts incorrectly scored | 0 | **0** |
| forecasts re-scored (>1 row) | — | 2,055 |

`distinct scored == max forecast id == total forecasts == 12,945`. Not
"drained to the arrival rate" — **actually complete**, with the id-prefix wall
gone. The 188 `pending_market_open` rows are forecasts on markets that have not
closed yet, which is the correct state, not a backlog.

The 2,055 forecasts carrying more than one score row are the append-only
currency repair working: a `pending_outcome` row superseded by a `scored` row
once the outcome arrived.

## 6. Selection efficiency (Gate 5)

| | |
|---|---:|
| productive outcome calls / total | **12,100 / 12,100 = 100%** |
| productive scoring selections / total | **11,100 / 11,100 = 100%** |
| terminal refetch rate | **0%** (was 100%) |
| skip rate | **0%** (was 100%) |
| unreachable selected items | **0** |
| provider cap respected | yes — exactly 100/cycle |
| score limit respected | yes — exactly 100 candidates/cycle |

Steady state: candidate pool **75**, full sweep **0.1 h**. The selector has
caught up with arrivals and is now tracking them, not chasing a backlog.

Rotation state uses `MAX(marketops_runs.id)`, so it remains correct under the
30-day prune that `retention_coverage` recommends for that table.

## 7. Scorability comparison (Gate 6)

| | before | after | class |
|---|---:|---:|---|
| forecasts | 12,759 | 13,077 | `population_expansion` |
| matured eligible | 11,923 | 12,889 | `population_expansion` |
| scored_current | 903 | **12,889** | `population_expansion` |
| legitimately pending | 10,801 | **188** | `pipeline_health_change` |
| scorable backlog | 1,055 | **0** | `pipeline_health_change` |
| stale-score backlog | 0 | 0 | — |
| inconsistent | 0 | 0 | — |
| unscorable | 0 | 0 | — |
| verdict | `OUTCOME_SYNC_COVERAGE_IS_THE_BLOCKER` | **`HEALTHY_SCORABILITY_PIPELINE`** | `pipeline_health_change` |

Blockers: **`[]`**. State histogram: `scored_current 12,889`,
`pending_market_open 188`. No impossible-timestamp findings.

### Representation — the result that matters most

| axis | worst |Δ| before | worst |Δ| after |
|---|---:|---:|
| domain | 9.14 pp (baseball) | **0.21 pp** |
| evidence depth | — | **0.15 pp** |
| forecaster/version | — | **0.14 pp** |

Every cohort on every measured axis is now `roughly_representative`. The scored
sample **is** the population. `composition_shift`.

## 8. Reliability comparison (Gate 7)

Sample size equals `scored_current` (12,889) as required, and stale rows cannot
enter — there are none.

| | before | after | class |
|---|---:|---:|---|
| sample size | 903 | 12,889 | `population_expansion` |
| prevalence | 0.3743 | 0.4258 | `composition_shift` |
| mean Brier | 0.180043 | **0.190836** | `composition_shift` |
| neutral baseline | 0.25 | 0.25 | — |
| base-rate baseline | 0.234201 | 0.244493 | `composition_shift` |
| skill vs neutral | 0.2798 | 0.2367 | `composition_shift` |
| skill vs base rate | 0.2312 | **0.2195** | `composition_shift` |
| ECE | 0.0464 | **0.0263** | `composition_shift` |
| MCE (measured) | 0.1650 | **0.0452** | `composition_shift` |
| Murphy reliability | 0.003585 | **0.000843** | `composition_shift` |
| Murphy resolution | 0.058839 | 0.053728 | `composition_shift` |
| Murphy uncertainty | 0.234201 | 0.244493 | `composition_shift` |
| reconstruction residual | +0.001096 | −0.000772 | — |
| populated / too-thin bins | 10 / 0 | 10 / 0 | — |
| verdict | `DOMAIN_HETEROGENEITY_DOMINATES` | `MULTIPLE_RELIABILITY_FINDINGS` | — |

Post-drain findings: `DOMAIN_HETEROGENEITY_DOMINATES` **and**
`COMPOSITION_SHIFT_DOMINATES`. The tool is telling us, correctly, that the
change in its own inputs dominates the change in its outputs.

**The Brier got worse and that is the honest number.** The old sample was
easier: uncertainty rose 0.2342 → 0.2445 because prevalence moved from 0.374
toward 0.5. Skill against the *appropriate* baseline fell only 0.2312 → 0.2195
— a 5% relative move — while calibration error fell by 43% (ECE) and 73% (MCE).
The pipeline did not get worse; the measurement got honest.

## 9. Domain findings (Gate 8)

| domain | matured | usable | cov | scored | prev | Brier | base | skill | ECE | MCE | share | limitation |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| sports_baseball | 10,148 | 10,148 | 100% | 10,989 | 0.4392 | 0.1950 | 0.2463 | **+0.2082** | 0.0281 | 0.0470 | 85.3% | dominates every aggregate |
| sports_soccer | 1,439 | 1,439 | 100% | 1,442 | 0.2975 | 0.1576 | 0.2090 | **+0.2459** | 0.0713 | 0.1926 | 11.2% | MCE still high |
| general | 290 | 290 | 100% | 299 | 0.4649 | 0.1822 | 0.2488 | **+0.2674** | 0.1088 | 0.3118 | 2.3% | poorly calibrated tails |
| sports_tennis | 108 | 108 | 100% | 129 | 0.5891 | 0.2510 | 0.2421 | **−0.0368** | 0.2138 | 0.4979 | 1.0% | modest n; worst calibration |
| politics | 30 | 30 | 100% | 30 | 0.6000 | 0.0867 | 0.2400 | **+0.6388** | 0.2423 | 0.4300 | 0.2% | **too thin — do not rank** |

### Tennis reassessment — the prior negative-skill finding

| | prior | baseline (n=67) | now (n=129) |
|---|---|---|---|
| skill vs base rate | negative on n=52 | **−0.1427** | **−0.0368** |
| Brier | — | 0.280513 | 0.250958 |
| ECE / MCE | — | 0.301 / 0.450 | 0.2138 / 0.4979 |

**It persists and attenuates.** Sample nearly doubled and skill moved from
−0.1427 toward zero, but remains negative: tennis is still the only domain that
fails to beat its own base rate. It is no longer a selected-sample artifact —
tennis is now `roughly_representative` (1.00% scored vs 0.99% of all). Its
calibration is by far the worst on record (MCE 0.4979).

Verdict: **`credible_current_finding`** that tennis does not beat base rate,
with the caveat that n=129 is modest and the effect is attenuating.

### Soccer reassessment — the prior figure was an artifact

| | baseline (n=34) | now (n=1,442) |
|---|---|---|
| Brier | **0.003297** | 0.157593 |
| prevalence | **0.0294** | 0.2975 |
| base-rate Brier | 0.028547 | 0.208995 |
| skill vs base rate | **+0.8845** | +0.2459 |
| populated bins | **3** | 10 |

**`contradicted_by_expanded_sample`.** The baseline sampled 34 soccer forecasts
whose outcomes were 97% "no", producing a near-degenerate base rate and a
Brier of 0.0033 across three populated bins. That was never a measurement. On a
42× larger, representative sample soccer looks like a normal, good domain — its
+0.2459 skill is the strongest of the three non-thin domains, which is a real
finding, just an order of magnitude less dramatic than the artifact suggested.

### Other cohorts

| axis | cohort | n | Brier | skill |
|---|---|---:|---:|---:|
| forecaster | baseball_evidence:v1 | 7,983 | 0.1868 | +0.2286 |
| forecaster | template_baseline:v1 | 4,615 | 0.1996 | +0.1958 |
| forecaster | soccer_evidence:v1 | 291 | 0.1616 | +0.2434 |
| evidence depth | source_backed | 8,298 | 0.1854 | +0.2316 |
| evidence depth | template_only | 4,591 | 0.2006 | +0.1923 |
| forecast risk | low | 2,728 | 0.1524 | +0.3376 |
| forecast risk | medium | 9,878 | 0.2004 | +0.1889 |
| forecast risk | high | 283 | 0.2285 | **+0.0351** |

Source-backed evidence beats template-only on a large representative sample
(+0.2316 vs +0.1923). Self-declared `high` forecast risk barely beats base rate
(+0.0351), which is at least directionally coherent.

Too thin to rank, listed for completeness: `research_completeness 0.00–0.49`
(n=11), `research_risk medium` (n=11), `resolution_risk medium` (n=16),
`tradeability needs_manual_review` (n=5), and **politics (n=30)**.

## 10. Calibration state (Gate 9)

`total=13,077  resolved=12,889  pending=188  unscorable=0`, overall Brier
0.190836, log loss 0.558614, absolute error 0.387787.

Champion/challenger: three forecaster families are present
(`baseball_evidence:v1` n=7,983, `template_baseline:v1` n=4,615,
`soccer_evidence:v1` n=291). They are **not** a paired head-to-head sample —
each family forecasts a different market population — so the Brier differences
between them are `composition_sensitive`, not a ranking. A genuine paired
comparison needs `champion-challenger-report` on markets both families covered.

No stale-score exclusions were necessary because there are none.

**The new aggregate must not be compared with the old one directly.** They
describe different populations: n=903 selected by id/alphabetical accident
versus n=12,889 representative. §8 states the composition changes explicitly.

## 11. Statistical interpretation (Gate 10)

**Pipeline question — did the repair produce a materially less selected scoring
population?** `credible_current_finding`. Coverage 14.5% → 100%, scored
population 903 → 12,889, worst domain representation gap 9.14 pp → 0.21 pp,
zero starvation, zero stale contamination.

**Forecast-quality question — what can now be said?** That the system beats a
base-rate benchmark by **+0.2195** Brier skill on a representative sample of
12,889, with ECE 0.0263 and MCE 0.0452. `credible_current_finding` — and the
first time that phrase has been available for an aggregate here. It says
nothing about profitability, edge or tradability, and no such claim is made.

**Domain question — which domains have enough current, representative data for
prospective research?** `sports_baseball` (n=10,989) and `sports_soccer`
(n=1,442): `credible_current_finding`. `general` (n=299): `descriptive_only` —
adequate n but ECE 0.1088 / MCE 0.3118. `sports_tennis` (n=129): usable for a
narrow negative result only. `politics` (n=30): `too_thin`.

**Negative-result question — which fail to beat a reasonable base rate?**
`sports_tennis`, skill −0.0368 on n=129, calibration the worst measured:
`credible_current_finding`, attenuating. Marginal: self-declared `high`
forecast risk (+0.0351, n=283) and the 2026-08-03 daily slice (−0.0009, n=210)
— both `descriptive_only`.

**Remaining-bias question.** Four, stated plainly:

1. **Baseball dominance.** 85.3% of the scored sample. Every aggregate is
   substantially a baseball measurement. Faithful to the population, but it
   means "overall Brier" is close to "baseball Brier".
2. **Temporal cohorts are domain-degenerate.** Recent daily slices run
   `top_domain_share` 0.99–1.00, so day-over-day comparisons are baseball-only
   comparisons wearing a date label. `composition_sensitive`.
3. **Thin domains stay thin.** Tennis and politics are representative *and*
   small; coverage cannot fix a domain the scanner rarely sees.
4. **Prevalence is not stationary.** 0.3743 → 0.4258 over the drain, so the
   base-rate benchmark itself moves. Any longitudinal claim must re-baseline.

One anomaly I did not chase and will not paper over: the reliability report's
`directional` block now shows `overprediction_weighted_share = 0.0` **and**
`underprediction_weighted_share = 0.0`, where the baseline had 0.1517 / 0.3909,
while `signed_calibration_gap` is a non-zero 0.0158. Both being exactly zero is
not credible. It affects no conclusion in this document — the ECE/MCE/Murphy
figures are computed independently — but it looks like a defect in that block
and should be investigated before anyone relies on those two fields.
`insufficient_evidence` pending that check.

## 12. Score-limit operating decision (Gate 11)

**KEEP REPAIR ENABLED AT SCORE LIMIT 100.** Recommendation only — no host
configuration was changed during this checkpoint.

The backlog is gone, so the limit is no longer draining anything; it now caps a
steady state that needs roughly 5–10 scorings per cycle. It cost nothing to run
at 100: zero file growth, zero new lock events, p95 duration inside threshold.
Returning it to 1000 would restore 1,000 commits per cycle on
`journal_mode=delete` to buy throughput nobody needs. Lowering it risks falling
behind arrivals for no benefit.

## 13. Prospective-research readiness (Gate 12)

**READY FOR PROSPECTIVE EXPERIMENT REGISTRY.** All seven conditions met:
coverage 14.5% → 100%; scoring past the legacy prefix (1,000 → 12,945, prefix
eliminated); no starvation (skip rate 0%, pool 75); no stale contamination
(stale 0, inconsistent 0, duplicates 0); two domains with adequate current
samples; remaining biases enumerated in §11; and 121 of 121 cycles clean with
zero new lock events.

The reason this matters: a pre-registered experiment is only meaningful if the
evaluation population is fixed and unbiased in advance. Until today it was
neither — it was whatever the id prefix happened to contain.
