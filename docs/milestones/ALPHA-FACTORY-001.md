# ALPHA-FACTORY-001 — the hypothesis factory

**Status: DESIGN ONLY. NOT IMPLEMENTED, NOT REGISTERED, NOT DEPLOYED.**
Written 2026-08-20.

This document specifies how a trading hypothesis **enters**, is **evaluated**,
**dies**, or **graduates**. It authorizes no capital, no orders, no execution,
no EV calculation and no new experiment. Every capability in
`docs/SAFETY_BOUNDARIES.md` remains exactly as governed there; gates **G9** and
**G10** below are specified and **structurally unreachable** under the current
boundary, and describing them is not authorization to build them.

It does **not** propose a parallel system. It generalises the machinery this
repository has already built and already broken:

| mechanism here | inherited from |
|---|---|
| git-backed immutable manifests, registry-assigned timestamps, hash-chained events, head pinning | `PROSPECTIVE-EXPERIMENT-REGISTRY-001` §2–§5, `-002B` §4, §11 |
| closed typed predicate schema, field registry, forbidden-field-by-identity | `-002A` §2–§5 |
| the evaluator computes / the author confirms; verdict precedence | `-002B` §2, §10 |
| typed selection method + resolved universe artifact | `-002C` §1 (M4) |
| governed re-pinning; only reasons that cannot move a number may declare comparability | `-002C` §1 (M6) |
| the Operative-Field Invariant; a required-but-unread field is forbidden | `QUANT-DECISION-KERNEL-001` §9.1 |
| kappa, adverse bounds never zeros, `net_conservative` as headline | `QUANT-DECISION-KERNEL-001` §7.4 |
| family = registered tests **plus** search variants; online alpha budget | `QUANT-DECISION-KERNEL-001` §9.3 |
| the evidence hierarchy; contemporaneous market baseline | `EDGE-DISCOVERY-001-PREREGISTRATION` §0 |
| real-but-uneconomic as an outcome, not a failure | `EDGE-DISCOVERY-001-VERDICT` §8, doctrine 2 |
| purged/embargoed walk-forward, block-count ESS, two null arms | `MARKET-MICROSTRUCTURE-EDGE-001` §4, §5 |
| binding stopping rule with a stated prior | `MARKET-MICROSTRUCTURE-EDGE-001` §2, §8 |
| provenance-first amendments that state what data they did **not** use | `PROD-ACTIVITY-PROFILE-001` Amendments 1–3 |
| typed absence, versioned features over an immutable tape | `MARKET-STATE-FABRIC-v1` §1, AGENTS.md doctrine 10 |

**Precondition.** The factory does not open until QDK-001 §9.2's **D1–D10** are
closed. Running a promotion ladder on a registry whose evaluation side does not
enforce its own manifest reproduces REGISTRY-001 §11's verdict at ten times the
cost: *a good filing cabinet with a strong lock and no inspector*. D2 (canon
digests pinned but never compared), D7 (eight required fields nothing reads) and
D8 (**no multiple-testing correction of any kind exists in `app/`**) are hard
blockers for §5, §6 and §7.3 respectively.

---

## 1. The governing assumption

> **Most strategies should die. The registry's job is to make dying cheap, fast
> and honest.**

