# QUANT-DECISION-KERNEL-001 — the measurement kernel

**Status: PLAN ONLY — NOT ACCEPTED, NOT BUILT.** No production code, no feature
flag, no migration, no schema change, no behaviour change, no deployment, and no
provider call has been made for this document. Nothing here is implemented and
nothing here authorizes a milestone. `docs/SAFETY_BOUNDARIES.md` governs
throughout and is not amended, narrowed, or reinterpreted by anything below.

**No capital. No wallet. No signing. No transaction construction, simulation,
or submission. No order placement. No dollar EV. No portfolio sizing. No trade
recommendation.** §2 restates every forbidden capability in full, because this
document's subject matter sits one inference away from several of them.

**Sources.** This document is a synthesis of six independently-produced research
tracks, all merged on `main`:

| track | file |
|---|---|
| Solana read-only ground truth | `docs/research/QDK-001-solana-ground-truth.md` |
| Solana AMM microstructure | `docs/research/QDK-001-solana-amm-microstructure.md` |
| Prediction-market decision mathematics | `docs/research/QDK-001-prediction-market-math.md` |
| CLOB microstructure and execution | `docs/research/QDK-001-clob-microstructure-execution.md` |
| Risk and position sizing | `docs/research/QDK-001-risk-and-sizing.md` |
| Evaluation methodology | `docs/research/QDK-001-evaluation-methodology.md` |

and of two milestone documents: `docs/milestones/SOLANA-ROUTE-OBSERVATION-001.md`
(PLAN ONLY — ACCEPTED, NOT BUILT) and
`docs/milestones/KALSHI-LIVE-TAPE-COLLECTOR-001.md` (DESIGN ONLY).

Where the tracks disagree with each other, §11 says so rather than averaging
them. Where a number is secondary-sourced it is marked. Where a claim was
verified in this session against the repository or against a primary source, it
is marked and the location is given.

---

## Table of contents

