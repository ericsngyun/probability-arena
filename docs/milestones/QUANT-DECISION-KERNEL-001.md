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

Five components from the original architecture are **cut**. Each is recorded
here with the number that kills it, so that a future reader who has the idea
again finds the refutation rather than the idea.

The test applied to each: *is there a measurement that makes this component's
expected value negative or unidentifiable, rather than merely small?*

### 3.1 Cross-venue semantic coherence arbitrage — DO NOT BUILD

**The killing arithmetic.** Break-even on a coherence basket is
`ε* = edge / (edge + basket_cost)`. For a 2¢ arbitrage on a $0.98 basket that is
`0.02 / 1.00 = 2%`. **A 2-cent coherence arbitrage is destroyed by a 2% chance
the legs fail to offset.**

On top of that, the fee hurdle alone — before spread, before depth, before leg
risk — is:

| structure | taker hurdle |
|---|---|
| at-the-money complement pair (0.50 / 0.50) | **3.50¢** |
| complement at 0.70 / 0.30 | 2.94¢ |
| 3-leg exhaustive partition | 4.67¢ |
| 5-leg | 5.60¢ |
| **10-leg** | **6.30¢** |

Kalshi's fee peaks *at the money*, which is exactly where the interesting
coherence violations are, and the hurdle **grows with leg count** (roughly
`1 − 1/N`), so wide partitions are worse rather than better despite offering more
apparent violations.

**Why the residual risk is not diversifiable.** The failure modes that make legs
not offset — resolution-criteria mismatch, different settlement source, timezone
boundary, void-vs-settle on the degenerate case, different dispute/oracle
process, semantic near-equivalence — are **adversarially selected**. The cases
where two propositions come apart are precisely the weird cases, and weird cases
are when prices move. No LLM semantic matcher should be credited with 98%
precision on that tail.

**The cut, precisely.** `cross_venue_equivalence` and any LLM-established
equivalence are **do-not-build**, not "deferred". What survives is narrower and
should be built first if any of it is built: **intra-venue structured families**
— complement pairs on one ticker family, threshold ladders (implication chains),
exhaustive partitions within one event — where the binding is `exact`, ε ≈ 0,
and mechanically verifiable. Measure how often those clear the 3.50¢ hurdle on
**executable** prices; if they never do, stop there, and that result is worth
having.

*(Source: `QDK-001-prediction-market-math.md` §7.4–§7.5, §7.8. The fee
coefficient is now primary-verified — see §3.4 and §11.)*

### 3.2 Hawkes processes — NO, on both lanes

**The algebraic argument, which applies to both venues.** With an exponential
kernel, define `S_ij(t) = Σ_{τ_k^j < t} exp(−β_ij(t − τ_k^j))`. That is *exactly*
an exponentially-weighted count of type-`j` events with decay `β_ij`. Then

```
λ_i(t) = μ_i + Σ_j α_ij · S_ij(t)
```

Since everyone fixes `β` on a grid anyway, **a Hawkes intensity is an affine
function of EWMA event counts**, and the `α_ij` are regression coefficients.
Kirchner (arXiv:1509.02017) proves the discrete-time statement: bin the timeline
and multivariate Hawkes becomes a VAR(p) on bin counts fit by conditional least
squares. So the honest question is not "Hawkes features versus EWMA features" —
they are the same basis. It is whether to fit the coefficients against an
arrival-rate likelihood or against the target we actually care about. For
prediction, discriminative fitting on the real objective dominates.

**The identification argument, which is decisive.** Filimonov & Sornette
(arXiv:1308.6756), verbatim:

> "calibration of the Hawkes process on mixtures of pure Poisson process with
> changes of regime leads to completely spurious apparent critical values for the
> branching ratio (n ≃ 1) while the true value is actually n = 0."

**Both of our data-generating processes are that mixture.** A memecoin's life
*is* a sequence of regime changes (launch burst → decay → pump → rug). A dormant
Kalshi contract that wakes on news *is* a Poisson process with a regime change.
Fitting a constant-`μ` Hawkes to either tape yields a large, stable, exciting,
**entirely spurious** near-critical branching ratio — and it will look like a
strong finding.

