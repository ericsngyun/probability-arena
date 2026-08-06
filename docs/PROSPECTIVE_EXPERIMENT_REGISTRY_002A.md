# PROSPECTIVE-EXPERIMENT-REGISTRY-002A — typed population enforcement

**Status:** implemented, reviewed, dark-deployed. **Registration still deferred**
until 002B adds result enforcement.

---

## 1. Why prose had to go

REGISTRY-001 let a manifest define its population in prose and guarded it with a
blocklist of suspicious words. An independent review broke that in one line:

> `"include forecasts in the cohort that beat the benchmark"` — **accepted**

That is exactly the post-hoc cohort selection the registry exists to prevent.
Worse, this repository's own tennis draft shipped `"member is not scored_current
at evaluation"`, semantically identical to a phrasing the test suite asserted was
rejected. The blocklist rejected one spelling and accepted its synonym inside our
own deliverable.

The lesson is not "write a longer blocklist". A blocklist rejects *spellings*;
membership needs a *decision procedure*. Prose is now rationale, and executable
authority is a closed typed schema.

**The security property is structural, not lexical.** A predicate referencing an
outcome, a score or any post-forecast fact cannot be *expressed*, because those
fields are not in the registry and the schema admits no free-text escape.

## 2. Predicate schema (version 1)

```json
{
  "schema_version": 1,
  "all":  [{"field": "domain", "operator": "eq", "value": "sports_baseball"},
           {"field": "forecast_created_at", "operator": "gte_registration_time"}],
  "none": [],
  "rationale": ["prose for humans; never authority, never in the digest"]
}
```

Closed, typed, versioned, serializable, and executable without `eval`. Unknown
keys at either level are rejected, so there is no place to hide an expression.
Bounds: ≤32 predicates, ≤64 set members, ≤128 characters per value.

## 3. Field registry (13 fields)

A field is admissible only when its value is fixed at the moment the forecast is
made. A test asserts every registered field has
`available_at == "forecast_creation"` and `immutable_after_forecast is True` —
so the allowlist cannot grow a post-forecast field without failing.

| field | source | type | nullable | note |
|---|---|---|---|---|
| `domain` | MarketResearchPacket.domain | string | yes | research packet domain, assigned before the forecast exists |
| `evidence_depth` | MarketForecastRecord.evidence_depth | string | yes | self-declared at forecast time; a property of the process |
| `forecast_created_at` | MarketForecastRecord.created_at | timestamp | no | the prospectivity anchor; compared UTC-aware |
| `forecast_risk` | MarketForecastRecord.forecast_risk | string | yes | self-declared risk band, not an observed result |
| `forecaster` | MarketForecastRecord.forecaster_name | string | no | forecaster family |
| `forecaster_version` | MarketForecastRecord.forecaster_version | string | yes | pins the model generation |
| `has_research_packet` | MarketForecastRecord.research_packet_id | bool | no | declared feature-presence flag |
| `has_resolution_assessment` | MarketForecastRecord.resolution_assessment_id | bool | no | declared feature-presence flag |
| `market_ticker` | MarketForecastRecord.market_ticker | string | no | market identity |
| `research_completeness` | MarketResearchPacket.research_completeness_score | number | yes | 0..1 completeness of the research packet |
| `research_risk` | MarketResearchPacket.research_risk | string | yes | research risk band |
| `resolution_risk` | MarketResolutionAssessment.resolution_risk | string | yes | PRE-resolution clarity assessment, not the resolution itself |
| `tradeability_at_forecast_time` | MarketResolutionAssessment.tradeability | string | yes | a researchability label recorded before the forecast; carries no price, side, size or execution meaning |

## 4. Forbidden fields

Rejected by **identity**, not vocabulary. These names exist only so the error
message can explain *why* a familiar name was refused instead of saying "unknown
field"; anything not in the registry is refused regardless.

| field | reason |
|---|---|
| `absolute_error` | derived from the outcome |
| `beat_baseline` | derived from the outcome; this is cohort-picking |
| `brier_score` | derived from the outcome |
| `calibration_result` | derived from the outcome |
| `closing_price` | observed after the forecast |
| `execution_result` | no execution surface exists |
| `final_price` | observed after the forecast |
| `future_evidence` | observed after the forecast |
| `future_market_status` | observed after the forecast |
| `log_loss` | derived from the outcome |
| `market_result` | post-resolution state |
| `market_status` | market status is mutable and observed later; use forecast-time fields instead |
| `outcome` | the outcome is the thing being predicted |
| `outcome_status` | post-resolution state |
| `pnl` | no capital surface exists |
| `post_close_price` | observed after the forecast |
| `post_forecast_liquidity` | observed after the forecast |
| `post_forecast_performance` | observed after the forecast |
| `profit` | not a research quantity in this repository |
| `resolution_result` | post-resolution state |
| `resolved_probability` | post-resolution state |
| `return` | not a research quantity in this repository |
| `score` | derived from the outcome |
| `score_status` | derived from the outcome |
| `scored_current` | derived from the outcome, and an evaluation-time status |
| `settlement_price` | post-resolution state |
| `was_resolved` | derived from the outcome |
| `winner` | post-resolution state |
| `winning_side` | post-resolution state |

## 5. Operators and types