- [§0. The inversion: this is a measurement kernel, not a trading kernel](#0-the-inversion-this-is-a-measurement-kernel-not-a-trading-kernel)
- [§1. Objective and falsifiable success criteria](#1-objective-and-falsifiable-success-criteria)
- [§2. The capability boundary, restated in full](#2-the-capability-boundary-restated-in-full)
- [§3. CUT LIST — components that must not be built](#3-cut-list--components-that-must-not-be-built)
- [§4. Architecture: two state engines, one decision interface](#4-architecture-two-state-engines-one-decision-interface)
- [§5. The feature schema](#5-the-feature-schema)
- [§6. The decision record schema](#6-the-decision-record-schema)
- [§7. Gates, guards and the scalars that make a result legible](#7-gates-guards-and-the-scalars-that-make-a-result-legible)
- [§8. The implementation ladder](#8-the-implementation-ladder)
- [§9. Evaluation and preregistration](#9-evaluation-and-preregistration)
- [§10. Lane ranking — a recommendation for Eric, not a decision](#10-lane-ranking--a-recommendation-for-eric-not-a-decision)
- [§11. Where the research disagrees with itself](#11-where-the-research-disagrees-with-itself)
- [§12. Open questions and decisions needed before any build](#12-open-questions-and-decisions-needed-before-any-build)
- [§13. Evidence ledger](#13-evidence-ledger)

---

## 0. The inversion: this is a measurement kernel, not a trading kernel

### 0.1 What the six tracks independently converged on

Six research tracks were run separately, on different literatures, with
different mandates. They converged on one conclusion, and it is not the
conclusion the kernel was scoped to serve:

> **We do not know whether we have an edge, and the architecture we were asked
> to design was designed to exploit one.**

The evidence for that sentence, assembled from four of the six tracks:

**(a) Growth and market-relative forecasting skill are the same quantity.**
For a binary contract at market price `q` with belief `p`, the Kelly fraction is
`f* = (p − q)/(1 − q)`, and substituting it into the expected log-growth
`g(f) = p·ln(1 + f·b) + (1 − p)·ln(1 − f)` gives, exactly,

```
g(f*) = p·ln(p/q) + (1 − p)·ln((1 − p)/(1 − q)) = KL(p ‖ q)
```

The maximum achievable log-growth of a Kelly-sized binary position **is** the
expected log-score advantage of our forecast over the market price. There is no
such thing here as "a good forecaster with no edge" or "an edge without
probabilistic skill". *(Derived and numerically confirmed to 6 decimal places in
`QDK-001-risk-and-sizing.md` §2.1; the same identity appears from the other
direction as the score-gap term of Theorem 8 in
`QDK-001-prediction-market-math.md` §1.1.)*

**(b) That quantity has never been computed in this repository.** Three
independent confirmations, all verified against the code in this session:

- `MarketForecastRecord` (`app/models.py:214`) stores `estimated_probability`, `confidence`,
  `evidence_depth`, and a large reasoning surface — and **no contemporaneous
  market price column**. Without `q` at forecast time, `ΔS = S(p,y) − S(q,y)` is
  not computable from the corpus at all.
- `app/services/forecast_reliability.py:234` computes
  `"brier_skill_vs_base_rate": skill(model, base)`. Grepping the file returns
  that key at lines 234, 360, 393, 449 and 464. **There is no market baseline
  anywhere in the module.** Every headline skill figure this project has ever
  reported is against a base rate.
- The market-anchored rows that *do* exist are `TemplateBaselineForecaster`
  output, which by its own docstring "anchors to the market midpoint … adds no
  independent information", i.e. `p ≡ q`. On those rows `ΔS ≡ 0` and the proper
  betting position is identically zero, by construction. **Supplied measurement
  (this session, not verified by this agent): those rows pair with ZERO
  source-backed forecasts — PAIRED = 0.** So today there is not one
  (source-backed forecast, contemporaneous price) pair in the corpus.

The consequence is blunt and should not be softened: **the +0.2286 baseball and
+0.2434 soccer skill figures are against the base rate and bear on nothing.**
Beating a base rate is nearly free and is worth exactly zero under (a).

**(c) The external evidence says the prior should be `e_net ≤ 0`.** Every
independent measurement of the same question points the same way:

| finding | source | quality |
|---|---|---|
| The best published agentic forecaster beats **market prices** by **+2.3 Brier-index — not statistically significant** (n=200 events). Every other system evaluated is at or below the market. | BLF, arXiv:2604.18576, Table via `QDK-001-prediction-market-math.md` §5.6 | fetched and read |
| **Six frontier LLMs run as end-to-end Kalshi agents with $10k each over 57 days ALL LOST MONEY, −16% to −31%.** Prompt-level risk guidance was "frequently ignored". | Prediction Arena, via arXiv:2607.03015 | secondary within a fetched paper |
| PolyBench's "2 of 7 models profitable" is **gross of exchange fees, at small size, five book levels deep**, and its own authors report profitability "violently contracts" with size | arXiv:2604.14199, `QDK-001-evaluation-methodology.md` §2.2 | fetched and read |
| Optimizing forecast accuracy (RLVR-style) **improved Brier while worsening return** | arXiv:2607.03015 | secondary |
| Our own six pre-registered EDGE-SELECTION-001 candidates all inverted out of sample; the negative control was best of eight | `docs/EDGE_SELECTION_RETIREMENT_2026_07_10.md` | in-repo, verified |
| After a single fee assumption, `cohorts_positive_after_costs: NONE` | COST-MODEL-001 | in-repo |

**(d) The one gate we did cross had no resolving power.** The MVP-005A gate
crossed on a paired **n = 36**. The minimum detectable edge at n=36 (one-sided
5%, 80% power, q=0.50) is `(1.645 + 0.842)·0.5/6 = 0.207` — **20.7 percentage
points.** Every conclusion drawn from it was drawn at a resolution roughly 20×
coarser than the effect being looked for.

### 0.2 The inversion, stated as a design rule

The kernel's first job is **measurement**, not exploitation. Concretely:

> **Every component of this kernel must be justified by a quantity it makes
> measurable, not by a decision it makes possible. A component that only becomes
> useful once an edge exists is deferred until an edge is measured.**

This is not caution for its own sake. It is the only ordering consistent with
(a): if growth *is* the log-score advantage, then the thing to build first is the
apparatus that computes the log-score advantage — and that apparatus is one
column, one baseline function, and a query. Everything downstream of it — proper
betting allocators, Kelly scaling, tail constraints, correlation clusters,
coherence detection — is machinery for converting a number we have never
computed into a position we are forbidden to take.

`QDK-001-risk-and-sizing.md` §9.7 reaches the same place from the sizing side
and says it more sharply: *"A sizing layer is the machinery for exploiting an
edge whose existence has not been established, and building it first inverts the
dependency."*

### 0.3 What survives the inversion, and why designing it now is still right

Three reasons the design work is not wasted, all of them measurement reasons:

1. **It tells us what to instrument before the window opens rather than after.**
   Residual correlation, abstention reason codes, cluster identity, `Δt`, and the
   modeled-vs-observed basis per cost term are all things that must be captured
   *while* data accrues. Adding them later invalidates the sample.
2. **It produces the feasibility numbers** — `n_required`, `DEFF`, `λ`, the
   abstention rate — that determine whether the programme can conclude at all.
   §9.4's answer (30,000–75,000 prospective decisions for a 1pp net edge) is a
   design output, and it should change the roadmap before it changes the budget.
3. **It fixes the vocabulary.** Several quantities in the original brief are not
   the quantities they are named after (§3, §6). Naming them correctly once is
   cheaper than discovering the equivocation inside a result.


## 1. Objective and falsifiable success criteria

### Objective

Build, in ordered and independently verifiable checkpoints, the smallest
apparatus that can answer **"do we have a market-relative edge, and can we model
what it would cost to take?"** — and that can return **NO** with evidence.

The kernel is two state engines (CLOB and AMM, §4) feeding one decision
interface whose output is an **evaluation record**, never an instruction. Its
primary product is a measurement of `ΔS` against the market price, a modeled
execution cost with a declared adverse bound on every term we are forbidden to
observe, and a typed abstention whenever either is unavailable.

### This is a DECISION milestone, and the negative verdict is a success

Following the pattern `SOLANA-ROUTE-OBSERVATION-001` §1 set: a run that
concludes `no_measurable_market_relative_edge`, with evidence, is a **success**
of this milestone and a **block** on everything downstream. A kernel that can
only conclude "yes" is not a measurement.

The honest prior, per §0.1(c), is that this is the outcome.

### Falsifiable success criteria

| id | criterion | falsified by |
|---|---|---|
| **SC-1** | **`ΔS` is computable.** Every forecast written after the Phase-0 checkpoint carries a contemporaneous, executable market quote (bid, ask, mid, source, age), or a typed absence naming why. | one forecast row written after the checkpoint with neither a quote nor a typed absence |
| **SC-2** | **Skill is reported against the market, not only the base rate.** Every reliability artifact reports `brier_skill_vs_market` alongside `brier_skill_vs_base_rate`, or reports the market baseline as typed-absent with its reason and denominator. | one reliability artifact carrying a base-rate skill number with no market-baseline field at all |
| **SC-3** | **`p ≡ q` rows are excluded from every skill claim and counted separately.** Midpoint-anchored forecasts are segmented by `forecaster_name` / `calibration_tags` and never enter a `ΔS` estimate. | one `ΔS` figure whose denominator includes a midpoint-anchored row |
| **SC-4** | **No number without its inputs.** Every derived field names its inputs and is typed-absent when any input is absent. Zero, `0.0`, `""`, `"unknown"`, `-1`, and the previous pass's value are all forbidden stand-ins. | one defaulted numeric; one non-absent derived field with an absent input |
| **SC-5** | **Net before gross, with a bound on every unobservable term.** No artifact carries a gross figure without `net_conservative` beside it, and every non-observable cost term carries a registered adverse bound rather than a zero. | one gross-only artifact; one cost term recorded as `0` that is not an affirmative observation of zero |
| **SC-6** | **Abstention is reachable and counted.** The decision interface can return `NO_TRADE` with a typed reason code, every candidate enters the denominator whether abstained or not, and the reason-code distribution is reported. | a decision path that cannot return `NO_TRADE`; a denominator containing only non-abstained candidates |
| **SC-7** | **The forbidden surface stayed empty.** No dollar EV, no side, no recommended size, no order, no wallet, no transaction bytes, no signing, no submission, no paid RPC, no paid trade feed, no SolanaTracker — in code, in schema, in artifact, including "disabled" and "placeholder" versions. | one such field, function, column, or call site |
| **SC-8** | **The verdict is reachable in all three directions.** The end-of-window report emits exactly one of `market_relative_edge_measured_positive` / `…_measured_null` / `…_not_evaluable`, derivable from persisted rows alone. | a report that cannot express "not evaluable"; a verdict needing an input outside the persisted rows |

SC-1..SC-3 are the whole point of the inversion. SC-4..SC-5 are what make the
number honest. SC-6 is what stops the kernel from being a machine that always
finds a trade. SC-7 is what keeps the boundary a boundary rather than an
intention. SC-8 is what makes it a measurement.


## 2. The capability boundary, restated in full

`docs/SAFETY_BOUNDARIES.md` governs. Nothing in this document amends, narrows,
widens, or reinterprets it. This section restates the boundary in full because
a decision kernel is exactly the artifact most likely to be read as a step
toward the things it forbids.

### 2.1 Forbidden today, with no implementation surface

Quoted or paraphrased from `docs/SAFETY_BOUNDARIES.md` "Forbidden today":

| capability | status | note for this kernel |
|---|---|---|
| **Dollar EV calculation** | ❌ none exists, **no unlocking milestone defined** | The brief's `EV` field is removed (§6). Edge is carried as dimensionless probability points, never a dollar amount. |
| **Trade recommendations** | ❌ none exists | A modeled `PAPER_SIMULATION` P&L and a `READ_ONLY_ROUTE_QUOTE` route are **evidence, never a recommendation** — "neither may carry or imply a side, an entry instruction, or an action". The brief's `side` and `recommended notional` fields are removed (§6). |
| **Paper trading / simulation** | ❌ none exists | `PAPER_SIMULATION` mode may produce **modeled** fills/P&L only, each artifact carrying a **model identifier** and a **modeled-vs-observed basis**. MVP-005B still governs whether such a lane is built. |
| **Portfolio sizing** | ❌ none exists | A modeled fill has a size, but that size is a **stated input**, declared modeled in its basis. "Nothing may derive, optimize, rank, or recommend a size from a modeled result." |
| **Order placement · live trading · autonomous trading** | ❌ none exists | Untouched. |
| **Wallet / private-key handling, custody** | ❌ none exists | The KALSHI-READONLY-AUTH-001 exception is confined to Kalshi read-scoped **request authentication in exactly one file**. It does not extend to route quoting or to any DEX/RPC surface. |
| **Swaps, transaction construction, signing** | ❌ none exists | A quote route being reachable "says nothing about its build/swap sibling on the same API". |
| **Real capital, real orders, real positions, real fills** | ❌ forbidden under **every** mode including `PAPER_SIMULATION` | — |

### 2.2 Forbidden by the SAFETY-BOUNDARY-ROUTE-QUOTE-001 amendment (2026-08-14)

Under `READ_ONLY_ROUTE_QUOTE` there is **no implementation surface** for:
requesting/fetching/receiving swap instructions, serialized transactions, or
transaction/instruction bytes from any endpoint including the build/swap sibling
of the quote route; constructing, assembling, encoding, or serializing a
transaction, instruction, or message by any means including client-side;
simulating a transaction against an RPC node (`simulateTransaction` and
equivalents); signing anything with any key; submitting, broadcasting, sending,
or relaying a transaction, or fetching a blockhash, priority fee, or nonce for
one; loading, deriving, generating, importing, holding, or referencing wallet
key material, seed phrases, or keypairs; or supplying a wallet address we
control as a quote's user/payer.

**And in neither mode:** a paid RPC endpoint, a paid trade/orderflow feed, or
SolanaTracker. "A route quote obtainable only by paying for it is not obtainable
under this amendment; the correct outcome is no quote, reported honestly, never
a purchase."

### 2.3 What this kernel *is* permitted to touch, and where each permission comes from

| surface | permission | gate |
|---|---|---|
| Reading already-persisted rows | always | none |
| Kalshi read-scoped market-data websocket | KALSHI-READONLY-AUTH-001 + the closed channel allowlist (`orderbook_delta`, `ticker`, `trade`, `market_lifecycle_v2`) | `KALSHI-LIVE-TAPE-COLLECTOR-001`, DESIGN ONLY |
| DexScreener read-only adapter, existing budget | existing | `OBSERVE_MAX_CALLS` |
| A free public Solana RPC, read-only history (`getTransaction`, `getSignaturesForAddress`, `getBlock`, `getAccountInfo`, `getFirstAvailableBlock`) | **argued permitted** in `QDK-001-solana-ground-truth.md` §1.1 — retrieval of settled history crosses none of §2.2's enumerated prohibitions | **NOT YET APPROVED.** Needs its own accepted milestone **and** a separately reviewed `BANNED_IDENTIFIER_FRAGMENTS` decision (§8, Tier 3) |
| An aggregator route quote | `READ_ONLY_ROUTE_QUOTE` | `SOLANA-ROUTE-OBSERVATION-001`, ACCEPTED, NOT BUILT, at CP-0 |

**Nothing in the fourth row is authorized by this document.** It is argued for,
with its reasoning, so that a human can rule on it.

### 2.4 The AST audit will fail, and that is the correct outcome

`BANNED_IDENTIFIER_FRAGMENTS` contains `kelly`, `position_siz`, `portfolio`,
`expected_value`, and `paper_trad`. Any implementation naming the concepts in
§5–§7 in the obvious way will fail `frontier-eval-report --include-safety`
**by design**, and per the amendment that failure is correct rather than a
nuisance. A Solana collector naming `swap` will fail for the same reason.

Two consequences, stated so neither is discovered mid-build:

1. **Unbanning an identifier fragment is a separate, separately-reviewed,
   Tier 3 decision.** It must not be smuggled into an implementation checkpoint.
   `SOLANA-ROUTE-OBSERVATION-001` already carries this as its own gate
   (`GATE-FU2`), and this kernel inherits the same shape.
2. **This document is markdown under `docs/`.** The canonical safety grep is
   scoped to `app/ --include="*.py"` and is unaffected by anything written here.

### 2.5 Reversibility tier for every action this document designs

| action | tier | rationale |
|---|---|---|
| Read-only queries over already-persisted rows (segmentation, residual correlation, join-coverage) | **1 — autonomous** | no writes, no external calls, no schema change |
| Adding a market-quote column set to `MarketForecastRecord` | **2 — single confirm** | additive migration, changes a live write path |
| Adding `brier_skill_vs_market` to `forecast_reliability` | **2 — single confirm** | changes a reported metric; pinned by `EVALUATION_CODE_FILES`, so it drifts every registered experiment (§9.2) |
| Enabling the registry `canon_digests` comparison | **2 — single confirm** | it will immediately fail a live registered experiment (§9.2). That is the point, and it needs a human to accept the consequence |
| Extending the sparse observer's schedule or population | **2 — single confirm** | touches a live prospective lane whose denominator is load-bearing |
| First free-RPC call of any kind, including reconnaissance | **3 — dual confirm** | opens a new external surface class; also requires the `BANNED_IDENTIFIER_FRAGMENTS` decision |
| Any prospective activation of a new collector | **3 — dual confirm** | matches `SOLANA-ROUTE-OBSERVATION-001` CP-7 and `KALSHI-LIVE-TAPE-COLLECTOR-001` CP10 |
| Anything in §3's cut list | **not applicable — do not build** | — |


## 3. CUT LIST — components that must not be built

*(section pending)*

## 4. Architecture: two state engines, one decision interface

*(section pending)*

## 5. The feature schema

*(section pending)*

## 6. The decision record schema

*(section pending)*

## 7. Gates, guards and the scalars that make a result legible

*(section pending)*

## 8. The implementation ladder

*(section pending)*

## 9. Evaluation and preregistration

*(section pending)*

## 10. Lane ranking — a recommendation for Eric, not a decision

*(section pending)*

## 11. Where the research disagrees with itself

*(section pending)*

## 12. Open questions and decisions needed before any build

*(section pending)*

## 13. Evidence ledger

*(section pending)*