**The venue-specific arguments, each independently sufficient.**

- *Solana:* the excitation of interest is bot response, whose kernel mass sits at
  lags of **one slot or less** (400 ms), which is precisely the region the slot
  lattice cannot resolve. A kernel that is entirely sub-resolution cannot be
  recovered by any amount of estimator care. Separately, the only direct study of
  AMM swap inter-arrivals (arXiv:2304.02180) finds **same-side arrivals
  approximately exponential** — no self-excitation — with what excitation exists
  being *cross*-side and attributed to arbitrage rebalancing.
- *Kalshi:* at `D = 8` the model has `D + 2D² = 136` parameters. At an optimistic
  5 book updates/minute/contract an 8-hour session yields ~2,400 events, ~300 per
  type — roughly **2 events per marginal parameter** before splitting into the
  pairwise coincidence cells that identify `α_ij`. Filimonov–Sornette's
  recommended 10–30 minute calibration window would contain **50–150 events in
  total**. There is no version of this arithmetic that works.

**The substitute, which is what the schema carries instead:**

> `event_rate_ewma[τ]` at **three halflives** per event type, fit
> **discriminatively** against the actual target (next mid change, markout,
> realized cost) — never against an arrival-rate likelihood.

It is O(1) per event, has no estimation step, no convergence condition, no
branching ratio to be spuriously near-critical, and is defined from the first
event rather than requiring a fit. Note also that the one direct empirical
comparison found (arXiv:2408.03594, SPA test) has *sum*-of-exponentials Hawkes
winning and **single-exponential Hawkes decisively rejected (p = 0.002)** while
a plain VAR on minute counts is not rejected — and "sum of exponentials" is
exactly "multiple timescales", which a multi-halflife EWMA set gives for free.

**One cheap experiment stays open, and it is a measurement not a commitment.**
Once a tape exists: compare (i) EWMA features alone, (ii) the same features with
Hawkes-MLE-fitted coefficients, (iii) a persistence baseline, out of sample on
our own target. Expected outcome: (ii) does not separate from (i). A day's work,
and it settles the question with our own data rather than by analogy to markets
running 10,000× our event rate.

*(Sources: `QDK-001-clob-microstructure-execution.md` §5;
`QDK-001-solana-amm-microstructure.md` §6.)*

### 3.3 `f_CVaR` in the per-position `min` — REMOVE

The loss distribution of a single binary position is **two-point**:
`+f` with probability `1 − p`, `−f·b` with probability `p`. Therefore for any
`α ≤ 1 − p`:

```
VaR_α = CVaR_α = f          exactly
```

**`f_CVaR` as a per-position term is a flat position cap wearing a risk-measure
costume.** It contains no information the cap did not already contain.

And it is worse than uninformative outside that region. At `p = 0.93` with
`α = 0.20 > 1 − p = 0.07` the "tail" contains *winning* outcomes and CVaR goes
**negative** (simulated: −0.0102) — the constraint reports the position as a
source of guaranteed profit and binds on nothing. **The sign of `α − (1 − p)`
silently switches the constraint between a flat cap and a no-op, and nothing in
the notation warns you.**

The same degeneracy applies to a long-only memecoin position: downside is bounded
by the stake and the fat tail is on the **upside**, where a loss-based measure
does not look. Simulated for a 90%-total-loss / 9.5%-modest-gain /
0.5%-Pareto(1.2) payoff, `CVaR95` estimates to the **full stake with zero
sampling variance at every n from 50 to 5,000**.

**What replaces it.** A book-level `EVaR_α(book) ≤ D_max` constraint, not a
per-position term — and with the honest caveat that *the entire informational
content of a book-level tail constraint is the estimate of ρ*: at ρ ≥ 0.6 the
5% tail is "everything loses at once" and `CVaR_α = Σf`, i.e. it degenerates back
into a gross-exposure cap. That makes correlation measurement a **prerequisite**
for the tail constraint, not a parallel concern.

