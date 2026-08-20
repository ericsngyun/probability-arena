# RISK-GOVERNOR-001 — the veto layer

**Status: RESEARCH AND DESIGN ONLY. NOT ACCEPTED, NOT BUILT, NOT AUTHORIZED.**
Written 2026-08-20.

No production code, no feature flag, no migration, no schema change, no
behaviour change, no deployment, and no provider call has been made for this
document. Nothing here is implemented and nothing here authorizes a milestone.

**NO CAPITAL PATH.** This system has never traded and this document does not
give it the ability to. It specifies a component that would one day sit *in
front of* capital and refuse it. Specifying a brake is not building an engine,
and this document must never be cited as evidence that an engine was approved.
`docs/SAFETY_BOUNDARIES.md` governs throughout and is **not** amended,
narrowed, or reinterpreted by anything below. Every row of its forbidden table
— dollar EV, trade recommendations, paper trading, portfolio sizing, order
placement, wallets, live execution — remains forbidden with no implementation
surface.

**Data hygiene for this document.** It reasons from `AGENTS.md`,
`docs/milestones/QUANT-DECISION-KERNEL-001.md`,
`docs/research/QDK-001-risk-and-sizing.md`,
`docs/milestones/MARKET-STATE-FABRIC-v1.md` and
`docs/experiments/MARKET-MICROSTRUCTURE-EDGE-001.md`. It consumed **no data
from any live experiment**, read nothing under `PROD-ACTIVITY-PROFILE-001`, and
touched nothing in `app/realtime/` or `scripts/`, which are frozen.

**The AST audit will fail on an implementation of this, and that is correct.**
`frontier-eval-report --include-safety` bans `kelly`, `position_siz`,
`portfolio`, `expected_value` and friends as identifiers anywhere in `app/`. An
implementation named the obvious way will fail the audit. The correct response
is a separate, narrowly-reviewed unbanning of the exact fragment in the exact
file at the time an implementation actually exists — never a rename to slip
past the scan.

---

## Table of contents

