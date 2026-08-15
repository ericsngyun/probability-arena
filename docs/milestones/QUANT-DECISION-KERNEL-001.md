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

### 5.0 Rules that bind every feature in this section

1. **Every feature is a function of archived evidence and a clock, and nothing
   else.** No feature may read a live socket, a REST endpoint, or a database at
   evaluation time. The schema must be computable by replaying the archive.
2. **A feature that cannot be computed is ABSENT with a typed reason.** Zero is a
   value, not "unknown". `0`, `0.0`, `""`, `"unknown"`, `-1`, and "the previous
   pass's value" are all forbidden stand-ins. Every feature is
   `Result[T, Absence]`, never `Optional[T]`, and never `T` with a sentinel.
3. **Every feature carries its own window and its own minimum sample count.** A
   feature computed from fewer observations than its floor is **absent, not
   noisy**. This generalises the existing `MIN_SAMPLES_FOR = {"p50": 3,
   "p95": 20, "p99": 100}` gate in `app/realtime/archive.py`.
4. **Exact integers and decimals, never floats.** Kalshi: integer price units
   (`app/realtime/fixedpoint.py`, 1 dollar = 10,000 units) and integer contract
   units. Solana: the raw `amount` string parsed as an exact integer, `decimals`
   as an integer, and **`uiAmount` never read** — it is a documented deprecated
   float. Reuse `app/realtime/canonical.py` (`parse_float=Decimal`,
   `parse_int=int`); one canonicalization, not two. This repository has already
   been burned by silent decimal truncation of ordinary venue JSON under a valid
   digest (KALSHI-ARCHIVE-REPLAY-INTEGRITY-001).
5. **A feature whose only possible answer is "the venue does not expose this"
   must not be a column.** It is presented as a bracket or as a stated limitation
   instead (§6.3).
6. **Regime is a conditioning variable, not a control.** No feature is scored the
   same way across regimes, and the regime label is itself a first-class feature.

### 5.1 The typed-absence vocabulary — one closed set, shared

Extending the vocabulary frozen in `SOLANA-ROUTE-OBSERVATION-001` §5.4 rather
than inventing a second one:

| code | meaning |
|---|---|
| `not_returned_by_venue` | the venue's response did not contain the field |
| `venue_returned_null` | the venue returned an explicit null |
| `venue_returned_unparseable` | present but not parseable under the canonical decoder |
| `derivation_input_absent` | a derived field whose input is absent |
| `no_response` | the request produced no response |
| `request_not_issued` | the request was never made (budget, gate, or policy) |
| `population_truncated` | the population was capped before this member |
| **`feed_not_available`** | the quantity requires a data source outside the boundary |
| **`insufficient_history`** | a window statistic whose window is not yet full |
| **`window_underfilled`** | fewer observations than the feature's declared floor |
| **`book_unpublished`** | sequence fault, pre-snapshot, or integrity halt |
| **`empty_side`** | the ladder side the feature needs has no levels |
| **`insufficient_depth`** | the ladder does not reach the level the feature requires |
| **`stale`** | newest evidence older than the feature's staleness bound |
| **`market_not_open`** | lifecycle state is not `open` |
| **`requires_join_unavailable`** | needs a second channel/source not present in this run |
| **`venue_does_not_expose`** | structurally unobtainable at any subscription |
| **`requires_submission`** | obtainable only by sending an order/transaction — permanently out of boundary |
| **`pre_graduation`** | undefined while the token is still on a bonding curve |
| **`observation_gap`** | the timestamp falls inside a period the tape was blind |

`requires_submission` is new and load-bearing: it is the honest label for landing
probability, our own MEV extraction, and our own quote-to-fill slippage. It is
**not** `feed_not_available` — no feed closes it, and conflating the two is how a
permanent limitation gets mistaken for a procurement problem.

### 5.2 CLOB engine (Kalshi) — the feature schema

Cost is per update on an incrementally maintained ladder; `L ≤ 99` on a 1¢ grid,
so `O(L)` is cheap in absolute terms. **Sample size is the binding constraint,
not CPU** — the only measured Kalshi rate on record is 4 records in ~2 minutes on
DEMO, so any tradeoff spending samples to save compute is backwards.

**Block K-A — level-1 book state.** *(all from `orderbook_delta`, per book event)*

| feature | definition | absence modes |
|---|---|---|
| `best_bid`, `best_ask` | max/min of the two ladders, already YES-scaled under `use_yes_price=true` | `empty_side`, `book_unpublished` |
| `bid_size`, `ask_size` | contract units resting at those prices | as above |
| `spread`, `spread_ticks` | `best_ask − best_bid`, in price units and in ticks | `empty_side` |
| `mid` | `(best_bid + best_ask)/2` | `empty_side` |
| `queue_imbalance` | `(Q_b − Q_a)/(Q_b + Q_a)`, in `[−1,1]` — **the Gould–Bonart signed form** | `empty_side` |
| `microprice` | `M + g(I, S, price_regime)` from a pooled fitted table | `empty_side`, `window_underfilled` |

> **Two conventions that look identical in code and are different variables.**
> Gould–Bonart's published coefficients are on the **signed** `[−1,1]` form.
> Stoikov's micro-price is on the `[0,1]` form `Q_b/(Q_b+Q_a)`. The schema names
> which is which on every field. Mixing them silently is a real and easy bug.

> **`weighted_mid` is deliberately excluded.** It is structurally capable of
> moving in the economically *wrong* direction: with a bid of 9, an ask of 1, and
> 27 resting one tick behind, cancelling the single ask — a removal of supply —
> makes it *fall*. On a thin binary book with a 1-contract top level that
> configuration is routine, and a fair-value estimator that mis-signs on a
> cancellation will systematically mis-sign the maker/taker decision.

**Block K-B — depth and its shape.** `depth[m]`, `cum_depth_within(k)`,
`depth_slope`, `depth_convexity`, `book_pressure_k`, and — the strongest feature
in the entire schema — **`fill_cost_curve(s)`**, the ladder walk tabulated on a
fixed size grid. Our feed carries **full-depth ladders** (at most 99 levels,
maintained as `dict[price_units, contract_units]` in `app/realtime/book.py`), so
`C_execution` is **exact, not modelled** — which most practitioners cannot
achieve, since the published literature overwhelmingly uses top-N L2 feeds.
`insufficient_depth` maps to a typed `UNFILLABLE`, never an extrapolated price.

**Block K-C — order flow.** `OFI(dt)` (Cont–Kukanov–Stoikov, all four
inequalities **weak** — getting the weak/strict distinction wrong silently
changes the estimator at exactly the events that matter most); `MLOFI[m](dt)`
(Xu–Gould–Howison, with the **cascade rule**: a change at level 1 writes into
every deeper component, so a 7-lot arrival inside the spread yields
`(7,10,10)`, not `(7,0,0)`); `integrated_OFI` (PC1); `signed_trade_imbalance`
(**exact, because `taker_side` is given — no Lee–Ready classification error,
which is where a meaningful chunk of the empirical literature spends its error
budget**); `trade_count`, `trade_volume`; `order_arrival_intensity`;
`cancellation_intensity` (a join, and an approximate one); `sweep_flag`;
`replenishment_time`; `resilience_ratio`.

> **Two implementation notes that are load-bearing.** (a) The event term must be
> computed on the **reconstructed best-quote sequence**, not by summing
> `delta_fp` — a delta at a non-best level that *becomes* best changes the
> estimator. (b) **Do not fit MLOFI with OLS.** Components correlate above 0.7
> for all pairs in large-tick books; under OLS only 11–21% of coefficients are
> significant and out-of-sample RMSE *increases* past M ≈ 5. Use Ridge with a
> cross-validated penalty, or the PC1 integration. Cont et al. (2014) concluded
> deeper levels do not matter and Xu et al. concluded they do; **the entire
> difference is OLS + in-sample R² versus Ridge + out-of-sample RMSE.**

**Block K-D — cost and adverse selection.** `effective_spread`,
`realized_spread(h)`, `price_impact(h)`, `markout(h)`, `beta_impact` (the depth
relation, with the exponent measured at 0.98, CI [0.88, 1.08]), and
`adverse_selection_proxy`. **`toxicity_vpin` is NOT in the schema** — §3.2, §4.3.

> **Porting adverse-selection magnitudes to Kalshi: do not rescale by tick
> size.** Albers et al.'s −0.8bp is on a ~0.02–0.03bp tick; ours is 100bp.
> **Adverse selection scales with volatility over the fill horizon, not with tick
> size.** Estimate it as a fitted fraction of that contract's own
> `normalized_vol` over the empirical time-to-fill. Order-of-magnitude
> expectation on an active contract: **0.5–1 tick per side. This is INFERRED and
> is a measurement task, not a result.**

**Block K-E — volatility and activity.** `realized_vol(dt)` in price units,
computed in **event time as well as clock time** and both reported;
`normalized_vol = realized_vol / sqrt(p(1−p))`, where the normalisation removes
the mechanical variance collapse near 0 and 1; `vol_of_vol`;
`volume_acceleration`; and

> **`event_rate_ewma[tau]` at three halflives, per event type — the Hawkes
> substitute (§3.2).** O(1) per event, defined from the first event, carrying its
> own `n_events_seen`, fit **discriminatively** against the target.

Suggested halflives `{1s, 10s, 60s}` — but at Kalshi's unmeasured production rate
these are a **hypothesis**, not a setting. The halflife set is a registered
parameter (§9.3), chosen once from the first measured rate and then frozen.

**Block K-F — regime and clocks.** `spread_regime` (`TOUCHING` = 1 tick /
`NARROW` 2–3 / `WIDE` 4–9 / `VERY_WIDE` at least 10 / `ONE_SIDED` / `NO_BOOK`) —
**the primary conditioning variable in the whole engine**; `depth_regime`;
`activity_regime`; `price_regime` (`TAIL_LOW` below 10¢ / `MID` / `TAIL_HIGH`
above 90¢), which drives both the fee curve and the payoff variance;
`lifecycle_state`; `time_of_day`, `day_of_week`; and **`time_to_close` — which
has no source in the current collector design** (§12, Q2).

> **Kalshi is two regimes wearing one name.** A contract with a 1¢ spread and
> hundreds resting at the touch is the *large-tick* regime where queue imbalance
> is strongest (out-of-sample AUC 0.76–0.81, `P(up)` about 0.85 at extreme
> imbalance). A dormant contract with a 7¢ spread and 3 contracts a side is where
> every estimator here degrades. **In `WIDE`/`VERY_WIDE`, do not attempt
> short-horizon prediction at all** — the spread alone exceeds the edge, and the
> micro-price is returned **absent** rather than extrapolated. An absent fair
> value is a usable input to a decision rule; a fabricated one is not.

> **And the venue's cost structure and its information structure point in
> opposite directions.** The fee falls by a factor of ~8 between P = 0.50 and
> P = 0.03, so tail contracts are dramatically cheaper to trade — but tail
> contracts have the thinnest books, widest spreads, fewest events, and least
> reliable estimators. This tension is real, has no clean resolution, and should
> be stated in any strategy proposal rather than resolved by picking whichever
> side favours the proposal.

**Block K-G — observation quality.** `data_age_us`, `book_staleness`,
`is_publishable`, `subscription_generation`, `observation_gap`, and
`clock_offset_bound` — which is **NOT MEASURED today** (`app/cli.py:717-722`
records this explicitly), so `data_age_us` carries an unquantified bias of
unknown sign until it is.

> **An observation gap is not latency, it is blindness.** A decision timestamped
> inside a gap is not a high-latency decision; it is an invalid one, and must be
> typed absent, never interpolated.

**What must NOT be a column, per rule 5:** queue position at fill and everything
derived from it (queue-position deciles, front-of-queue indicators,
priority-adjusted fill models); order-level cancellation attribution; any
own-order, own-position, or own-fill feature (structurally forbidden, not merely
unavailable); cross-venue features; and `event_clock_phase` as a general column —
admit it only per contract family, once a family-specific source exists.

### 5.3 AMM engine (Solana) — the feature schema

**Block S-A — pool state.** `pool_tvl_usd`; `quote_reserve_usd_est` = `L/2`
(**derived, and valid ONLY for uniform CPMM** — it must be **typed-absent** on
CLMM/DLMM, where the conversion has *unbounded* error in both directions);
`fee_bps` read **from the pool, never from a per-dex default table**; `dex_id`
verbatim and unmapped; `pool_kind` in `{cpmm, clmm, dlmm, bonding_curve,
unknown}` where `unknown` is a first-class value; `pool_age_seconds`;
`tvl_log_return`; `tvl_drawdown_from_peak`.

> **Three incompatible impact mathematics coexist in this market.** Smooth
> constant-product (Raydium AMM v4/CPMM, PumpSwap); **stepped constant-SUM**
> inside a Meteora DLMM bin, where price impact within a bin is exactly **zero**
> until the bin's output side is exhausted and then jumps; and concentrated
> liquidity (Orca Whirlpools, Raydium CLMM), where TVL may sit far from the
> active price. Applying the constant-product formula to a DLMM pool is not an
> approximation with a small error — **it is the wrong functional form.** This is
> why `pool_kind` exists and why `quote_reserve_usd_est` must be absent off-CPMM.
> Measuring the venue mix in our own `dex_id` column is a free query and should
> be the *first* AMM study run.

> **A 100x trap worth naming: Raydium AMM v4 encodes its fee as x/10,000 while
> CPMM and CLMM encode as x/1,000,000.** Reading one with the other's
> denominator is a hundred-fold error in the fee term.

**Block S-B — price.** `price_usd` (provider mid); `log_return`;
**`price_churn_window`** — deliberately **not** named `realized_vol`, because at
these pool sizes what it measures is predominantly the *impact footprint of
individual swaps*, not information arrival, and calling it volatility imports
every classical volatility intuition, all of which are wrong here;
`price_change_5m/1h`; `market_cap`, `fdv`; `fdv_to_tvl`.

**Block S-C — flow. This block's status changed, and the change is significant.**
`swap_count_by_direction`, `net_signed_flow`, `trade_size_distribution`,
`unique_signers_window`, and `liquidity_add/remove_events` were all classified as
requiring a paid per-trade feed and therefore permanently closed. **They do
not** — §5.4 derives every one of them from free read-only chain history. They
are reclassified from "structurally closed" to "**pending the Phase-1 collector
and its approval**", which is a different and much better status. Until that
collector exists they carry `feed_not_available`; the code must not be written as
though the closure were permanent.

Available today with no new capability: `volume_5m/1h/24h_usd`, `volume_to_tvl`,
and **`tvl_jump_unexplained`** — a TVL change too large to be explained by
reported volume, i.e. a **liquidity-removal detector built entirely from data we
already persist**, and the closest thing to a rug signal inside the current
boundary. It is a *detector*, not an *attribution*: it cannot distinguish an LP
withdrawal from a single enormous swap the provider under-reported, and the
feature must carry that ambiguity rather than resolve it. Its likeliest failure
is that the "unexplained" residual is dominated by provider volume-reporting
error, so it fires everywhere and means nothing — **check that first**, on tokens
whose TVL is stable.