*(Source: `QDK-001-risk-and-sizing.md` §4.2–§4.3, §9.2.)*

### 3.4 CLOB microstructure as a standalone alpha source — REMOVE

**VERIFIED against Kalshi's primary fee schedule (effective 2026-07-07, verified
in this session — this supersedes the secondary-sourcing caveat carried by
`QDK-001-clob-microstructure-execution.md` §10.3 S3 and
`QDK-001-risk-and-sizing.md` §11.4):**

```
taker fee = round up( M × 0.07 × C × P × (1−P) )     peak 1.75¢/contract at P = 0.50
maker:      M defaults to 0                          i.e. maker fees are typically zero
```

Now take the single strongest short-horizon result in the microstructure
literature — Gould & Bonart's large-tick regime, `P(up) ≈ 0.85` at extreme queue
imbalance — and give it the most generous possible reading: a **perfect**
one-tick-ahead prediction, traded costlessly at the mid. Its expected value is

```
0.85 × (+1¢) + 0.15 × (−1¢) = +0.70¢ per contract
```

against a **round-trip taker fee of 3.50¢** at mid-range prices. The best-case
documented microstructure edge is **one fifth** of the venue's round-trip taker
cost.

> **On Kalshi, microstructure is an execution-cost tool, not an alpha source.**
> Its job is to make `C_execution` smaller and to avoid adverse fills on a
> position taken for a probability-forecast reason. Any proposal to trade a pure
> microstructure signal should be measured against 0.70¢-versus-3.50¢ and,
> absent a specific reason it does not apply, declined.

**One correction the primary schedule forces, and it changes the reasoning
without changing the conclusion.** The research track computed the maker leg at
`0.0175 · C · P · (1−P)` (25% of taker, peak ≈0.44¢) and concluded that maker
beats taker per filled contract by `s + (f_T − f_M) − A ≈ 2.31 − A` cents. With
**M defaulting to 0** the maker fee term vanishes and the maker's
per-filled-contract advantage is *larger*, not smaller. **Maker is therefore not
killed by fees. It is killed by adverse selection and by the unobservability of
queue position** (§3.4b). The taker-only recommendation survives — but for a
different reason than the research doc gave, and the distinction matters because
"fees make maker uneconomic" is false and would be corrected by the first person
who reads the schedule.

**3.4b — why maker is nonetheless off the table for the first measurement.**
Kalshi's websocket is **L2**: `orderbook_delta` carries `side`, `price_dollars`
and a single net `delta_fp` per price level, with **no order IDs and no
per-order events**. Consequences:

- Queue-ahead *at submission* is observable — it is exactly `Q_near`.
- Queue-ahead *thereafter* is not: when a level shrinks by `δ` we cannot tell
  whether the cancellations came from ahead of or behind us. Our position evolves
  as an **interval**, `Q_ahead ∈ [max(0, Q_ahead − δ), Q_ahead]`.
- The technique Albers et al. used to recover exact queue position at fill
  exploits a **Binance idiosyncrasy** (a public trade feed publishing all maker
  fills for a taker order in execution-priority order with unique identifiers).
  Kalshi's `trade` channel publishes **aggregate prints only**. It does not port.
- The uniform-cancellation assumption needed to model it **cannot be validated
  from observation alone** — identifying it requires placing orders and observing
  our own fills, which is forbidden. It is a permanently unfalsifiable parameter
  under this capability boundary.

Therefore: **maker paper P&L must be reported as a bracket**
`(optimistic, point, pessimistic)`, never a single number, and **the first
prospective paper P&L is taker-only** — more expensive, exactly computable from
the visible ladder, and free of unidentifiable parameters. Honest beats cheap.

Also note the mechanical fill/return relationship, which holds *by construction
on any CLOB* rather than as an empirical regularity: under a requote-on-move
policy, `P(fill | favourable next move) = 0` and `P(fill | adverse next move) = 1`.
Fill rate is therefore a **diagnostic and never an objective**, and naive
two-sided quoting at the touch with requote-on-move — measured at annualized
Sharpe **−109**, "a recipe for poverty" — should be named as a **prohibited
baseline** so nobody rediscovers it.