Closed set: ``eq`, `not_eq`, `in`, `not_in`, `lt`, `lte`, `gt`, `gte`, `exists`, `not_exists`, `gte_registration_time`, `before_declared_end``.

Each field declares which operators it supports. Enforced per pair: type
compatibility, cardinality (set operators need a non-empty list), null semantics
(a comparison against NULL is false — a nullable field cannot silently *widen* a
population), and timezone-aware timestamps.

**Naive timestamps are refused, not assumed.** Guessing a zone would shift a
prospectivity boundary by up to a day, which is precisely the quiet error that
turns a "prospective" experiment retrospective.

## 6. Structural prospective boundary

`forecast_created_at gte_registration_time` is **injected when absent**, and a
manifest that declares it digests identically to one that omits it. Omission
cannot be a loophole.

This is a direct correction of REGISTRY-001, where an *optional* `start_time` was
the only prospectivity control — leaving the field out skipped the check entirely
and produced a "prospective" experiment with no time bound at all.

The boundary instant comes from the registration **event**, never a hand-authored
manifest timestamp, and `gte` means a forecast created at exactly the boundary is
a member (asserted by test rather than left to inference).

## 7. Population reconstruction

`reconstruct_population` returns `population_count`, `eligible_count`,
`pre_registration_excluded`, `post_end_excluded`, per-rule exclusion counts,
missing-field counts, and a deterministic `membership_digest` over the sorted
member ids. Provider-free, write-free, bounded examples only.

**It cannot see an outcome or a score.** Predicates are handed a narrow
`ForecastFacts` struct built from forecast-time columns, and a structural test
asserts the module neither imports `MarketOutcomeRecord`/`ForecastScoreRecord`
nor reads `scored_current`, `brier_score`, `winning_side`, `outcome_status`,
`resolved_probability` or `score_status`. A test adds an outcome mid-run and
asserts the membership digest does not move.

## 8. Canonicalization — and its stated boundary

Clauses sorted, duplicates collapsed, set members sorted and deduplicated,
numbers normalized to float (`1` and `1.0` are one threshold), timestamps
normalized to UTC, rationale excluded from the digest.

**This is syntactic canonicalization and nothing more.** It does *not* solve
Boolean equivalence: `lt 5` and `lte 4.999` may describe the same population and
will digest differently. Contradiction rejection is deliberately narrow —
`eq`/`not_eq` on the same value, `exists`/`not_exists` on the same field, two
different `eq` values — and no constraint solving is claimed.

## 9. Drift

`population_reference_snapshot` pins the predicate schema version, field-registry
version and digest, the population-logic file digests, and the forecast model
reference. `classify_population_drift` returns `none`,
`non_material_documentation`, `material_predicate_schema`,
`material_field_registry`, `material_population_logic` or `unknown`.

**Missing or unresolvable references yield `unknown`, never `none`.** Reporting a
clean bill of health from absent evidence is the exact failure this series hit
once already, when B3 pinning silently recorded nulls and drift then reported
"no drift" forever.

## 10. `experiment-registry-report`

Specified in the REGISTRY-001 CLI contract and never implemented; that gap was
disclosed rather than dropped, and is closed here. It shows identity, manifest
version and digest, state, event-chain validity, immutable references, predicate
schema version, canonical predicates, drift, registration/collection/result
state, validation errors and warnings.

Zero provider calls, zero writes, text/JSON parity, secret-free — and it **fails
closed**, exiting 1 when the manifest digest or event chain is broken. Proven:
exit 0 clean, exit 1 after a single-field edit. A report that renders cleanly
over a tampered experiment is worse than no report.

## 11. Repaired drafts — still drafts

**`baseball-prospective-calibration-stability`**
- manifest digest `ea10e4e289ef2266…`
- predicate digest `89d179c92d99bf47…`
- predicates: domain eq 'sports_baseball' · forecast_created_at gte_registration_time · forecaster eq 'baseball_evidence' · forecaster_version eq 'v1' · has_research_packet eq True

**`soccer-prospective-reliability`**
- manifest digest `8f0b1ad5996dddb0…`
- predicate digest `39748b85ce3abb60…`
- predicates: domain eq 'sports_soccer' · forecast_created_at gte_registration_time · forecaster eq 'soccer_evidence' · forecaster_version eq 'v1' · has_research_packet eq True

**`tennis-base-rate-falsification`**
- manifest digest `f2928de83a286ef5…`
- predicate digest `84944c4315500b9d…`
- predicates: domain eq 'sports_tennis' · forecast_created_at gte_registration_time · has_research_packet eq True

### The tennis repair

The draft previously excluded rows that were `"not scored_current at
evaluation"`. That made membership a function of the outcome pipeline's
progress — the cohort would have become *the rows that happened to get scored*.
Membership now uses only forecast-time fields. Scoring state is an **evaluation**
concern, recorded as rationale prose:

> pending rows remain pending; unscorable rows are reported; sample-floor and
> matured-fraction rules determine evaluability.

## 12. What this milestone does NOT do

No result recording. No metric computation. No sample-floor or maturity
enforcement. No registration. Those are 002B, and until they exist the registry
is a population boundary with no inspector at the other end — which is why
registration remains deferred.

## 13. Rollback

Revert the commits. No migration, no schema change, no production write, no
MarketOps behaviour change. Nothing has been registered, so nothing is orphaned.