**Block S-D — venue dispersion.** `pair_count`, `single_venue`,
`tvl_herfindahl`, `best_pair_share`, and `route_hops`/`route_split` from a quote
response.

**Block S-E — actor structure.** Already modelled in
`CryptoTokenLifecycleSnapshot` and `CryptoTokenActorObservation`:
`top10_holder_pct`, `creator_holding_pct`, `sniper_pct`, `bundler_pct`,
`insider_pct`, `holder_count`.

> **`sniper_pct`, `bundler_pct` and `insider_pct` are PROVIDER-DEFINED labels
> with unpublished thresholds.** They may be excellent; we cannot know. Nothing
> in the current schema records the vendor's definition alongside the value, and
> treating them as ground truth imports an unaudited dependency. The schema must
> carry `label_source` and `label_definition_ref` beside each.

One derived feature worth adding because it has an actual derivation behind it
rather than two independently-thresholded numbers: a holder controlling fraction
`h` of the float faces an exit of roughly `2h × (FDV/TVL)` in `tau` terms, so
**`exit_pressure = top10_holder_pct × fdv_to_tvl`**. Both inputs are already
persisted, it costs one multiplication, and it is testable against the decay
outcome. It is a **worst-case** measure — it assumes a single-transaction exit
into the same pool — and must be named as one.

> **Also worth an explicit decision rather than inheritance:** the risk engine's
> `min_liquidity_usd = 5000.0` is **above 62% of the observed population**. A
> threshold that most of the population fails is not discriminating within the
> population — it is selecting a different one.

**Block S-F — lifecycle, and this block dominates.** `lifecycle_stage` over the
six observable transitions: S0 pre-liquidity, S1 bonding curve, S2 graduation,
S3 post-graduation peak, S4 decay, S5 death or removal. Plus
`bonding_curve_state`, `curve_progress_pct`, `graduated_or_migrated`,
`token_age_seconds`, and

> **`tvl_decay_ratio` = `L_birth / L_t` — the highest value-per-unit-effort
> feature in the entire schema.** It is two columns we already persist and one
> division, and §3.5 shows it drives realized execution cost more strongly than
> any impact term.

**The strongest reason lifecycle is state and not a control.** Between birth and
the 6h/24h horizon the interquartile liquidity ratio widens from **2.16x to
5.98x** — a **2.77x widening of dispersion**. At birth these tokens look nearly
alike; by the horizon they have fanned out. **The lifecycle is not a uniform
decay applied to a population; it is a sorting process.** That reframes the
research question from "what will the price do?" to **"can the sort be predicted
at birth, before the fan-out has happened?"** — a classification problem over
features we already hold, with an outcome we can already compute, needing no new
capability and not one flow feature.

A second structural result points the same way: effective depth is
**non-monotonic** across the lifecycle. It rises through the bonding curve, peaks
at or shortly after graduation, and then decays — so far that the decayed
post-graduation pool at our measured median (about $1,430 of quote reserve) is
**shallower than the pump.fun curve was on its very first buy** (30 SOL virtual).
A token that has "made it" is, at the horizon where we observe it, a worse venue
to transact than the launchpad it escaped.

Three sampling facts that must appear on the face of any AMM result:

- **Our 6h/24h observations sit deep in S4**, between roughly one and nine
  liquidity half-lives after birth (median half-life bracketed at **2.7–10.7h**,
  a bracket rather than a point because the sample mixes two horizons and the
  exponential form is assumed). We are measuring the *result* of the sorting
  process, not the process.
- **Roughly 40% of births are enrollable** — 41.4% in one 25h window (n=411),
  and 59.8% NULL `initial_liquidity_usd` over a larger 7,447-birth sample. Use
  "roughly 40%" as the durable claim and 41.4% as one window's reading. Every
  rate is reported against that ceiling **on the face of the report**, not in a
  footnote: a "90% coverage" claim over a denominator that has already discarded
  ~60% of births is a ~37% claim.
- **Our ~395 births/day is 1.6%–3.7% of published pump.fun launch rates.** Our
  population is not a sample of launches; it is a sample of *tokens a provider
  chose to surface*, already filtered by roughly 30–60x. That is probably *good*
  for a trading question and *bad* for any claim about memecoins in general, and
  no base rate of ours is comparable to a published one without saying so.

### 5.4 The Solana realized-fill corpus — the session's biggest win

This is the one place where a capability the project believed permanently closed
turns out to be open, and it deserves to be stated precisely.

**The claim.** Read-only realized *third-party* execution IS obtainable on
Solana, free, without ever constructing, simulating, signing, or submitting a
transaction. The crux is that **the AMM's own vault balances are inside the swap
transaction's `preTokenBalances`** — verified in the Agave validator source
(`ledger/src/token_balances.rs`, `collect_token_balances`), which iterates
**every account key of the transaction**, skipping only invoked program IDs and
the token programs themselves, and records a balance for each SPL token account.
A swap cannot debit a vault it did not reference, so the vault is necessarily in
the key list and necessarily recorded.

**Verified live on mainnet in this session** (supplied to this document, not
performed by the agent that wrote it): two real swaps on our own cohort-8 token
were fetched; pool vault deltas and trader deltas **conserved to the base unit**;
realized price computed directly from the response. This supersedes checks C1 and
C2 of `QDK-001-solana-ground-truth.md` §13, which that document could only state
as human-runnable because it made no RPC call.

Four consequences that are better than the original proposal assumed:

1. **No second RPC call.** Pre-state costs zero additional requests, which
   roughly halves the rate budget.
2. **No prior-slot lookup**, which does not exist on free infrastructure anyway —
   `getAccountInfo` has no historical-slot parameter, and `minContextSlot` is a
   *freshness floor*, not a time machine.
3. **Intra-block ordering is handled for free.** Three swaps hitting one pool in
   one block each carry their own pre-state, reflecting the preceding two. A
   design reconstructing state from "the last observation of the pool" would get
   all but the first wrong.
4. **It is self-verifying.** `post = pre + delta` must hold on every vault. A
   violation means the parse is wrong, not that the chain is.

**The corrected realized-price formula**, because the naive version
double-counts:

```
amount_in      = negative token_delta of the taker in mint_in    exact integer, base units
amount_out     = positive token_delta of the taker in mint_out   exact integer, base units
realized_price = amount_out / amount_in                          exact rational, decimal-normalized
network_fee    = meta.fee                                        lamports - a SEPARATE ADDITIVE TERM
```

> **`realized_price` is gross of the network fee and NET of every in-protocol
> fee.** The pool fee, the platform fee, and any creator fee are already inside
> `amount_out`, because they were taken before the taker received anything. That
> is exactly the right quantity for calibrating execution — it is the all-in
> price the taker actually got. A formula of the shape
> `(delta quote + fees) / (delta base)` **double-counts** them. `meta.fee` is the
> *network* fee — size-independent, additive — and it belongs in its own column,
> never inside the price.

Two further naming disciplines that keep this honest. The impact measure derived
here is **impact versus the pre-trade mid**, which is what an AMM fill model
needs and is *better* than quote-versus-fill for calibration because it has no
dependence on when the trader fetched their quote — but calling it "realized
slippage" without qualification would be an equivocation, and the column must be
named for what it is. And **priority fees are invisible**: Jito-style tips are
ordinary SOL transfers and are therefore *detectable* as lamport outflows, but
attributing them requires a tip-account allowlist. Recording an unattributed
lamport outflow is honest; asserting a tip is not.

**The detection method is allowlist-free, and that is the point.** A swap is
defined by *what moved*, not by which program ran it: one party's holding of mint
A decreased and their holding of mint B increased, with a non-signer account set
moving oppositely. That needs no IDL, no discriminator table, and **no venue
allowlist** — so it detects venues nobody has enumerated. On a memecoin tape
where new venues appear continuously, an allowlist-shaped detector produces a
corpus whose *absences are undetectable*. Venue labelling becomes a **result**
(group by program ID, label the head by hand once) rather than an input: an
unknown venue shows up as an unlabelled program ID with a measured share of the
tape — visible, quantified, and honestly unnamed — instead of as a hole.

It also converts a named silent-wrongness risk into a measurement. Per-mint
deltas must net to zero across the transaction **except** for transfer-fee or
mint/burn mints — so if the taker lost 100 and the vault gained 98, the 2 is
**measured, not assumed away**. Token-2022 transfer fees stop being unobservable
the moment realized fills are read, and the row carries a
`token_conservation_gap`.

**Where it fully works and where it does not.**

| venue class | pre-trade state recoverable? |
|---|---|
| Constant-product (Raydium v4/CPMM, PumpSwap, Orca legacy) | **YES, fully** — both vaults are in `preTokenBalances` |
| pump.fun bonding curve | **YES, derivably** — virtual and real reserves move by identical amounts on every buy and sell, so they differ by a constant fixed at curve creation; those constants live in one `Global` account readable by a single free call. **They are MUTABLE** (`set_params`), so they must be read, slot-stamped, and a corpus spanning a change must be segmented. Treating them as compile-time constants is a silent-wrongness bug. |
| Concentrated liquidity (Whirlpool, Raydium CLMM) | **NO, not fully** — tick arrays are not token accounts and cannot be read historically |
| Meteora DLMM | **NO, not fully** — per-bin liquidity and the active bin, same reason |

CLMM/DLMM rows are therefore **realized outcomes without their full causal
state**, and must carry a typed `pool_state_completeness` in
`{complete, partial_clmm, partial_dlmm, unknown}`. **A calibration that pools
them is fitting a curve to points whose x-coordinate is partly unknown, and it
will look better than it is.**

**Retrospective backfill is NOT OBTAINABLE, and that is the schedule fact.**
Archival access is a paid product, forbidden by the amendment. And
`getTransaction` returns `null` for a pruned transaction *indistinguishably* from
not-found and from not-yet-confirmed — so a backfill collector **cannot tell
"this swap did not exist" from "this node forgot it"**, which is disqualifying on
its own for a corpus whose value depends on knowing its own denominator. A
**prospective** collector never touches the retention horizon: it reads
minutes-old data on a node holding days, a `null` at t+2min means "not confirmed
yet" and is retryable, and the denominator is *what you asked for*. This is the
same shape the project has been forced into twice already —
CRYPTO-COVERAGE-REPAIR-001 drained the historical recoverable pool 1,043 to 106
in a single pass.

> **The corpus does not exist until collection starts, and no amount of later
> effort recovers the days not collected. Calendar time is the scarce resource
> here, and this is the only place in the entire document where that is true.**

**The rate reality.** The public endpoint's documented limits are 100 requests
per 10s per IP overall, **40 for any single method**, 40 concurrent connections,
and **100 MB per 30s of response data**. A whole-chain firehose needs roughly one
to two orders of magnitude more bandwidth than that cap allows, so it is out.
Targeted collection scoped to the sparse lane's cohort is in: one
`getSignaturesForAddress` per tracked token per poll, plus one `getTransaction`
per new signature. At a conservative 25% of the single-method cap (about 1 req/s
sustained) that is ~86,400 requests/day, and at an **INFERRED** 2,000–5,000
usable records/day a 100k-fill corpus accumulates in roughly three to seven
weeks. **403 is a stop condition for the pass, not a retry**, connection reuse is
required (the connection-*rate* cap binds before the request cap), and
`getFirstAvailableBlock` must be recorded per pass so the retention horizon is
observed rather than assumed. The limits are documented as subject to change
without notice, so **any capacity claim built on them must be re-measured, never
inherited** — the same discipline CRYPTO-QUERY-PLAN-AND-DENOMINATOR-RECOVERY-001
learned when 28 reviewer trials failed to predict a 45-second block in
production.

**Two traps that produce a silently biased corpus rather than an error.**
(a) Omitting `maxSupportedTransactionVersion: 0` errors on v0 transactions — and
aggregator-routed swaps are overwhelmingly v0 with lookup tables — so a naive
retry-and-skip loop builds a corpus biased *away from exactly the routed swaps
that matter most*. (b) Lamport balance indices resolve as
`static ++ loaded.writable ++ loaded.readonly`; getting the order wrong
attributes deltas to the wrong accounts with no error. The **token side is
immune**, because each balance element carries its own `mint` and `owner` — which
is a good reason to build the primary measurement on token balances rather than
on lamport indices.

**Four fields are contractually nullable** — `preTokenBalances`,
`postTokenBalances`, `innerInstructions`, `logMessages` — and `owner` and
`programId` may be omitted per element. A `null` `preTokenBalances` on a swap
means **that swap is unusable**, not that it moved no tokens. And **failed
transactions are still returned and still charged a fee**, so a realized-price
corpus must filter on a null `err` before computing anything.

**Do not build this on log parsing.** Logs are truncatable
(`--log-messages-bytes-limit`), contractually nullable, and venue-private and
unversioned; balances are consensus state. A truncated log yields a *partial*
parse, which is the fabrication shape this repository most wants to avoid — a
plausible number derived from incomplete evidence. Logs are a **corroborating**
channel whose disagreement with the balance derivation is a bug signal, and
nothing more.

**What this does and does not dissolve.** It dissolves *"validating a fill model
requires realized slippage, which requires a paid per-trade feed"* — the premise
is false. It does **not** dissolve landing probability, our own MEV extraction,
or our own quote-to-fill slippage, all of which remain `requires_submission`. And
`SOLANA-ROUTE-OBSERVATION-001` §8.3's finding — *"Any `PaperFill` this project
ever writes is a MODEL OUTPUT, never a measurement"* — **stands in full.** What
changes is the *quality of the model's basis*: the slippage component moves from
"MODELED, assumed" to "MODELED, calibrated against N observed realized fills in
stratum S". That is the difference between a boundary field that is honest but
vacuous and one that carries information.

**One bonus that is arguably larger than the corpus.** A realized-execution
corpus is the missing comparator for `SOLANA-ROUTE-OBSERVATION-001`'s own SC-5:
*did the venue's reported price impact for size X on pool P match what actually
happened to real traders at comparable size on P in the same minutes?* As scoped,
that milestone can establish that a quote was **recorded faithfully** but not
that the quote was **right**. This turns its verdict from a completeness audit
into a genuine falsification test.

### 5.5 The AMM calibration principle: measure the residual, not the curve

The single largest methodological weakness of an observational fill corpus is
that **you observe the sizes traders chose**, and traders see the pool before
they size. That is the classical simultaneity problem — the same reason a demand
curve cannot be estimated from a scatter of equilibrium prices and quantities —
and it is not fixed by collecting more data.

Quantified against our own designed ladder: the ladder spans `size/TVL` from
0.35% to 17%, a **~50x range**, chosen deliberately to be non-degenerate. A
trader targeting a 0.5–2% slippage budget on a constant-product pool is choosing
`size/TVL` around 0.005–0.02, a **~4x range**. So the corpus may carry **an order
of magnitude less variation in the covariate that matters**, while carrying
orders of magnitude more rows. **Rows are not information here.** And the loss is
concentrated in the worst place: almost no observations near the top rung,
because nobody voluntarily accepts 17% impact — exactly where a fill model is
least certain and a bad extrapolation most expensive.