*(Source: `QDK-001-clob-microstructure-execution.md` §6.3, §6.5, §6.6, §7,
§11.4; fee schedule primary-verified in this session.)*

### 3.5 The N4 $500 Solana rung — economically incoherent

For a uniform constant-product pool the invariant forces both sides to hold equal
value at the pool's marginal price, so the quantity that enters the impact
formula is

```
τ = 2 · notional / L
```

**not** `notional / L`. `SOLANA-ROUTE-OBSERVATION-001` §4.1 tabulates N4 $500 as
"17% of the median pool"; the operative `τ` is **35%**. (That document explicitly
warns against reading its percentages as impacts — §4.2 — so this is supplying
the missing curve model, not correcting an error.)

At the measured median observation-time pool of **$2,860** (cohort 8, n=42):

| rung | notional | entry cost at p25 $1,936 | at **p50 $2,860** | at p75 $11,578 |
|---|---|---|---|---|
| N1 | $10 | 1.28% | 0.95% | 0.42% |
| N2 | $50 | 5.42% | 3.75% | 1.11% |
| N3 | $150 | 15.75% | 10.74% | 2.84% |
| **N4** | **$500** | **51.90%** | **35.22%** | 8.89% |

And the capacity arithmetic, taking the *strongest* version of the opportunity
(all ~170 eligible births per 25h traded, along the measured median liquidity
decay path):

| clip | 5% gross edge | **10% gross edge** | 20% edge | 50% edge |
|---|---|---|---|---|
| $10 | +$67/day | +$152 | +$322 | +$832 |
| $50 | +$156 | **+$581** | +$1,431 | +$3,981 |
| $150 | −$793 | +$482 | +$3,032 | +$10,682 |
| **$500** | −$14,646 | **−$10,396** | −$1,896 | +$23,604 |

> **At a 10% gross edge — which would be extraordinary — the $500 clip loses
> $10,396/day while the $50 clip makes $581/day. Same signal, same population,
> opposite sign, purely from sizing.** A $500 clip is unprofitable even at a 20%
> gross edge.

**The cut:** N4 is removed from the ladder. The interior optimum is
**$50–$150**, and it is interior — bigger is strictly worse and the arithmetic
changes sign between $150 and $500. Note that removing a rung from a **frozen
preregistered ladder** is not a free edit: `SOLANA-ROUTE-OBSERVATION-001` §4.5
freezes it and §4.5.1 records the V1→V2 supersession openly. This must be a
**declared V3 amendment with its own record**, made *before* any quote is
evaluated, not a quiet drop.

**The deeper finding this exposes, which matters more than the rung.** The
dominant execution cost on this asset class is **not** entry impact. An
instantaneous round trip on a constant-product curve costs only `2f`
*independent of size*, because impact is recovered on the way back down the same
curve. The money goes to **liquidity decay** — entering on a fat curve and
exiting on a thin one. At the measured median 4.75× decay from birth to horizon,
with the token's true price **completely unchanged**:

| notional | entry slip | exit slip | **net round trip** |
|---|---|---|---|
| $10 | 0.40% | 0.94% | −1.04% |
| $50 | 0.99% | 3.63% | −3.16% |
| $150 | 2.46% | 9.87% | **−8.11%** |
| $500 | 7.61% | 27.38% | **−22.23%** |

Liquidity decay is a **lifecycle** variable, not a market-impact variable, and it
is the one uncertainty source of the five that is observable inside our current
boundary. That is where the AMM engine's effort belongs.

*(Source: `QDK-001-solana-amm-microstructure.md` §2.3–§2.5, §9.1–§9.2.)*

### 3.6 Cut-list summary

