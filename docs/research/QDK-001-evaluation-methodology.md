# QDK-001 — Evaluation Methodology for the Decision Kernel

**Status: RESEARCH ONLY — DESIGN, NOT BUILT.**
No production code, no schema, no migration, no feature flag, no provider call, and no
trading surface is created by this document. It specifies how a claim produced by the
decision kernel would be *evaluated*. It authorizes no milestone and opens no gate.

**Track:** QDK-001 (evaluation methodology)
**Branch:** `QDK-001-evaluation-methodology`
**Date:** 2026-08-14

---

## Table of contents

- [§0. Scope, and what this document is not](#0-scope-and-what-this-document-is-not)
- [§1. Why this track exists](#1-why-this-track-exists)
- [§2. Citation verification](#2-citation-verification)
- [§3. Our own failure record — nine named failure modes](#3-our-own-failure-record--nine-named-failure-modes)
- [§4. Position: is historical backtesting admissible at all?](#4-position-is-historical-backtesting-admissible-at-all)
- [§5. The prospective evaluation protocol, end to end](#5-the-prospective-evaluation-protocol-end-to-end)
- [§6. Time-consistent validation: purge, embargo, walk-forward](#6-time-consistent-validation-purge-embargo-walk-forward)
- [§7. Transaction-cost realism: gross, net, and the headline rule](#7-transaction-cost-realism-gross-net-and-the-headline-rule)
- [§8. Universe construction and survivorship](#8-universe-construction-and-survivorship)
- [§9. The preregistration record schema](#9-the-preregistration-record-schema)
- [§10. Evaluator-enforced floors](#10-evaluator-enforced-floors)
- [§11. Multiple testing](#11-multiple-testing)
- [§12. Structural fix table: failure mode to mechanism](#12-structural-fix-table-failure-mode-to-mechanism)
- [§13. What evidence would justify real capital](#13-what-evidence-would-justify-real-capital)
- [§14. Known limitations of this protocol](#14-known-limitations-of-this-protocol)
- [§15. Open questions](#15-open-questions)

---

## §0. Scope, and what this document is not

This document specifies the **evaluation protocol** — the procedure that decides whether
a claim produced by the decision kernel is real. It is deliberately written before the
kernel produces claims, because a protocol written after the first result is not a
protocol, it is a rationalization.

**In scope:** admissibility of historical evidence; prospective evaluation design;
time-consistent validation; cost realism; universe and survivorship handling; a binding
preregistration record; evaluator-enforced floors; multiple-testing control; the
evidence bar for real capital.

**Explicitly out of scope, and unchanged by this document:**

- Every rule in `docs/SAFETY_BOUNDARIES.md` continues to bind. Read-only external
  interaction, no wallet key material, no real fills/orders/positions/capital, no paid
  RPC or trade feeds, and **dollar EV remains forbidden with no unlocking milestone**.
- `docs/ADR/ADR-004-calibration-before-ev.md` sequencing continues to bind: no EV design
  work until a challenger beats the baseline on resolved outcomes over a meaningful
  sample, and no paper trading until the EV design is explicitly accepted. **This
  document does not amend that ordering and does not satisfy any of its gates.**
- `docs/ADR/ADR-001-read-only-first.md`: "Capability expansion is milestone-gated."
  §13 below states what evidence *would* justify opening a gate. It does not open one.
- No implementation surface is created — no functions, fields, tables, endpoints, or CLI
  commands, "including 'disabled' or 'placeholder' versions"
  (`docs/SAFETY_BOUNDARIES.md:226`).

One boundary rule deserves to be restated because §7 depends entirely on it. Under the
`PAPER_SIMULATION` capability mode, every artifact carrying a modeled fill or modeled
P&L must carry, **on the artifact itself**, (1) a named versioned **model identifier**
and (2) an explicit **modeled-vs-observed basis** naming which inputs were observed
evidence and which were assumptions. "An artifact missing either field is out of
boundary… An aggregate, export, or summary inherits both fields, or is not produced"
(`docs/SAFETY_BOUNDARIES.md:132-155`). The evaluation protocol below treats this as a
*measurement* requirement, not merely a safety one: a net-of-cost number whose cost
basis is untraceable is not a weaker result, it is not a result.

---

## §1. Why this track exists

Two independent bodies of evidence say that evaluation, not modeling, is the binding
constraint on this kind of work.

**The field is not doing this well.** A May 2026 audit of 77 agentic-trading studies
found that only 19 even close the loop from action to evaluated outcome, and within
that subset the methodological hygiene is close to absent: 2/19 report a time-consistent
split, 1/19 an explicit transaction-cost model, 1/19 any universe or survivorship
handling, and none reach the top reproducibility tier (§2). PolyBench, evaluating seven
frontier models against real prediction-market order books, found only two profitable —
and the losses were attributed to overconfidence and calibration failure "despite strong
surface-level fluency" (§2). The base rate for "LLM expresses high confidence about a
market and is wrong" is high.

**We are not immune, and we have the receipts.** Our own pre-registered edge-selection
exercise failed out of sample in the most complete way available: all six candidates
inverted, the mean gap closure ran −1.22 to −1.74 against a baseline of −0.06, and the
negative control — the cohort pre-registered as the thing expected to be *worst* —
posted the best number of all eight. Our own retirement document concluded: "**The
policy search overfit** … The apparent frictionless shadow edge was selection noise
amplified by an 18-policy search, and it was additionally uneconomic at realistic
friction" (`docs/EDGE_SELECTION_RETIREMENT_2026_07_10.md` §5).

That episode is the single most valuable asset this track has, and §3 mines it for nine
specific, named, structurally-fixable failure modes. The governing insight is already
written down in `docs/PROSPECTIVE_EXPERIMENT_REGISTRY_001.md` §1 and cannot be improved
on:

> The threat this addresses is not an adversary. It is us, six weeks from now, looking
> at a null result and noticing that the finding would be significant if the window
> started a week later, or if tennis were excluded, or if ECE were the primary metric
> instead of Brier skill. Every one of those edits is individually defensible and
> collectively fatal.

---

## §2. Citation verification

Both citations supplied to this track **check out**. Details below, with the nuances
that matter for how much weight each can carry.

### 2.1 The agentic-trading audit — VERIFIED

**Xia, You, Wang, Liu, Qi, Wu, Zhang — "Agentic Trading: When LLM Agents Meet Financial
Markets", arXiv:2605.19337, submitted 2026-05-19.**
<https://arxiv.org/abs/2605.19337>

Every figure in the brief is confirmed against the abstract, verbatim:

| Claim as briefed | Paper says | Verdict |
|---|---|---|
| audit of 77 studies | "an audit-oriented evidence map of 77 included studies" | **VERIFIED** |
| 19 satisfy closed-loop evaluation | "A primary empirical subset (n=19) satisfies the minimum boundary of Action Output plus Closed-Loop Evaluation" | **VERIFIED** |
| 2/19 time-consistent splits | "only 2/19 studies report extractable time-consistent split protocols" | **VERIFIED** |
| 1/19 explicit transaction-cost model | "1/19 reports an explicit transaction-cost model" | **VERIFIED** |
| 1/19 universe/survivorship | "1/19 documents universe or survivorship handling" | **VERIFIED** |
| none at top reproducibility tier | "no study reaches R3 reproducibility" | **VERIFIED** |

Two additional figures not in the brief, both worth carrying: **11/19 report execution
timing or semantics**, and **15/19 are coded R0** — the *lowest* reproducibility tier.
The snapshot was screened through 2026-03-09, and the authors name the field's
bottleneck as "protocol incomparability".

**Weight this can carry.** This is a survey of *reporting*, not of *truth*. "1/19
reports an explicit transaction-cost model" means one paper disclosed one; it does not
establish that the other 18 charged nothing, only that a reader cannot tell. That is
still damning for our purposes — an evaluation you cannot audit is one you cannot
inherit — but the correct inference is "the field's published protocols are
uninterpretable", not "the field's strategies are all fake".

### 2.2 PolyBench — VERIFIED, with a material caveat the headline hides

**Cheng, Liu, Long — "PolyBench: Benchmarking LLM Forecasting and Trading Capabilities
on Live Prediction Market Data", arXiv:2604.14199, submitted 2026-04-03.**
<https://arxiv.org/abs/2604.14199>

| Claim as briefed | Paper says | Verdict |
|---|---|---|
| news synchronized with CLOB state | "synchronously coupling each snapshot with a Central Limit Order Book (CLOB) state and a real-time news stream" | **VERIFIED** |
| 38,666 markets | "38,666 binary prediction markets spanning 4,997 events" | **VERIFIED** |
| 2 of 7 frontier models profitable | "only two of seven models achieve positive financial returns — MiMo-V2-Flash at 17.6% CWR and Gemini-3-Flash at 6.2% CWR — while the remaining five incur losses" | **VERIFIED** |
| high expressed confidence despite losses | losses attributed to "overconfidence and calibration failures, despite strong surface-level fluency" | **VERIFIED** |

**The caveat — this is a finding, not a footnote.** PolyBench's execution simulation
walks the book from the best ask against the top **five** levels of depth, and the paper
contains **no exchange fee or commission term**. It also reports that "model
profitability violently contracts" at larger size as "high-volume trades exhaust the top
levels of the captured order book". So the celebrated "2 of 7 profitable" is a
**gross-of-fees, small-size, five-levels-deep** number. On Polymarket that is a
survivable omission; under our own Kalshi cost model it would not be — COST-MODEL-001
found that a round-trip fee assumption alone flipped *every* frictionless-positive
cohort negative (§3, F5). **INFERRED (high confidence): the true count of net-profitable
models in PolyBench at realistic size and with fees is ≤2, plausibly fewer.** This is
the single best external argument for the net-only headline rule in §7.

**The second PolyBench finding matters more than the first, and the brief did not
mention it.** The paper's defense against training-data contamination is not a filter,
a cutoff, or a decontamination pass. It is that the markets *had not resolved yet*:

> "Decentralized prediction markets are structurally immune to this problem, because
> they forecast events that have **not yet occurred** at evaluation time."

The evaluation ran on live markets collected 2026-02-06 to 02-12 with resolutions
extending to 02-21. The strongest benchmark in this space, asked how it avoids
contamination, answers **"we evaluated prospectively."** That is not a stylistic
preference. It is an admission that the resolved-historical route was not defensible,
and §4 builds on it.

### 2.3 Our own measured figures — VERIFIED in-repo

| Figure | Source | Verdict |
|---|---|---|
| 41.4% of births enrollable; 58.6% never acquire `initial_liquidity_usd` | `docs/milestones/SOLANA-ROUTE-OBSERVATION-001.md:507-513` | **VERIFIED** (n=411 births, one 25h window, single provider) |
| Candidates inverted to −1.22 … −1.74 closure; control best at +0.33 | `docs/EDGE_SELECTION_RETIREMENT_2026_07_10.md:20-29` | **VERIFIED** |
| Brier worsened 0.1800 → 0.1908 on the honest sample | `docs/OUTCOME_SYNC_POST_DRAIN_BASELINE_2026_08.md` | **VERIFIED** |

One correction to the brief's framing, because precision here is load-bearing. The
41.4%/58.6% pair is an **enrollment-eligibility** fact measured over a single 25-hour
window from a single provider, and the source document flags its own stability as
**UNMEASURED**. A separate and larger sample gives a consistent but distinct figure:
`initial_liquidity_usd` is NULL on 4,453 of 7,447 birth events, **59.8%**
(`docs/CRYPTO_QUERY_PLAN_AND_DENOMINATOR_RECOVERY_001.md:727`). Treat "roughly 40%
enrollable" as the durable claim and 41.4% as one window's reading.

---

## §3. Our own failure record — nine named failure modes

Everything below is drawn from our own documents. Each mode gets an identifier, the
evidence, and the structural fix that §5–§11 must deliver. "Structural" means *the
protocol makes the mistake impossible or automatically visible* — not that a future
operator is instructed to be careful. Instructions to be careful are what failed.

### F1 — Selection performed on the evaluation data

The prereg itself admits it: "EDGE-FILTER-001 searched **18 shadow policies** and
reported the best performers on the same windows used to find them — a selection
procedure whose winners are upward-biased by construction"
(`docs/EDGE_SELECTION_PREREG_2026_07_09.md` §1). The remedy applied was purely temporal
— freeze the policies, count only post-lock windows — with **no holdout, no re-selection,
and no discounting of the discovery estimates**.

Worse, the effective search was far wider than 18. EDGE-POLICY-001 had already sliced the
same watchlist rows 13 ways and TRIGGER-TIMING-001 replayed 8 timing policies over *the
same persisted ticks*. Roughly **39+ variants were evaluated on one ~260-row,
baseball-dominated window** before anything was frozen. One of the 18 (`exclude_worst_series`)
was itself data-derived in-sample — a search inside a search.

**Fix:** §9 requires the registration record to declare `search_history` — the total
count of variants evaluated on any overlapping data, transitively — and §11 makes the
multiplicity correction a function of that declared count, computed by the evaluator.
An undeclared prior search is the one lie this protocol cannot detect; §14 says so.

### F2 — Uncorrected multiplicity, and no notion of significance at all

Six candidates were each given an independent shot at the same fixed bar (60m
moved-toward rate ≥ 0.55, positive mean closure). There is **no alpha, no confidence
interval, no significance test, and no correction of any kind** anywhere in the EDGE
lane. A grep for Bonferroni/FDR/multiplicity/p-value across `app/`, `docs/`, `tests/`
returns hits only in the *later* registry work.

**Fix:** §11. Confirmatory claims require an interval, and the interval is adjusted for
the declared family size by the evaluator.

### F3 — Sample floors declared, then ignored

The prereg's own gate 1 is "final-horizon **n ≥ 75** hard, preferred ≥ 150". The primary
candidate `require_gap_follows_move_totals_only` was locked in on **n=26**;
`gap_follows_move_and_high_liquidity` on **n=8**. No candidate cohort except the two
broadest came near the hard gate at lock. The floor was a sentence in a document that
nothing checked.

Compounding it: the validation window reports **n=297 for the baseline only**. Per-candidate
n on the decisive window is **not recorded anywhere**. Since candidates are strict subsets,
several were almost certainly retired on n well below their own readability bar.

**Fix:** §10. The evaluator computes n itself, refuses to emit a supporting verdict below
the floor, and reports per-arm n as a mandatory field. A result that does not carry its
own denominator is not a result.

### F4 — A contaminated negative control, wired as a disjunction

The negative control was `spread_only` — restricted to spread markets. But spread
markets had *already been identified in-sample as the worst-performing type*. The
"negative control" was therefore a data-derived worst cohort, not an independent null.
Its out-of-sample inversion to best-in-class (+0.33 closure, 0.375 toward) is precisely
the regression to the mean you would predict, and it is direct evidence that the
**entire ranking, winners and losers alike, was noise**.

The control-anomaly clause was supposed to catch exactly this: the prereg says if
`spread_only` turns non-adverse then "**all candidate results in that window are
suspect**". But the code implemented adversity as `(toward < 0.50) or (closure < 0)`
(`app/services/edge_selection.py:201-216`), so `toward = 0.375` alone kept the status at
`control_consistent` even though closure was positive *and best of all eight cohorts*.
The circuit breaker was wired as an OR and did not trip.

**Fix:** §5.6 requires the control to be **mechanism-independent and specified without
reference to any in-sample ranking**, and §10 requires anomaly clauses to be
conjunctions evaluated per-condition, with each condition's truth value reported
separately. A guard that reports one boolean cannot be audited.

### F5 — Costs bolted on after the hypothesis was locked

The prereg's seven success gates contain **no cost, spread, fee, or executable-price
term**. Every metric was midpoint-to-midpoint and frictionless, as
`app/services/edge_cost.py:3-4` states plainly. Costs arrived later, via a separate
milestone, and the result was decisive: "**every positive-frictionless cohort is
`cost_killed`** — all four follows-move cohorts (frictionless +0.10..+0.30) go NEGATIVE
after the fee assumption (−0.03..−0.21) … `cohorts_positive_after_costs: NONE`".

The sequencing is the lesson. **The candidates were already uneconomic at the moment
they were locked, and nobody had checked.** Weeks of out-of-sample waiting were spent
validating hypotheses that a fee calculator would have killed on day one.

**Fix:** §7 and §9. The cost model is a *registered field*, committed with the
hypothesis, and the evaluator computes net first. A registration whose cost model is
absent is rejected at registration, not discovered at evaluation.

### F6 — A ratio metric reported as a bare mean, with no dispersion

The headline metric was `closure = (later_mid − measured_mid) / signed_gap`
(`app/services/edge_followthrough.py:31`). Three defects compound:

1. It is stored under the key `mean_gap_closure_pct`. **It is not a percentage.** It is a
   dimensionless multiple of the measured gap. Anyone reading "closure −1.22" as −1.22
   percentage points is off by roughly an order of magnitude; against a mean |gap| ≈ 0.111
   it is ≈13 probability points of adverse movement.
2. Because it is a *ratio*, cohorts selected for small gaps get mechanically amplified
   magnitudes, and a single row with a tiny denominator can dominate a 30-row mean. The
   candidate cohorts were, by construction, small-gap selections.
3. **No SD, no IQR, no CI, no trimming, no winsorizing is reported anywhere** — only means.

**Fix:** §9 requires `primary_metric` to declare its unit and its aggregation, and §11
requires an interval on any confirmatory claim. §5.5 forbids an unbounded ratio as a
primary metric outright.

### F7 — Silent survivorship inside the horizon

The follow-through sample takes the last tick in `(t, t+60m]`; if there is none, the row
is dropped from the final count. So **markets that closed, settled, or simply stopped
quoting inside the hour silently leave the denominator** — and, worse, a market whose
last tick was at t+7m contributes a *seven-minute* closure pooled with genuine
sixty-minute ones. No document discusses this.

**Fix:** §8. Attrition is a reported quantity, not a filter. A member that leaves the
denominator leaves it into a typed disposition bucket, and the buckets must sum to the
enrolled count.

### F8 — A selected sample presented as a population

Forecast quality was measured for months on 903 forecasts that were the oldest ids of the
alphabetically-first ~100 tickers — an artifact of two ordering defects, not a sample.
Correcting it moved the measurement to 12,889 forecasts and made the headline **worse**:
mean Brier 0.180043 → **0.190836**.

The explanation is exact and is the most instructive single paragraph in our repo: the
old subpopulation had prevalence 0.3743, so Brier's irreducible uncertainty term
`ō(1−ō)` was lower (0.2342 vs 0.2445). "**The Brier got worse and that is the honest
number.** The old sample was easier… The pipeline did not get worse; the measurement got
honest." Skill against the appropriate baseline moved only 0.2312 → 0.2195 while
calibration error fell 43% (ECE) and 73% (MCE).

Two prior "findings" were destroyed by the same correction. Soccer's celebrated Brier of
0.003297 (n=34) became 0.157593 (n=1,442): "The baseline sampled 34 soccer forecasts
whose outcomes were 97% 'no' … **That was never a measurement.**"

**Fix:** §8.4. Every result carries a representativeness statement — the composition of
the evaluated sample against the composition of the registered universe — and the
evaluator refuses a favourable verdict when composition drift is material. Note that the
mechanism for this already exists in-repo (`RELIABILITY_SAMPLE_NOT_REPRESENTATIVE`,
`COMPOSITION_SHIFT_DOMINATES`, `SCORED_SAMPLE_IS_NOT_REPRESENTATIVE`) and was
branch-only and undeployed during the EDGE episode.

### F9 — Unlogged protocol deviation at the decision point

The prereg declared five validating windows and named the rolling-7d window
(from ~2026-07-16T19:00Z) "**the primary decision window**". It also declared: "**All
windows count**: every post-lock window run is part of the record, including failures."
The retirement trigger required failure "on **successive** validation windows".

What happened: the decision was taken on a measurement at **2026-07-11 07:15 UTC** — five
days before the declared primary window, on a ~36.25-hour cut that was **not one of the
five declared windows** (it falls after the "next 24h" window and ~12 hours *before* the
"next 48h" window even opens), from **one** window where the rule said successive.

The direction matters and cuts both ways. Every deviation made the protocol *stricter*,
the cost model had independently killed the same cohorts, and the substantive conclusion
is almost certainly correct. But this is the mirror image of p-hacking — optional
stopping on a negative result — and **it is not labelled as a protocol deviation
anywhere**. `docs/ROADMAP.md` records it as "the locked protocol did its job", which is
true of the finding and overstated about the process.

**Fix:** §10. The evaluator, not the author, determines whether the stopping condition is
satisfied, and any evaluation run outside the declared window is stamped as a deviation
that downgrades the verdict — **in both directions**. A protocol that only polices
favourable deviations teaches you to reach negative conclusions sloppily, and the habit
does not stay in the negative direction.

---

## §4. Position: is historical backtesting admissible at all?

**Position: for any hypothesis whose decision function contains an LLM, historical
backtesting over resolved markets is INADMISSIBLE as confirmatory evidence. It remains
admissible for falsification, and for three narrow non-inferential engineering uses.
Confirmation requires prospective evaluation. There is no methodological patch that
rescues the confirmatory case.**

I hold this position strongly, and it is not the fashionable "backtests are hard, be
careful" hedge. The argument is that the confirmatory case fails for **three independent
reasons, each individually sufficient**, and that the failure is **directionally biased**
in a way that has a clean asymmetric consequence.

### 4.1 Three independent killers

**(a) Outcome contamination is unfalsifiable and unbounded.** To evaluate an LLM on a
resolved historical event you must assume the model does not know how it turned out. For
a frontier model, that assumption is not testable by us: we cannot audit the training
corpus, the cutoff is a marketing claim rather than a verifiable boundary, and
retrieval-augmented configurations make "cutoff" meaningless anyway. Every proposed
mitigation — entity masking, date scrubbing, paraphrase — reduces to *hoping* the model
cannot re-identify an event from its structure, and a model good enough to forecast an
event from its structure is good enough to recognize it. The mitigation and the
capability are the same capability.

**(b) Context leakage through the retrieval path.** Even with a clean model, the news and
market context assembled for a historical decision point is assembled *now*, from sources
that have been edited, re-dated, back-linked, and in some cases written after resolution.
PolyBench synchronizes news to the snapshot timestamp precisely because this is hard —
and it is far harder retrospectively than prospectively. This is a familiar shape here:
our registry moved membership from prose to a closed typed schema over 13 forecast-time
fields, each carrying `available_at="forecast_creation"`, exactly because human-audited
"we only used information available then" claims kept failing.

**(c) No counterfactual book.** Our own route-observation design states the general form
of this without hedging: "**This milestone therefore has no ground truth to validate
against and cannot acquire one within its boundary.** That is a permanent limitation of
the result, not a gap more work will close." A historical book snapshot tells you what
the book *was*, not what it would have been had we traded into it. On Solana, realized
slippage is "NOT OBSERVABLE prospectively", landing probability and MEV extraction are
"NOT OBSERVABLE WITHOUT SUBMITTING A TRANSACTION", and priority fees are not merely
unmeasured but *forbidden to fetch*. A backtested fill is a model scored against another
model.

### 4.2 The asymmetry, and the rule that follows

All three biases point the same way. Contamination **inflates** measured skill. Leaked
context **inflates** it. A frictionless or top-of-book fill assumption **inflates** it.
There is no plausible mechanism by which contamination makes a real edge look fake.

That asymmetry gives a principled rule rather than a blanket ban:

> **The Asymmetry Rule.** A historical backtest may produce a *negative* result that
> binds, and may never produce a *positive* result that binds.
>
> If a strategy fails a backtest that is biased in its favour, the failure is strong
> evidence. If it passes, the pass is uninformative, because the bias alone is
> sufficient to explain the pass.

This is the rule I recommend adopting, and it is genuinely useful rather than merely
restrictive. It makes backtesting a **cheap, fast killing field** — the correct place to
send a hypothesis first, precisely because it is rigged in the hypothesis's favour.
Anything that dies there dies for free, before consuming weeks of prospective
observation. Our own history is the demonstration: the useful output of the entire
EDGE-SELECTION lane was a *refutation*, and the cost model killed the same cohorts even
faster and even more cheaply.

### 4.3 What historical data remains admissible for

Three non-inferential uses, none of which produce a claim about edge:

1. **Falsification / pre-screening** (the Asymmetry Rule). A registered hypothesis that
   fails a favourably-biased backtest is retired without prospective cost.
2. **Cost-model and infrastructure calibration.** Fitting a fill or fee model against
   historical book evidence is a claim about *the market's microstructure*, not about our
   skill, and the LLM is not in that loop. Contamination is irrelevant to it.
3. **Power and feasibility analysis.** Arrival rates, attrition curves, and the time
   required to reach a sample floor are properties of the data-generating process. We
   already do this — 002C measured baseball at 410.7 arrivals/day (feasible), tennis at
   1.2/day (marginal), soccer at 0.0/day (not feasible) — and it is the single
   highest-leverage use of history we have, because it prevents registering an experiment
   that cannot conclude.

### 4.4 The two objections worth answering

**"This makes research impossibly slow."** It makes *confirmation* slow and leaves
*refutation* fast, which is the correct allocation. And the practical cost is smaller
than it looks: prospective evaluation was already the only path here. Our forecasting lane
generates hundreds of scoreable baseball arrivals per day. The binding constraint on
conclusion time is the sample floor and the arrival rate, not the admissibility rule.

**"PolyBench backtests against historical CLOB snapshots, so historical evaluation must
be fine."** No — and this is the crux. PolyBench's markets were **unresolved at the moment
the models were queried**. The order books are historical; the *outcomes were in the
future*. That is prospective evaluation with a replayed execution environment, which is a
legitimate and different thing, and it is exactly the design §5 adopts. The benchmark
that looks most like a backtest is, on inspection, the strongest evidence for this
section's position.

### 4.5 Consequence for the kernel

The decision kernel's confirmatory evidence comes from **prospective evaluation only**.
Historical data enters the protocol at exactly two points: as a pre-screen that can only
kill (§5.2), and as an input to the cost model and the power analysis (§7, §5.1). No
other path exists, and §9's registration record makes the path a typed field rather than
a convention.

## §5. The prospective evaluation protocol, end to end

*(to be filled)*

## §6. Time-consistent validation: purge, embargo, walk-forward

*(to be filled)*

## §7. Transaction-cost realism: gross, net, and the headline rule

*(to be filled)*

## §8. Universe construction and survivorship

*(to be filled)*

## §9. The preregistration record schema

*(to be filled)*

## §10. Evaluator-enforced floors

*(to be filled)*

## §11. Multiple testing

*(to be filled)*

## §12. Structural fix table: failure mode to mechanism

*(to be filled)*

## §13. What evidence would justify real capital

*(to be filled)*

## §14. Known limitations of this protocol

*(to be filled)*

## §15. Open questions

*(to be filled)*