*(That 4x figure may be pessimistic: memecoin traders are frequently not
slippage-disciplined, with tolerances of 10–50% routine and panic exits
size-insensitive. It is an empirical question and it is cheap to settle — a
histogram of realized `amount_in / pool_pre_in` on the first collected sample. If
the p5–p95 range spans 20x or more the bias is mild; if it spans 4x or less the
residual reframe below is mandatory rather than advisable.)*

Four further selection effects, each with its sign: **landed-only** (aborted
swaps are censored, biasing realized slippage *down*); **trader composition**
(early memecoin flow is bots and snipers, so we would fit a model of *bot*
execution rather than of a discretionary entry); **life-stage clustering**
(volume is violently front-loaded, so the corpus's pool-state distribution is not
the distribution at the horizons we care about); and the inherited **~40%
enrolment ceiling**.

> **The reframe that makes the bias much less damaging — and it is the design
> rule, not a caveat.** We are **not** fitting impact as a function of size and
> state from scratch. For a constant-product pool that function is *known
> analytically*, and §5.4 recovers the reserves exactly. The corpus's job is to
> measure the **residual**:
>
> ```
> residual = realized_price − analytic_price(pre_state, amount_in)
> ```
>
> That residual is where the unmodellable content lives — fee tiers, Token-2022
> transfer fees, multi-pool routing, same-block contention, sandwich extraction —
> and there is good reason to expect it to be far **less size-dependent** than
> the impact itself, because its leading terms are proportional or additive
> rather than curvature. **Endogeneity in size damages structure estimation badly
> and residual estimation much less.**
>
> *Whether the residual really is size-independent is the whole rescue, and it is
> testable only once a corpus exists. It is §12's first open question.*

Three consequences to build in from the start:

1. **Report per-stratum n by `size/TVL` decile, always.** A stratum below n = 30
   is reported `too_thin_to_calibrate`, in the spirit of the labels this
   repository already uses.
2. **Declare an explicit extrapolation boundary** — the observed support of
   `size/TVL` — and **refuse to emit a modeled number outside it**, rather than
   emitting one with a wide interval. EDGE-SELECTION-001 is directly on point:
   candidates that looked good in-sample inverted out-of-sample, and the negative
   control beat them.
3. **Treat the corpus as calibrating and falsifying a known model, never as
   discovering one.**

Two things the corpus also buys that no quote endpoint can:

- **The abort rate is directly observable.** Failed transactions come back with a
  non-null `err` and a charged `fee`, so the landed-only censoring becomes a
  **quantified** bias rather than an invisible one — strictly better than the
  quote lane, which cannot see aborts at all. And a failed attempt is not free: a
  slippage revert is included in the block and charged the base fee plus the full
  priority fee, so **any honest fill model must carry a failure branch with a
  non-zero cost**, not a retry-until-success loop.
- **Sandwich contamination becomes a tag rather than a hidden bias.** Same-block
  grouping is free from the per-signature slot, and ordering can be reconstructed
  from the **balance chain** — each transaction's `preTokenBalances` on the
  shared vault must equal the previous one's `postTokenBalances` — rather than
  trusted from array order. That chain is a total order, it is self-verifying,
  and a break in it is a *detected gap* rather than a silent mis-ordering. A
  corpus that silently mixes sandwiched and clean fills produces a slippage model
  inflated by the sandwich rate times the average extraction, **with no way to
  decompose them after the fact** — pessimistic in a way that looks like honest
  conservatism and is actually an artifact. Tag each row `clean` / `sandwiched` /
  `same_block_contended` / `undetermined`.
- **But the observed population rate is a LOWER BOUND and a prior, never a
  prediction.** Private-orderflow extraction may not appear as adjacent
  same-block public transactions at all, and observed rates are conditioned on
  *other traders'* slippage tolerances, priority fees, and routing — none of
  which we would share. **Our own extraction remains `requires_submission`.**

One correction this forces on `SOLANA-ROUTE-OBSERVATION-001` §8.2, which should
be made openly rather than reinterpreted later. That document says the milestone
"cannot acquire [ground truth] **within its boundary**". The claim splits:
*"this milestone, as scoped, has no ground truth to validate against"* is
**upheld**; *"and cannot acquire one within its boundary"* should be
**retracted** — it is the **scope**, not the safety boundary, that forecloses it.
The distinction is load-bearing, because as written a future reader concludes
that ground truth requires an amendment, and therefore never looks. It does not
require an amendment. It requires a different milestone.


## 6. The decision record schema

### 6.1 The reconciliation, field by field

The brief specified a decision record with eighteen fields. Several are now
unsupportable — either because a cut removed the quantity, or because the
capability boundary forbids the field, or because the field names a quantity that
cannot be identified from observation. **Removing them is not tidying. A field
that cannot be honestly populated is a field that will be dishonestly
populated.**

| brief's field | verdict | why |
|---|---|---|
| instrument | **KEEP** | — |
| **side** | **REMOVE** | `docs/SAFETY_BOUNDARIES.md`: evidence artifacts may not "carry or imply a side, an entry instruction, or an action". Side may exist only *inside* a `PAPER_SIMULATION` as a declared modeled input, on an artifact carrying a model identifier and a modeled-vs-observed basis. It is not a field of an observation record. |
| p_market | **KEEP — the single highest-value field in the schema** | Without it `ΔS` is not computable and §0's whole finding stands unmeasured. |
| p_model | **KEEP** | — |
| p_conservative | **KEEP, but typed-absent by default** | Requires a *measurable* dispersion. Self-reported posterior width is unfalsifiable from outcome data — a layer reporting ±2pp and one reporting ±15pp produce identical likelihoods for every outcome sequence provided their means agree. And the one measured proxy is weak: median across-trial spread of 0.27 logits, with shrinkage a measured **no-op in 791/791 folds**. Inter-trial spread measures prompt sensitivity, not epistemic uncertainty. The field carries `dispersion_source` in `{bootstrap_refit, ensemble_disagreement, regime_residual, none}`; `none` means the value is **absent**, never equal to `p_model`. |
| **EV** | **REMOVE and RENAME** | Dollar EV is forbidden with **no unlocking milestone defined**. Replaced by dimensionless `edge_gross_prob_points` and `edge_net_prob_points` — never a dollar amount, never a currency unit. |
| expected execution | **KEEP as `execution_cost`** | With a per-term `basis` in `{OBSERVED, MODELED, BOUNDED}` and an `adverse_bound` on every BOUNDED term. CLOB: exact ladder walk. AMM: closed form plus residual. |
| liquidity loss | **KEEP on CLOB, RENAME on AMM** | On a CLOB this is the liquidity-loss term of the profit decomposition and is computable from the full ladder. On an AMM there is no order book to integrate a price-impact function over — the analogous quantity is curve impact, and it is a *different object*. Recording both under one name would be an equivocation. `clob_liquidity_loss` and `amm_impact_cost` are separate fields, each absent on the other venue. |
| fill probability | **KEEP, HEAVILY RESTRICTED** | CLOB taker: `not_applicable` — the visible ladder fills or it returns `UNFILLABLE`. CLOB maker: a **BRACKET** `(optimistic, point, pessimistic)`, never a scalar, because queue position at fill is unobtainable (§3.4b). AMM: this is *landing probability* and it is **`requires_submission`** — permanently typed-absent. Do not fabricate it, and do not let the maker bracket's existence suggest the AMM one is merely unbuilt. |
| expected post-fill alpha | **KEEP on CLOB only** | This is `markout(h)` and it requires the `trade` channel. Report a **vector** of horizons — and at Kalshi's event rates a clock-time horizon of 1–5s will frequently contain **zero** book events, so **event-time horizons are mandatory alongside clock time** and a markout across an interval with no book event is `stale`, never zero. On an AMM there is no maker and no analogue: absent. |
| **CVaR** | **REMOVE from the per-position record** | §3.3. Two-point loss means `CVaR = f` exactly, and negative when the tail probability exceeds `1−p`. Replaced by a book-level EVaR constraint, which is not a per-decision field. |
| **drawdown contribution** | **REMOVE as a per-position number** | Per-position drawdown contribution is not well-defined; the operative objects are the book-level `lambda_dd` and the state-dependent wealth multiplier. The record carries a **reference** to the book-state artifact and its version, not a number of its own. |
| **portfolio correlation** | **REPLACE with cluster membership** | A scalar correlation per position is not identifiable. The operative object is `cluster_id` + `graph_version` + `graph_coverage` in `{analysed_unrelated, analysed_related, not_analysed}`, with `not_analysed` forcing abstention. **The null hypothesis for correlation is DEPENDENCE**: an unresolved relation counts as correlated. In inference the cost of wrongly assuming dependence is lost power; in risk the cost of wrongly assuming independence is the blow-up. |
| **recommended notional** | **REMOVE** | Portfolio sizing is forbidden with no implementation surface, and "recommended" is a recommendation. |
| **max notional** | **REPLACE with `notional_ladder_ref`** | Size is a **declared input**, referenced by the digest of a frozen preregistered ladder (the `SOLANA-ROUTE-OBSERVATION-001` §4 pattern), never an output. |
| exit policy | **KEEP, with a hard guard** | Must reference an **executable exit quote**, never a reported mid. Carries `exit_mark_basis` in `{executable_exit_quote, ladder_walk, NOT_MARKABLE}`. §7.3. |
| reason codes | **KEEP — mandatory, typed, closed, ordered** | Free text is forbidden: prose is paraphrase-bypassable and cannot be aggregated. All binding codes are retained, not just the first. |
| evidence hash | **KEEP** | — |
| model version | **KEEP, EXTENDED** | Plus the `PAPER_SIMULATION` modeled-vs-observed basis **per field**, not per artifact. |

### 6.2 Fields the brief did not ask for and the research demands

| field | why it is not optional |
|---|---|
| **`delta_t_to_resolution`** | `g` is per **resolution**, not per unit time. An edge of 0.005 nats resolving in an hour and one resolving in six months differ by a factor of about 4,000. **This is the single largest economic omission in the original form.** It changes which position gets a cluster budget, and it interacts with §9.4: a 2pp edge on six-month markets cannot accumulate 30,000 observations in any human timeframe, so **long-horizon markets are unvalidatable regardless of their edge.** |
| **`kappa`** | The cost-kill multiple (§7.4). Hard gate at 2. |
| **`net_partial`, `net_conservative`** | Net computed twice — observed and modeled terms alone, then again with every BOUNDED term charged at its adverse bound. **`net_conservative` is the headline.** |
| **`regime`** | The conditioning variable. No number in this record means the same thing across regimes. |
| **`staleness_ms`, `observation_gap_member`** | A decision inside an observation gap is invalid, not high-latency. |
| **`abstained`, `denominator_member`** | Every candidate enters the denominator whether abstained or not. A prospective record containing only taken decisions is a selected sample and cannot support §9's acceptance test. |
| **`is_extrapolation`** | True when an input sits outside the calibrator's or the cost model's fitted support. Refuse rather than widen the interval. |
| **`search_history_ref`** | Ties the record to the declared variant count that enters the multiplicity family (§9.3). |

### 6.3 The record

`QDK-EVAL-1`. **This is an evaluation artifact, not an instruction.** It carries
no side, no dollar amount, no recommended size, and no action.

```
EvaluationRecord
  # --- identity and provenance ---
  record_id, venue_id, instrument_ref, evaluated_at
  model_id, model_version                  # PAPER_SIMULATION requirement
  basis_by_field: {field -> OBSERVED|MODELED|BOUNDED}   # per FIELD, not per artifact
  evidence_digest                          # sha256 over the canonical preimage
  search_history_ref, registry_experiment_id

  # --- belief and market ---
  p_market               {value, bid, ask, mid, source, quote_age_ms, basis}
  p_model                {value, forecaster_name, forecaster_version,
                          prompt_version, is_midpoint_anchored: bool}
  p_calibrated           {value, calibrator_id, fit_through, is_extrapolation}
  p_conservative         Result[{value, alpha, dispersion_source}, Absence]
  edge_gross_prob_points  dimensionless probability points
  edge_net_prob_points    dimensionless, after execution_cost

  # --- execution, per notional rung of the referenced ladder ---
  notional_ladder_ref    digest of the frozen preregistered ladder
  per_rung: [ {
      rung_id,
      execution_cost     {terms: [{name, value, basis, adverse_bound?}],
                          total_partial, total_conservative,
                          unfillable_shortfall?},
      clob_liquidity_loss  Result[value, Absence]    # CLOB only
      amm_impact_cost      Result[value, Absence]    # AMM only
      fill                 CLOB taker: not_applicable
                           CLOB maker: {optimistic, point, pessimistic}
                           AMM:        Absence(requires_submission)
      post_fill_markout    Result[[{horizon, kind: clock|event, value}], Absence]
      kappa                cost-kill multiple at this rung
      sign_flip_size       the size at which net edge crosses zero, if inside the ladder
  } ]

  # --- time and exit ---
  delta_t_to_resolution  {expected, p90, source}
  exit_policy            {rule_id, exit_mark_basis:
                          executable_exit_quote|ladder_walk|NOT_MARKABLE}

  # --- portfolio context, by reference only ---
  cluster_id, graph_version,
  graph_coverage         analysed_unrelated | analysed_related | not_analysed
  book_state_ref         {version, lambda_calib, lambda_dd, wealth_multiplier}

  # --- state and quality ---
  regime                 venue-specific closed enum
  staleness_ms, observation_gap_member: bool

  # --- the decision, which is an evaluation ---
  outcome                EVALUABLE | NO_TRADE
  reason_codes           [closed enum], ordered, ALL binding codes retained
  abstained              bool
  denominator_member     bool          # always true; recorded so it cannot be dropped
```

### 6.4 Four invariants on this record

1. **`basis_by_field` is per field.** A single artifact-level "modeled" tag lets
   an observed quantity and an assumed one sit side by side indistinguishably.
   The boundary's requirement is that the basis travels *with the number*, and at
   the sample sizes of §9.4 it is the only thing that stops thirty thousand
   modeled fills built on an optimistic assumption from measuring the assumption
   and being mistaken for evidence.
2. **`per_rung` is a list, not a scalar, and the ladder is referenced by
   digest.** A single "the" execution cost hides that the cost curve is a
   function of size and that the sign can flip inside the ladder (§4.4).
3. **`outcome = NO_TRADE` is reachable from the schema itself**, not by a
   sentinel value in a numeric field. §7.1.
4. **`denominator_member` is always `true` and is recorded anyway.** It exists so
   that any future filtering of the denominator is a visible schema-level act
   rather than a silent `WHERE` clause.

### 6.5 What must NOT appear, restated as a schema rule

No `side`. No `dollar_ev`, `expected_value_usd`, or any currency-denominated
expectation. No `recommended_size`, `target_size`, or `max_size` as an *output*.
No `order_type`, `limit_price`, `time_in_force`. No `position`, `fill`, `trade`,
or `pnl` table outside an accepted `PAPER_SIMULATION` lane. No wallet, key,
signature, blockhash, priority fee, nonce, or transaction/instruction field.

**Including "disabled" and "placeholder" versions** — `docs/SAFETY_BOUNDARIES.md`
bans those explicitly, and a placeholder column is how a boundary erodes without
anyone deciding to erode it.