| # | component | killed by |
|---|---|---|
| 1 | Cross-venue semantic coherence arbitrage | ε* = 2% on a 2¢ arb; 3.50¢–6.30¢ fee hurdle peaking at the money |
| 2 | Hawkes processes, both lanes | intensity ≡ affine in EWMA counts; regime-switching Poisson calibrates to n≈1 with true n=0; sub-slot kernel (Solana); 136 params on ~50–150 events (Kalshi) |
| 3 | `f_CVaR` in the per-position `min` | two-point loss ⇒ CVaR = f exactly; negative when α > 1−p |
| 4 | CLOB microstructure as standalone alpha | a perfect one-tick prediction is 0.70¢ vs a 3.50¢ round-trip taker fee |
| 5 | The N4 $500 Solana rung | τ = 2·notional/L ⇒ 35% of the median pool; −$10,396/day at a 10% gross edge |

Two things are **not** on this list and should not be inferred onto it. The
**AMM engine** is not cut — §3.5 cuts one rung and redirects the engine's effort
from impact to decay. **Intra-venue coherence on exact bindings** is not cut —
§3.1 cuts the cross-venue and LLM-established cases only.


## 4. Architecture: two state engines, one decision interface

### 4.1 The dependency direction

```
                     +--------------------------------------+
                     |  DecisionInterface                   |
                     |  (venue-agnostic; emits an           |
                     |   EvaluationRecord or NO_TRADE)      |
                     +--------------------+-----------------+
                                          | consumes a VenueState
                    +---------------------+---------------------+
                    |                                           |
        +-----------v-----------+                   +-----------v-----------+
        |  CLOB state engine    |                   |  AMM state engine     |
        |  (Kalshi)             |                   |  (Solana)             |
        |  book + trade tape    |                   |  pool state + curve   |
        +-----------+-----------+                   +-----------+-----------+
                    |                                           |
        +-----------v-----------+                   +-----------v-----------+
        | canonical archive     |                   | sparse observer /     |
        | (replay-deterministic)|                   | realized-fill corpus  |
        +-----------------------+                   +-----------------------+
```

Dependencies point **downward only**. Neither engine imports the other. The
decision interface imports neither engine's internals — it consumes a
`VenueState` whose contract is stated in §4.4 and is deliberately thin, because
the two engines share almost no mathematics and a fat shared interface would be a
lie about how much they have in common.

The archive/observer layer is *already* the boundary this repository enforces:
replay determinism on the Kalshi side (`app/realtime/archive.py`,
`app/realtime/book.py`), typed non-observation and denominator preservation on
the Solana side (`app/services/crypto_sparse_observation.py`). Neither engine may
read a live socket or a REST endpoint at evaluation time. **Every feature must be
computable by replaying evidence**, which is what makes any of this auditable.

### 4.2 The one-sentence statement of the difference

> In a **CLOB** the price is uncertain and the state is observable. In an **AMM**
> the price is a *known function* of state, and what is uncertain is **which
> state your transaction will be applied to**.

That inversion drives everything. It means the thing classical microstructure
spends most of its effort estimating — price impact — is a **closed-form
identity** on an AMM, and the thing classical microstructure takes for granted —
that your order interacts with the book you just observed — is the AMM's whole
problem.

### 4.3 What does NOT transfer — the non-transfer table

This table exists because the failure mode it prevents is specific and
expensive: import a CLOB feature library, compute forty features on an AMM, find
no signal, and conclude the market is efficient — when in fact thirty of the
features measured objects that do not exist.

**CLOB to AMM: structurally absent (the object has no referent).**