The correct prior is **net edge ≤ 0**. That is not pessimism and not a posture;
it is what this repository has measured. `g(f*) = KL(p‖q)` makes tradable growth
and log-score advantage over the market the *same quantity*, and we have never
measured positive forecast skill against the market. EDGE-DISCOVERY-001 ran four
preregistered experiments and all four failed; its one real, out-of-sample
replicating effect (E2's 1-hour lead, `+0.0236`) was **uneconomic against a
`0.0336` cost floor** — 70% of what it needed, and 70% is a loss.

Three consequences bind the whole design:

1. **This is a falsification machine, not a discovery machine.** Every gate is
   written as a *kill criterion first*. A gate whose natural reading is "how do
   I pass this" has been written backwards. The expected output of the factory
   is a large, well-documented graveyard and an occasional
   `REAL_BUT_UNECONOMIC`.
2. **A null result is a successful run.** The factory's health is measured by
   how fast and how cheaply it kills, not by graduation count. Its headline
   statistic is the **graduation rate over all registrations including kills**
   (§7.7); publishing a rate that excludes kills is forbidden, because that is
   survivorship inside our own registry.
3. **Cheap must not mean free.** Thousands of cheap experiments beat a handful
   of elaborate ones *only if each one costs something*. In this factory a test
   costs a draw from a family-wide alpha budget, paid at registration,
   **non-refundable on death** (§7.3). Without that, "run many, kill most" is
   just an unaccounted multiple-comparisons machine.

**The one thing that is not a failure.** A signal can be real, replicating and
still uneconomic. That outcome has its own terminal status
(`REAL_BUT_UNECONOMIC`), it is a scientific result, it is retained and citable,
and it must never be recorded under a failure label. Mislabelling it teaches the
factory to stop looking for real effects.

---

## 2. Scope, and what the factory is not

**In scope.** Hypotheses of the form `H : X_t → Y_{t+h}` over Kalshi market
state, where `X` is a versioned function of the immutable tape and `Y` is a
future *price* quantity. Read-only throughout G0–G8.

**Out of scope, deliberately.**

* Any hypothesis whose label is a settlement outcome rather than a price. Those
  belong to the retained sports controls and are stopped.
* Cross-market and cross-venue structure. Real, deferred, and it multiplies the
  selection surface (`MARKET-STATE-FABRIC-v1` §5).
* Any LLM in the synchronous market-data path. Agents propose and contextualise;
  deterministic systems falsify. `Inference Cost` is a real term in the net-edge
  identity and is measured, not assumed.
* Capital. G9/G10 require a separately accepted milestone that does not exist.

**Ladder mapping.** `docs/ROADMAP_ALPHA_FACTORY.md` sketched G0–G9 with capacity
at G5 and risk at G6. This spec re-orders them — **execution** at G5,
**regimes** at G6, and **capacity** absorbed into G10 (SCALE) — because capacity
is meaningless before an execution model exists, and regime fragility kills more
strategies than notional does. Stated explicitly rather than changed silently.

---

## 3. The strategy object

One committed artifact per strategy: `strategies/<strategy_id>/manifest.json`,
canonical JSON, written **once**. It is a superset of the REGISTRY-002 experiment
manifest and reuses its validation, digesting and chain machinery rather than
restating them.

**Two structural rules govern every field.**

* **The Operative-Field Invariant.** Every field the evaluator reads is typed
  and closed — an enum, a number, a timestamp, a digest, or a predicate over the
  allowlisted field registry. Every prose field is non-operative and **provably
  unread**, enforced by an AST test asserting no evaluator branch depends on it.
  **A field that is required but unread is forbidden.**
* **Registry-assigned fields are excluded from the hypothesis digest**, so the
  digest a reviewer verifies before freezing equals the one stored after.
  Otherwise showing it for confirmation is theatre.

### 3.1 Schema

```jsonc
{
  "schema_version": 1,
  "strategy_id": "kalshi-micro-ofi-30s-001",        // immutable, never reused
  "parent_strategy_id": null,                        // set when derived from a killed strategy
  "family_id": "sha256:…",                           // (target_class, universe_digest, epoch_id)
  "alpha_draw": 0.0125,                              // charged to the family ledger at freeze

  "hypothesis": {
    "statement": "…",                                // prose, non-operative
    "target_class": "delta_mid_cents",               // enum, closed
    "horizon_s": 30,                                 // primary, exactly one
    "secondary_horizons_s": [1, 5, 300],             // declared, correction applies
    "direction": "unsigned",                         // signed | unsigned — existence vs sign
    "null_hypothesis": "…",
    "predicted_effect": {"point": 0.9, "interval": [0.4, 1.6], "unit": "cents"}
  },

  "mechanism": {
    "class": "liquidity_provision_asymmetry",        // enum, closed
    "causal_statement": "…",                         // prose, non-operative, human-reviewed at H0
    "predicts_sign": true,
    "predicts_magnitude_scale": true,
    "predicts_regime_dependence": ["seconds_to_close_lt_600"],
    "would_be_falsified_by": ["…"]                   // reviewer's declared falsifier, checked at G6/G7
  },

  "feature_version": "sha256:…",                     // AST of feature code + lags + windows + absence policy
  "data_version": {
    "segment_digests_head": "sha256:…",              // archive manifest head at freeze
    "discovery_segments": ["sha256:…", "…"],         // enumerated, closed
    "confirmation_segments": null                    // MUST be null at freeze — see §7.1 anchor 3
  },

  "universe_ref": {"id": "prod-panel-2026Q3", "digest": "sha256:…"},
  "regime": {
    "partition_fields": ["time_of_day_bucket", "seconds_to_close_bucket",
                         "realized_vol_quintile", "spread_quintile", "series"],
    "declared_scope": ["all"],                       // the regimes the hypothesis claims
    "min_regimes_positive": 4                        // of 5, declared before data
  },

  "cost_model": {
    "id": "kalshi-taker-v1", "version": 1,
    "half_spread": "OBSERVED_ROW_STATE",
    "fees": {"basis": "venue_schedule_2026_07_07", "verified_against_fills": false},
    "bounded_terms": {"queue_position": "adverse", "partial_fill": "adverse",
                      "impact": "adverse", "latency": "adverse"},
    "kappa_floor": 2.0
  },

  "risk_model": {
    "max_concurrent_positions": 0,                   // 0 until G9 exists
    "per_strategy_loss_limit": null,
    "correlation_group": "microstructure_ofi",
    "tail_metric": "cvar_95_blockwise",
    "ruin_constraint": "declared_at_G9"
  },

  "discovery_interval":    {"start": "…Z", "end": "…Z", "purge_s": 300, "embargo_s": 300},
  "confirmation_interval": {"start": "…Z", "end": "…Z", "min_blocks": 12000},

  "evaluation": {
    "primary_metric": {"name": "oos_r2_vs_baseline", "definition_digest": "sha256:…"},
    "baseline": {"tier": "B2_contemporaneous_market", "spec": "microprice_at_t",
                 "definition_digest": "sha256:…"},
    "ci_policy": "block_bootstrap_by_market_v1", "seed": 20260820, "resamples": 2000,
    "ess_unit": "blocks", "block_length_s": 300,
    "noise_floor_arms": ["shuffled_labels", "shifted_features", "time_shifted_labels"],
    "positive_controls": ["known_effect_sentinel", "cost_term_wiring", "prospectivity_refusal"],
    "stopping_rule": {"kind": "fixed_blocks_and_end", "min_blocks": 12000,
                      "not_before": "…Z", "not_after": "…Z"},
    "interim_looks": 0
  },

  "status": "PROPOSED",                              // registry-assigned, never author-supplied
  "frozen_at": null,                                 // registry-assigned
  "safety_boundary": "read-only; no capital, no orders, no EV, no sizing"
}
```

### 3.2 Field rationale — why each exists, and what goes wrong without it

| field | why it exists | failure mode without it |
|---|---|---|
| `strategy_id` | the immutable identity a kill attaches to | identities get reused, and a killed strategy quietly becomes a live one with the same name |
| `parent_strategy_id` | makes "kill it, tweak it, resubmit it" **visible and countable** | the same idea is tested twenty times and the family counts one |
| `family_id` | binds the test to a multiplicity budget | per-experiment FDR with no family; the factory's true error rate is unmeasured |
| `alpha_draw` | the *price* of the test, paid before data | tests are free, so running more of them is always rational |
| `hypothesis.target_class` / `horizon_s` | exactly one primary target, fixed | horizon shopping — four horizons, report the best |
| `hypothesis.direction` | preregisters *existence*, not sign, where the mechanism does not fix the sign | a fade result gets re-read as a lead result after the fact (EDGE-DISCOVERY §3 handles this correctly; do not lose it) |
| `predicted_effect.point/interval` | the strongest anti-gaming device in the schema: G7 checks *did it do what you said*, not merely *is it significant* | any nonzero effect in the right direction counts as replication |
| `mechanism.*` | a correlation with no mechanism has no reason to persist out of sample, and no way to predict its own regime dependence | the factory becomes a data-mining loop with a prose wrapper |
| `mechanism.would_be_falsified_by` | the reviewer's own pre-declared falsifier, machine-checked later | review becomes assent; a reviewer whose falsifiers never fire is not reviewing (§8) |
| `feature_version` | pins the exact computation; `feature_set_version + tape_hash → identical result` | recomputing features after a "fix" silently changes what was tested (§7.8 class 1) |
| `data_version.segment_digests` | pins the *bytes*, not a date range | a backfilled or repaired segment silently changes the dataset under a fixed date rule (§7.8 class 2) |
| `data_version.confirmation_segments = null` | the confirmation data must **not exist** at freeze | prospectivity rests on trusting a clock we control (§7.1) |
| `universe_ref` | selection is the largest silent-overfitting surface in the lane | markets get chosen after seeing which produced attractive results |
| `regime.partition_fields` / `min_regimes_positive` | fixes the robustness test before the answer is visible | a one-regime effect is re-narrated as a general one, or a general failure is rescued by "it works in the afternoon" |
| `cost_model.half_spread = OBSERVED_ROW_STATE` | the floor must bind **at the row's own state** | a pooled average floor lets small predicted moves on wide spreads count as edge |
| `cost_model.bounded_terms` | unknown costs become *declared adverse bounds*, never zeros | a silent zero is unfalsifiable; an adverse bound is a claim that can be checked |
| `cost_model.kappa_floor` | robustness of the economic verdict to our own cost-model error | a result that dies if costs are 1.2× the model reads as a graduate |
| `risk_model.correlation_group` | two graduates may be one bet | independent gate passes get portfolio-summed as if independent |
| `discovery_interval` / `confirmation_interval` | separates the data that generated the idea from the data that tests it, with purge and embargo | label windows overlap feature windows and every result is flattered |
| `confirmation_interval.min_blocks` | ESS floor in the honest unit | 360,000 rows quoted where ~12,000 blocks exist — a lie of arithmetic |
| `primary_metric.definition_digest` | name, definition and hypothesis must be mutually consistent at registration | QDK D10, already live: `name: "mean_brier"` with a *skill* formula and a hypothesis in skill terms — three quantities in one immutable record |
| `ci_policy.seed` / `resamples` | pinned constants | an evaluator who can reroll keeps rolling until the interval clears zero |
| `noise_floor_arms` | doctrine 4 — the measurement reports when it is meaningless | a single-null benchmark reports scheduler noise as a result and passes for the wrong reason (CP5) |
| `positive_controls` | doctrine 7 — every metric needs a test that proves it CAN fail | a plausible benign value emitted by a broken path: clean-looking data, convincing statistics, no alert |
| `stopping_rule` | closed typed vocabulary; prose is never executable | the terminal moment becomes "the first evaluation after the number looks good" |
| `interim_looks: 0` | a decision moment chosen by a human who has seen the data is not a stopping rule | unbounded inflation, undetectable after the fact |
| `status` / `frozen_at` registry-assigned | prospectivity holds **by construction** rather than by promise | REGISTRY-001 H1/H2: hand-written start times that had already passed |

**Forbidden by identity, not vocabulary** (extending `-002A` §4 to the price
domain): any feature or predicate field naming `settlement_price`,
`final_price`, `closing_price`, `future_mid`, `realized_pnl`, `winning_side`,
`beat_baseline`, `best_performing`, or any post-`t` quantity. A blocklist rejects
spellings; an allowlisted field registry rejects the *capability*. The security
property is structural.

---

## 4. States and terminal statuses

```
PROPOSED ─► FROZEN ─► G1 … G8 ─► SHADOW ─► TINY_CAPITAL ─► SCALE
                │
                └──► one terminal status ──► RETAINED_AS_CONTROL
```

Append-only. **No edge returns to an earlier gate.** A strategy that fails a gate
is terminal; re-specification requires a **new** `strategy_id`, a **new**
`alpha_draw`, a `parent_strategy_id` link, and a confirmation interval that does
**not overlap** the parent's. This single rule is what closes the
kill-tweak-resubmit loop, which is otherwise the cheapest way to defeat every
statistical control below it.

| terminal status | meaning | is it a failure? |
|---|---|---|
| `KILLED_NO_MECHANISM` | G0: correlation with no causal statement, or a mechanism that predicts neither sign nor regime | yes |
| `KILLED_INFEASIBLE` | the ESS floor is unreachable within the confirmation interval at the measured arrival rate | no — a feasibility fact |
| `VOID_MEASUREMENT` | a positive control failed, replay determinism failed, or a pipeline defect was demonstrated | **not a finding at all**; nothing else may be reported |
| `KILLED_DATA_INVALID` | G1: an unverified field semantic, an imputed absence, a completeness claim a source cannot support | yes |
| `KILLED_NO_INFORMATION` | G2: effect does not exceed the larger noise floor, or CI includes zero after family correction | yes |
| `KILLED_REDUNDANT` | G3: no increment over the contemporaneous market baseline | yes — and the most common outcome |
| `REAL_BUT_UNECONOMIC` | G2 and G3 passed; `net_conservative ≤ 0`, or `kappa < 2` | **NO.** A scientific result. Retained, citable, publishable internally |
| `EXECUTION_UNFALSIFIABLE` | G5: the economics require a fill class no available evidence can confirm or refute (Kalshi maker fills, absent queue position) | no — a scope limit |
| `KILLED_REGIME_FRAGILE` | G6: effect concentrated in, or sign-flipped within, an undeclared regime | yes |
| `KILLED_FAILED_REPLICATION` | G7: forward effect outside the declared prediction interval | yes |
| `KILLED_BACKTEST_DIVERGENCE` | G8: shadow net departs the G7 estimate beyond tolerance | yes, and requires a named root cause |
| `INVALIDATED_PROTOCOL` | undeclared interim look, unlogged deviation, post-freeze threshold change, broken chain | not a result in either direction |
| `RETIRED_SUPERSEDED` | a graduate replaced by a descendant | no |

Every terminal status transitions to `RETAINED_AS_CONTROL` (§7.7). **Nothing is
deleted, ever.**

---

## 5. The promotion ladder

Each gate is evaluated by the **evaluator, from the committed record**. The
author supplies a `strategy_id`, a confirmation, and non-operative notes.
*A value an author can supply is a value an author can choose.*

**Verdict precedence at every gate**, before any number is looked at:
chain integrity → code **and canon** drift → data quality → prospectivity →
stopping-rule satisfaction → ESS floors → positive controls → **then** the
number. Integrity beats arithmetic always.

### 5.1 The ladder at a glance

| gate | question | PASS | KILL |
|---|---|---|---|
| **G0** PROPOSED | is this a hypothesis or a hunch? | typed mechanism predicting sign **and** regime dependence; manifest validates; family assigned; `alpha_draw` available; feasibility ≥ 80%; freeze anchors present (§7.1) | no mechanism, or mechanism predicts neither sign nor scale → `KILLED_NO_MECHANISM`; ESS unreachable → `KILLED_INFEASIBLE`; budget exhausted → registration **refused** (not a kill — never registered) |
| **G1** DATA VALID | can the signal even be measured correctly? | every venue field on the doctrine-8 verified list; typed absence throughout (`PRESENT`/`EMPTY`/`NOT_PROVIDED`), drop-not-impute, drop rate reported; source completeness capability inherited by every feature; replay determinism `feature_version + tape_hash → identical digest`; all G1 positive controls non-benign | any unverified field semantic, any `None → 0`, any unsequenced source described as complete, any imputation → `KILLED_DATA_INVALID`. A failed positive control → `VOID_MEASUREMENT`, and **no other G1 output may be read** |
| **G2** STATISTICALLY INFORMATIVE | does it predict anything, out of sample? | effect exceeds the **larger** of the noise-floor arms; block-bootstrap CI excludes zero at the family-adjusted level; realised ESS ≥ `min_blocks`; effect reported **beside** both floors | below the floor → `KILLED_NO_INFORMATION`; ESS short at the terminal end → `inconclusive_ess_floor`, then `KILLED_INFEASIBLE`; any interim look → `INVALIDATED_PROTOCOL` |
| **G3** INCREMENTAL VS BASELINE | does it beat the **contemporaneous market price**? | §5.2 | §5.2 |
| **G4** ECONOMICALLY MATERIAL | does the effect exceed half-spread + fees **at the row's own state**? | §5.3 | §5.3 |
| **G5** EXECUTION POSITIVE | does a modelled fill survive contact with a real one? | execution model calibrated against realized fills with bounded bias; `net_conservative` positive at the **p95 adverse fill**; taker-only unless a fill corpus exists | net requires maker rebate or queue priority no evidence supports → `EXECUTION_UNFALSIFIABLE`; modelled-vs-realized error biased optimistic → kill |
| **G6** ROBUST ACROSS REGIMES | is it one effect, or one lucky regime? | sign consistent across **all** declared partition fields; `net_conservative > 0` in ≥ `min_regimes_positive`; no single regime contributing > 50% of total effect; the reviewer's `would_be_falsified_by` conditions checked and not met | concentration or sign flip in an undeclared regime → `KILLED_REGIME_FRAGILE`. Narrowing the hypothesis to the surviving regime is **forbidden** — it requires a new `strategy_id` and a new `alpha_draw` |
| **G7** PROSPECTIVELY REPLICATED | did it do what you said it would, on data that did not exist when you said it? | forward effect **inside `predicted_effect.interval`**, on the untouched confirmation interval; **plus** a second independently registered, non-overlapping replication that independently clears G2–G4 | point estimate outside the declared interval → `KILLED_FAILED_REPLICATION`; any undeclared look, or any confirmation segment whose digest existed at `frozen_at` → `INVALIDATED_PROTOCOL` |
| **G8** SHADOW | does the whole pipeline, in production, reproduce the study? | full decision records emitted on the live plane, no capital; shadow `net_conservative` within declared tolerance of the G7 estimate over a declared duration; decision-record completeness ≥ threshold; latency budget met; **zero** LIVE-plane p99 degradation; zero safety-boundary violations | divergence beyond tolerance → `KILLED_BACKTEST_DIVERGENCE` **with a named root cause** (which look-ahead, which cost term). "Unexplained" is not an accepted root cause |
| **G9** TINY CAPITAL | **UNREACHABLE.** Requires a milestone that does not exist | realized fills match shadow within tolerance; per-strategy loss limit unbreached; kill switch tested **before** funding | realized cost exceeds modelled cost by more than the kappa margin → immediate stop |
| **G10** SCALE | **UNREACHABLE.** does it survive notional? | capacity curve measured, not assumed; marginal `net_conservative > 0` at the proposed size | marginal net ≤ 0 → the size above that point is the cap, not a target |

**Gates cannot be skipped, reordered, or evaluated out of sequence.** A gate
evaluated before its predecessor passed is `INVALIDATED_PROTOCOL`.

### 5.2 G3 — INCREMENTAL VS BASELINE (the first of the two that get faked)

**The baseline is the contemporaneous market price. Not zero. Not a naive
model. Not a prior version of ourselves.**

The evidence hierarchy is binding:

```
beats base rate < beats naïve model < beats MARKET PRICE
                < survives executable price < survives fees/slippage
                < prospective positive expectancy
```

Only the last two bear on capital. The repo's own `brier_skill_vs_base_rate`
figures sit at level 1 and must never be presented as market-relative skill.

**Baseline tiers, and their standing:**

| tier | spec | standing |
|---|---|---|
| **B0** unconditional base rate | prevalence, `Δ = 0` | **level 1. Bears on nothing.** May not be a `baseline.tier` value |
| **B1** naive model / random walk | `Δmid = 0` | **positive control only.** If the candidate cannot beat B1, the pipeline is broken and no other result may be read |
| **B2** contemporaneous market | `mid` for information-class hypotheses, **`microprice`** for microstructure-class, **executable touch** for anything that will touch capital | **the gate.** This is what "incremental" means |
| **B3** incumbent | the best already-graduated strategy in the same `correlation_group`, at its current `feature_version` | additionally required once any graduate exists |

**PASS.** All of:

1. Nested comparison `M_baseline ⊂ M_candidate`, evaluated **once** on the
   confirmation data, both models fit with the **same tuning budget** (same
   number of free parameters, same hyperparameter search size, same data). A
   baseline given less freedom than the candidate is not a baseline.
2. **Same information-set timestamp.** The harness asserts baseline and
   candidate read state as of an identical `t`. A baseline with any lag is a
   handicap, not a comparison.
3. Paired difference CI (clustered block bootstrap, pinned seed) excludes zero
   at the family-adjusted level.
4. The improvement is not confined to rows where the baseline is undefined.
   Rows with a one-sided or absent book are **uncovered, never guessed**, and
   are reported separately; they may not carry the result.
5. **The shrinkage diagnostic passes.** Mandatory, reported on every G3
   artifact:

   > regress the candidate signal on the baseline —
   > `signal = α + β · baseline`, report `β` and `R²`.

   `β < 1` with high `R²` is the signature of *the model is the market,
   blurred* — the mechanism that explains all four EDGE-DISCOVERY failures
   (`logit(p) = −0.094 + 0.568·logit(q)`, R² = 0.661). A candidate that is a
   shrunk copy of the baseline will track its short-run drift and lose at
   resolution, and it will look predictive the whole way.

**KILL.**

* `baseline.tier` is B0 or B1 → **registration refused at G0**, not a kill at
  G3. Level-1 evidence cannot be registered as a gate.
* Baseline read at any lag behind the candidate → `VOID_MEASUREMENT`.
* Baseline fit with less freedom than the candidate → `VOID_MEASUREMENT`.
* No holdout increment → `KILLED_REDUNDANT`.
* High `R²` on the baseline with no increment → `KILLED_REDUNDANT`, and the
  shrinkage diagnostic is recorded as the cause of death, so the next author
  does not rediscover it.
* Increment present only in uncovered rows → `KILLED_REDUNDANT`.

**How this gate is faked, and what stops it:** a stale baseline timestamp
(stopped by rule 2, asserted in the harness rather than promised); a baseline
with fewer free parameters (rule 1); a baseline swapped from `microprice` to
`mid` after seeing that `mid` is easier to beat (stopped by the frozen
`baseline.definition_digest`); "we beat the base rate" quietly presented as
market-relative (stopped by B0 being unregistrable and by the metric-naming
rule).

### 5.3 G4 — ECONOMICALLY MATERIAL (the second)

> **The effect must exceed half-spread + fees at the row's own state.**

Not on average. Not pooled. **At the row's own state.** A 0.4-cent predicted
move on a 2-cent spread is a measurement of the spread.

**Computation, in this order — and the order is the control:**

1. Per row `i`, compute `floor_i = half_spread_i + round_trip_fee_i`, both from
   that row's own observed book state and price.
2. The strategy's **acting set** `A = { i : |ŷ_i| > floor_i }`, declared by the
   strategy's own threshold rule at registration — not chosen afterwards.
3. `net_partial` = expectancy over `A` charging only observed and modelled cost
   terms.
4. `net_conservative` = the same, charging **every bounded term at its declared
   adverse bound**. Adverse bounds are registered **before data is seen**.
5. `kappa` = the multiple of total modelled cost at which `net_conservative`
   crosses zero. **The evaluator computes kappa, not the author.**
6. `|A| / n` is reported. A strategy that acts on 0.2% of rows has an ESS of
   `|A|` blocks, not `n`.

**`net_conservative` is the headline. The evaluator refuses to render any
artifact carrying a gross number without the net beside it,** and refuses any
effect size printed without its cost floor.

**PASS.** `net_conservative > 0` on the confirmation interval, CI excluding
zero at the family-adjusted level, **and `kappa ≥ 2`**, **and** the fee schedule
attested verified at **H2** (§8).

| kappa | verdict |
|---|---|
| **< 1** | already dead at the modelled cost |
| **1 – 2** | **not robust; may not support a confirmatory claim.** A result that dies if costs are twice the model has not survived our own measurement error |
| **≥ 2** | reportable, with kappa stated on the artifact |

**KILL / terminal.**

* `net_conservative ≤ 0` **after G2 and G3 passed** → `REAL_BUT_UNECONOMIC`.
  This is a result, not a failure. It is retained and citable. E2 is the
  canonical instance: a genuine 1-hour lead at 70% of its floor.
* `net_conservative ≤ 0` where G2 or G3 had **not** passed → `KILLED_NO_INFORMATION`
  / `KILLED_REDUNDANT`, whichever gate actually failed. Do not launder a
  statistical failure into an economic one; it flatters the idea.
* `kappa < 2` → `REAL_BUT_UNECONOMIC`, sub-status `fragile_to_cost_model`.
* Pooled floor used where a per-row floor was specified → `VOID_MEASUREMENT`.
* Any bounded cost term set to zero → `VOID_MEASUREMENT`. `L_ρ ≥ 0` is one-way;
  a silent zero cannot be falsified later, an adverse bound can.
* **Fee schedule not verified against venue documentation and realized fills**
  → the gate is **`UNEVALUATED`**, never `PASSED`. `UNEVALUATED` does not
  advance. This is deliberate: on Kalshi today, with the fee minimum
  `ceil_to_cent` binding and no realized-fill corpus, the honest state of this
  gate for most strategies is *unevaluated*.

**Two measured facts this gate exists to respect.** Mean realised taker fee
(1.75–1.84¢) *exceeded* mean half-spread (1.52¢) in EDGE-DISCOVERY-001 — the
binding constraint was the fee **minimum**, which does not improve with better
execution. And a counterfactual all-maker fill still left net ≈ −1.06¢: **fees
alone are not binding; fees plus spread are.** Any cost model that treats
execution quality as the lever has already misread the problem.

---

## 6. Sizing the ladder honestly

`docs/ROADMAP_ALPHA_FACTORY.md` sets the bar that would change everything, and
it is modest, not spectacular: `E[r_5m | signal] = 8 bps` against a `4 bps`
all-in executable hurdle, with robust prospective replication. That is the
target shape. Until then: **the best trade is still no trade.**

**The factory's own stopping rule, binding.** If **20–30** well-preregistered
hypotheses against clean production data produce no economically meaningful
prospective edge, **reconsider the venue and the thesis** rather than building
another architectural layer. This is checked at H6 (§8) and it is the one gate
in this document that can kill the factory rather than a strategy.

---

## 7. Anti-gaming machinery

This is the actual content of the spec. Every mechanism below exists because a
specific control in this repository was defeated by a specific move.

### 7.1 Preregistration and cryptographic timestamping

**The artifact.** `strategies/<id>/manifest.json` (written once, never
overwritten) · `events.jsonl` (hash-chained, `prev`/`seq`) · `head.json`
(pinning `event_count` + `terminal_event_hash`, updated in the *same operation*
as every append) · `amendments.jsonl` (hash-chained) · `results/` (append-only,
each file's digest recomputed by the chain verifier).

A prefix of a valid hash chain is itself a valid chain, which is why the head
pin exists: it is what makes suffix truncation, middle deletion, append-without-head
and missing-head all fail closed. **This is tamper-evident, not tamper-proof.**
Anyone with write access can rewrite both files; the evidence lands in a git
diff. Overstating that would be the same error as the prose blocklist.

**Three freeze anchors, in ascending strength. All three are required.**

1. **Registry-assigned `frozen_at`** from the real clock, refusing any confirmed
   operation more than 300 s from it. Closes the "caller chose the clock" route
   that once turned `still_collecting` into `supports_hypothesis`.
2. **RFC-3161 timestamp token** over the hypothesis digest, from a public TSA,
   stored beside the manifest and verifiable offline against the TSA
   certificate. This is what makes the freeze independent of a git commit date,
   which is author-settable via `GIT_COMMITTER_DATE`.
3. **Data non-existence — the only anchor that does not require trusting
   anything we control.** At freeze, `data_version.confirmation_segments` is
   `null`, and the archive's own hash-chained manifest head is pinned into the
   manifest. At G7 the evaluator recomputes which tape segments the confirmation
   window covers and **refuses if any of them was reachable from the pinned
   archive head at `frozen_at`**. Prospectivity then rests on a property of the
   data rather than on a promise about the clock: *the data you were tested on
   did not exist when you wrote the test.*

**What is refused at registration** (superset of REGISTRY-001 §6, extended):
multiple primary metrics · missing ESS floor · missing stopping rule · a
baseline at tier B0/B1 · outcome-derived or post-`t` fields in any predicate ·
author-supplied `frozen_at` or `status` · duplicate `strategy_id` · overwriting
a frozen manifest · a confirmatory strategy with no family or no alpha draw ·
an exhausted family budget · a `primary_metric.name` inconsistent with its
`definition_digest` or with the hypothesis statement (QDK D10 — three different
quantities in one immutable record, already latent in a live registration) ·
trading vocabulary · credential-shaped values · path traversal in `strategy_id`.

### 7.2 Amendments that stay honest

The pattern is `PROD-ACTIVITY-PROFILE-001` Amendments 1–3, promoted from a
writing habit to a schema. What made those legitimate was the **order**:
provenance first, then what data the amendment did *not* use, then a plain
statement about whether any threshold moved.

```jsonc
{
  "amendment_seq": 1,
  "amended_at": "…Z",                      // registry-assigned
  "class": "basis_clarification",           // enum, closed — see table
  "provenance": [                           // FIRST. enumerated, digested, closed.
    {"artifact": "tape/p4-window", "digest": "sha256:…", "created_at": "…Z"},
    {"artifact": "tests/test_peak_estimator", "digest": "sha256:…"}
  ],
  "data_not_used": {
    "statement": "uses no data from this strategy",
    "proof": {"kind": "empty_result_set", "confirmation_segments_at_amendment": []}
  },
  "thresholds_moved": [],                   // EMPTY IS A LEGAL AND EXPECTED VALUE
  "comparability": "prior_observations_remain_comparable",
  "reviewers": ["…", "…"],                  // neither may be the author
  "rationale": "…"                          // prose, non-operative
}
```

**Amendment classes and what each may do:**

| class | may it move a threshold? | when permitted |
|---|---|---|
| `basis_clarification` | **no** | any time. Makes an existing basis explicit; names fields; changes no decision boundary |
| `defect_correction` | **only pre-freeze** | a measurement is demonstrably wrong, and the demonstration rests **entirely on artifacts outside this strategy's discovery and confirmation sets** |
| `circularity_repair` | **no** | the preregistration is unsatisfiable as written (Amendment 2's case: a universe rule defined by its own output) |
| `scope_narrowing` | — | **FORBIDDEN after G2.** Requires a new `strategy_id` and a new `alpha_draw` |
| `threshold_change` | — | **FORBIDDEN after `frozen_at`, without exception.** Attempting it is `INVALIDATED_PROTOCOL`, not a downgrade |

**Four binding rules.**

1. **Provenance is stated first and is enumerated with digests.** An amendment
   whose provenance cannot be resolved to committed artifacts is refused. This
   is what separates "the peak estimator was demonstrably phase-dependent on the
   frozen P4 tape" from "we reconsidered."
2. **The amendment must state what data it did NOT use, with proof the evaluator
   can recompute** — an empty confirmation set, an archive head that has not
   advanced, a window count of zero. *"It uses no data from this experiment,
   because none exists — not one window has been captured"* is the model
   sentence, and it is checkable.
3. **`thresholds_moved: []` must be written explicitly.** An empty list is the
   expected value and it is not optional, because *"we checked and nothing
   needed changing"* and *"we did not check"* are indistinguishable in a
   document that stays silent. The evaluator refuses an amendment that omits
   the field.
4. **Only classes that cannot move a number may declare comparability.**
   Anything semantic — **including a defect fix** — forces a new strategy
   version, because observations either side of it are not measuring the same
   thing. An amendment names the digest it moved **to**, so it cannot
   pre-authorize a future change.

**Deviations** (departures discovered during a run, as distinct from planned
amendments) follow EDGE-DISCOVERY-001 §8: logged with reason and timestamp
**before** the affected number is reported. An unlogged deviation invalidates
the affected gate. Deviations are stamped **in both directions** — an
out-of-window evaluation is a deviation whether it favours the result or not.
A protocol that only polices favourable deviations teaches you to reach negative
conclusions sloppily, and the habit does not stay in the negative direction.

### 7.3 Multiple testing across the whole factory

**D8 is the starting point: no multiple-testing correction of any kind exists in
`app/` today.** Per-experiment FDR would not be enough even if it did. A factory
running many hypotheses has a family-wide error rate, and the family is not the
experiment.

**Defining the family.**

```
family_id = sha256( target_class ‖ universe_digest ‖ epoch_id )
```

An **epoch** is a declared calendar span bound to a frozen universe artifact.
Rolling the epoch requires a new universe digest, and **does not reset spent
alpha** unless the new epoch's confirmation data is disjoint from the old — the
evaluator recomputes segment-digest overlap and refuses otherwise.

**Family size is not the count of registrations.**

```
m = registered_confirmatory_strategies
  + machine_counted_search_variants        ← the whole point
  + descendant_registrations               (each new strategy_id from a killed parent)
  + declared_secondary_horizons_and_cells
```

The second term is what a naive family misses. QDK-001 §9.3 measured the gap
directly: a family counting only registered experiments counted six candidates
and missed the eighteen-policy search that generated them, where the real prior
search was **39+ variants over one ~260-row window**.

**Search variants are machine-counted, not author-declared.** Every model fit
performed against registry-governed data through the sanctioned harness
increments the family's variant counter automatically, writing to
`families/<id>/search_log.jsonl`. The data-access path requires a harness token
bound to a `family_id`; there is no other read path to the governed tape from
the QUANT plane. **A mismatch between the machine count and the author's
declared count invalidates the strategy.** This does not close the hole (§9),
but it moves the honest default from "declare your search" to "your search is
counted."

**Two lanes, and they are not interchangeable.**

| lane | correction | what a survivor means |
|---|---|---|
| **Discovery / pre-screen** | Benjamini–Hochberg at FDR *q*, over the pre-declared candidate set | **a reason to register. Never a finding.** A BH survivor buys the right to spend alpha, nothing else |
| **Confirmatory** | family-wide alpha spending (below), Holm–Bonferroni within any single registration's declared cells | a claim, subject to §7.3's replication rule |

**The alpha budget ledger.** `families/<id>/policy.json` (written once) and
`families/<id>/budget.jsonl` (append-only, hash-chained, head-pinned). The
policy pins, and can never be edited — editing requires a **new family**:

* `alpha_family` — the family's FWER/online-FDR wealth at epoch open;
* `reward b0` — the credit a rejection earns, `b0 ≤ alpha_family`;
* the `alpha_j` sequence rule — how each successive test's level is derived;
* the discovery-lane `q`.

Tracking is **online** (LORD-style alpha investing), because the factory is a
stream of tests over time, not a fixed batch:

```
W_0 = alpha_family
test j:  W_j = W_{j-1} − alpha_j          (charged at FREEZE, before any data)
         W_j += b0                        (ONLY on a confirmed rejection at G7)
registration refused when  alpha_j > W_{j-1}
```

**Three properties of that construction, each deliberate.**

1. **Alpha is spent at registration, never at evaluation.** A test that is
   killed at G1 has already cost the family. A budget charged at evaluation
   would let an author register a hundred strategies and pay for one.
2. **Death does not refund.** This is the entire economic content of "cheap must
   not mean free." A refund on death restores infinite free testing by the
   simple expedient of killing everything that does not work — which is exactly
   what the factory does by design.
3. **Only a *confirmed replication* earns wealth back.** Not a G2 pass, not a G4
   pass — a G7 pass, which requires a second independently registered
   non-overlapping replication. Wealth is earned by the one event that is
   genuinely hard to fake.

**Cross-family leakage.** The same data window may not be charged to two
families without a declared overlap adjustment. The evaluator recomputes block
overlap from segment digests and refuses a confirmatory evaluation whose blocks
overlap another family's confirmatory blocks beyond the declared fraction. This
is the control against the most natural evasion available: **gerrymandering the
family** by splitting one search across two `target_class` values so each draws
its own budget. `target_class` is a closed enum precisely to make that visible.

**The protocol rule that is stronger than the arithmetic.** A single passing
strategy is not a finding. A confirmatory claim requires a **second,
independently registered, non-overlapping replication**, because replication is
robust to the one thing no correction is robust to: an **undeclared prior
search**. A search can inflate one window; it cannot easily inflate two disjoint
prospective windows in the same direction.

**Budget reporting.** Every result artifact carries, on the artifact itself:
`family_id` · `m` with its four components broken out · `W` before and after ·
the adjusted level the CI was computed at. **The raw interval never appears
without the multiplicity-adjusted one beside it.**

### 7.4 Purged and embargoed splits; effective sample size

**Splits.** Purged, embargoed, walk-forward by wall-clock time. Train on window
*k*, test on window *k+1*, **never the reverse**.

* **Purge.** Any training row whose *label window* overlaps a test row's
  *feature window* is removed. Purge count reported.
* **Embargo.** `≥ max(primary horizon, all secondary horizons)` between train
  and test, and `≥ declared feature lookback` in the reverse direction. Both
  ends are pinned in the digest.
* **Clustering.** Standard errors clustered by **market**; intervals from a
  **block bootstrap** with block length `≥ max(horizon, target autocorrelation
  decay)`, seed and resample count pinned at registration.

**Effective sample size is reported in blocks, and only in blocks.**

> The design yields 6 windows × 25 min × 40 markets ≈ **360,000 rows at 1 Hz**;
> at a 30 s horizon that is closer to **~12,000 quasi-independent blocks**.
> **Quoting 360,000 would be a lie of arithmetic.**

Every artifact reports `blocks_total`, `blocks_train`, `blocks_test`,
`blocks_in_acting_set`, `markets`, `windows`, and `rows` — with **rows last and
never as a headline**. `ess_unit` in the manifest may only be `blocks`.

**Feasibility at registration, not at expiry.** From the arrival rate measured
at registration, the modelled probability of reaching `min_blocks` within the
confirmation interval must be **≥ 80%**. Below that, registration is refused as
`registration_blocked_infeasible`. Registering a strategy that cannot reach its
floor produces a guaranteed inconclusive result at expiry — *an empty ritual
with a digest attached*, which would be the first thing in this registry that
looked like governance without being it. And: **lowering a floor to make a
strategy feasible is the same act as raising one after seeing results.**

### 7.5 The mandatory noise floor (doctrine 4)

> *A measurement must report its own noise floor, not just its result. An
> assertion that cannot tell you when it is meaningless is worse than no
> assertion.*

**Three null arms minimum**, run identically to the real arms, on the same data,
through the same code path:

| arm | construction | catches |
|---|---|---|
| **shuffled labels** | targets permuted within market | the pipeline's own noise floor |
| **shifted features** | features from market *A* against labels from market *B*, same window | a "signal" that is really a shared time-of-day or venue-wide effect |
| **time-shifted labels** | same market, labels offset by > embargo | residual autocorrelation masquerading as prediction |

**No effect smaller than the largest floor may be described as real, regardless
of its p-value.** Every effect size in every artifact is printed **beside** the
floors. A single-null benchmark is forbidden: CP5's two-null-arm design exposed
a ~200,000 ns/ev floor against a ~900 ns signal and so caught a leaked process
pinning a core for two hours — a single-null benchmark would have reported
scheduler noise as an overhead result and **passed its gate for the wrong
reason**.

### 7.6 Mandatory positive controls (doctrine 7)

> *Every important metric needs a POSITIVE-CONTROL test: force the underlying
> condition to occur, and prove the metric becomes non-benign.* The failure
> class this targets is **a plausible benign value emitted by a broken path** —
> it does not crash, does not alert, and yields clean-looking datasets and
> convincing statistics.

**Per-metric controls, run before any gate's number is read:**

| force this | this must become non-benign |
|---|---|
| inject a synthetic effect at the declared minimum detectable size | G2 detects it at the declared power |
| shuffle the labels | the effect collapses into the noise-floor band |
| set the baseline equal to the candidate | G3 returns exactly "no increment" |
| set fees and spread to zero | G4 flips from fail to pass — **proves the cost term is wired at all** |
| set fees and spread to 10× | G4 fails and kappa moves — proves kappa is live |
| point the confirmation window at the discovery window | G7 **refuses** (prospectivity check is live) |
| corrupt one tape segment digest | G1 → `VOID_MEASUREMENT` |
| omit one ladder side from a snapshot | the row is **dropped as `NOT_PROVIDED`**, not imputed |
| add a known-inactive market to the universe | the universe rule excludes it |
| truncate `events.jsonl` / break the budget head | registration and evaluation both refuse |
| disconnect the metrics lane | the reachability test **fails** |

**Any positive-control failure is `VOID_MEASUREMENT`, not a result.** Nothing
else from that run may be reported. A missing measurement is not zero; a
disconnected metric is not healthy.

**Two factory-level sentinels, registered in every family, every epoch, and run
through the entire ladder alongside real strategies.** These are the strongest
device in this document, because they test the *gates* rather than the
strategies:

* **`NULL_SENTINEL`** — built on a provably uninformative feature (seeded PRNG
  bound to the tape hash, so it is reproducible and cannot be cherry-picked).
  **It must die at G2.** If it survives G2, the gate is broken and **every G2
  result in that family in that epoch is void.**
* **`KNOWN_EFFECT_SENTINEL`** — built on a synthetic feature carrying an
  injected effect of exactly the declared minimum detectable size, sized to sit
  *just below* the cost floor. **It must survive G2 and G3 and die at G4.** If
  it dies at G2, the factory is underpowered and every G2 kill in that epoch is
  `inconclusive`, not `killed`. If it survives G4, the economic gate is not
  biting and every G4 pass in that epoch is void.

The sentinels are counted in `m` and pay alpha like anything else. Their cost is
the price of knowing the ladder works. **A ladder with no sentinel cannot tell a
strategy that died from a gate that kills everything.**

### 7.7 Killed strategies are retained as negative controls

**Nothing is deleted.** Every terminal strategy moves to `RETAINED_AS_CONTROL`
and keeps its manifest, digests, feature definitions at pinned versions,
results, amendments and chain. This is the EDGE-DISCOVERY-001 §6 disposition
generalised: *STOP developing them. DO NOT delete them.*

**Five reasons, and each is load-bearing:**

1. **Instant comparison.** Any new technique is scored immediately against
   `market / retained control / candidate`. The ΔS instrument is the precedent —
   a 90-second, read-only, market-relative falsification harness, and *the
   durable asset* the milestone that produced it should be remembered for.
2. **A killed strategy is the cheapest available negative control.** A new
   pipeline that "discovers" a strategy already killed on non-overlapping data
   has a **defect**, not a finding. That check is free and catches look-ahead
   regressions before they reach a real hypothesis.
3. **Resurrection detection.** A new registration whose hypothesis digest,
   feature set and universe fall within a declared similarity bound of a
   retained control **must** cite the parent and state what changed. Without
   this, the same idea is tested repeatedly under new names, and the family
   count — the only thing standing between the factory and its own multiplicity —
   is wrong.
4. **Survivorship.** Deleting failures makes the graduation rate a lie. The
   factory's headline statistic is computed over **all** registrations including
   kills, and the retained graveyard is what makes that computable. Of 77
   audited agentic-trading papers, 1 documented survivorship; not documenting it
   is the field norm and it is the thing to refuse.
5. **The negative control must be mechanism-independent and specified without
   reference to any in-sample ranking.** A previous "negative control" in this
   repo was a *data-derived worst cohort*, and its out-of-sample inversion to
   best-in-class was exactly the regression to the mean you would predict. A
   retained control is specified by its frozen manifest, which is what makes it
   a legitimate control.

**Retained controls are re-scored every epoch at low frequency.** A retained
control that starts **passing** is an alarm about the pipeline before it is a
discovery: it must be investigated as a possible look-ahead regression, and if
it survives that, **re-registered as a new strategy with a new alpha draw**
before any claim. It may never be promoted in place.

### 7.8 Data and feature versioning against silent look-ahead

The rule the whole substrate rests on:
**`feature_set_version + tape_hash → identical result`.** A determinism test
recomputes the feature matrix from the tape and compares digests; a mismatch is
`VOID_MEASUREMENT`.

`data_version` pins **segment digests**, never a date range, because a date
range silently changes when a segment is repaired or backfilled.
`feature_version` pins the AST of the feature code together with its declared
lags, windows, typed-absence policy and drop policy — not just the file, because
the same code with a different lag is a different feature.

**Seven look-ahead classes, and the structural prevention for each:**

| # | class | how it happens | prevention |
|---|---|---|---|
| 1 | **recompute drift** | features recomputed after a bug fix now use information the original did not | features are pure functions of the tape at a pinned code digest; a semantic change creates a **new** `feature_version`, and results across versions are **never pooled**. An amendment may not declare comparability across a semantic feature change — including a defect fix |
| 2 | **backfill leakage** | the archive gains a segment covering an already-evaluated window | `data_version` pins segment digests; a new segment changes the digest and forces a new `strategy_id` or an explicit re-evaluation record. Segments are append-only and never edited in place |
| 3 | **cross-source ordering** | trade frames live on their own sid with their own sequence domain; only our receive timestamps relate them, and those carry collector latency | every cross-source feature declares a **lag** at registration, fixed by measurement before first use (measured max interarrival: 580 ms). Results reported at the declared lag **and at double it**; lag-dependence is reported as a **timing artefact**, not a finding. This is the single most likely way a microstructure result comes out fake |
| 4 | **sampling-density leakage** | event-triggered sampling makes density a function of activity, which correlates with volatility, which is the thing being predicted | fixed wall-clock grid, declared in `feature_version`; a row is emitted only when the book is `publishable` |
| 5 | **universe leakage** | markets chosen after seeing which produced attractive results | universe is a **separately committed artifact** with a **typed** `selection_method` (`exhaustive_series` · `exhaustive_event` · `scheduled_fixtures` · `random_sample_seeded`), resolved by digest, member count checked, **created before registration**. Prose selection methods are how `"hand picked after looking at results"` once passed |
| 6 | **absence as value** | `None → 0`, where the zero has economic meaning | `LadderState = NOT_PROVIDED \| EMPTY \| PRESENT(levels)`; absence is structurally representable, drop-not-impute, drop rate reported. `depth = 0` means the venue said the book is empty; `depth = unknown` means the venue said nothing |
| 7 | **baseline staleness** | candidate reads `t`, baseline reads `t − δ` | one information-set timestamp, **asserted by the harness**, not promised by the author (§5.2 rule 2) |

**And doctrine 8, which sits upstream of all seven.** Before any venue field
becomes an experimental variable — `timestamp` · `sequence` · `trade side` ·
`size` · `volume` · `open interest` · `book update` · `market status` ·
`liquidity` — re-read it across a known interval and observe **what actually
moves it**. The tape-manifest tool once reported that 73,057 of 73,630 markets
were stale: precise, dramatic, reproducible and **wrong**, because
`updated_time` is a market *definition* timestamp. Ten markets re-read 180 s
apart moved it 0/10 while lifetime volume moved 10/10. **The failure was not
noise. Noise is obvious. This was a confident false finding produced by assuming
a field meant what its name suggested.** G1 refuses any feature reading a field
that is not on the verified list.

---

## 8. Human-in-the-loop

**The failure mode is a rubber-stamp gate that always passes.** Five structural
devices, then the six decision points.

1. **Asymmetric authority.** The machine computes every verdict. A human may
   **veto** a pass, or **supply a judgment the machine cannot make**. A human
   may **never overturn a computed KILL.** Someone who believes a killed
   strategy deserves another look registers a new `strategy_id` and pays alpha.
   There is no "approve despite."
2. **Typed answers, non-operative reasons.** Each human gate presents the
   machine's computation and asks a **closed enum** question. The accompanying
   free text is retained and diffed, and is provably unread by the evaluator
   (Operative-Field Invariant). A reason that could change a verdict is a place
   an author can choose the verdict.
3. **Base-rate accountability — the direct antidote.** Every human decision
   point publishes its own historical answer distribution. **A gate whose
   approval rate is ≥ 95% over an epoch is flagged as a rubber stamp**, and
   every decision it made in that epoch is re-reviewed by a second reviewer who
   did not make them. The statistic is on the epoch report at H6.
4. **Declared falsifiers, machine-checked.** At H0 and H1 the reviewer writes,
   **before results exist**, what observation would make them reject
   (`mechanism.would_be_falsified_by`). At G6/G7 the evaluator checks whether
   those conditions occurred and reports it. **A reviewer whose declared
   falsifiers never fire is not reviewing.**
5. **Author ≠ reviewer**, recorded by identity, at H2, H3 and H4. Amendments
   require **two** reviewers, neither the author.

| | point | what the human is actually deciding | machine cannot do it because | if the human cannot answer |
|---|---|---|---|---|
| **H0** | Hypothesis admission, before freeze | Is there a **mechanism** that predicts the sign, the rough magnitude scale, and where it should *stop* working? And: is this worth an alpha draw against the other things this family could test? | Mechanism plausibility is a claim about the world, not about the data. A machine can check a mechanism field is *present*; only a human can judge whether it is a mechanism or a paraphrase of the correlation | refuse registration. An unjudgeable mechanism is `KILLED_NO_MECHANISM` |
| **H1** | Measurement admission, G1 exit | For any venue field **not already on the verified list**: was the doctrine-8 verification experiment adequate? Is the positive-control set adequate for *this* metric? | The machine checks fields are on the list. Deciding whether a *new* field's verification actually established causation of change is judgment | the field does not enter the list; features using it are refused |
| **H2** | **Cost-model verification, G4 entry** | Is the fee schedule verified against **venue documentation and realized fills**? Are the declared adverse bounds genuinely adverse? | We have no realized-fill corpus on Kalshi. There is no machine-checkable source of truth for the fee schedule | **the gate is `UNEVALUATED`, never `PASSED`.** `UNEVALUATED` does not advance. This is the most consequential decision in the ladder — see §9 |
| **H3** | Amendment legitimacy | Does the amendment's provenance rest **only** on artifacts outside this strategy's data sets? Is its `class` honestly assigned, or is a threshold change wearing a clarification's label? | Class assignment is an intent judgment. The machine enforces the *consequences* of a class; only a human can catch a misassigned one | reject the amendment. A rejected amendment leaves the manifest untouched, which is the safe direction |
| **H4** | Shadow promotion, G8 entry | Does running this on EVO-X2 threaten the **LIVE** plane? Is the kill switch tested? Are the resource slices real cgroups rather than conventions? | An operations judgment about a shared production host, not a statistical one | do not promote. The collector is never starved for a study |
| **H5** | **Capital authorization, G9** | Does the entire body of evidence justify risking money, **knowing this ladder can be wrong**? | Non-delegable. Not implied by G0–G8 all passing, and the document must say so in those words | no capital. Requires a separately accepted milestone that does not exist today |
| **H6** | Epoch review — the factory's own health | Are the sentinels behaving? What is the graduation rate over **all** registrations? What is each human gate's approval rate? Is the alpha budget being consumed on real hypotheses or on variants of one? **And the binding one:** have 20–30 well-preregistered hypotheses now produced no economically meaningful prospective edge — in which case reconsider the venue and the thesis rather than building another architectural layer | This is the only decision that can kill the factory rather than a strategy | the epoch does not close, and no new family may open |

**One duty that is cheap and easy to skip.** At every kill, a human signs that
the terminal status was recorded *and* the negative control retained. It takes
seconds and it is what makes death a **completed action** rather than an
abandonment. Abandoned strategies are how a graveyard quietly becomes a
selection filter.

---

## 9. What this specification cannot prevent

Stated plainly, because a ladder this elaborate is itself a source of false
confidence.

1. **Undeclared prior search outside the harness.** The variant counter is
   machine-derived only for fits that read governed data through the sanctioned
   path. A notebook, a scratch copy of the tape, or a conversation about what
   looked interesting increments nothing. The disjoint prospective replication
   at G7 is a **mitigation, not a solution**, and undeclared search remains the
   protocol's known hole. Treat it as a known hole rather than a solved problem.
2. **Hypothesis laundering through mechanism prose.** A fluent post-hoc
   mechanism for an effect already observed is indistinguishable from a prior
   one. The freeze anchors prove **when** the hypothesis was written; they prove
   nothing about **whether the author had already looked**.
3. **One analyst across many strategies.** Knowledge carries between strategies
   in a human head. No registry can purge a mind, and the same person choosing
   the next hypothesis is a correlated selection process the arithmetic does not
   model.
4. **A cost model wrong in the same direction every time.** Kappa protects
   against *magnitude* error in the modelled terms. It does not protect against
   a **systematically omitted** term, because the omitted term is not in the
   multiple. Only realized fills close this, and we have none on Kalshi.
5. **Regime change after graduation.** Every gate here is backward-looking. A
   strategy can pass all eleven and be dead the day it is funded because the
   venue changed. The factory has no gate that observes the future.
6. **Correlated survivors.** FDR and FWER control the error rate of the family;
   they say nothing about the **correlation of the survivors' P&L**. Two
   graduates in one `correlation_group` may be one bet, and this spec records
   the group without solving the portfolio problem.
7. **Adverse selection we cannot observe.** With no queue position on Kalshi
   L2, maker economics are unfalsifiable under observe-only. The spec's response
   is to **refuse to pass** such strategies (`EXECUTION_UNFALSIFIABLE`) — a
   scope limit, not a solution.
8. **Tampering by anyone with write access.** Chains, heads and digests are
   **tamper-evident, not tamper-proof.** Both files can be rewritten together;
   the evidence lands in a git diff, and that is the whole of the guarantee.
9. **Look-ahead inside the venue's own data.** If Kalshi assigns a timestamp at
   publication rather than at occurrence, every control here inherits the leak.
   Doctrine 8 is the only defence and it is manual, per field, forever.
10. **Ceremony mistaken for proof.** Eleven gates produce a document that *looks*
    like evidence. The sentinels (§7.6) and the all-inclusive graduation rate
    (§7.7) are the only two things that keep the ladder honest about **itself**;
    if either is dropped for convenience, the rest of this document becomes
    decoration.

---

## 10. Implementation ladder (not authorized here)

Sequenced so each step is validatable and none of it touches the frozen
collector or `scripts/`:

| step | deliverable | gate |
|---|---|---|
| **A0** | close QDK-001 §9.2 **D1–D10** in the existing registry | hard precondition; nothing below starts first |
| **A1** | strategy-object schema + validation + digest, reusing REGISTRY-002 machinery | schema tests, digest determinism, forbidden-field-by-identity |
| **A2** | family + alpha ledger (`policy.json`, `budget.jsonl`, head pin) | budget-exhaustion refusal, no-refund-on-death, cross-family overlap refusal |
| **A3** | freeze anchors 1–3, incl. the **data-non-existence** check at G7 | positive control: point confirmation at discovery, expect refusal |
| **A4** | harness token + machine-counted search log | declared-vs-counted mismatch invalidates |
| **A5** | evaluator for G1–G3, verdict precedence, shrinkage diagnostic | `NULL_SENTINEL` dies at G2; `KNOWN_EFFECT_SENTINEL` survives G2/G3 |
| **A6** | cost model, per-row floor, `net_conservative`, kappa, `UNEVALUATED` state | zero-cost and 10×-cost controls both move the verdict |
| **A7** | amendment schema + class enforcement + two-reviewer rule | post-freeze threshold change invalidates |
| **A8** | epoch report: graduation rate over all registrations, sentinel status, human approval rates | H6 has something to decide on |

**Explicitly absent from this ladder:** any execution surface, any EV
computation in any unit, any sizing, any order path, any capital. G9 and G10 are
specified so that the ladder is honest about where it ends — **not** so that they
can be built.

## 11. Rollback

Additive and inert: new committed directories, new CLI verbs, no migration, no
schema change, no production write, no MarketOps behaviour change, no timer, no
change to `app/realtime/` or `scripts/`. Revert the commits. Anything already
frozen remains in git history, which is the point.

---

**This document authorizes nothing.** It specifies a machine for falsification.
The correct prior is that its output will be a large graveyard, a handful of
`REAL_BUT_UNECONOMIC` results, and — if the venue does not cooperate — the H6
decision to reconsider the thesis rather than build another layer.

> **The best trade is still no trade.**