## 7. Gates, guards and the scalars that make a result legible

### 7.1 Abstention is a separate GATE, not a `min()` term

The original form was

```
f_actual = min( lambda*f_Kelly(p_conservative), f_liquidity, f_CVaR,
                f_concentration, f_drawdown )
```

**A `min` over positive quantities is positive. The formula as written has no way
to abstain** — yet abstention is the default and the most frequently correct
action. Encoding "calibration is unknown for this regime, so we decline" as "the
liquidity cap happened to be 0" is not a representation, it is a coincidence.

Three further structural problems with that form, beyond §3.3's removal of
`f_CVaR`:

- **`f_concentration` is not a cap at all — it is a joint allocation.** `min`
  takes a function of one position. "These five positions together must not
  exceed `B_c`" is not expressible that way, and any per-position translation
  (`B_c/5`) is wasteful when edges differ and wrong when a sixth arrives.
- **`f_drawdown` is already inside `lambda`**, since `lambda = lambda_calib x
  lambda_dd` and `lambda_dd` *is* the drawdown constraint. A separate term
  applies it twice. The genuinely distinct term the form is missing is a
  **state-dependent** multiplier that tightens as wealth approaches the halt
  floor — size on the *excess over the stop level*, not on total wealth.
- **Three terms are missing entirely:** a minimum size `f_min` below which
  discretisation and fees dominate and the correct action is `NO_TRADE` rather
  than a tiny position; `delta_t`, because `g` is per resolution; and the
  target-versus-increment distinction — the output must be a **target with a
  no-trade band**, because every rebalance pays the full cost wedge and a system
  chasing a moving target pays it repeatedly for no edge.

**The revised shape:**

```
0. GATE       codes <- evaluate_gates(state, forecast, book, graph, wealth)
              if codes is non-empty:  return NO_TRADE(codes)      # THE DEFAULT

1. BELIEF     p_cal <- per-regime log-odds recalibration
              p_con <- alpha-quantile of a MEASURED dispersion, else ABSENT

2. CEILING    lambda <- lambda_calib x lambda_dd x wealth_multiplier
              f_ceil <- min( lambda * kelly(p_con, q),
                             f_liquidity,        # fillable depth
                             f_position_cap )    # a stated flat cap

3. ALLOCATE   choose {f_i} maximising the sum of  g(f_i) / delta_t_i
              subject to  0 <= f_i <= f_ceil_i
                          cluster budgets, book budget,
                          book-level EVaR, and K_eff >= K_min

4. DISCRETISE if rounding pushes the realised fraction above f_i: NO_TRADE
              if f_i < f_min:                                     NO_TRADE
              if the move is inside the no-trade band:            HOLD

5. EMIT       f_i, the BINDING constraint, the slack on every other constraint,
              lambda's three factors separately, p_con and its dispersion source,
              delta_t, cluster id + graph version, model identifier, and the
              modeled-vs-observed basis for every input.
```

**Step 5 is not decoration.** A size with no record of *why* it is that size
cannot be audited and cannot be debugged when the drawdown comes — and under the
`PAPER_SIMULATION` amendment it **may not be produced at all** without the model
identifier and the modeled-vs-observed basis travelling with the number.

**A note on step 2 that keeps Kelly in its place.** Kelly is a **ceiling, not the
allocator.** Its optimality is conditional on a known `p`, which is the one thing
we do not have: at `q = 0.90`, an *unbiased* forecaster with 3pp of honest noise
turns a real +3pp edge into **negative** expected growth, because the
estimation-error penalty carries a `1/(1−q)²` factor. **2x Kelly is exactly zero
growth**, and the bias that produces 2x Kelly is `delta = e` — so **a 2pp
forecasting bias zeroes out the growth of a genuine 2pp edge**, and a forecaster
calibrated to ±2pp is considered excellent. Meanwhile the growth curve is
symmetric about full Kelly and the *risk* curve is not: 0.75x and 1.25x both earn
93.7% of optimal growth, but the latter has a 12.0% chance of ending below where
it started against 2.4%. **Underbetting is nearly free; overbetting is not**, and
every error source pushes size upward. A "20% maximum drawdown at no more than 5%
probability" statement implies **lambda_dd ≈ 0.139**, about one-seventh Kelly,
earning 26% of the optimal growth rate. That is the price, and the only two knobs
are the drawdown and its probability.

*(One thing that does not work and should not be attempted: "Bayesian Kelly".
For a single binary contract `g` is **linear in `p`**, so the expected objective
under the posterior is identical to the objective at the posterior mean. The
width, skew and shape of the posterior have **literally zero** effect on the
Bayes-optimal log-growth bet. Anyone implementing "Bayesian Kelly" expecting it
to shrink positions has implemented Kelly-at-the-mean with extra steps. The
shrinkage must be justified as **ambiguity aversion against an unidentifiable
posterior width**, which is an honest reason — and it is why `p_conservative`
uses a *measured* dispersion or nothing.)*

*(A second thing worth carrying: the allocator and the scale are different axes
and they compose. Proper-scoring-rule betting fixes direction and relative
weights across simultaneous markets and is defined only up to a positive scalar;
Kelly fixes total exposure. They are not competitors on one axis, and the
candidate space is a grid — allocator x scale x gate — not a list. For a binary
market the Brier-rule proper bet and the raw margin rule are literally the same
allocator up to a constant of 2.)*

### 7.2 The abstention reason codes

Typed, closed, ordered, and **all binding codes retained**. Free text is
forbidden for the same reason the registry replaced its prose leakage guard with
a closed typed predicate schema: prose is paraphrase-bypassable and cannot be
aggregated.

| code | condition |
|---|---|
| `KILL_SWITCH_ACTIVE` | manual halt, or the drawdown stop fired |
| `STALE_STATE` | quote, forecast, graph, or calibration older than its regime's freshness bound |
| `OBSERVATION_GAP` | the decision timestamp falls inside a period the tape was blind |
| `BOOK_UNPUBLISHABLE` | sequence fault or integrity halt on the venue state |
| `CALIBRATION_UNKNOWN_FOR_REGIME` | fewer than the floor of scored forecasts in this regime cell, or the calibration-slope CI half-width exceeds its bound |
| `CALIBRATION_FAILED_FOR_REGIME` | the regime shows negative skill — and this repository has a live instance in tennis |
| `MODEL_VERSION_UNTESTED_IN_REGIME` | this model version has no prospective record in this regime |
| `GRAPH_COVERAGE_UNKNOWN` | the correlation graph did not analyse this instrument. Absence of an edge is not evidence of independence |
| `EXECUTION_COST_EXCEEDS_EDGE` | net edge is at or below zero |
| `KAPPA_BELOW_FLOOR` | the cost-kill multiple is below 2 (§7.4) |
| `EDGE_BELOW_MINIMUM` | net edge below the smallest edge the sample size can ever validate |
| `LIQUIDITY_BELOW_THRESHOLD` | resting size or pool depth below what the rung implies, or depth unmeasured |
| `UNFILLABLE_AT_RUNG` | the visible ladder cannot fill this rung |
| `EXTRAPOLATION_OUTSIDE_SUPPORT` | an input sits outside the calibrator's or the cost model's fitted support |
| `TAIL_UNVERIFIED_FOR_REGIME` | the prospective sample is too small to have seen a rare common-mode regime. **"Unmeasured" is not "satisfied"** |
| `TAIL_CONSTRAINT_BINDING` | book-level EVaR at its limit |
| `CORRELATED_EXPOSURE_AT_LIMIT` | cluster budget exhausted |
| `BUDGET_EXHAUSTED` | book budget at its limit |
| `SIZE_ROUNDS_ABOVE_LIMIT` | integer or lot rounding would push the realised fraction above the ceiling |
| `RESOLUTION_CRITERIA_AMBIGUOUS` | the resolution rule is not machine-checkable, or its source is disputed |
| `REGIME_SHIFT_SUSPECTED` | the recent residual distribution differs from the calibration window — i.e. our errors have become correlated |
| `NOT_MARKABLE` | no executable exit quote exists, so the position could not be honestly marked (§7.3) |

Four properties this layer must have:

1. **Abstention is not a failure to be minimised.** The `NO_TRADE` rate is not a
   KPI to drive down. A system abstaining on 99% of candidates is behaving
   correctly if 99% of candidates fail a gate. Pressure to "increase coverage"
   goes through changing a threshold **explicitly, with the change recorded** —
   never through weakening a gate's evaluation.
2. **The reason-code distribution is the single most informative diagnostic the
   kernel produces.** If `EXECUTION_COST_EXCEEDS_EDGE` is 95% of abstentions, the
   programme's binding constraint is the cost model and the venue, not the
   forecaster — and that is worth knowing **before** spending two years
   collecting trades. §8 makes measuring it a Phase-0 item for exactly this
   reason.
3. **Every candidate counts toward the denominator**, abstained or not.
4. **No gate may be bypassed by a flag.** A "force" path is how every one of
   these systems eventually fails. If a gate must be relaxed, the threshold
   changes and the change is versioned into the prospective record — which
   invalidates the prior sample for the acceptance test, as it should.

A related sequencing result worth recording, because it is measured rather than
argued: in a controlled replay with the forecaster held fixed so every policy saw
identical probabilities, **edge-proportional sizing with no selection gate
returned −55.5%, roughly five times worse than flat stakes at −4.7%**, because it
concentrates capital on confidently-wrong high-edge forecasts. **Selection
precedes sizing**, and the risk constraints live in deterministic code outside
the prompt — in the same study, prompt-level risk guidance was "frequently
ignored" and only hard-coded constraints held.

### 7.3 The mark-to-market guard

This is the most concrete, quantified hazard in the entire document for a paper
ledger, and it is an implementation requirement rather than an aspiration.

The price a data provider reports after a swap is the **post-trade marginal
price**, not the execution price, and in a thin pool these diverge violently —
with the reported price always moving *further* than the execution price for a
buy.

| pool TVL | notional | avg exec vs spot | **reported marginal move** | **phantom gain if marked at the reported price** |
|---|---|---|---|---|
| $1,936 | $500 | +51.90% | **+129.79%** | **+51.27%** |
| **$2,860** | **$500** | **+35.22%** | **+82.04%** | **+34.63%** |
| $2,860 | $150 | +10.74% | +22.05% | +10.21% |
| $11,578 | $500 | +8.89% | +18.00% | +8.37% |
| $67,119 | $500 | +1.74% | +3.00% | +1.24% |

> **A $500 buy into the median observed pool raises the reported price by 82%
> and, if the resulting position is marked at that reported price, shows an
> instantaneous 35% "profit" that is entirely the trade's own footprint.**

**The rule:**

> **A position is marked off an executable EXIT quote, never off a reported
> price.** Any modeled-fill to position to modeled-P&L chain that marks using
> `CryptoPriceTick.price_usd` **will manufacture profits out of its own simulated
> impact**. If no executable exit quote or exit-side ladder walk is available,
> the position is `NOT_MARKABLE` — a typed absence and an abstention code — and
> **not** marked at the mid "for now".

The CLOB analogue is the same rule with a different mechanism: mark off the
**bid** side of the executable ladder for a long, walked to the position size,
not off the mid. A mid-price mark on a thin binary book manufactures half the
spread per position on every valuation.

And the same discipline governs coherence detection, which is where naive engines
die: **a constraint on probabilities is not a constraint on quotes.** To capture a
violation you buy every leg at its **ask**. Evaluating on midpoints manufactures
phantom edge equal to half the summed spread — on an N-leg partition, `N/2`
spreads of pure fiction.

### 7.4 Kappa — the cost-kill multiple

Because a meaningful part of the cost stack is **bounded rather than measured**,
a single net number understates the fragility. Every result therefore reports one
robustness scalar:

> **kappa = the multiple of total modeled cost at which the net result crosses
> zero.**

| kappa | verdict |
|---|---|
| **below 1** | already dead — negative at the modeled cost |
| **1 to 2** | **not robust; may NOT support a confirmatory claim.** Our cost stack has non-closable terms and a stale liquidity estimate. A result that dies if costs are twice the model has not survived our own measurement error |
| **2 or more** | reportable, with kappa stated on the artifact |

Kappa is cheap to compute, hard to game (the *evaluator* computes it, not the
author), and it is exactly the number that would have made the EDGE candidates'
fragility legible before the cost model existed: at frictionless +0.10..+0.30
going to −0.03..−0.21 under a single fee assumption, **their kappa was well below
1.**

**Adverse bounds, never zeros.** Every non-observable cost term carries a
**declared adverse bound registered before data is seen**, and net is computed
twice: `net_partial` at the observed and modeled terms alone, and
`net_conservative` with every bounded term charged at its adverse bound.
**`net_conservative` is the headline.** This converts an unknown into a stated
worst case — a claim that can be falsified later — rather than into a silent
zero, which cannot.

The terms this applies to, per venue:

| venue | term | basis |
|---|---|---|
| Kalshi | half-spread | OBSERVED from the snapshot |
| Kalshi | taker fee | MODELED — round trip at both ends, no maker rebate, no rounding down |
| Kalshi | executable touch | OBSERVED; rows without a usable touch are **uncovered, never guessed** |
| Kalshi | queue position, partial fill, market impact | **BOUNDED** — declared, never silently zero |
| Solana | price impact at notional | OBSERVED from the quote, never recomputed from our own mid |
| Solana | route and pool fees | OBSERVED where the venue reports it; **no default fee, no assumed bps, no per-dex fee table** |
| Solana | priority fee, base fee | **BOUNDED — non-closable.** Fetching a priority fee is *forbidden* |
| Solana | Token-2022 transfer fee and hooks | **BOUNDED** from a quote; **measurable** from a realized fill (§5.4) |
| Solana | realized slippage, landing probability, MEV | **BOUNDED — `requires_submission`** for our own; **measurable as a population lower bound** for third parties (§5.4) |

Note what §5.4 does to two of those rows: it moves Token-2022 transfer fees and
third-party realized slippage from "bounded forever" to "bounded from a quote,
measured from a fill". That is the concrete payoff of the corpus, expressed in
the only currency that matters here — a cost term that stops being an assumption.

One rounding detail that is not a rounding detail: the Kalshi fee is rounded up
to the whole cent **on the order**, not per contract, so the overhead is at most
`1/C` cents per contract. Per-contract taker cost at P = 0.50 is **2.00c at
C = 1**, 1.80c at C = 10, 1.75c at C = 100 — and at P = 0.03 a one-contract order
pays the 1c minimum against a 0.21c marginal rate, roughly **5x the marginal
rate**. This implies a real `f_min`: **require at least 10 contracts, prefer 20**,
and make the rounding overhead an explicit line in the cost record rather than an
approximation.

### 7.5 Delta-t — the term whose omission is the largest economic gap

`g` is per **resolution**. Capital is committed for the duration, so the quantity
to rank and budget is `g(f) / delta_t`, and `delta_t` is the **pessimistic tail**
across every leg, not the expected value — markets extend, sports get postponed,
elections get contested.

Two consequences:

1. **A held-to-resolution position pays fees once; a microstructure round trip
   pays twice.** That asymmetry is another argument for microstructure as an
   execution-quality tool rather than a signal.