| construct | why it does not exist on an AMM | replacement |
|---|---|---|
| Queue position, queue-ahead, FIFO priority | There is no queue. A swap executes atomically against the curve at inclusion. | **Nothing structural.** The nearest analogue is transaction *landing*, which is an uncertainty, not a state variable — and it is unobservable without submitting. |
| Queue imbalance `I` | Requires two sides with independent depths. A CPMM has **one** state `(x,y)` serving both directions; the reserve ratio **is the price**, so it cannot predict the price. | **Nothing.** Depth is symmetric by construction. |
| OFI, MLOFI, integrated OFI | Built from **placements and cancellations**. An AMM emits neither. | Signed swap flow — a *different and weaker* variable (§4.5). |
| Cancellation intensity, book flickering, spoofing | Nothing to cancel. | **Nothing.** The nearest economically similar behaviour is LP add/remove, which is capital movement, not intent signalling. |
| Microprice, i.e. `M + g(I,S)` | Requires a bid, an ask, and a spread to correct for. There is one marginal price and no bid-ask bounce to de-noise. | The marginal price `y/x` is already the correct fair value. |
| Bid-ask spread, effective spread, realized spread, price improvement | There is no quoted spread. | `2f`, the doubled pool fee — plus the decay asymmetry of §3.5, which has no CLOB analogue at all. |
| Order-book slope, depth-at-k-levels | Levels do not exist. | The curve itself. Depth at any size is closed-form and continuous — strictly more informative than a level ladder. |
| Fill probability conditional on queue position | An LP is not "filled"; anything crossing its range trades against it unconditionally. | Not a decision variable. |
| Maker/taker fee differential | LPs earn the pool fee; swappers pay it. There is no rebate/fee choice at order time. | — |
| Book publishability, sequence-gap unpublishing | Chain state is canonical and totally ordered by slot. Entirely different failure model. | Slot-chain continuity. |

**CLOB to AMM: the estimator dies even though the concept survives.**

| construct | why the estimator fails | replacement |
|---|---|---|
| Kyle's lambda, and every regression-estimated impact coefficient | It estimates the derivative of price with respect to signed volume *statistically*, because in a CLOB impact is not observable ex ante. On an AMM it is a **closed form**. Regressing to recover a coefficient we can compute exactly adds estimation error to an identity — and will absorb liquidity-decay effects into what looks like an impact coefficient. | `S(tau,f) = (f + gamma*tau)/(1 + gamma*tau)` evaluated at the pool state. |
| Amihud illiquidity | A proxy for a quantity we can compute — and badly contaminated, because the reported return is largely *self-generated impact*. | Pool TVL and `tau`. |
| Roll's implied spread from autocovariance | Assumes bid-ask bounce between two quotes. Consecutive prints walk a deterministic curve. Returns noise or a negative variance. | `2f`, known exactly. |
| Realized volatility from a provider mid | Not wrong so much as **misnamed**: at our pool sizes it predominantly measures the impact footprint of individual swaps. | Keep the number, rename it `price_churn`, drop the volatility interpretation. |
| VPIN / order-flow toxicity | Needs signed volume; its volume-bucketing is calibrated to a market-maker inventory problem no AMM LP faces; and it has been **refuted in its home market** — Andersen & Bondarenko, with near-perfect trade classification: *"We conclude that VPIN is not suitable for capturing order flow toxicity."* | LVR — a *realized accounting* quantity rather than a latent-variable estimate. Note LVR is the **LP's** cost and we are on the taker side; it identifies who the informed counterparty is, it is not a taker edge. |

**AMM to CLOB: what does not transfer the other way.**

| construct | why |
|---|---|
| The closed-form cost function | A CLOB's `C_execution` must be reconstructed from a book that may be stale, may be unpublishable, and may not contain enough depth to fill at all. `UNFILLABLE` is a typed outcome. |
| Liquidity decay as the dominant cost | On Kalshi the dominant cost is the **fee**, which is larger than the tick. Decay has no analogue. |
| The pool algebra, virtual reserves, bonding-curve progress | No pool, no invariant. |
| Loss-versus-rebalancing | No LP. |
| Landing probability, slot inclusion, MEV sandwiching, priority fees | No mempool, no leader, no block. |
| The `p(1-p)` variance normalisation | Kalshi-specific; a memecoin's payoff is not binary. It must be **dropped** on the AMM side, not carried across. |

**Transfers directly, and it is a short list:** signed trade imbalance, trade
count, volume acceleration, realized volatility and vol-of-vol (with the
normalisation dropped), regime labels and event-time clocks (with different cut
points), and the *discipline* of §7.2 — that execution cost comes from the actual
state of the venue at decision time, and that "cannot fill" is a typed outcome
rather than an extrapolated price.

### 4.4 The `VenueState` contract — deliberately thin

