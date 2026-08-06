# PROSPECTIVE-EXPERIMENT-REGISTRY-002C — governance closure and feasibility

**Status:** five governance findings closed. **Baseball registered and
collecting; soccer and tennis explicitly blocked drafts.**

---

## 1. The five findings

### M4 — identifier-universe authority

002B presence-checked five keys and scanned `selection_method` prose for nine
substrings. A review defeated it in one line:

> `selection_method: "hand picked after looking at results"` — **accepted**

because "results" was not on the list and the digest was never resolved to
anything. That is the prose-blocklist mistake one layer down.

A universe is now a **separately committed artifact** (`universes/<id>.json`)
with a strict schema and a **typed** selection method — `exhaustive_series`,
`exhaustive_event`, `scheduled_fixtures`, `random_sample_seeded` — each a rule
checkable against the world rather than a sentence about intent. Resolution is
real: the file must exist, hash to the referenced digest, declare a member count
matching its own members, and have been **created before registration**.
Enumerated tickers must lie inside the resolved universe, so a legitimate
artifact cannot be cited beside a hand-picked subset.

### M6 — governed re-pinning

`experiment_results.py` pinned itself as a metric reference, so any edit — including
fixing anything a review found — made drift material for **every** registered
experiment, permanently, and the only escape was editing a registered manifest:
the one thing the registry exists to prevent. Fail-closed was right;
permanently-closed was not.

References are never overwritten. An **append-only, hash-chained, head-pinned
amendment** records the old and new digests, a typed reason, the affected
experiments, a reviewer and review reference, and whether prior collection
remains comparable.

The hard rule: only reasons that **cannot move a number** — documentation,
comments/typing, non-semantic refactor — may declare comparability. Anything
semantic, *including a defect fix*, forces a new experiment version, because
observations either side of it are not measuring the same thing. An amendment
names the digest it moved **to**, so it cannot pre-authorize a future change.

### M8 — tennis decision rule

A negative point estimate cannot confirm persistent underperformance; at n=129
the −0.0368 reading is well inside noise. Adds `ci_upper_bound_lt_zero`, the
falsification counterpart of the existing lower bound. Tennis now requires the
**whole interval below zero**, and its `known_limitations` say plainly that an
inconclusive result is the likely outcome — the honest expectation, not a flaw.

### M9 — NULL accounting

Reconciliation now separates `declared_rule_exclusions` ("the rule said no")
from `unknown_exclusions` ("we could not tell"), and reports
`unknown_retentions` — rows kept in the cohort on the strength of a **missing**
value, previously counted nowhere. Missing-field tallies are taken after the
window filters, so out-of-window rows no longer read as a data problem inside
the cohort.

### M5 — operator prose

Bounded at 2000 characters and secret-scanned because it lands in a committed
artifact. Stated in the code: **bounding prose is hygiene, not semantic
control.** The real guarantee is that nothing reads it back, and a test walks
the AST to assert no branch depends on it.

## 2. Arrival-rate feasibility — the blocker

Measured read-only against production, 2026-08-06:

| domain | last forecast | arrivals / calendar day (14 d) | floor | days to floor | window | verdict |
|---|---|---:|---:|---:|---:|---|
| `sports_baseball` | 2026-08-06 21:03 | **410.7** | 500 | **~1.2** | 180 d | **FEASIBLE** |
| `sports_tennis` | 2026-08-02 12:12 | **1.2** | 200 | **~164** | 180 d | **MARGINAL** |
| `sports_soccer` | **2026-07-23 01:32** | **0.0** | 300 | **∞** | 180 d | **NOT FEASIBLE** |

**Soccer has produced nothing for 14 days.** It ran at 78–130 forecasts/day
through 2026-07-19, dropped to 3 on 07-23, and then stopped. That is a hard stop,
not a slowdown, and its cause is outside this milestone.

**Tennis is marginal in a way the headline rate hides.** The 4.2/day figure is
the mean over days that *had* arrivals — only 4 of the last 14. Per calendar day
it is 1.2, needing ~164 days against a 180-day window, on a domain that has
already gone quiet once.

Registering an experiment that cannot reach its floor produces a guaranteed
`inconclusive_sample_floor` at expiry. That is not a null result — it is an empty
ritual with a digest attached, and it would be the first thing in this registry
that looked like governance without being it.

## 3. Registration disposition — decided

| experiment | state | disposition |
|---|---|---|
| `baseball-prospective-calibration-stability` | **registered / collecting** | — |
| `soccer-prospective-reliability` | draft | `registration_blocked_data_generation_inactive` |
| `tennis-base-rate-falsification` | draft | `registration_blocked_insufficient_recent_arrival_cadence` |

### Baseball — registered 2026-08-06T21:31:23.855645Z

| | |
|---|---|
| manifest digest | `2632e027a9e4bb8c…` |
| predicate digest | `d2b42d69f74089ba…` |
| registration event hash | `3978fc504973d4d3…` |
| authoritative head | `ac2e7f56c6235b57…`, `event_count = 2` |
| registration commit | `e11a3a93d6639533…` |
| window | start = registration instant (registry-assigned), end = `unbounded` |
| primary / baseline | `mean_brier` / `base_rate_brier` |
| decision rule | `primary_metric_delta_gt_zero` |
| CI policy | `cluster_bootstrap_by_market_v1` |
| floor / maturity | 500 / 0.9 |
| stopping rule | `fixed_sample_and_end`, not_before 2026-08-06T04:00Z, not_after 2027-02-02T04:00Z |

Fourteen pre-registration checks passed. The manifest carried **no**
author-supplied registration timestamp — the registry assigned it, which is what
makes prospectivity structural rather than promised.

### Soccer and tennis

Their blocking status is recorded as **append-only governance events**
(`experiments/draft-dispositions.jsonl`, hash-chained and head-pinned), not
written into the manifests. A blocking status is non-authoritative operational
state; editing a draft to carry it would change its digest and make the eventual
registration a different document. Soccer's digest is byte-identical to 002B.

Neither floor nor window was changed. Lowering a floor to make an experiment
feasible is the same act as raising one after seeing results.

Next: `SOCCER-FORECASTER-LIVENESS-001` and `TENNIS-EXPERIMENT-FEASIBILITY-001`,
the latter requiring a predeclared feasibility threshold (recommended ≥80%
modeled probability of reaching the floor without changing forecaster or
protocol).

## 4. What is deployed

Everything in §1, dark. No experiment registered, no result recorded, no
universe artifact committed (none is needed — no draft enumerates tickers).

## 5. Rollback

Revert the commits. No migration, no schema change, no production write, no
MarketOps change.