2. **Long-horizon markets are unvalidatable at any edge.** §9.4 needs
   30,000–75,000 prospective observations. A 2pp edge on six-month markets cannot
   accumulate that in any human timeframe. The correct response to a long-horizon
   opportunity is to **decline it rather than lower the bar** — `ADR-004` already
   says the quiet part: *"If challengers never beat the baseline, the correct
   outcome is improving models — not lowering the gate."*

For scale, on a captured edge held to resolution:

| captured edge | 7d | 30d | 90d | 365d |
|---|---|---|---|---|
| 1c | 68.9% | 13.0% | 4.2% | 1.0% |
| 2c | 186.7% | 27.9% | 8.5% | **2.0%** |
| 5c | 1350.6% | 86.7% | 23.1% | 5.3% |

A 2-cent arbitrage on a one-year market annualizes to **2.0%** — worse than
T-bills, for unbounded tail risk and full operational cost.


## 8. The implementation ladder

### 8.0 The ordering principle

> **Order by what unblocks the edge measurement, not by architectural elegance.**

Concretely, each rung is ranked by three questions, in this order: *does it
unblock `ΔS`?* — *is it time-sensitive, i.e. does delay destroy data that cannot
be recovered?* — *does it change the sample size of everything after it?*
Architectural completeness ranks last, and several architecturally natural
components are deliberately built late or not at all.

### 8.1 Critique of the proposed ordering

The proposed ladder was: **Phase 0** store contemporaneous market price, add a
market baseline, fix the registry `canon_digests` defect; **Phase 1**
`MARKET-STATE-OBSERVATION-001` extending the running sparse observer plus the
Solana forward realized-fill collector; **Phase 2+** anything that consumes an
edge, gated on Phase 0 showing `ΔS > 0` with adequate power.

**It is right in shape and wrong in four places.** Taken in descending order of
consequence.

**C1 — the Phase-2 gate as stated would freeze the programme for twenty years,
and it is the most important correction here.** "Phase 0 showing `ΔS > 0` with
adequate power" is ambiguous between two very different sample requirements, and
the natural reading picks the wrong one. The 30,000–75,000 figure in §9.4 is the
sample needed to demonstrate a **1pp net P&L edge**. `ΔS` is a **score**
difference — bounded, far lower variance than a return, and computable on
resolved forecasts with no trading at all. The research tracks say this from two
directions: *"the ΔS channel is cheaper to measure than the P&L channel… prove
ΔS > 0 first, on paper, and only then bake off allocators"*, and the calibration
slope is **roughly 15× cheaper in sample than the P&L test**.

> **The Phase-2 gate must be `ΔS > 0` measured on scores with event-clustered
> standard errors, not the P&L power number.** And "adequate power" must be a
> *declared number* — the `ΔS` minimum detectable effect at the available
> effective N — fixed at P0 and registered, not asserted afterwards.

**C2 — the Solana collector should not be gated behind Phase 0 at all, and this
is the change with the largest calendar consequence.** It answers a **different
question** (§4.6: "can we model execution?" versus "do we have an edge?"), so
gating it on the edge measurement is a category error. More importantly, it is
the **only item on the entire ladder where delay destroys data**: retrospective
backfill is not obtainable (§5.4), so every day not collected is permanently
lost, while every other rung operates on data that will still be there next
month.

Its long pole is **approval, not engineering**: it needs its own accepted
milestone *and* a separately reviewed `BANNED_IDENTIFIER_FRAGMENTS` decision
(Tier 3). So:

> **Start the approval clock in Phase 0 even though the build is Phase 1.**
> Writing the milestone and requesting the Tier-3 decision costs nothing and
> unblocks nothing else; waiting until Phase 0 completes costs weeks of corpus
> that cannot be recovered.

**C3 — Phase 0 is missing three items, and one of them may collapse months of
the schedule.**

*(a) The retrospective `q` may already exist, and nobody has looked.* The
prediction-market track concluded that computing `ΔS` historically would need a
join against `MarketPriceTick` and that "per the roadmap the Probability lane has
**no live tape writer**, so that coverage is unlikely to exist historically". But
**`MarketPriceTickBucket` (`app/models.py:319`) carries `open_bid`, `close_bid`,
`open_ask`, `close_ask`, `open/high/low/close_mid`, `spread_avg`, `domain`, and
`tick_count` in 300-second buckets keyed `(market_ticker, bucket_start)`** — and
per OPS-013/OPS-014 the raw-tick retention reduction pruned 380,850 raw ticks
with **buckets intact**, precisely so that raw ticks need not be kept forever.

> **So an executable-quote `q` at five-minute resolution may be recoverable for
> the entire forecast history without any live tape writer.** Coverage is
> unmeasured. Measuring it is one `JOIN` and it is the cheapest query in this
> whole document. If it returns high coverage, the historical `ΔS` measurement
> stops being a Phase-1 wait and becomes a Phase-0 result.
>
> The caveats are real and must be stated with any number it produces: a 5-minute
> bucket is not the quote at forecast creation, it is the quote within ±5
> minutes; buckets exist only where aggregation ran and the ticker was on the
> watchlist; and this is adequate for `ΔS` (a score comparison) but **not** for
> `L_ρ` (an execution price).

*(b) Residual correlation must be measured in Phase 0, because it sets the
sample size of everything after it.* `DEFF = 1 + (m−1)ρ`, and every `n_required`
in §9.4 scales linearly with it. It costs **no new data** — residuals
`r_i = Y_i − p̂_i` already exist for every scored forecast. **Measuring it first
is the difference between planning for 15,000 and discovering at trade 15,000
that you needed 73,000.** And the correlation that matters is not event
correlation but **model-error correlation** — the mechanism nobody instruments,
and the one with a live in-repo instance: all six EDGE-SELECTION-001 candidates
failed out-of-sample *together*.

*(c) The abstention denominator must be instrumented in Phase 0.* If
`EXECUTION_COST_EXCEEDS_EDGE` fires on 95–99% of candidates, the programme's
binding constraint is **venue selection and the cost model**, not the forecaster,
and the entire kernel above it is premature. That is measurable **before any
trade**. And it should **reuse the existing `edge_precheck` surface** —
probability-gap measurement with validity checks, already built with no dollar
EV, side, size, or action fields by construction — rather than introducing a new
one. Adding a typed reason code to an existing observation surface is a much
smaller act than creating a candidate pipeline.

**C4 — the registry item is right but under-specified, and it has a consequence
that needs a human.** Two corrections:

*(a) `canon_digests` is not the biggest hole.* Verified in this session:
`experiment_registry.py:500` writes
`"canon_digests": {f: _file_digest(root / f) for f in CANON_FILES}` where
`CANON_FILES = ("docs/PROJECT_CANON.md", "docs/SAFETY_BOUNDARIES.md")`, and
`_evaluation_code_drift` at line 802 reads **only**
`refs.get("evaluation_code_digests")`. `canon_digests` is written and never
compared. Confirmed drift: `SAFETY_BOUNDARIES.md` pinned at `d6c38783…`, current
`c5cb2936…` — **drifted for 8 days while `status()` reported clean.** But the
*larger* hole is that evaluation is permitted from `collecting` and even
`registered` states, recorded merely as a deviation — which contradicts both the
registry's own design and `status()`'s own `evaluation_permitted = state in
(MATURED, EVALUATED)`, and **reopens the peek-and-lock route**. Fix both, and fix
the state gate first.

*(b) Turning the check on will immediately fail a live registered experiment,
and that is the point.* The drift is real and present. It must be handled as a
**declared amendment with a recorded reason**, not a silent re-pin — and it is a
Tier-2 decision because someone has to accept that a registered experiment
becomes non-comparable. Note also that **adding `brier_skill_vs_market` to
`forecast_reliability.py` is itself a drift event**, since that file is in
`EVALUATION_CODE_FILES`. Both changes should land in **one declared window** so
the registry reports one amendment rather than two.

**C5 — a missing rung between Phase 0 and Phase 1: the free recalibration
control arm.** If the market price is miscalibrated with a fitted intercept and
slope, then `p_recal = σ(â + b̂·logit q)` is a **forecast derived from the price
alone** — no research, no LLM, no evidence pipeline. It costs one logistic
regression on data Phase 0 produces.

Its expected value here is **low and it should still be run**, for a reason that
is not about its own P&L: it is the correct **control arm** for `ΔS`. If our
forecaster does not beat a free price transform, the forecaster is not the
product, and we should trade the transform and skip the apparatus. Conditional
calibration is **a competitor to our forecasting stack, not a layer on top of
it**.

The honest expectation is null: by model-free reliability decomposition, **every
Kalshi domain outside Politics has a reliability component rounding to
0.000–0.001** — there is essentially nothing to recalibrate — and Politics is the
one domain we have no forecaster for. That is a **coverage** problem, not a
statistics problem, and infinite data does not fix it.

**C6 — two smaller ordering points.** Within Phase 0, put the **prospective
capture first** (a forecast written today without `q` can never have its `ΔS`
computed) and the retrospective analyses after (existing rows are not going
anywhere). And be precise about "extend the sparse observer": that is the right
call for the *realized-fill collector's population, cadence discipline, and
denominator preservation* — but the sparse lane's own sampling design (exactly
two observations, at 6h and 24h) is close to the **worst possible design for
microstructure**, and its 24h coverage of **4.6%** makes any 24h-horizon
experiment **not evaluable**. Extending its schedule also risks the denominator
that lane exists to protect. **Extend its population and its plumbing; do not
change its horizons without a separate decision.**

**What the proposed ordering got right, and it is the important part.** The
inversion itself — measurement before exploitation — is correct and is the whole
document. The choice to extend a lane that is already running rather than build a
parallel service is correct and matches this repository's own history. And
storing `q` on every forecast is correctly identified as the cheapest
high-value change available; it is first on the revised ladder too.

### 8.2 The revised ladder

Every rung is independently verifiable and states what would falsify it. Tiers
are from §2.5.

---

#### PHASE 0 — make the edge measurable. Days, cheap, unblocks everything.

**P0.0 — Measure the retrospective `q` join coverage.** *(Tier 1, hours)*
One query: for each `MarketForecastRecord`, is there a
`MarketPriceTickBucket` row for the same `market_ticker` whose
`bucket_start` window contains `created_at`, carrying a usable
`close_bid`/`close_ask`? Report coverage overall and by `domain`,
`forecaster_name`, and month.
**Verified by:** a coverage number with its denominator, and a stated
bucket-to-forecast time offset distribution.
**Falsified by:** coverage near zero, in which case historical `ΔS` genuinely
waits for prospective capture and P0.1 becomes the critical path.
**Why first:** it is the cheapest query in the document and it can collapse
months of schedule.

**P0.1 — Store the contemporaneous market quote on every forecast.**
*(Tier 2, additive migration)*
Add to `MarketForecastRecord`: `market_yes_bid`, `market_yes_ask`,
`market_mid`, `market_spread`, `quote_observed_at`, `quote_source`
(`tick|bucket|scan|absent`), and a typed absence when no quote existed.
**Verified by:** SC-1 — every forecast written after this checkpoint carries a
quote or a typed absence; a query returning zero rows with neither.
**Why second and not first:** it is the only rung whose delay is irreversible for
*future* rows, so it must land early — but P0.0 may show the *past* is already
covered, which changes how urgently everything downstream is needed.

**P0.2 — Segment the corpus by `p ≡ q`.** *(Tier 1)*
Count, by `forecaster_name`, `forecaster_version`, `evidence_depth`, and
`calibration_tags`, how many forecasts are midpoint-anchored. The supplied
finding is **PAIRED = 0** — the market-anchored `template_baseline` rows pair
with zero source-backed forecasts.
**Verified by:** a segmentation table whose parts sum to the corpus.
**Falsified by:** a non-zero paired count, which would be *good news* and would
make P1.0 immediately runnable.
**Consequence if PAIRED = 0 holds:** **P0.1 alone unblocks nothing.** A market
price stored beside a forecast that *is* the market price yields `ΔS ≡ 0` by
construction. The measurement additionally requires a **non-anchored forecaster
running on tickers where `q` is recorded** — which is a prerequisite the proposed
ladder does not name, and which is the real gate on Phase 1.

**P0.3 — Add a market baseline to reliability reporting.** *(Tier 2)*
`brier_skill_vs_market` alongside `brier_skill_vs_base_rate` in
`app/services/forecast_reliability.py`, with the market baseline **typed-absent**
(and its denominator reported) where no `q` exists.
**Verified by:** SC-2; and a reliability artifact on a corpus with no `q` must
report the absence rather than silently omit the field.
**Note:** this file is in `EVALUATION_CODE_FILES`, so this is a **drift event**
for every registered experiment. Land it with P0.5 in one declared window.

**P0.4 — Measure residual correlation and `DEFF`.** *(Tier 1)*
`ρ_model = corr(r_i, r_j)` over residuals grouped by feature, model version,
regime, and time bucket, on the ~12,945 already-scored forecasts. Report
`K_eff = K/(1 + (K−1)ρ)` and the implied `DEFF`.
**Verified by:** a `DEFF` with a cluster definition and an interval.
**Why it must be here:** every `n_required` downstream scales linearly with it.

**P0.5 — Close the registry defects.** *(Tier 2)*
In order: (i) refuse evaluation from non-matured states rather than annotating
it; (ii) compare `canon_digests`, not merely record them; (iii) resolve repo
paths from the module rather than `cwd`. Then handle the **already-drifted**
`SAFETY_BOUNDARIES.md` pin as a declared amendment with a recorded reason.
**Verified by:** a test that a registered experiment with a mutated canon file
reports drift; a test that evaluation from `collecting` is refused.
**Falsified by:** the drift check passing on the live baseball experiment, which
would mean the comparison is not actually wired.

**P0.6 — Instrument the abstention denominator.** *(Tier 2)*
Extend the existing `edge_precheck` surface to emit a typed reason code and a
`denominator_member` flag per candidate, with **no side, no size, no dollar
amount**. Report the reason-code distribution.
**Verified by:** SC-6 — the distribution sums to the candidate count.
**What it decides:** if `EXECUTION_COST_EXCEEDS_EDGE` dominates, the binding
constraint is the venue and the cost model, and Phase 1's priorities change.

**P0.7 — Open the Solana collector's approval track.** *(Tier 3 request; no code)*
Write the milestone for the read-only realized-fill collector and request the
`BANNED_IDENTIFIER_FRAGMENTS` decision. **No RPC call, no code, no flag.**
**Verified by:** an accepted-or-refused decision on the record.
**Why in Phase 0:** it is pure calendar. The corpus cannot be backfilled.

**Phase 0 exit criterion:** `ΔS` is either *computed* (if P0.0 found coverage and
P0.2 found paired rows) or *demonstrably blocked on a named prerequisite*, with
`DEFF`, the abstention distribution, and the `ΔS` minimum detectable effect all
declared numbers rather than assertions.

---

#### PHASE 1 — the two collection lanes. Weeks, and one of them is time-critical.