Because §4.3 is long, the shared interface must be short. Anything richer would
be a false claim of commonality.

```
VenueState                              # what BOTH engines must supply
  venue_id            enum{kalshi, solana}
  instrument_ref      opaque, venue-scoped
  observed_at         timestamp
  evidence_digest     sha256 over the canonical encoding of the state
  staleness           duration since the newest accepted evidence
  is_evaluable        bool             # false => the decision interface must abstain
  absence             Absence | None   # typed, from the closed vocabulary of 5.1

  fair_value          probability or price, with its own basis enum
  cost_curve(size)    size -> Result[ExecutionCost, Absence]
                                       # the ONLY execution primitive both engines share
  exit_mark(size)     size -> Result[ExitMark, Absence]
                                       # 7.3: NEVER a reported mid

  regime              venue-specific closed enum (spread_regime, or
                      pool_kind x lifecycle_stage), carried opaquely

ExecutionCost
  cost_terms[]        {name, value, basis: OBSERVED|MODELED|BOUNDED,
                       adverse_bound: number?}
  total_partial       observed + modeled terms only
  total_conservative  every BOUNDED term charged at its adverse bound
  unfillable_shortfall  typed; a refusal, never an extrapolated price
```

Four invariants this contract holds:

1. **`cost_curve` is a function of size and is convex-or-staircase, never a
   constant.** On a CLOB it is the ladder walk against the visible book; on an
   AMM it is the curve algebra. This is what makes "the same signal at different
   notionals can have opposite sign" true rather than rhetorical. Worked on a
   real-shaped Kalshi ladder (40 contracts at 52c, then 200 at 55c, with
   `p_hat = 56c`): the identical signal is worth **+90c at 40 contracts and
   −58c at 240**, and total EV is maximised *at the top-of-book size* because
   stepping one level up costs a full 100bp of notional at once. There is no
   smooth impact curve to trade off against; there is a staircase.
2. **`UNFILLABLE` is a typed outcome carrying its shortfall, never a price.** A
   model that extrapolates past the visible book is inventing liquidity, and on
   Kalshi the ladder frequently ends well before $1.00.
3. **`exit_mark` never returns a reported mid.** §7.3 is the guard, and it is the
   difference between a paper ledger and a fiction.
4. **`is_evaluable == false` is a first-class answer.** A refusal is a valid,
   recordable decision outcome; a decision made on a stale book is not.

### 4.5 The structural asymmetry, stated once because it sets the ceiling

The strongest verified result in the CLOB literature is Cont–Kukanov–Stoikov's:
short-horizon price changes are explained far better by **order-book events**
(R² = 65%) than by **trades** (R² = 32%), and in the joint regression the trade
term loses significance in 69% of subsamples.

**An AMM emits only swap flow — the weaker variable — and there is no
construction that recovers the stronger one, because the information is never
created.** The two engines are therefore not two implementations of one design.
They have different achievable ceilings.

> **AMMs are harder to predict and easier to price. CLOBs are easier to predict
> and harder to price.** Any plan that treats them as symmetric is wrong about
> both.

### 4.6 Which question each engine is for

| | CLOB engine (Kalshi) | AMM engine (Solana) |
|---|---|---|
| answers | **"Do we have an edge?"** | **"Can we model execution?"** |
| because | it has the arrival rate (410.7 baseball arrivals/day), calibration instruments, resolved binary outcomes, and a live registered experiment | it has a free, read-only, **realized third-party fill** corpus and an analytically-known cost curve whose residual can be measured |
| its cost model is | reconstructed from a book — exact where the ladder reaches, `UNFILLABLE` where it does not | closed-form from reserves, plus a residual that is measurable against real fills |
| its binding constraint is | **sample size** (§9.4) | **capacity** — a few hundred dollars a day at the optimal clip even at an extraordinary edge (§3.5) |
| its worst failure mode | the fee exceeds every documented signal | marking a position off a reported price and manufacturing a 35% phantom gain (§7.3) |

**Both are needed for a paper P&L, and neither is sufficient.** §10 turns this
into a lane-ranking recommendation for Eric.


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