- [§1. What the governor is, and the one property that matters](#1-what-the-governor-is-and-the-one-property-that-matters)
- [§2. Placement, and the rule that keeps the LLM plane out](#2-placement-and-the-rule-that-keeps-the-llm-plane-out)
- [§3. The prior that sets every default](#3-the-prior-that-sets-every-default)
- [§4. Typed absence — the encoding contract](#4-typed-absence--the-encoding-contract)
- [§5. `B_t` — the state-dependent risk budget](#5-b_t--the-state-dependent-risk-budget)
- [§6. The position ceiling — a `min` over independent constraints](#6-the-position-ceiling--a-min-over-independent-constraints)
- [§7. Kelly as an upper theoretical reference, and what `λ` should be](#7-kelly-as-an-upper-theoretical-reference-and-what-λ-should-be)
- [§8. High-volatility operating mode](#8-high-volatility-operating-mode)
- [§9. Prediction-market specifics that break the equities intuition](#9-prediction-market-specifics-that-break-the-equities-intuition)
- [§10. What the governor needs and does not have](#10-what-the-governor-needs-and-does-not-have)
- [§11. The escalation ladder](#11-the-escalation-ladder)
- [§12. Kill switches](#12-kill-switches)
- [§13. Observability — auditing a veto after the fact](#13-observability--auditing-a-veto-after-the-fact)
- [§14. Positive-control matrix](#14-positive-control-matrix)
- [§15. What would falsify this design](#15-what-would-falsify-this-design)
- [§16. What this design cannot do](#16-what-this-design-cannot-do)
- [§17. Placeholder register](#17-placeholder-register)
- [§18. Open decisions](#18-open-decisions)

---

## 1. What the governor is, and the one property that matters

The governor is a **deterministic, synchronous, non-learning** component that
receives a typed proposal from a strategy and returns exactly one of:

```
NO_TRADE(codes)            — the default, and the most frequently correct answer
CEILING(f, binding, slack) — a ceiling, never an instruction
HOLD                       — inside the no-trade band; do nothing, pay nothing
```

Three properties define it, in order of importance.

**(1) It can say NO, and `NO_TRADE` is a first-class action.** This is the whole
component. Everything else is machinery in service of it. The failure mode that
kills these systems is not a bad size — it is a system with **no structural way
to decline**. QDK §7.1 identified this precisely: a `min` over positive
quantities is positive, so the formula in the brief *cannot abstain*. Encoding
"we do not know this market's calibration, so we decline" as "the liquidity cap
happened to be zero" is not a representation; it is a coincidence that will stop
holding the moment a term changes.

`NO_TRADE` is therefore:

* a **return value**, not a fallthrough;
* carrying a **closed, typed, ordered reason-code set** — free text is forbidden
  for the same reason PROSPECTIVE-EXPERIMENT-REGISTRY-002A replaced its prose
  leakage guard with a typed predicate schema: prose is paraphrase-bypassable
  and cannot be aggregated;
* **counted in the denominator** — every candidate enters the denominator
  whether vetoed or not, or the abstention rate is unmeasurable;
* **not a KPI to drive down.** A governor that vetoes 99% of candidates is
  behaving correctly if 99% of candidates fail a gate. Pressure to "increase
  coverage" goes through changing a *threshold*, explicitly, versioned into the
  record — never through weakening a gate's evaluation.

**(2) It has veto authority over every strategy, and the veto is not
overridable.** No `force` flag. No `--yes-i-know`. No per-strategy exemption
list. A `force` path is how every one of these systems eventually fails, because
the path exists for months before it is used and nobody re-reviews it on the day
it is. If a gate must be relaxed, the *threshold* changes, the change is
versioned into the prospective record, and **that invalidates the prior sample
for any acceptance test** — as it should.

**(3) It is monotone: it may only ever reduce.** No input to the governor may
increase a position. This applies to the correlation graph (QDK §6.4 asymmetry
rule), to the volatility state, to the drawdown state, and to every future
input anyone adds. A graph silence, an `INDEPENDENT` finding, a
`MUTUALLY_EXCLUSIVE` edge that mathematically justifies a hedging credit — none
of them may grow a position. A one-directional governor is wrong in the
direction that costs growth; a two-directional governor is wrong in the
direction that costs the bankroll.

### 1.1 What it is not

* It is **not** an allocator. It does not choose what to trade or which side.
* It is **not** a forecaster, and it never computes or adjusts `p`.
* It is **not** an optimizer. It emits a ceiling; the allocator lives elsewhere
  and is separately gated.
* It is **not** a source of confidence. A `CEILING` return is not an
  endorsement. The governor's silence about a candidate's merit is not
  approval — it means only that no gate fired.

---

## 2. Placement, and the rule that keeps the LLM plane out

```
                 ASYNCHRONOUS PLANE                    │   SYNCHRONOUS PLANE
                 (may be slow, may call models)        │   (deterministic, bounded)
  ─────────────────────────────────────────────────────┼──────────────────────────────
  research / forecasting (LLM)                         │
      ↓ writes p̂ + model id + version                  │
  scoring, calibration fitting (β̂ per regime)          │
      ↓ writes a VERSIONED CALIBRATION ARTEFACT        │
  correlation graph construction                       │
      ↓ writes a VERSIONED GRAPH SNAPSHOT + coverage   │
  cost / fill / markout model fitting                  │
      ↓ writes a VERSIONED COST ARTEFACT               │
  ─────────────────────────────────────────────────────┤
                                                       │  strategy proposes
                                                       │      ↓
                                                       │  ┌─────────────────┐
                                                       │  │  RISK GOVERNOR  │  ← reads only
                                                       │  │  (this document)│    versioned
                                                       │  └─────────────────┘    artefacts
                                                       │      ↓
                                                       │  NO_TRADE | CEILING | HOLD
```

### 2.1 The rule, stated as an invariant

> **The agent/LLM plane is never in the synchronous risk path.** The governor
> makes no model call, executes no prompt, parses no natural language, and
> reads no free-text field. Its entire input surface is typed numeric and
> enumerated values plus artefact version identifiers.

Enforcement, so this is a property and not a promise:

| enforcement | mechanism |
|---|---|
| no model call | the governor's module has a dependency-direction test: no import path reaches any provider client, HTTP client, or prompt module. This is the same shape as the audit that (wrongly) forbade the CP3.5 wiring — so when it is written, it must assert that the *permitted* imports exist, not only that forbidden ones do not (doctrine 4) |
| no free text | every governor input field is a typed scalar, an enum member, or a typed absence. There is no `str` field except artefact identifiers and version hashes, which are compared, never interpreted |
| no unbounded latency | the governor has a declared wall-clock budget; exceeding it is a fault, not a slow success, and the fault is `NO_TRADE(GOVERNOR_TIMEOUT)` |
| artefacts are frozen at read | the governor reads a calibration/graph/cost artefact **by version**, and records that version in the decision record. A mid-decision artefact swap is a fault |

**Why this is non-negotiable, with the measurement behind it.** Prediction
Arena ran six frontier LLMs as end-to-end Kalshi agents with $10k each over 57
days. **All six lost money, −16% to −31%**, and prompt-level risk guidance was
"frequently ignored" — only hard-coded constraints held (QDK §0.1(c)). A risk
rule expressed in a prompt is a suggestion. A risk rule expressed as a return
value in deterministic code is a rule.

The second measurement is about ordering, not plane: in a controlled replay with
the forecaster held fixed so every policy saw identical probabilities,
**edge-proportional sizing with no selection gate returned −55.5%, roughly five
times worse than flat stakes at −4.7%**, because it concentrates capital on
confidently-wrong high-edge forecasts (QDK §7.2). **Selection precedes sizing.**
The governor's gate runs before any size is computed, not as a post-filter on
one.

---

## 3. The prior that sets every default

QUANT-DECISION-KERNEL-001 established the identity that makes this document's
defaults non-arbitrary. For a binary contract at market price `q` with belief
`p`, the Kelly fraction is `f* = (p − q)/(1 − q)`, and substituting it into the
expected log-growth gives, exactly:

```
g(f*) = p·ln(p/q) + (1 − p)·ln((1 − p)/(1 − q)) = KL(p ‖ q)
```

> **Tradable growth and log-score advantage over the market are the same
> quantity.** There is no such thing here as "a good forecaster with no edge" or
> "an edge without probabilistic skill."

Three consequences that the governor is built on:

1. **We have never measured positive skill against the market.** Every headline
   skill figure this project has reported is `brier_skill_vs_base_rate` —
   level-1 evidence that bears on nothing. The market-anchored rows that exist
   are `p ≡ q` by construction, so `ΔS ≡ 0` on them. **PAIRED = 0.**
2. **The correct prior is `e_net ≤ 0`.** Externally: the best published agentic
   forecaster beats market prices by +2.3 Brier-index and **not significantly**
   (n=200); six frontier LLM agents all lost; optimizing forecast accuracy
   improved Brier while *worsening* return. Internally: all six EDGE-SELECTION
   candidates inverted out of sample and the negative control was best of eight;
   all four EDGE-DISCOVERY experiments failed; the one real replicating effect
   (E2's 2.36pt one-hour lead) was uneconomic at 70% of a 3.36pt cost floor. And
   the sports forecaster is understood mechanically: `logit(p) = −0.094 +
   0.568·logit(q)`, R² = 0.661 — **the model is the market, blurred.**
3. **Therefore the governor's modal output is `NO_TRADE`, and that is the
   correct behaviour, not a defect.** Under `e_net ≤ 0`, the Kelly numerator
   `(p_con − q)` is negative for most candidates, so the ceiling is ≤ 0 before
   any other constraint is consulted. A governor that rarely vetoes, given this
   prior, is broken.

**The design rule this produces:**

> **Every threshold defaults to its most restrictive admissible value, and
> loosening it requires a measurement, named, with its denominator and its
> minimum detectable effect. Tightening requires nothing.**

---

## 4. Typed absence — the encoding contract

Doctrine 10 in one line: *never encode epistemic absence as a numerical market
state.* This repository has been bitten by it more than by any other class.

| | means |
|---|---|
| `depth = 0` | the venue said the book is empty |
| `depth = NOT_PROVIDED` | the venue said nothing |
| `σ = 0` | measured, and the market did not move |
| `σ = NOT_MEASURED` | we did not measure it |
| `ρ = 0` | the pair was analysed and found unrelated |
| `ρ = COVERAGE_UNKNOWN` | the pair was never analysed |

### 4.1 The governor's absence rule

> **Every governor input is a tagged value. An input tagged absent maps to the
> most restrictive value its term can take — and the reason code that names the
> absence is emitted whether or not that term ends up binding.**

The second half is the part people drop, and it is the important half. If
`f_liquidity` is 0 because depth is `NOT_PROVIDED`, the *number* is the same as
if depth were genuinely 0 — but the *record* is not, and the record is what a
human audits. Concretely:

**Worked example — two vetoes that look identical and are not.**

| | case A | case B |
|---|---|---|
| venue said | ladder present, zero resting size at the touch | no ladder for this generation (awaiting snapshot) |
| `depth_ask_l1` | `EMPTY` | `NOT_PROVIDED` |
| `f_liquidity` | `0` | `0` |
| `min(...)` | `0` | `0` |
| verdict | `NO_TRADE` | `NO_TRADE` |
| **reason code** | `LIQUIDITY_BELOW_THRESHOLD` | **`BOOK_UNPUBLISHABLE`** |
| what a human learns | this market is genuinely untradeable right now | **our collector is blind and every other feature on this row is also suspect** |

Case B is an infrastructure incident wearing a market-condition costume. If both
emit the same code, the incident is invisible for as long as the market happens
to be thin. The numbers being equal is exactly why the codes must differ.

### 4.2 The closed reason-code set

Inherited from QDK §7.2, extended with the codes this document's new terms
require. Ordered; when multiple fire, **all** are recorded and the first in this
order is the reported `primary_code`.

| # | code | condition |
|---|---|---|
| 1 | `KILL_SWITCH_ACTIVE` | manual halt, or a §12 switch has fired |
| 2 | `GOVERNOR_TIMEOUT` | the governor exceeded its wall-clock budget |
| 3 | `GOVERNOR_INPUT_INCOHERENT` | inputs failed an internal consistency check (e.g. `bid > ask`, negative depth, artefact version mismatch mid-decision) |
| 4 | `OBSERVATION_GAP` | the decision timestamp falls inside a period the tape was blind |
| 5 | `BOOK_UNPUBLISHABLE` | sequence fault or integrity halt on venue state; ladder `NOT_PROVIDED` |
| 6 | `STALE_STATE` | quote, forecast, graph, calibration, or cost artefact older than its regime's freshness bound |
| 7 | `CLOCK_SKEW_EXCEEDED` | collector-to-venue or host-to-NTP skew beyond bound — every freshness test is meaningless below this |
| 8 | `FILL_MODEL_ABSENT` | **new.** No fill model exists, or its version is not registered (§10) |
| 9 | `MARKOUT_MODEL_ABSENT` | **new.** No markout model exists for this strategy class (§10) |
| 10 | `COST_MODEL_UNVERIFIED` | **new.** The cost model has never been checked against a realised fill (§10) |
| 11 | `CALIBRATION_UNKNOWN_FOR_REGIME` | fewer than the floor of scored forecasts in this regime cell, or the `β̂` CI half-width exceeds its bound |
| 12 | `CALIBRATION_FAILED_FOR_REGIME` | the regime shows negative skill — live instance: tennis |
| 13 | `MODEL_VERSION_UNTESTED_IN_REGIME` | this model version has no prospective record in this regime |
| 14 | `DISPERSION_SOURCE_ABSENT` | **new.** `p_conservative` has no *measured* dispersion source; a self-reported posterior width is not one (§7.2) |
| 15 | `GRAPH_COVERAGE_UNKNOWN` | the correlation graph did not analyse this instrument. Absence of an edge is not evidence of independence |
| 16 | `EXECUTION_COST_EXCEEDS_EDGE` | net edge at or below zero at the row's own state |
| 17 | `KAPPA_BELOW_FLOOR` | the cost-kill multiple is below 2 (QDK §7.4) |
| 18 | `EDGE_BELOW_MINIMUM` | net edge below the smallest edge the sample can ever validate |
| 19 | `LIQUIDITY_BELOW_THRESHOLD` | resting size below the rung's requirement, **or depth unmeasured** |
| 20 | `UNFILLABLE_AT_RUNG` | the visible ladder cannot fill this rung |
| 21 | `EXTRAPOLATION_OUTSIDE_SUPPORT` | an input sits outside the calibrator's or cost model's fitted support |
| 22 | `TAIL_UNVERIFIED_FOR_REGIME` | the prospective sample is too small to have seen a rare common-mode regime. **"Unmeasured" is not "satisfied"** |
| 23 | `TAIL_CONSTRAINT_BINDING` | book-level EVaR at its limit |
| 24 | `CORRELATED_EXPOSURE_AT_LIMIT` | cluster budget exhausted |
| 25 | `SETTLEMENT_EXPOSURE_AT_LIMIT` | **new.** Terminal-settlement notional at its cap (§9.1) |
| 26 | `RESOLUTION_CONCENTRATION_AT_LIMIT` | **new.** Too much exposure resolving inside one time bucket (§9.1) |
| 27 | `CAPITAL_LOCK_BUDGET_EXHAUSTED` | **new.** Capital-days budget exhausted (§9.2) |
| 28 | `BUDGET_EXHAUSTED` | book budget at its limit |
| 29 | `BUDGET_BELOW_FLOOR` | **new.** `B_t < B_min` — the state-dependent budget has collapsed (§5.4) |
| 30 | `VOL_STATE_PROHIBITS_CLASS` | **new.** The current volatility state prohibits this strategy class (§8.5) |
| 31 | `VOL_STATE_UNKNOWN` | **new.** Volatility state could not be computed. Not "calm" (§8.3) |
| 32 | `SIZE_ROUNDS_ABOVE_LIMIT` | integer or lot rounding would push the realised fraction above the ceiling |
| 33 | `RESOLUTION_CRITERIA_AMBIGUOUS` | the resolution rule is not machine-checkable, or its source is disputed |
| 34 | `REGIME_SHIFT_SUSPECTED` | recent residual distribution differs from the calibration window — our errors have become correlated |
| 35 | `NOT_MARKABLE` | no executable exit quote exists, so the position could not be honestly marked |

Codes 1–10 are **infrastructure** codes. Their rate is a health metric, not a
market metric, and mixing them into "the market was unattractive" is the single
most likely way this component lies to its operator.

---

## 5. `B_t` — the state-dependent risk budget

```
B_t = B_0 · f(σ_t) · g(liquidity_t) · h(drawdown_t) · j(modelUncertainty_t)
```

`B_t` is a **book-level** quantity: the total fraction of the modelled bankroll
the governor will permit to be at risk at time `t`. It is not a position size
and it is never divided evenly across candidates.

### 5.1 Why multiplicative, and the one thing multiplicative gets wrong

Multiplicative is right because it makes the budget **monotone decreasing in
every factor independently**: any one factor going bad shrinks the budget
without needing to argue about weights. Each factor is confined to `[0, 1]`, so
`B_t ≤ B_0` always — the budget can never grow above its declared ceiling, which
is §1's monotonicity property expressed arithmetically.

What multiplicative gets wrong: **a product of four small-but-nonzero factors is
small and nonzero**, and every downstream `> 0` check passes. Four factors at
0.3 give `B_t = 0.0081·B_0` — a budget that is arithmetically alive and
economically meaningless, and that will produce sizes below `f_min` where the
1-cent fee minimum dominates. Hence §5.4's floor, which is not optional
decoration but the repair for the shape's known defect.

### 5.2 The four factors

Every threshold below is a **PLACEHOLDER AWAITING MEASUREMENT** unless marked
otherwise, and is registered as such in §17. They are shape and direction, not
calibration.

#### `f(σ_t)` — volatility

| input | source | absence |
|---|---|---|
| `realized_vol_Δ` at Δ ∈ {30 s, 300 s} | MARKET-STATE-FABRIC-v1 §4, derived from 1 s mid changes | `NOT_MEASURED` |
| `quote_reversal_Δ` | fabric §4 | `NOT_MEASURED` |
| `σ_ref` — the regime's reference volatility | a measured baseline distribution that **does not yet exist** | `ABSENT` |

```
f(σ_t) = clamp( (σ_ref / σ_t)^γ , 0, 1 )        γ = 1  [PLACEHOLDER]
f(σ_t) = 0                                      if σ_t or σ_ref is absent
```

Note the exponent choice is a placeholder and the *reference* is the harder
problem: `σ_ref` requires a measured volatility distribution per regime, and no
such measurement exists. Until it does, `f(σ_t) = 0` and the governor vetoes
everything on `VOL_STATE_UNKNOWN`. **That is the intended behaviour at rung 0.**

A trap worth naming (doctrine 8): `σ_t` computed on an event-triggered sample is
not volatility, it is activity. Fabric §2 fixes the sampling grid at wall-clock
1 s for exactly this reason. If anyone later computes `σ_t` per-event, the
quantity stops meaning what its name says and the whole factor silently
inverts — high activity would *raise* the sample density that estimates the
volatility that is supposed to cut the budget.

#### `g(liquidity_t)` — liquidity

| input | source | absence |
|---|---|---|
| `depth_bid_5c`, `depth_ask_5c` | fabric §3 | `NOT_PROVIDED` |
| `spread` | fabric §3 | undefined if either side is `EMPTY` |
| `levels_bid`, `levels_ask` | fabric §3 | `NOT_PROVIDED` |

```
g(liq_t) = clamp( D_book(t) / D_ref , 0, 1 ) · clamp( s_ref / spread_t , 0, 1 )
g(liq_t) = 0        if any component is NOT_PROVIDED
```

`D_book(t)` is aggregate *executable* depth across the book, computed by walking
the ladder — never inferred, never synthesised. **This is a visible-depth
measure and therefore an upper bound on true available liquidity**, biased in
the dangerous direction, and it stays that way until the fill model of §10
exists. That bias is registered, not hidden.

#### `h(drawdown_t)` — wealth state

This factor is the **Grossman–Zhou** shape: size on the excess over the halt
level, not on total wealth.

```
κ(W) = clamp( (W_t − W_hard) / (W_0 − W_hard) , 0, 1 )
h(drawdown_t) = κ(W_t)
```

With `W_hard = 0.80·W_0` (the hard-halt level, §12), `κ` is 1.0 at epoch start,
0.5 at `W = 0.90·W_0` — which is exactly the soft-halt level — and 0 at the hard
halt. The budget therefore approaches zero *continuously* as the halt
approaches, rather than being fine until it is not.

**This is the non-redundant part of a drawdown constraint.** The other part —
the constant fraction implied by a max-drawdown tolerance — lives inside `λ`
(§7.2) and must not be applied twice.

#### `j(modelUncertainty_t)` — model uncertainty

The factor most likely to be quietly set to 1.0 by someone in a hurry. Its
inputs are all *measurable*, which is the point:

| input | definition | absence |
|---|---|---|
| `β̂_r` CI half-width | Cox calibration slope per regime, log-odds space | `CALIBRATION_UNKNOWN_FOR_REGIME` |
| `ρ̂_model` | `corr(r_i, r_j)` over residuals `r = Y − p̂`, grouped by feature / model version / regime / time bucket | `COVERAGE_UNKNOWN` |
| regime-shift statistic | distance between the recent residual distribution and the calibration window's | `NOT_MEASURED` |
| cost-model residual | `Ĉ(s) − C_realized(s)` — **does not exist yet** (§10) | `ABSENT` |

```
j(mu_t) = clamp(1 − w_β·(CI_half_width/CI_bound), 0, 1)
        · clamp(1 − w_ρ·(ρ̂_model/ρ_bound),        0, 1)
        · clamp(1 − w_shift·(shift_stat/shift_bound), 0, 1)
j(mu_t) = 0     if any input is absent
```

All weights `w_*` are **PLACEHOLDERS**. The structure matters more than the
weights: `j` is the only factor that reads *our own error* rather than the
market's state, and QDK §6.2(c) is blunt that model-error correlation is "the
one nobody instruments" and simultaneously **the cheapest to measure**, because
the residuals already exist on 12,945 scored forecasts. There is direct local
precedent for why it matters: all six EDGE-SELECTION-001 candidates failed out
of sample **together**, and the `spread_only` negative control beat them. Six
"independent" policies producing one correlated failure is this mechanism in
pure form.

### 5.3 Composition with the position ceiling

`B_t` and the `min` of §6 are different objects and must not be confused:

```
B_t          book-level: total fraction at risk across all open positions
f_ceil,i     position-level: the most this ONE candidate may take
```

Both bind. A candidate passes only if it clears its own ceiling **and** the
incremental book exposure stays within `B_t`. The order is: `B_t` is computed
first and is an input to the per-position tail term (§6.2), so a collapsed
budget vetoes before any per-position arithmetic runs.

### 5.4 The floor, and the ratchet

**The floor.** `B_min` is a declared minimum below which the budget is treated as
zero:

```
if B_t < B_min:   NO_TRADE(BUDGET_BELOW_FLOOR)
```

`B_min` is derived, not chosen: it is the smallest book budget at which the
minimum viable position (§6.2 `f_min`) is still expressible. Kalshi's fee rounds
up to the whole cent **on the order**, so per-contract taker cost at P = 0.50 is
2.00¢ at C = 1, 1.80¢ at C = 10, 1.75¢ at C = 100 — and at P = 0.03 a
one-contract order pays the 1¢ minimum against a 0.21¢ marginal rate, roughly
**5× the marginal rate**. This implies a real `f_min`: **at least 10 contracts,
prefer 20** (QDK §7.4). `B_min` follows from `f_min` and the declared bankroll.

**The ratchet.** Within an epoch, `B_t` may fall freely and may rise only
subject to:

| rule | reason |
|---|---|
| **Down is immediate.** Any factor deteriorating applies on the next decision | the cost of being slow to shrink is the whole bankroll |
| **Up requires dwell.** A factor may only improve the budget after it has held its improved value for a declared dwell period | prevents a single clean tick from re-opening the budget mid-incident |
| **Up is capped by the epoch's high-water `B`.** `B_t` never exceeds `B_0` | §1 monotonicity |
| **After a kill switch, up requires a human.** No automatic recovery | §12 |

Asymmetric hysteresis is the entire point: the system enters caution on one
observation and leaves it on a sustained pattern.

---

## 6. The position ceiling — a `min` over independent constraints

The brief's form:

```
position_t = min( λ·f_Kelly, f_CVaR, f_liquidity, f_inventory, f_drawdown )
```

The shape is right and should be kept: **a `min` over caps is a statement that
says "no more than this, for each of several independent reasons", and any one
of them binds alone.** That is exactly the semantics a veto layer wants, and it
has the property that adding a constraint can only ever reduce — §1's
monotonicity, again, expressed arithmetically.

Three of the five terms as literally named are degenerate. This document does
not average that disagreement away; it states it.

### 6.1 Reconciliation with QUANT-DECISION-KERNEL-001

| brief term | QDK finding | this document's resolution |
|---|---|---|
| `λ·f_Kelly` | Kelly is a **ceiling, not the allocator** (§2.7) | **kept, unchanged.** It is the only term that is an upper bound derived from the edge itself |
| `f_CVaR` | **CUT from the per-position `min`** (§3.3). A single binary position's loss distribution is two-point, so for any `α ≤ 1 − p`, `VaR_α = CVaR_α = f` **exactly** — a flat cap wearing a risk-measure costume. Worse: at `p = 0.93, α = 0.20 > 1 − p`, the "tail" contains *winning* outcomes and CVaR goes **negative** (−0.0102), reporting the position as guaranteed profit. The sign of `α − (1 − p)` silently switches the constraint between a flat cap and a no-op and nothing in the notation warns you | **slot kept, term replaced by `f_tail`** — the *marginal* book-level tail contribution (§6.2). This is a genuine per-position number that is not a disguised flat cap, and it preserves the brief's `min` shape without preserving its degeneracy |
| `f_liquidity` | kept (§7.1) | **kept**, with typed absence and the visible-depth bias registered |
| `f_inventory` | not in QDK's form; QDK had `f_concentration`, and found it **is not a cap at all — it is a joint allocation** (§3.3) | **kept and redefined** as the prediction-market-specific term this system actually needs: terminal-settlement inventory (§9.1). Cluster concentration is handled as a joint constraint in the allocator, not as a `min` term, per QDK |
| `f_drawdown` | **already inside `λ`** (`λ = λ_calib × λ_dd`), so a separate term applies it twice (§3.3) | **kept, redefined** as the distance-to-halt cap — the genuinely distinct, state-dependent part QDK says the form is missing. `λ_dd` stays in `λ`; `f_drawdown` is about the *remaining* room, not the *tolerance* |

**Three terms the brief's form omits entirely**, per QDK §7.1, and which the
governor must carry:

* `f_min` — a **minimum** size below which discretisation and fees dominate and
  the correct action is `NO_TRADE`, not a tiny position. A `min` cannot express
  a minimum; it is a gate.
* `Δt` — `g` is per *resolution*, so the rankable quantity is `g/Δt` (§9.2).
* the **target-versus-increment** distinction — the output is a target with a
  no-trade band, because every rebalance pays the full cost wedge and a system
  chasing a moving target pays it repeatedly for no edge.

### 6.2 The repaired terms

```
f_ceil,i = min( λ · f_Kelly(p_con,i, q_i),      # §7 — the edge ceiling
                f_tail,i,                        # marginal book tail
                f_liquidity,i,                   # executable depth
                f_inventory,i,                   # terminal settlement
                f_drawdown )                     # distance to halt
```

**`λ · f_Kelly(p_con, q)`** — §7. Note `p_con` is a *conservative quantile of a
measured dispersion*, applied after log-odds recalibration. If no measured
dispersion source exists, this term is not "Kelly at the point estimate"; it is
`NO_TRADE(DISPERSION_SOURCE_ABSENT)`.

**`f_tail,i`** — the largest `f_i` such that adding position `i` to the current
book keeps `EVaR_α(book ∪ {i}) ≤ D_max`. Properties:

* It is a **per-position number derived from a book-level constraint**, so it
  varies with what is already held — which is the information the degenerate
  per-position CVaR did not contain.
* Its **entire informational content is the estimate of `ρ`.** At `ρ ≥ 0.6` the
  5% tail is "everything loses at once", `CVaR_α = Σf` exactly, and the
  constraint degenerates back into a gross-exposure cap (`CVaR95/Σf = 1.000` in
  QDK §4.3's simulation). That is not a reason to drop it; it is a reason to
  **report the degeneracy on the artefact** so nobody reads a gross cap as a
  tail measurement.
* EVaR, not CVaR: convex (so the constrained problem stays convex), conservative
  (right direction of error for a constraint whose input is the least reliable
  number in the system), and its dual is a KL ball, which is the honest way to
  price model error into a tail constraint. It is **not** more robust to
  estimate — the MGF is dominated by the same handful of tail points.
* **The tail must never be estimated purely empirically at our sample sizes.**
  The constraint binds on `max(empirical, scenario)` where the scenario floor is
  an explicitly enumerated, hand-specified set of common-mode events with
  *assigned* probabilities. Justification, and it is decisive: for a common-mode
  regime of frequency 0.6%, at n = 50 **59% of samples contain no instance of
  the regime that defines the tail**, and those samples produce a confident,
  low-variance, systematically-too-small estimate. A once-a-year event in a
  system producing 250 observations a year is absent from the sample 37% of the
  time. That is an identification problem; no estimator fixes it.
* Every tail estimate is emitted **with the number of tail observations behind
  it.** A `CVaR99` from 250 observations is the average of two numbers and will
  be reported to four decimal places unless the count travels with it.
* If regime coverage is below the size at which a `π = 1/250` event would be
  expected to appear: `TAIL_UNVERIFIED_FOR_REGIME`. **Unmeasured is not
  satisfied.**

**`f_liquidity,i`** — the fraction fillable at the executable ladder within the
declared slippage allowance, computed by walking the ladder to the rung's size.
Never from mid, never from a synthesised depth curve. Depth `NOT_PROVIDED` →
`0` with `LIQUIDITY_BELOW_THRESHOLD` *and* the `BOOK_UNPUBLISHABLE` code that
explains why (§4.1). **Registered bias: this is visible depth, an upper bound,
and it is wrong in the dangerous direction until §10's fill model exists.**

**`f_inventory,i`** — §9.1. A cap on *terminal-settlement-exposed* notional per
resolution cluster, not per market. This is the term with no equities analogue.

**`f_drawdown`** — the distance-to-halt cap, position-independent within a
decision:

```
f_drawdown = c_dd · (W_t − W_hard) / W_t          c_dd  [PLACEHOLDER]
```

Distinct from `λ_dd` (a fixed fraction implied by a stated tolerance) and from
`h(drawdown_t) = κ(W)` (a budget factor). Applying all three is not
double-counting only if each is defined on a different object: `λ_dd` on the
tolerance, `κ(W)` on the book budget, `f_drawdown` on the single position. If an
implementer cannot state which object a drawdown term is defined on, they are
double-counting.

### 6.3 Order of operations

```
0. GATE        codes ← evaluate_gates(state, forecast, book, graph, cost, wealth, vol)
               if codes ≠ ∅:                            return NO_TRADE(codes)   # THE DEFAULT

1. BUDGET      B_t ← B_0 · f(σ) · g(liq) · h(dd) · j(mu)
               if B_t < B_min:                          return NO_TRADE(BUDGET_BELOW_FLOOR)

2. BELIEF      p_cal ← σ( â_r + β̂_r · logit(p̂) )        per-regime recalibration
               p_con ← Q_α( MEASURED dispersion )       else NO_TRADE(DISPERSION_SOURCE_ABSENT)

3. CEILING     λ     ← λ_calib × λ_dd × κ(W) × λ_evidence
               f_ceil,i ← min( λ·f_Kelly(p_con,q), f_tail, f_liquidity,
                               f_inventory, f_drawdown )
               if f_ceil,i ≤ 0:                         return NO_TRADE(binding code)

4. ECONOMIC    net edge at the row's own state, after half-spread + round-trip fees
               if net ≤ 0:                              return NO_TRADE(EXECUTION_COST_EXCEEDS_EDGE)
               if κ_cost < 2:                           return NO_TRADE(KAPPA_BELOW_FLOOR)

5. DISCRETISE  if realised_fraction(round(f)) > f:      return NO_TRADE(SIZE_ROUNDS_ABOVE_LIMIT)
               if f < f_min:                            return NO_TRADE(EDGE_BELOW_MINIMUM)
               if |f − f_current| < no_trade_band:      return HOLD

6. EMIT        the decision record of §13 — ALWAYS, including for every NO_TRADE
```

**Rounding is systematically upward-biased in risk** if implemented as
`max(1, round(...))`, which is the obvious implementation. Step 5 is written so
that rounding failure produces `NO_TRADE`, never a bigger bet.

**Step 4 is not redundant with step 3.** Step 3 asks "how much may we hold";
step 4 asks "is there anything left after the venue takes its cut". They fail
for different reasons and produce different reason codes, and QDK §7.2 notes
that if `EXECUTION_COST_EXCEEDS_EDGE` dominates the abstention distribution,
then the programme's binding constraint is the venue and the cost model, **not
the forecaster** — which is worth knowing before spending two years collecting
trades.

**Worked example — a candidate that is statistically real and still vetoed.**
Take the strongest short-horizon result in the microstructure literature, given
the most generous possible reading: a *perfect* one-tick-ahead prediction at
`P(up) ≈ 0.85` under extreme queue imbalance, traded costlessly at the mid.

```
gross EV   = 0.85·(+1¢) + 0.15·(−1¢)  =  +0.70¢ / contract
round-trip taker fee at P = 0.50      =   3.50¢ / contract
net                                    =  −2.80¢ / contract
```

The best-case documented microstructure edge is **one fifth** of the venue's
round-trip taker cost. Step 3 would have returned a positive ceiling. Step 4
returns `NO_TRADE(EXECUTION_COST_EXCEEDS_EDGE)`. **A `min` over caps would have
approved this trade**; the gate is what declines it, which is why the gate is
step 4 and not a footnote.

The corollary, stated as policy: **on Kalshi, microstructure is an
execution-cost tool, not an alpha source.** Any proposal to trade a pure
microstructure signal is measured against 0.70¢-versus-3.50¢ and, absent a
specific stated reason it does not apply, declined.

---

## 7. Kelly as an upper theoretical reference, and what `λ` should be

### 7.1 Why Kelly is a ceiling and never an instruction

Kelly is in the `min` because it is the only term that is an upper bound derived
from **the edge itself**. Every other term is a bound derived from the venue, the
book, or our wealth. That is its correct and only role.

It is not an instruction to wager, for four reasons in descending order of
strength.

**(a) Its optimality is conditional on a known `p`, and `p` is the one thing we
do not have.** This is not a philosophical objection. It is measured, and the
sign flips inside the error bar of its own input.

`f*` is **linear in `p` with slope `1/(1−q)`**, so a bias `δ` in `p` produces an
error in `f` of `δ/(1−q)` — amplified without limit as `q → 1` — while `g` is
concave, so the damage grows quadratically as the bias grows linearly.

| condition | result |
|---|---|
| **2× Kelly** | **exactly zero growth.** Past 2×, growth is negative — you lose money *while holding a genuine edge* |
| the bias that produces 2× Kelly | `δ = e`, the edge itself. Realistic net edges are 1–3 pp, so **a 2 pp forecasting bias zeroes out the growth of a genuine 2 pp edge** — and a forecaster calibrated to ±2 pp is considered excellent |
| `q = 0.90`, `p_true = 0.93`, bias +3 pp | +0.0055 → **−0.0041**, and it demands **60% of bankroll on one contract** |

**(b) Bias is not required. Unbiased noise alone does it.** `g` is concave, so by
Jensen `E[g(f(p̂))] < g(f(E[p̂]))` even when `E[p̂] = p_true` exactly. The
expansion is `E[g] ≈ g(f*) + ½·g''(f*)·Var(p̂)/(1−q)²`, so the penalty carries a
`1/(1−q)²` factor:

| `q` | `p_true` | sd(`p̂`) | E[g] as % of `g*` |
|---|---|---|---|
| 0.50 | 0.550 | 0.02 | 84.0% |
| 0.50 | 0.550 | 0.05 | **21.7%** |
| 0.90 | 0.930 | 0.02 | 37.2% |
| **0.90** | **0.930** | **0.03** | **−136.0% (negative growth)** |

> **At `q = 0.90`, an unbiased forecaster with 3 pp of honest noise turns a real
> +3 pp edge into negative growth.** No overconfidence, no model error — just
> ordinary sampling noise on an honest estimate. **Favourites are the trap**, and
> the `1/(1−q)` amplification is where an unconstrained implementation blows up
> first.

**(c) The error is asymmetric, and every error source pushes the same way.** The
growth curve is symmetric about full Kelly; the risk curve is not.

| λ (×Kelly) | % of `g*` | median max drawdown | P(drawdown > 50%) | P(end < start) |
|---|---|---|---|---|
| 0.14 | 26.0% | 21.4% | 0.0008 | 0.0016 |
| 0.50 | 74.9% | 61.4% | 0.8576 | 0.0097 |
| **0.75** | **93.7%** | 78.6% | 0.9990 | **2.4%** |
| 1.00 | 100.0% | **89.4%** | **1.0000** | 6.3% |
| **1.25** | **93.7%** | 95.4% | 1.0000 | **12.0%** |
| 2.00 | −2.8% | 99.9% | 1.0000 | 50.6% |

`0.75×` and `1.25×` earn **identical** growth (93.7%) at 2.4% versus 12.0% risk
of ending below where you started. **Underbetting is nearly free; overbetting is
not** — and every failure mode above (bias, noise, correlation, rounding) pushes
`f` *upward*. That asymmetry, not caution as a temperament, is the entire
argument for `λ < 1`.

Note also the fourth row: full Kelly has a **median maximum drawdown of 89.4%**
over 1,000 bets and `P(drawdown > 50%) = 1.0000` across all 20,000 simulated
paths — **with `p` known exactly.** Full Kelly is not a strategy a human can run.

**(d) It composes wrongly across positions.** Kelly for `K` simultaneous
positions is a joint optimisation, not `K` independent applications. Applying
`f*` independently and summing assumes `ρ = 0`; with `K` positions at fraction
`f` and pairwise correlation `ρ`, the aggregate stake variance is
`K·f²·(1 + (K−1)ρ)`, inflating the effective single position by `√(1 + (K−1)ρ)`.

> At `K = 10, ρ = 0.3` that is **√3.7 = 1.92**: a portfolio of ten "individually
> half-Kelly" positions runs at roughly **full Kelly in aggregate** — the exact
> regime the tables above show as maximally dangerous. **Each individual position
> passes its own check.** The failure is silent by construction.

### 7.2 `λ` is not a tweak. It is an admission of estimation error.

The name for `λ` is not "the fractional Kelly knob". It is **the fraction of the
theoretical optimum we are willing to claim, given that we cannot verify the
input the optimum is computed from.** It is priced, decomposed, and each factor
is separately measurable or separately absent.

```
λ = λ_calib × λ_dd × κ(W) × λ_evidence
```

| factor | what it admits | how it is obtained | absence |
|---|---|---|---|
| `λ_calib` | systematic overconfidence in `p̂` | **measured**: the Cox calibration slope `β̂` per regime, in log-odds space, applied as `p_cal = σ(â + β̂·logit(p̂))` **before** any Kelly computation — shrinking log-odds and shrinking `f` are different operations, and only the former has a probabilistic justification | `CALIBRATION_UNKNOWN_FOR_REGIME` → **`NO_TRADE`, never a pooled slope** |
| `λ_dd` | path risk given `p` | **derived from a stated tolerance**: `λ_dd = 2/(1 + ln ε / ln α_dd)` | the tolerance is a declared human input; there is no default |
| `κ(W)` | remaining room before the halt | `clamp((W − W_hard)/(W_0 − W_hard), 0, 1)` | a wealth read failure is `GOVERNOR_INPUT_INCOHERENT`, never `κ = 1` |
| `λ_evidence` | **that we have not yet measured an edge at all** | the escalation rung's declared value (§11) | at rung 0, `λ_evidence = 0` |

**`p_conservative`, and doctrine 14.** Separately from `λ`, the *input* to Kelly
is the adverse end of the uncertainty interval, never the point estimate:

```
p_con = Q_α( measured dispersion )        applied AFTER recalibration
```

Because `f*` is linear in `p`, the quantile has an exact interpretation:

```
P( f(p_α) > f*(p_true) ) = P( p_α > p_true ) = α       (if the dispersion is calibrated)
```

> **Choosing the α-quantile sets the probability of overbetting relative to true
> Kelly to exactly α.** That is a real design knob with a stated meaning, not a
> heuristic fudge.

The dispersion **must come from a measurable source** — bootstrap/refit
dispersion, ensemble disagreement, or regime-conditional residual dispersion. A
self-reported posterior width is **unfalsifiable by the only data available**:
`p` is never observed, only the binary outcome `Y` is, and a forecaster reporting
±2 pp and one reporting ±15 pp produce **identical likelihoods for every possible
outcome sequence** provided their means agree. No dispersion source → `NO_TRADE
(DISPERSION_SOURCE_ABSENT)`.

Two traps worth naming so nobody re-derives them:

* **"Bayesian Kelly" does nothing here.** For a single binary contract `g` is
  *linear in `p`*, so the expected objective under the posterior is identical to
  the objective at the posterior mean. The width, skew and shape of the posterior
  have **literally zero** effect on the Bayes-optimal log-growth bet. Anyone
  implementing "Bayesian Kelly" expecting shrinkage has implemented
  Kelly-at-the-mean with extra steps. The shrinkage must be justified as
  **ambiguity aversion against an unidentifiable posterior width** — an honest
  reason, and the reason `p_con` requires a *measured* dispersion or nothing.
* **The quantile controls the frequency of overbetting, not its magnitude**, so
  it is **not a substitute for `λ`.** `p_con` handles parameter uncertainty in
  `p`; `λ_dd` handles path risk given `p`; `λ_calib` handles systematic
  overconfidence. Three different jobs; all three needed.

### 7.3 What `λ` should be, given `e_net ≤ 0`

**At escalation rung 0 (shadow), `λ_evidence = 0`, therefore `λ = 0`, therefore
the Kelly ceiling is zero and every candidate is `NO_TRADE`.** This is not a
degenerate configuration to be worked around. It is the direct arithmetic
consequence of §3: if we have never measured a positive market-relative edge,
the growth-optimal claim on the bankroll is zero. The governor is *correct*
today, and it is correct by returning nothing.

**At the first rung that touches capital**, the recommendation is:

> ### λ ≤ 0.15 — approximately one-seventh Kelly
> ### ⚠ PLACEHOLDER. DERIVED FROM A TOLERANCE STATEMENT NOBODY HAS MADE YET.
> ### NOT CALIBRATED AGAINST ANY DATA OF OURS. See §17.

The derivation, so the number can be argued with rather than inherited:

| max drawdown from epoch start | `α_dd` | ε = 0.20 | ε = 0.10 | **ε = 0.05** | ε = 0.01 |
|---|---|---|---|---|---|
| 10% | 0.90 | 0.123 | 0.088 | 0.068 | 0.045 |
| **20%** | **0.80** | 0.244 | 0.177 | **0.139** | 0.092 |
| 30% | 0.70 | 0.363 | 0.268 | 0.213 | 0.144 |
| 50% | 0.50 | 0.602 | 0.463 | 0.376 | 0.262 |

A "**20% maximum drawdown at no more than 5% probability**" statement implies
`λ_dd = 0.139`. **The 0.139 is exact arithmetic on an input that does not
exist**: the tolerance `(20%, 5%)` is a *human policy choice nobody has made*, so
the number is a placeholder standing in for a decision, not a measurement.
Changing the tolerance is the only legitimate way to change the number, and the
table prices both knobs. Quoting `0.139` to three digits without this paragraph
attached would be precisely the kind of precise-looking uncalibrated number this
document exists to prevent.

Three things make `λ ≈ 0.14–0.15` the right order of magnitude rather than a
timid one:

1. **The price is known and it is payable.** `λ = 0.14` earns **26% of the
   optimal growth rate**. That is the cost of the drawdown statement, stated
   openly rather than discovered later.
2. **It is *robust*, not merely safe** — the strongest argument. Simulating
   `λ·f*(p̂)` with noisy `p̂` and mapping realised growth back onto the `g(λ)`
   curve:

   | λ intended | s = 0.00 | s = 0.01 | s = 0.02 | s = 0.03 |
   |---|---|---|---|---|
   | **0.14** | 0.140 | 0.140 | 0.138 | **0.138** |
   | 0.50 | 0.500 | 0.490 | 0.463 | 0.428 |
   | 1.00 | 1.000 | 0.800 | 0.601 | **0.420** |

   Read it correctly: **noise gives you the risk profile of `λ` and the growth of
   `λ_eff < λ`.** At `λ = 1.0, s = 0.03` you take full-Kelly drawdowns (median
   peak 89%) while earning what an honest `λ = 0.42` would have earned. But at
   `λ = 0.14` the effective value moves 0.140 → 0.138 across the *entire* range
   of noise. **Low `λ` stops depending on a quantity we cannot measure.** That is
   the property. Safety is a side effect of it.
3. **A separate `λ_evidence` factor keeps the two admissions apart.** `λ_dd`
   admits *we cannot tolerate the path*; `λ_evidence` admits *we have not
   established the edge*. Collapsing them into one number would let a future
   operator "improve" the drawdown tolerance and silently claim edge evidence
   that does not exist.

### 7.4 The ratchet on `λ`

> **`λ` is not a performance dial. It may not be raised because results have
> been good.**

| operation | requirement |
|---|---|
| **lower `λ`** | any operator, any time, no evidence required, effective immediately |
| **raise `λ_dd`** | a new declared drawdown tolerance, recorded as a dated human decision, opening a **new epoch with a new `W_0`** |
| **raise `λ_calib`** | it *is* `β̂`; it moves only when `β̂` is refitted on schedule, per regime, at `n ≥ 500` per cell |
| **raise `λ_evidence`** | the rung evidence of §11, requiring a **prospective** sample of the size §11.1 states |
| **raise `λ` because P&L is good** | **forbidden.** The point estimate of the optimal `λ` is itself upward-biased for exactly the reason §7.1(b) describes. `λ` is validated on a **lower confidence bound** on realised growth, never on the point estimate (doctrine 14) |

### 7.5 The free-parameter audit (doctrine 15)

> **A bounding statistic must not depend on a free parameter.** Where one does,
> either the parameter is fixed by a declared, dated human decision *before* data
> is seen, or the bound is reported across the whole admissible range and the
> **worst value is the operative one**.

This document contains four such parameters. Hiding them would make every number
in it look more calibrated than it is.

| parameter | where | why it is dangerous | disposition |
|---|---|---|---|
| `α` in `EVaR_α` / `CVaR_α` | `f_tail` (§6.2) | **the canonical instance.** For a two-point binary loss the sign of `α − (1 − p)` silently switches the constraint between an exact flat cap and a **no-op that reports the position as guaranteed profit** (`CVaR = −0.0102` at `p = 0.93, α = 0.20`). The notation gives no warning | `α` **declared and dated before any data**; the constraint additionally evaluated at `α ∈ {0.01, 0.05, 0.10}` with the **most binding** value operative. If candidate ranking changes with `α`, the result is an artefact of `α` and is reported as one |
| `α` in `p_con = Q_α(dispersion)` | §7.2 | it sets the overbetting frequency exactly — a real knob, and therefore one an operator can turn until a candidate passes | declared before data; admissible range **`α ∈ [0.10, 0.25]`**, toward the low end when dispersion comes from a single source rather than bootstrap **and** ensemble agreement. Sensitivity across the range travels on the artefact |
| `γ` in `f(σ_t)` | §5.2 | an exponent chosen after seeing which markets it excludes is not a bound, it is a fit | `γ = 1` **[PLACEHOLDER]**, frozen until a measured volatility-versus-loss relationship exists. No tuning against outcomes |
| the `κ ≥ 2` cost-kill floor | §6.3 step 4 | a threshold on a robustness scalar | **not free** — inherited unchanged from QDK §7.4, where 2 is justified as "our cost stack has non-closable terms and a stale liquidity estimate; a result that dies if costs are twice the model has not survived our own measurement error." `κ` is computed by the **evaluator**, not the author, which is what makes it hard to game |

The general rule: **if changing a free parameter within its admissible range
turns a veto into an approval, the correct output is the veto.**

---

## 8. High-volatility operating mode

### 8.1 The four principles

1. **Mechanical.** Entry into and exit from every volatility state is a pure
   function of observables. No discretionary override, no "the operator judged
   conditions to be normal", no per-strategy exemption.
2. **Asymmetric hysteresis.** Enter fast (one observation), leave slow (a
   sustained dwell). The cost of being late to caution is the bankroll; the cost
   of being early to leave it is a little growth.
3. **Latching on faults.** A state entered because of an *infrastructure* fault
   rather than a market condition does not un-latch automatically at all — it
   requires the §12 reset path.
4. **Ratchet-only.** The volatility state may only reduce `B_t`, tighten a gate,
   or prohibit a class. It may never raise a budget, relax a gate, or enable a
   class — so no future trigger can be written in the wrong direction even by
   accident.

### 8.2 The state ladder

| state | name | meaning |
|---|---|---|
| `V0` | CALM | all volatility observables inside their reference bands |
| `V1` | ELEVATED | one or more observables outside band, none extreme |
| `V2` | STRESSED | multiple observables extreme, or one extreme with degraded liquidity |
| `V3` | DISLOCATED | venue-level or infrastructure-level condition; the state of the market is not reliably knowable |
| `VU` | **UNKNOWN** | the state could **not be computed**. Distinct from `V0`, and treated as strictly worse than `V3` |

> **`VU` is the state this system is in today**, because `σ_ref` does not exist
> (§5.2). `VU` is not a transitional inconvenience — it is the honest label for
> "we do not know this market's volatility regime", and encoding it as `V0`
> would be the `None → 0` defect doctrine 10 forbids, applied to a regime label
> instead of to a price.

### 8.3 Triggers — observable, deterministic, every threshold a loud placeholder

All inputs are MARKET-STATE-FABRIC-v1 fields or governor-internal counters.

> ### ⚠ NOT ONE `k` IN THIS TABLE HAS BEEN MEASURED.
> Every `_ref` value and every `k` multiplier is a **PLACEHOLDER AWAITING
> MEASUREMENT** (§17). The table expresses **shape and direction only**. An
> implementation that ships these as numeric literals has shipped an
> uncalibrated governor and must be treated as such.

| # | trigger (observable) | source | threshold | → state |
|---|---|---|---|---|
| T1 | `realized_vol_30s > k1 · σ_ref(regime)` | fabric `realized_vol_Δ` | k1 **[PLACEHOLDER]** | `V1` |
| T2 | `realized_vol_30s > k2 · σ_ref(regime)` | fabric | k2 **[PLACEHOLDER]** | `V2` |
| T3 | `spread > k3 · s_ref(regime)` | fabric `spread` | k3 **[PLACEHOLDER]** | `V1` |
| T4 | `depth_5c < k4 · D_ref(regime)`, either side | fabric `depth_*_5c` | k4 **[PLACEHOLDER]** | `V1` |
| T5 | T2 **and** T4 simultaneously | — | — | `V2` |
| T6 | `quote_reversal_30s > k6 · reversal_ref` | fabric `quote_reversal_Δ` | k6 **[PLACEHOLDER]** | `V1` |
| T7 | `dist_to_bound < k7` — contract near 0 or 1 | fabric `dist_to_bound` | k7 **[PLACEHOLDER]** | `V1`, this market only |
| T8 | `seconds_to_close < k8` | fabric covariate | k8 **[PLACEHOLDER]** | `V1`, this market only |
| T9 | order-book sequence gap, or `PublicationState` not publishable | collector state | any occurrence | **`V3`, latching** |
| T10 | `subscription_generation` changed inside the freshness window | collector state | any occurrence | **`V3`, latching** |
| T11 | clock skew beyond bound | host/venue clocks | **[PLACEHOLDER]** | **`V3`, latching** |
| T12 | `book_age_ms` beyond the regime's staleness bound | fabric covariate | **[PLACEHOLDER]** | `V2` |
| T13 | realised markout worse than bound over the trailing window | **markout model — DOES NOT EXIST** | n/a | `V2` (§10) |
| T14 | realised fill rate diverges from the fill model's prediction | **fill model — DOES NOT EXIST** | n/a | `V2` (§10) |
| T15 | `Ĉ(s) − C_realized(s)` exceeds its declared adverse bound | **cost model — UNVERIFIED** | n/a | **`V3`, latching** (§10) |
| T16 | any volatility observable is `NOT_MEASURED` | — | any occurrence | **`VU`** |

Two things about this table are load-bearing:

* **T7 and T8 are prediction-market triggers with no equities analogue.** A
  Kalshi contract is bounded in `[0,1]`: a 1¢ move at mid 0.50 and a 1¢ move at
  mid 0.02 are not the same event — the second is a 50% relative move that cannot
  continue far in one direction. A volatility measure that ignores the bound sees
  structurally different behaviour near the boundary **and calls it a regime**.
  T7 makes the boundary an explicit trigger rather than letting it contaminate
  `σ`. T8 exists because a market's terminal hours are a different process, not a
  noisier version of the same one.
* **T13–T15 are inert today, and their inertness is itself a trigger.** See §8.5
  and §10: at `V1` and above, a strategy class whose supervising trigger is
  inoperative is **prohibited**, not merely unsupervised. This is the mechanism
  that stops §10's missing models from being a footnote.

**Dwell and exit.** Entry is on a single qualifying observation. Exit from `Vn`
to `Vn−1` requires the triggering observable to sit inside the lower band for a
declared dwell `T_dwell` **[PLACEHOLDER]** with no re-trigger in that window.
`V3` and `VU` never exit automatically.

### 8.4 Responses — deterministic, and the mapping is the specification

Every response is a *function of the state*, applied without discretion. All
numeric multipliers `m1..m13` are **PLACEHOLDERS**. The *ordering* — monotone
non-increasing left to right in every row — is **binding and must hold under any
future calibration**. A calibration that produces `m2 > m1` is not a calibration,
it is a bug.

| # | required response | `V0` | `V1` | `V2` | `V3` / `VU` |
|---|---|---|---|---|---|
| R1 | **reduce notional** — multiply `B_t` | ×1.0 | ×m1 **[PH]** | ×m2 **[PH]** | **×0** |
| R2 | **reduce gross** — cap `Σ f_i` | `B_total` | ×m3 **[PH]** | ×m4 **[PH]** | **0 (no new exposure)** |
| R3 | **reduce correlated exposure** — cluster budget `B_c` | `B_c` | ×m5 **[PH]** | ×m6 **[PH]** | **0** |
| R4 | **demand larger edge** — required net-edge multiple over the cost floor | ×1.0 | ×m7 **[PH]** | ×m8 **[PH]** | **∞ (nothing qualifies)** |
| R5 | **increase uncertainty haircut** — `α` for `p_con` moves toward its adverse end | `α_declared` | tighter | tightest admissible | n/a |
| R6 | **limit maker inventory** — cap on resting size | `I_max` | ×m9 **[PH]** | **0 (no resting orders)** | **0** |
| R7 | **increase post-fill markout scrutiny** — window and threshold | baseline | shorter window, tighter bound | tightest; a breach latches `V3` | n/a |
| R8 | **prohibit strategy classes** | — | §8.5 | §8.5 | **all classes** |
| R9 | **raise the `κ` cost-kill floor** | 2 | m10 **[PH]** | m11 **[PH]** | n/a |
| R10 | **shorten every freshness bound** | baseline | ×m12 **[PH]** | ×m13 **[PH]** | n/a |

Two remarks:

* **R1 and R2 are not the same response.** R1 shrinks the *risk budget* — how
  much may be lost. R2 shrinks *gross* — how much is deployed. In a venue where
  positions can be near-perfectly correlated (§9.3), gross and risk diverge
  sharply, and a system that controls only one of them controls neither.
* **R5 is the only response that touches the belief pipeline**, and it does so by
  moving a *declared* parameter within its *declared admissible range* (§7.5). It
  never edits `p̂`, never re-runs a model, and never calls the asynchronous plane
  (doctrine 12).

### 8.5 Strategy-class prohibition

Classes are declared, typed, and closed. A strategy declares its class at
registration; the governor never infers it.

| class | `V0` | `V1` | `V2` | `V3`/`VU` | note |
|---|---|---|---|---|---|
| `HELD_TO_RESOLUTION_TAKER` | allowed | allowed | allowed, reduced | prohibited | pays fees **once** — the cheapest class on this venue |
| `SHORT_HORIZON_TAKER_ROUNDTRIP` | allowed | **prohibited** | prohibited | prohibited | pays the round trip **twice**; §6.3 shows the best documented microstructure edge is one fifth of that cost |
| `MAKER_PASSIVE` | allowed **only if** the markout model exists | prohibited | prohibited | prohibited | see below |
| `NAIVE_TWO_SIDED_QUOTE_AT_TOUCH_WITH_REQUOTE_ON_MOVE` | **prohibited at every state** | — | — | — | a **named prohibited baseline**, recorded so nobody rediscovers it |
| `CROSS_MARKET_COHERENCE` | prohibited (not built; cut by QDK §3.1) | — | — | — | |
| any class whose supervising trigger (T13/T14/T15) is inoperative | allowed only at `V0` | **prohibited** | prohibited | prohibited | §10 |

**Why `MAKER_PASSIVE` is prohibited above `V0`, and why the reason is not
fees.** Kalshi's maker fee multiplier `M` defaults to **0**, so maker fees are
typically zero and the maker's per-filled-contract advantage over taker is
*larger* than a naïve reading suggests. "Fees make maker uneconomic" is **false**
and would be corrected by the first person to read the schedule. Maker is killed
by two other things:

* **Queue position is unobservable after submission.** Kalshi's websocket is L2:
  `orderbook_delta` carries `side`, `price_dollars` and a single net `delta_fp`
  per price level — **no order IDs, no per-order events**. Queue-ahead *at
  submission* is observable; thereafter, when a level shrinks by `δ`, we cannot
  tell whether the cancellations came from ahead of or behind us, so our position
  evolves as an **interval** `Q_ahead ∈ [max(0, Q_ahead − δ), Q_ahead]`. The
  uniform-cancellation assumption needed to model it **cannot be validated from
  observation alone** — identifying it requires placing orders and observing our
  own fills, which is forbidden. It is a **permanently unfalsifiable parameter
  under this capability boundary**, and §7.5's rule applies: a bound that rests on
  an unidentifiable parameter is not a bound.
* **The fill/return relationship is mechanically adverse.** Under a
  requote-on-move policy, `P(fill | favourable next move) = 0` and
  `P(fill | adverse next move) = 1`. This holds **by construction on any CLOB**,
  not as an empirical regularity. **Fill rate is therefore a diagnostic and never
  an objective.** Naive two-sided quoting at the touch with requote-on-move has
  been measured at annualized Sharpe **−109** — hence the permanent prohibition
  above.

Consequence for reporting as well as for sizing: **maker P&L must be reported as
a bracket `(optimistic, point, pessimistic)`, never a single number**, and the
first prospective measurement is **taker-only** — more expensive, exactly
computable from the visible ladder, and free of unidentifiable parameters.
Honest beats cheap.

### 8.6 Worked example — the state machine doing its job

Assume, hypothetically, that reference values have been measured. Placeholders
are shown as the multipliers they are.

```
t0   V0   σ = 1.0·σ_ref   spread = 1c   depth_5c = 800
          B_t = B_0 · 1.0 · 1.0 · 0.95 · 0.60 = 0.57·B_0
          Kelly ceiling λ·f_K = 0.021, and it is the min
                                        → CEILING(0.021), binding = KELLY

t1   T1 fires (σ = 2.4·σ_ref) → V1
     R1 ×m1 · R4 ×m7 · R6 ×m9 · R10 ×m12
          net edge 1.4c against a cost floor of 3.5c·m7
                                        → NO_TRADE(EXECUTION_COST_EXCEEDS_EDGE)

t2   T4 also fires (depth_5c = 90) → T5 → V2
     R6 forces resting size to 0; MAKER_PASSIVE and SHORT_HORIZON prohibited.
     Existing inventory is NOT liquidated by the governor (§9.2).
     The governor controls NEW exposure. It is not an exit engine.

t3   T9 fires (orderbook sequence gap) → V3, LATCHING
     B_t = 0. Every class prohibited.
                                        → NO_TRADE(BOOK_UNPUBLISHABLE)
     Note the CODE ORDER: BOOK_UNPUBLISHABLE (5) outranks
     LIQUIDITY_BELOW_THRESHOLD (19), so the record says "we were blind",
     not "the market was thin". §4.1.

t4   σ returns to 1.1·σ_ref, spread 1c, depth 750, sequence clean.
     V3 does NOT exit. It is latched. Reset requires §12 and a human.
```

The step that matters is `t4`. Every other transition is arithmetic. `t4` is
where an operator wants to type "conditions look fine now", and the design does
not let them.