**P1.0 — `MARKET-STATE-OBSERVATION-001`: extend the running sparse observer.**
*(Tier 2)*
Extend **population and plumbing**, not horizons (§8.1 C6). It already has a live
cohort, a growing corpus, denominator preservation, typed non-observation, and a
proven bounded-pass shape. Building a parallel service would duplicate all four
and inherit none.
**Verified by:** the existing lane's own invariants continue to hold — no change
to its 6h/24h denominators, no change to enrolment eligibility, `external_calls`
accounted per pass.
**Falsified by:** any movement in the sparse lane's coverage numbers attributable
to this change.

**P1.1 — The Solana forward realized-fill collector.** *(Tier 3 to activate)*
Gated on P0.7. Shape: default-OFF flag, bounded pass, deterministic population
(the sparse lane's cohort, inheriting and **stating** its ~40% enrolment
ceiling), typed non-observations, no raw payload persisted (only a body digest
chained into the row digest), `getFirstAvailableBlock` recorded per pass, `403`
as a stop condition. Row shape per §5.4, with `pool_state_completeness`,
`mev_class`, and `token_conservation_gap`.
**Verified by:** `post = pre + delta` on every vault on every row; per-mint
conservation to the base unit except where `token_conservation_gap` is non-zero;
digest reproducibility; zero paid calls; zero rows in a success state missing a
recorded status, digest, or latency.
**Falsified by:** one balance identity violation; one paid call; one row with a
defaulted numeric.
**What it must NOT build:** no `PaperFill`, `PaperOrder`, `Position`, or
`RealizedPaperPnL` row, table, column, or placeholder. The corpus is evidence;
the consumer that turns evidence into a modeled fill is a different, later,
separately-accepted question.

**P1.2 — The free-recalibration control arm.** *(Tier 1)*
Fit one global two-parameter recalibration on the Phase-0 `q` data, with
**event-clustered** standard errors and a **permutation null reported alongside
every R²**. At most three coarse horizon buckets and two domain groups — six
cells, not two hundred. **Hard gate: train-early / test-late.**
**Verified by:** an out-of-time result with its permutation null.
**Expected outcome:** null on sports. **That is a useful result**, and it is the
control arm for P1.3.

**P1.3 — Compute `ΔS` prospectively.** *(Tier 1)*
`mean[S(p,y) − S(q,y)]` per domain, event-clustered, on prospectively-generated
non-anchored forecasts only, excluding every `p ≡ q` row.
**Verified by:** SC-3 and SC-8; a verdict reachable in all three directions.
**This is the gate on everything in Phase 2.**

---

#### PHASE 2 — only if `ΔS > 0`. Anything that consumes an edge.

**Gated on P1.3 returning `market_relative_edge_measured_positive` at the
declared `ΔS` power, and on P1.2 not beating it.** If `ΔS ≤ 0` everywhere, **the
correct action is to stop**, and that is a legitimate and valuable outcome —
indeed the one the external evidence predicts.

In rough order, none of it authorized here: the cost model and `κ` computation
against the Phase-1 corpus; the taker-only CLOB execution layer; the allocator ×
scale × gate bake-off, **paired on one shared forecast stream** (which cuts the
required N by roughly an order of magnitude) and with a real `ABSTAIN` arm,
because a bake-off that cannot return "none of these" is not an experiment; and
only then, and separately accepted, anything resembling a modeled paper P&L
under `PAPER_SIMULATION`.

**Explicitly not in Phase 2 at any point:** everything in §3's cut list.

### 8.3 The ladder at a glance

| rung | tier | new data? | unblocks | time-critical? |
|---|---|---|---|---|
| P0.0 join coverage | 1 | no | possibly the entire historical `ΔS` | no |
| P0.1 store `q` | 2 | prospective | `ΔS` on all future rows | **yes, for future rows** |
| P0.2 segment `p ≡ q` | 1 | no | names the real Phase-1 prerequisite | no |
| P0.3 market baseline | 2 | no | SC-2; drift event | no |
| P0.4 residual correlation | 1 | no | every `n_required` downstream | no |
| P0.5 registry defects | 2 | no | trusting any confirmatory verdict | no |
| P0.6 abstention denominator | 2 | prospective | tells us if the venue is the constraint | no |
| P0.7 collector approval | 3 | no | P1.1 | **yes, pure calendar** |
| P1.0 extend sparse observer | 2 | prospective | AMM lifecycle features | no |
| P1.1 realized-fill collector | 3 | prospective | the execution model's only ground truth | **yes, not backfillable** |
| P1.2 recalibration control | 1 | no | the control arm for `ΔS` | no |
| P1.3 compute `ΔS` | 1 | no | **the Phase-2 gate** | no |

Two rungs are time-critical and they are **P0.1 and P0.7/P1.1**. Everything else
can be reordered without cost.

### 8.4 What is deliberately absent from this ladder

- **A live Kalshi tape writer is not a Phase-0 dependency.** `ΔS` needs the
  *forecast-time* quote, which the scan path and the tick buckets already supply.
  The tape is needed for `L_ρ` — the execution-cost measurement — which is a
  Phase-2 concern. Requiring it in Phase 0 over-constrains the schedule.
- **No forecaster work.** This ladder measures the forecaster we have. If P1.3
  returns null, improving the forecaster is the correct next move — but that is a
  different milestone and it should not be pre-empted by building machinery for
  an edge that does not exist.
- **No sizing layer.** §7 designs it so we know what to instrument; nothing on
  this ladder builds it.
- **No coherence engine**, intra-venue or otherwise. If it is ever built, it
  starts at intra-venue complement pairs with exact bindings, measures how often
  the 3.50¢ hurdle is cleared on executable prices, and **stops there if the
  answer is never** — a result worth having on its own.


## 9. Evaluation and preregistration

### 9.1 The Operative-Field Invariant

Our registry's original leakage guard was a prose blocklist, and review defeated
it with a synonym. The fix was applied to membership — a closed typed predicate
schema — but it was **not applied as a rule**, and the same mistake recurred one
layer down when a `selection_method` prose scan accepted "hand picked after
looking at results". **A defect that recurs after being fixed is a missing
invariant, not a missing patch.**

> **The Operative-Field Invariant.** Every field the evaluator reads is **typed
> and closed** — an enum, a number, a timestamp, or a predicate over an
> allowlisted field registry. Every prose field is **non-operative and provably
> unread**, enforced by an AST test asserting that no evaluator branch depends on
> it.
>
> **A field that is required but unread is FORBIDDEN.** It is a recorded promise,
> and recorded promises are how "a good filing cabinet with a strong lock and no
> inspector" happens.

The precedent already exists in-repo: operator notes are bounded, secret-scanned,
and covered by an AST test asserting no branch reads them. That pattern becomes
the rule for all prose.

The third clause is not cosmetic. The current manifest carries **eight required
fields that no evaluator code reads** — `domain_sample_floors`,
`evaluation_horizons`, `missing_data_policy`, `canceled_void_policy`,
`conflict_policy`, `stale_score_policy`, `invalidating_conditions`, and
`multiple_testing_policy`. Each must become **typed-and-read** or be **demoted to
explicit non-operative rationale**. There is no third option, because the middle
state is precisely what lets an author believe a promise is binding when it is
not.

### 9.2 Registry defects that must close before any confirmatory claim

Verified against the current implementation in this session where marked.

| # | defect | status | consequence |
|---|---|---|---|
| **D1** | Evaluation permitted from `collecting` and `registered`, recorded only as a deviation | in-repo | **Reopens the peek-and-lock route.** Nothing forces maturation before a terminal verdict is pinned, and it contradicts `status()`'s own `evaluation_permitted = state in (MATURED, EVALUATED)` |
| **D2** | `canon_digests` pinned but never compared | **VERIFIED this session** — written at `experiment_registry.py:500` over `CANON_FILES = ("docs/PROJECT_CANON.md", "docs/SAFETY_BOUNDARIES.md")`; `_evaluation_code_drift` at `:802` reads only `evaluation_code_digests` | **Real, present, undetected drift** in the document that defines the safety boundary: pinned `d6c38783…`, current `c5cb2936…`, undetected for 8 days while `status()` reported clean |
| **D3** | The "universe created before registration" check is skipped at its only call site | in-repo | The universe guarantee is unreachable from the register path |
| **D4** | `_evaluation_code_drift` resolves paths against `Path.cwd()` | **VERIFIED this session** — `status()` calls it with no `repo_root` | Raises from another directory instead of reporting drift |
| **D5** | A dead `if False and …` branch in registration validation | in-repo | Harmless today; load-bearing the moment validation changes |
| **D6** | The `record-result` text renderer raises on a renamed field, and text is the default format with no test | in-repo | The default invocation of the enforcement command tracebacks |
| **D7** | Eight required manifest fields never read | in-repo | §9.1 |
| **D8** | `multiple_testing_policy` is free text validated only as non-empty | in-repo | **No multiple-testing correction of any kind exists in `app/`** |
| **D9** | Exceeding the event/result caps makes chain verification report "not intact" | in-repo | Permanently bricks an experiment rather than refusing the append |
| **D10** | `primary_metric.name` is never cross-checked against its definition or the hypothesis | in-repo | Already latent in a live registered experiment: `name: "mean_brier"` with a *skill* formula as its definition and a hypothesis stated in skill terms — **three different quantities in one immutable record** |

D10 deserves emphasis because the manifest is immutable: the evaluator will
compute one quantity while the hypothesis asserts another, and it cannot be fixed
afterwards. **A registration whose metric name, definition, and hypothesis are
not mutually consistent must be rejected at registration.**

### 9.3 What the evaluator must enforce

**The enforcement principle:** *the evaluator computes; the author confirms.* The
author supplies only the experiment id, a confirmation, and non-operative notes.
Every quantity that could change a verdict — population, membership, n, metric,
cost, interval, window satisfaction — is recomputed from the committed record.
**A value an author can supply is a value an author can choose.**

Verdict precedence runs **integrity, then drift, then data quality, then
stopping, then floors, then the number** — integrity beats arithmetic always:

1. Chain integrity — broken chain, refuse.
2. **Code *and canon* drift** — both compared (D2).
3. Population reconstruction — rebuilt from forecast-time fields only,
   independently recomputed, refused on disagreement.
4. **Representativeness** — evaluated-sample composition against registered
   universe composition across the declared stratification fields; material drift
   **blocks a favourable verdict**; degenerate strata (prevalence within 5% of 0
   or 1) are reported `inconclusive` and may not contribute to a headline.
5. **Disposition balance** — the buckets must sum **exactly** to the enrolled
   count, and the evaluator refuses an unbalanced result. Once enrolled, a member
   never leaves the denominator; it moves to a typed disposition. **NULL is not
   death**: collapsing "we failed to observe" into "it died" would, at 4.6% 24h
   coverage, manufacture a ~95% death rate out of a monitoring gap.
6. Stopping-rule satisfaction — from the clock and the data, with out-of-window
   evaluation stamped a deviation **in both directions**. A protocol that only
   polices favourable deviations teaches you to reach negative conclusions
   sloppily, and the habit does not stay in the negative direction.
7. **Sample floors, total and per-arm**, evaluator-computed.
8. **Cost** — `net_conservative` computed before gross is displayed; refuse any
   artifact carrying gross without net; compute kappa and refuse a confirmatory
   claim below the floor.
9. The number, **with a multiplicity-adjusted cluster-bootstrap interval** as the
   headline. The raw interval never appears without it.
10. Control check — each anomaly condition evaluated and reported **separately**,
    combined as a **conjunction**. A guard that reports one boolean cannot be
    audited, and the last one was wired as an OR and did not trip. The control
    must be **mechanism-independent and specified without reference to any
    in-sample ranking** — the previous "negative control" was a data-derived
    worst cohort, and its out-of-sample inversion to best-in-class was exactly
    the regression to the mean you would predict.

**Multiplicity.** The family is *"every confirmatory test over the same
population in one epoch, **plus every variant evaluated on overlapping data
during the search that produced them**"*, so
`m = family_size_declared + search_history.variants_evaluated`. The second term
is the whole point: a family counting only registered experiments counts six
candidates and misses the eighteen-policy search that generated them — and the
real prior search was **39+ variants over one ~260-row window**. Holm–Bonferroni
for confirmatory claims; Benjamini–Hochberg only to rank pre-screen candidates
for registration, where an FDR-surviving result is *a reason to register*, never
a finding.

**Secondary metrics go into a sealed section the verdict function provably cannot
read**, enforced by an AST test. This converts "ECE is descriptive only and
cannot be promoted" from a prose promise into a structural property. Without it,
a null primary plus an interesting secondary is the most natural rationalization
available — and it is the one the registry's own founding document predicted we
would reach for.

**Interim looks default to zero and are metric-blind.** They may return liveness
fields only — arrival count, coverage fraction, error states — never the primary
metric, any secondary metric, any per-arm breakdown, or any quantity from which
those are recoverable. An implementation that cannot enforce that separation must
return nothing. The point is not that someone might peek; it is that **a decision
moment chosen by a human who has seen the data is not a stopping rule**. An
undeclared look **invalidates** the experiment rather than downgrading it,
because unlike a window deviation it is unbounded in how much it can inflate the
result.

**One protocol rule that is not arithmetic and is stronger than the arithmetic.**
A single passing experiment is not a finding. A confirmatory claim requires a
**second, independently registered, non-overlapping replication**, because
replication is robust to the one thing the correction is not: an **undeclared
prior search**. A search can inflate one window; it cannot easily inflate two
disjoint prospective windows in the same direction. Undeclared search remains the
protocol's known hole and should be treated as a known hole rather than a solved
problem.

### 9.4 The sample-size reality, stated plainly

Per-trade Sharpe for a fixed-stake binary is `(p−q)/sqrt(p(1−p))` — **the price
cancels**. Required n for a one-sided test at alpha = 0.05, 80% power:

| net edge | base n | **x DEFF 2.2** | **x DEFF and Bonferroni-20** |
|---|---|---|---|
| 0.5 pp | 61,820 | 136,003 | 292,850 |
| **1 pp** | **15,451** | **33,991** | **73,191** |
| 2 pp | 3,858 | 8,488 | 18,276 |
| 3 pp | 1,712 | 3,765 | 8,107 |

> ### 15,451 trades to detect a 1pp net edge; 33,991 with correlation; 73,191 with multiplicity. **Plan on 30,000–75,000 prospectively-recorded executable decisions.**

The minimum detectable edge is the more useful planning instrument, because it
answers "what can I conclude from the sample I will actually have?":

| n | MDE | interpretation |
|---|---|---|
| **36** | **20.7 pp** | the MVP-005A paired sample. **Detects nothing real.** |
| 100 | 12.4 pp | — |
| 500 | 5.6 pp | larger than any plausible gross edge |
| 1,000 | 3.9 pp | about the round-trip cost wedge at q = 0.50 |
| 2,000 | 2.8 pp | |
| 5,000 | 1.8 pp | |
| 15,451 | 1.00 pp | |
| 100,000 | 0.39 pp | |

> **The gate recorded as "crossed" had no resolving power.** MVP-005A's paired
> n = 36 could only ever have detected a **20.7 percentage-point** edge — roughly
> twenty times coarser than the effect anyone is looking for.

**Read the n=1,000 row against the cost wedge.** At `q = 0.50` you must be right
by **3.75 percentage points** more often than the market to net 1pp, so a
thousand prospective trades can only detect an edge roughly equal to the cost of
trading. Below about n = 2,000 the experiment cannot see anything smaller than
its own friction.

**The feasibility arithmetic, and it is the uncomfortable part:**

| qualifying trades/day | n=3,858 (2pp) | n=15,451 (1pp) | n=33,991 | n=73,191 |
|---|---|---|---|---|
| 1 | 10.6 yr | 42 yr | 93 yr | 200 yr |
| **10** | **1.1 yr** | **4.2 yr** | **9.3 yr** | **20 yr** |
| 50 | 77 d | 309 d | 1.9 yr | 4.0 yr |
| 100 | 39 d | 155 d | 340 d | 2.0 yr |

> **At 10 qualifying trades per day, the full-correction 1pp test takes twenty
> years.** And the qualifying rate is *after* abstention: at a 95% abstention rate
> — reasonable, since `EXECUTION_COST_EXCEEDS_EDGE` alone will remove most
> candidates — **50 qualifying trades per day requires observing 1,000 candidates
> per day.**

**The correct conclusion is not to abandon the programme and not to lower the
bar.** It is to **stop treating "build the trading system" as the milestone** and
start treating "raise the candidate rate and shrink the confidence interval" as
the milestone, because those are what the timeline is actually made of. It is
also the strongest argument for §8's C1: **`ΔS` is a much cheaper channel than
P&L, and it answers the only question that matters first.**

*(On sequential testing: anytime-valid confidence sequences permit continuous
monitoring without inflating type-I error and cut expected sample size by roughly
30–50% **under a strong alternative** — but under a **weak** alternative, which
§0's negative prior makes the relevant case, they require **more** samples than
the fixed-sample test, because the price of optional stopping is paid up front.
Use them for the *safety* property — stopping early on evidence of harm — not as
a way to make the number smaller.)*

### 9.5 Backtesting admissibility

> **The Asymmetry Rule.** A historical backtest may produce a *negative* result
> that binds, and may **never** produce a *positive* result that binds.

Three independent reasons, each individually sufficient, all biased in the
**same** direction — toward inflating measured skill. **Outcome contamination**
is unfalsifiable and unbounded for a frontier model, and every proposed
mitigation reduces to hoping the model cannot re-identify an event from its
structure — when a model good enough to *forecast* an event from its structure is
good enough to *recognize* it; the mitigation and the capability are the same
capability. **Context leakage** through a retrieval path assembled now, from
sources edited, re-dated and back-linked since. And **no counterfactual book**: a
historical snapshot tells you what the book *was*, not what it would have been
had we traded into it, so a backtested fill is a model scored against another
model.

This makes backtesting a **cheap, fast killing field** — the correct place to
send a hypothesis first, precisely because it is rigged in the hypothesis's
favour. Our own history demonstrates it: the useful output of the entire
EDGE-SELECTION lane was a *refutation*, and the cost model killed the same
cohorts faster and cheaper still. **Run the cost-first check before anything
else: every one of the six EDGE candidates would have died there, weeks earlier
and for free.**

Historical data remains admissible for exactly two non-inferential uses:
**cost-model calibration** — a claim about the market's microstructure with no
LLM in the loop, so contamination is irrelevant — and **power and feasibility
analysis**, since arrival rates and attrition curves are properties of the
data-generating process. The second is the highest-leverage use of history we
have, because it prevents registering an experiment that cannot conclude: it
already blocked two of three drafts, measuring baseball at 410.7 arrivals/day
(feasible), tennis at 1.2/day (marginal), and soccer at 0.0/day (not feasible).

**Note the one place the Asymmetry Rule reverses**, because it determines what
first capital would ever be for. For *skill*, contamination inflates, so a
positive backtest proves nothing. For *costs*, every unmeasured term is charged
adversely, so the modeled cost is a conservative **upper** bound — which means a
fill-model validation **can only bring good news or a corrected model, never a
hidden loss** of the kind that killed the EDGE candidates. If real capital is
ever authorized, the coherent first deployment is therefore an experiment whose
**primary metric is fill-model error, not P&L**, at a size where total loss is
operationally irrelevant and is budgeted as the cost of the measurement.
**No amount of paper trading closes the fill-model gap. The only instrument that
measures a fill is a fill.**

### 9.6 Time-consistent validation, where it applies

Purge and embargo govern the pre-screen and cost-model fitting only. Naive k-fold
is invalid here for four reasons, and "time series are ordered" is the weakest of
them; the one our data punishes hardest is **entity clustering** — dozens of
Kalshi markets belong to one game, many observations belong to one token, and
**the independent unit is the event, not the row**.

| lane | label horizon | purge | embargo | cluster unit |
|---|---|---|---|---|
| Kalshi intraday follow-through | 60 min | 60 min | **24 h** | game / event |
| Kalshi resolution calibration | to settlement | full declared max | **7 d** | event |
| Solana short bands | 15 min / 1 h | = band | 6 h | token |
| Solana long bands | 6 h / 24 h | = band | **48 h** | **token + launch-hour cohort** |

Both are applied **at the cluster level, not the row level** — purging a row
while its sibling markets on the same game remain in training accomplishes
nothing. Where a length is uncertain the protocol takes the **longer** value,
because under the Asymmetry Rule an over-long embargo costs sample (making a kill
harder, i.e. conservative) while a too-short one manufactures false survival.
And a walk-forward result is reported as **the vector of fold results plus the
worst fold**, never as the mean across folds — a mean across folds is exactly how
"one fleeting 48h window printed a favourable flag" became a candidate.

These lengths are **reasoned from cluster structure, not measured from an
autocorrelation decay**, and re-deriving them empirically is §12's
highest-value methodological follow-up.


## 10. Lane ranking — a recommendation for Eric, not a decision

**This is a recommendation for Eric. It is not a decision and this document does
not take one.** The reason it needs a human is that the two lanes are ranked
differently by two defensible criteria, and the memory index and the evaluation
protocol currently point in **opposite** directions.

### 10.1 The tension, stated without softening

The paper-execution ledger ranks **Solana #1** on dependency grounds: its
dependency chain is shorter, because the Probability lane still has no live tape
writer.

The evaluation protocol points the other way, and it does so on its own logic
rather than on preference:

| | Solana | Kalshi |
|---|---|---|
| arrival rate | ~395 births/day, ~40% enrollable | **410.7 baseball arrivals/day** |
| **evaluability** | **24h coverage 4.6%** — a 24h-horizon experiment is currently **not evaluable**, and P0 should *refuse to register it* | resolved binary outcomes with 100% scoring coverage on 12,945 forecasts |
| calibration instruments | none deployed for this lane | deployed, plus representativeness instruments that exist and were branch-only during the last failure |
| live registered experiment | none | **yes** |
| capacity | **$50–$150 clips**; even a 10% sustained gross edge is a few hundred dollars a day (§3.5) | fee-bound, but not capacity-bound at any size we would trade |
| what it can answer | **"can we model execution?"** — and it is the *only* lane with free realized-fill ground truth | **"do we have an edge?"** — and it is the only lane where `ΔS` is computable at all |
| what it cannot answer | whether we have an edge — no market-relative benchmark exists for a memecoin | what a fill actually costs — queue position at fill is unobtainable, so maker P&L is a bracket forever |

### 10.2 The recommendation

> **Rank Kalshi first for the edge question, and start the Solana collector's
> approval track immediately anyway.**

The two are not competing for the same resource. Kalshi's Phase-0 rungs are
queries and one additive migration — days of work on data that is not going
anywhere. The Solana collector's cost is **calendar**: its corpus cannot be
backfilled, so the expensive thing is the *waiting*, and the waiting starts when
approval lands, not when code lands.

Three supporting reasons, and one honest counter-argument.

**(a) The evaluation protocol will keep refusing Solana long-horizon
experiments, correctly and unhelpfully.** At 4.6% 24h coverage the honest
denominator is not the enrolled cohort. This is a coverage problem the sparse
lane exists to fix prospectively, and until it does, a 24h Solana claim is not
evaluable at any sample size.

**(b) Solana's capacity ceiling caps the value of winning.** Even at an
extraordinary 10% sustained gross edge, the optimal $50 clip across every
eligible birth yields **$581/day**. That is a real number and it is not a number
that justifies unbounded engineering — and it should be stated before, not after,
the work.

**(c) Kalshi is where `ΔS` is even defined.** Growth equals the log-score
advantage over the market price (§0.1a). A memecoin has no market-implied
probability of anything, so there is no `q` and no `ΔS`. **The lane that can
answer the project's first question is Kalshi, and it is not close.**

**The counter-argument, which is real.** Solana is the lane where the *execution*
side can actually be validated against ground truth, and the CLOB lane's
execution model has a **permanently unfalsifiable parameter** in queue position.
So if the question is "can we ever trust a modeled fill?", Solana answers it and
Kalshi does not. That is exactly why the recommendation is not "deprioritise
Solana" but "**start its approval clock now and let it accumulate while Kalshi
answers the edge question**".

### 10.3 What would change this recommendation

- **P0.0 returns near-zero join coverage AND P0.2 confirms PAIRED = 0.** Then
  Kalshi's `ΔS` measurement needs a non-anchored forecaster running prospectively
  against a recorded quote, which is a longer chain than it looks, and the
  relative ranking narrows.
- **The Solana collector's Tier-3 approval is refused.** Then the Solana lane has
  no ground truth and no path to one, and it becomes a pure lifecycle-prediction
  lane — still worth running, but no longer the execution-validation lane.
- **The sparse lane's prospective 24h coverage rises materially.** That is being
  measured now, and it is the single number that would most improve Solana's
  standing.


## 11. Where the research disagrees with itself

Recorded rather than averaged, because a synthesis that smooths over a
contradiction hides the place where someone is wrong.

**11.1 — Is realized slippage observable on Solana? The tracks directly
contradict each other.**
`SOLANA-ROUTE-OBSERVATION-001` §8.1 row 6 and §11.4 of the AMM track both state
that realized slippage is **permanently unobservable within the boundary**, and
the AMM track explicitly says it *agrees* with the milestone. The ground-truth
track says that is false, and demonstrates why.

**Resolution: the word "realized slippage" is doing two jobs.** *Third-party*
realized execution is free, read-only, and richer than a paid trade feed (which
typically reports price and size but **not** the pre-trade pool state that makes
them meaningful). *Our own* quote-to-fill slippage remains unobservable, because
we place no order. Both tracks are right about their own referent and the
equivocation is in the shared name. This document uses `requires_submission` for
the second and never the first.

**11.2 — The AMM track's tier table is now stale, and it matters.**
Following from 11.1: features C3–C7 (`swap_count_by_direction`,
`net_signed_flow`, trade-size distribution, unique signers, LP add/remove events)
are classified there as tier T3 / `feed_not_available`, on the grounds that they
need a paid per-trade feed. **They do not.** The balance-delta detector derives
direction, size, signer, and counterparty from free `getTransaction` responses.

The claim "Block C is structurally closed" is therefore **false**, and §5.3
reclassifies it. Two honest qualifications: it is rate-limited and scoped to
tracked tokens rather than a firehose, so it is not the unconstrained T3 the
table imagined; and it needs an approval that does not yet exist. But
"structurally closed" and "pending approval and a rate budget" are very different
statuses, and code written under the first assumption would be wrong.

**11.3 — The Kalshi fee schedule: three tracks marked it secondary; it is now
primary-verified, and the correction changes a conclusion's reasoning.**
`QDK-001-clob-microstructure-execution.md` §10.3 S3 and
`QDK-001-risk-and-sizing.md` §11.4 both flag the fee formula as INFERRED from
secondary sources, and the prediction-market track lists confirming it as an open
question. The primary schedule (effective 2026-07-07, verified this session)
confirms the taker coefficient and the 1.75¢ peak — **and shows the maker
multiplier `M` defaults to 0**, i.e. maker fees are typically zero, not 25% of
taker.

**Consequence:** every maker/taker threshold computed from a 0.44¢ maker fee is
wrong in the maker's favour, and the sentence "fees make maker uneconomic" is
false. §3.4 restates the taker-only recommendation on its correct basis — adverse
selection and unobservable queue position — and the round-trip *maker* fee hurdle
of 0.875¢ used in the microstructure-is-not-alpha comparison should be read as
**zero**, which makes the taker comparison (0.70¢ versus 3.50¢) the load-bearing
one.

**11.4 — Kelly's role: the two tracks say different things and both are right.**
The risk track concludes Kelly is a **ceiling, not the allocator**; the
prediction-market track concludes proper betting is the **allocator** and Kelly
is the **scale**. These are not in conflict — they are two axes, and the
candidate space is a grid `(allocator × scale × gate)` rather than a flat list.
§7.1 composes them. Note the corollary the prediction track supplies: for a
binary market, the Brier-rule proper bet and the naive raw-margin rule are the
**same allocator**, differing only by a factor of 2, so two of the brief's
"competing candidates" are one candidate.

**11.5 — Conservative Kelly: the most attractive sizing rule and the least ready
to ship.** The prediction-market track calls it "the most promising candidate in
the whole set"; the same track then shows the only measured dispersion proxy is a
no-op (791/791 folds) and the risk track shows a self-reported posterior width is
**unfalsifiable from outcome data**. Not a contradiction, but the enthusiasm
ordering differs enough to mislead a reader who reads one section. §6.1 resolves
it: `p_conservative` is **typed-absent** unless a measured dispersion exists, and
building the dispersion is prerequisite research, not a detail of the sizing rule.

**11.6 — Backtesting admissibility versus the allocator bake-off.** The
evaluation track rules historical backtesting inadmissible for confirmation
whenever the decision function contains an LLM. The prediction-market track
designs a bake-off across allocators on a shared forecast stream. **These are
compatible if and only if the distinction is made explicit:** with the forecast
stream *frozen*, an allocator's decision function contains no LLM, so a
retrospective allocator comparison over prospectively-generated forecasts is
admissible where LLM-forecast backtesting is not. **But it multiplies looks**, so
every arm enters `search_history` and the multiplicity family. This nuance is
absent from both tracks and needs to be stated before anyone runs the grid.

**11.7 — Sample size: the headline number is for the wrong claim if quoted
loosely.** The risk track's 30,000–75,000 is the **P&L** requirement; the
prediction track says `ΔS` is much cheaper and the risk track separately says
calibration is ~15× cheaper in sample. Quoting the headline as the gate for the
edge measurement would be a category error, and §8's C1 is the correction.

**11.8 — The enrolment ceiling: 41.4% versus 59.8%.** The route-observation
milestone measures 41.4% enrollable over one 25h window (n=411); a larger
7,447-birth sample gives 59.8% NULL, i.e. ~40% enrollable. The evaluation track
already flags this. **Use "roughly 40%" as the durable claim** and 41.4% as one
window's reading, and note the source document flags its own stability as
UNMEASURED.

**11.9 — MEV: the tracks agree on the verdict and disagree on the emphasis.**
The ground-truth track establishes that population-level extraction is observable
as a lower bound; the AMM track establishes that MEV is **not the dominant
adverse-selection channel** — structural liquidity decay is, at −22% of notional
on a $500 round trip at the measured median, with the token's price unchanged.
Both hold. The practical ordering is the AMM track's: *the domain's folklore is
systematically biased toward the two things that are least accessible — sniping
and MEV — and away from the two that are most measurable — decay and
concentration.*

**11.10 — Two claims that are secondary-sourced and were used anyway, flagged
here so they are not inherited as fact.** The public-endpoint transaction-history
retention figure commonly repeated as "3–4 days" is **not adopted as a number**
by the ground-truth track and is not adopted here — retention is an operator
configuration, not a protocol guarantee. And three Solana program IDs (PumpSwap,
Orca Whirlpool, Meteora DLMM) are secondary-sourced; they are **not a blocker**,
precisely because the balance-delta detector does not depend on them — they are
labels applied after detection.


## 12. Open questions and decisions needed before any build

### 12.1 Decisions that need Eric, in priority order

| # | decision | tier | why it blocks |
|---|---|---|---|
| **Q1** | **Lane ranking.** §10 recommends Kalshi first for the edge question with the Solana approval clock started immediately. The memory index currently ranks Solana #1 on dependency grounds. These conflict and the conflict should be resolved explicitly rather than drift. | 2 | sets Phase-0 emphasis |
| **Q2** | **Approve the read-only Solana realized-fill collector's milestone, and the `BANNED_IDENTIFIER_FRAGMENTS` decision it needs.** No code, no call — an approval to *write* the milestone and take the naming decision. | **3 — dual confirm** | it is the only rung where waiting destroys data |
| **Q3** | **Accept that enabling the `canon_digests` comparison will immediately fail a live registered experiment.** The drift is real and 8 days old. It must be a declared amendment with a recorded reason, not a silent re-pin. | 2 | blocks trusting any confirmatory verdict |
| **Q4** | **Amend the frozen Solana notional ladder to V3, dropping N4 $500** (§3.5), *before* any quote is evaluated. | 2 | the ladder is a preregistration; a quiet drop would void it |
| **Q5** | **Should `trade` be in the default Kalshi collector subscription?** Without it there is no signed trade imbalance, no effective or realised spread, no price impact, no markout, no sweep detection. It is already allowlisted and already entitled, so this is configuration, not a boundary change — but it changes archive volume against unmeasured rotation constants. | 2 | most of blocks K-C and K-D are unpopulated without it |
| **Q6** | **Where does `time_to_close` come from?** For an expiring contract it is arguably the most important conditioning variable, and it is not on the websocket. A one-shot read-only REST metadata fetch is inside the capability boundary but outside the collector milestone's stated non-goals. **Do not add a REST loop silently.** | 2 | a whole conditioning dimension |
| **Q7** | **Is `min_liquidity_usd = 5000` intended to exclude 62% of the observed population?** A threshold most of the population fails is selecting a different population, not discriminating within one. | 1 | changes the AMM denominator |

### 12.2 Open questions this document could not close

1. **Is the AMM residual actually size-independent?** The entire rescue of the
   selection-bias problem (§5.5) rests on it, and it is testable only once a
   corpus exists. **If it is not, the corpus calibrates far less than claimed.**
2. **What is the real `size/TVL` support in observed swaps?** If p5–p95 spans 20×
   or more the selection bias is mild; if it spans 4× or less the residual
   reframe is mandatory rather than advisable. One histogram on the first sample.
3. **What fraction of our own tokens trade on venues where the pre-trade state is
   fully recoverable?** §5.3 assumes the alignment between "where memecoins trade
   in their first hours" and "where the constant-product derivation works". It
   should be measured — one `GROUP BY dex_id` — not assumed.
4. **What is the retrospective `q` join coverage?** P0.0. Possibly the single
   highest-value unanswered question in the document.
5. **What is our effective N** — the count of *distinct resolved events*, not
   records? Event clustering has been measured elsewhere at roughly **50× the
   naive Fisher errors**, so this is the real sample size and it is unknown.
6. **What are the empirically correct purge and embargo lengths?** §9.6's values
   are reasoned from cluster structure, not measured from an autocorrelation
   decay. Answerable from data we already have.
7. **What is `L_ρ` on Kalshi at our size?** Every offline result in the
   proper-betting literature assumes it is zero, and the theory says the
   divergence rent and `L_ρ` cancel exactly in the idealized case. It is the whole
   backtest-to-live gap and it is unmeasured.
8. **How do we calibrate a belief *spread*?** It blocks conservative Kelly, and
   inter-trial variance is demonstrably not it.
9. **What is the production Kalshi event rate?** Every sample-size judgement in
   the CLOB engine is conditioned on a number measured exactly once, at 4 records
   in ~2 minutes on DEMO.
10. **Does the public Solana endpoint tolerate sustained ~1 req/s from one IP?**
    The documented limit says yes; "subject to abuse" and "may change without
    prior notice" say measure it.
11. **How is a multiplicity `family_id` scoped in practice?** "Same population,
    one epoch" is a definition, not a procedure. Two experiments over baseball a
    month apart — one family or two? Too narrow reintroduces uncorrected
    multiplicity; too broad makes every experiment unpowered.
12. **Is the ~40% Solana enrolment exclusion stable, and what is its
    composition?** One window, one provider. If it is provider-specific, a second
    provider changes the ceiling and every Solana denominator with it.
13. **Does the Kalshi venue coalesce multiple order events into one delta?** It
    determines whether `order_arrival_intensity` is a count or an undercount.
14. **What is the clock-offset bound?** Currently NOT MEASURED, biasing
    `data_age_us` by an unknown sign.

### 12.3 What this document explicitly does not do

It designs no production code, proposes no order placement, and enables nothing.
It does not produce a forecast. It does not propose a strategy — §3.4's
comparison is the reason: the best-case documented microstructure edge does not
cover the venue's round-trip fee, so the honest output of that track is an
**execution-quality layer** for positions taken on forecast grounds, not a
signal. It authorizes no milestone, opens no gate, and satisfies none of
`ADR-004`'s sequencing conditions, which remain unchanged.

And it should be said out loud rather than discovered: **this protocol makes
research slower and some hypotheses unaskable.** That is the intended trade. A
sufficiently short-lived opportunity cannot be validated this way, and the honest
response to such an opportunity is to decline it rather than lower the bar.


## 13. Evidence ledger

Four tiers, and the tier is stated wherever a number is used.

- **VERIFIED (this session)** — read directly out of this repository or a primary
  source during the writing of this document, with the location given.
- **VERIFIED (research track)** — verified in one of the six QDK-001 tracks
  against a primary source that track fetched and read.
- **SUPPLIED** — measured elsewhere and handed to this document. Not verified by
  its author.
- **SECONDARY / INFERRED** — plausible, commonly asserted, or derived. Not
  confirmed against a primary source. **Do not build on these without checking.**

### 13.1 VERIFIED in this session, against this repository

| claim | location |
|---|---|
| `MarketForecastRecord` has **no** contemporaneous market-price column | `app/models.py:214`, full column list read |
| `forecast_reliability.py` computes `brier_skill_vs_base_rate` and **contains no market baseline** | `app/services/forecast_reliability.py:234`, plus 360, 393, 449, 464 |
| `canon_digests` is **written and never compared** | written at `app/services/experiment_registry.py:500` over `CANON_FILES = ("docs/PROJECT_CANON.md", "docs/SAFETY_BOUNDARIES.md")`; `_evaluation_code_drift` at `:802` reads only `refs.get("evaluation_code_digests")` |
| `_evaluation_code_drift` resolves against `Path.cwd()` and `status()` calls it with no `repo_root` | `experiment_registry.py:802`, `:843` |
| `MarketPriceTickBucket` carries `open_bid`, `close_bid`, `open_ask`, `close_ask`, OHLC mid, `spread_avg`, `domain`, `tick_count`, in 300s buckets keyed `(market_ticker, bucket_start, bucket_seconds)` | `app/models.py:319` |
| `MarketPriceTick` carries `yes_bid`, `yes_ask`, `midpoint`, `spread`, indexed on `(market_ticker, observed_at)` | `app/models.py:295` |
| The forbidden-capability table, and the two amendments' exact wording | `docs/SAFETY_BOUNDARIES.md` |
| `SOLANA-ROUTE-OBSERVATION-001` is PLAN ONLY, ACCEPTED, NOT BUILT, at CP-0 | that document's status block and §9 |
| `KALSHI-LIVE-TAPE-COLLECTOR-001` is DESIGN ONLY; the only `Transport` implementation is `FixtureTransport` | that document's status block and §1 |

### 13.2 VERIFIED in this session, against a primary external source

| claim | note |
|---|---|
| Kalshi taker fee `round up(M × 0.07 × C × P × (1−P))`, peak **1.75¢** at P = 0.50; **maker `M` defaults to 0** | Kalshi primary fee schedule, effective 2026-07-07. **Supersedes** the secondary-sourcing caveats in `QDK-001-clob-microstructure-execution.md` §10.3 S3 and `QDK-001-risk-and-sizing.md` §11.4 |
| AMM vault balances are present in a swap's `preTokenBalances`; pool vault deltas and trader deltas conserve to the base unit; realized price computes directly | two real mainnet swaps on our own cohort-8 token. **SUPPLIED to this document** — the writing agent made no RPC call. Supersedes checks C1/C2 of `QDK-001-solana-ground-truth.md` §13 |

### 13.3 SUPPLIED — measured elsewhere, not verified here

| claim | source of the measurement |
|---|---|
| **PAIRED = 0** — market-anchored `template_baseline` rows pair with zero source-backed forecasts | supplied this session |
| `SAFETY_BOUNDARIES.md` pinned `d6c38783…`, current `c5cb2936…`, drifted 8 days undetected | supplied; the *mechanism* is verified in §13.1 |
| Cohort-8 observation-time liquidity distribution (n=42): p25 $1,936, p50 $2,860, p75 $11,578, p95 $67,119 | `SOLANA-ROUTE-OBSERVATION-001` §4.2, itself supplied to that document |
| Birth-to-horizon liquidity decay 4.75× at the median; IQR ratio 2.16× → 5.98× | same, §14.1 M12 |
| 411 births / 25h, 170 (41.4%) enrollable; 59.8% NULL over 7,447 births | same §14.1 M13; the larger figure from the query-plan document |
| 12,945 forecasts scored, coverage 100%, baseball skill +0.2286 (n=7,983), soccer +0.2434, tennis negative — **all against the base rate** | `docs/OUTCOME_SYNC_POST_DRAIN_BASELINE_2026_08.md` |
| All six EDGE-SELECTION-001 candidates inverted out of sample; `spread_only` control best of eight | `docs/EDGE_SELECTION_RETIREMENT_2026_07_10.md` |
| `cohorts_positive_after_costs: NONE` | COST-MODEL-001 |
| MVP-005A gate crossed on paired n = 36 | `docs/SAFETY_BOUNDARIES.md`, EV-calculation row |
| 410.7 baseball arrivals/day; tennis 1.2/day; soccer 0.0/day | registry 002C feasibility measurement |
| 24h Solana observation coverage 4.6%; median last tick ~83 min after birth | `crypto_sparse_observation` module docstring and CRYPTO-COVERAGE-REPAIR-002 |
| Only measured Kalshi rate: 4 records in ~2 minutes on DEMO | `segment.py`, demo validation |

### 13.4 Derived in a research track — arithmetic, reproducible, not a measurement

`g(f*) = KL(p‖q)`; 2× Kelly is exactly zero growth; the `1/(1−q)²` estimation
penalty; `λ = ½` giving 74.9% of growth at 50% of volatility; full Kelly's median
peak drawdown of 89.4%; one-period Bayesian Kelly equalling Kelly at the
posterior mean; `VaR = CVaR = f` for a single binary; `K_eff ≤ 1/ρ`; per-trade
Sharpe `(p−q)/sqrt(p(1−p))`; `n = 15,451` and its corrections; MDE 20.7pp at
n = 36; `τ = 2·notional/L`; the entry-cost, round-trip, capacity, and
mark-to-market tables of §3.5 and §7.3; the 14.6958× bonding-curve ceiling; the
fee/edge comparisons of §3.4.

**These are properties of models, not measurements of markets.** Every one of
them is exact arithmetic given its inputs, and every one of them inherits the
tier of its inputs.

### 13.5 SECONDARY or INFERRED — do not inherit as fact

| claim | status |
|---|---|
| Public Solana endpoint transaction-history retention ("3–4 days") | **not adopted as a number** by the research track or by this document. Retention is an operator configuration, not a protocol guarantee |
| PumpSwap, Orca Whirlpool, Meteora DLMM program IDs | secondary only. **Not a blocker** — the balance-delta detector does not depend on them |
| Solana RPC custom error codes (`-32001`, `-32004`, `-32007`, `-32009`) | read from a secondary rendering |
| 2,000–5,000 usable Solana swap records/day | **INFERRED**, dominated by an unmeasured swaps-per-signature yield |
| Kalshi `trade` and `market_lifecycle_v2` payload shapes | INFERRED from a third-party mirror; **UNVERIFIED on our own wire** — the demo capture was 4 records and contained no trade print |
| Kalshi adverse selection ≈ 0.5–1 tick per side | **INFERRED** from a volatility-scaled port of a Binance measurement. A measurement task, not a result |
| `DEFF = 2.2` (m = 5, ρ = 0.3) and `k = 20` hypotheses | illustrative design values. **Every sample size in §9.4 scales linearly with the first**, which is why P0.4 measures it |
| ~95% abstention rate | inferred from the cost wedge exceeding most edges. Measurable before any trade — P0.6 |
| `α ∈ [0.10, 0.25]`, `β̂ ∈ [0.85, 1.15]`, n ≥ 500 per regime cell | judgment, anchored to a simulated CI table |
| Purge/embargo lengths of 24h / 7d / 48h | reasoned from cluster structure, **not measured** from an autocorrelation decay |
| Solana priority-score formula; prio-graph look-ahead depth | **UNVERIFIED** — the authoritative write-up was unreachable. And the prio-graph scheduler is now the deprecated path |
| pump.fun "graduates at ~$69,000 market cap" | **FAILED VERIFICATION.** The invariant is 85.00536 SOL, token-denominated and exact |
| pump.fun trading fee of 1% | **STALE.** 1% is the legacy fallback used only when the fee-config account is absent; the current bonding-curve total is **1.25%** |
| "Solana transaction ordering is deterministic FIFO" | **FALSE.** Priority-ordered greedy scheduling under account-lock-aware deferral |
| "No public mempool, therefore no sandwiching on Solana" | **FALSE, and measured to be false** — 521,903 instances costing >$7.7M over four months, via private orderflow into bundles |

### 13.6 The standing rule this document inherits

Two of the three arXiv citations handed to the prediction-market track were
materially mis-paraphrased in ways that would have propagated into design: an
update equation that was the cited paper's **rejected** branch, and a full set of
calibration figures from **v1** of a paper whose **v2** changed all of them.

> **A citation is not usable until someone has read the equation.** That is the
> standing rule, not a one-off.

The same applies inside this document: every number above traces to a tier, and a
number whose tier is SECONDARY or INFERRED may inform a design and may not
support a claim.

