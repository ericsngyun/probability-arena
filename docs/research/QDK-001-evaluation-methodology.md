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

Eight phases. The ordering is the protocol — a phase may not be skipped, reordered, or
run concurrently with a later one. Phases 0–1 use historical data and can only kill
(§4). Phases 2–6 are the confirmatory path. Phases 7–8 are downstream gates that this
document specifies but does not open.

```
  P0 FEASIBILITY ──► P1 PRE-SCREEN ──► P2 REGISTER ──► P3 COLLECT ──► P4 MATURE
   (historical,        (historical,      (commit,        (dark,         (wait for
    power only)         kill only)        no data seen)   no looks)      labels)
                                                                            │
       ┌────────────────────────────────────────────────────────────────────┘
       ▼
  P5 EVALUATE ──► P6 REPLICATE ──► P7 MODELED PAPER P&L ──► P8 CAPITAL GATE
   (evaluator      (independent      (SAFETY_BOUNDARIES      (§13; not
    computes,       second window)    PAPER_SIMULATION)       opened here)
    one verdict)
```

### 5.1 P0 — Feasibility and power (historical, non-inferential)

**Purpose:** determine whether the experiment *can* conclude before anyone commits to
running it. This phase exists because our registry already proved its value: 002C
measured arrival rates and blocked two of three drafts before they wasted a window —
baseball 410.7 arrivals/day (≈1.2 days to a 500 floor, FEASIBLE), tennis 1.2/day
(≈164 days against a 180-day window, MARGINAL), soccer 0.0/day since 2026-07-23
(NOT FEASIBLE).

**Outputs, all required before P2:**

| Output | Definition |
|---|---|
| `arrival_rate` | Registered-universe members per calendar day, measured over ≥14 days |
| `attrition_curve` | Fraction of enrolled members reaching each declared horizon with a real observation |
| `expected_scoreable_n` | `arrival_rate × window_days × P(reaches terminal horizon)` |
| `time_to_floor` | Days until `expected_scoreable_n` ≥ `sample_floor` |
| `minimum_detectable_effect` | The smallest effect the floor can resolve at the corrected alpha (§11) |

**Binding rule.** If `time_to_floor` exceeds the declared window, registration is
**refused**, not attempted-and-abandoned. The `minimum_detectable_effect` must be
declared in the registration record and must be *smaller than the hypothesized effect* —
an experiment powered only to detect an effect larger than the one you predict is a
coin flip wearing a lab coat.

The attrition curve is not optional decoration. Our crypto lane's real numbers are
brutal: 24h coverage of **4.6%**, with a median last tick ~83 minutes after birth
(`docs/milestones/CRYPTO-COVERAGE-REPAIR-002.md`). Any 24h-horizon crypto experiment
whose power analysis assumes it observes what it enrolls is off by more than an order of
magnitude.

### 5.2 P1 — Pre-screen (historical, kill-only)

Run the hypothesis against history under the **most favourable** admissible assumptions:
full contamination allowed, frictionless, no attrition. Then apply the Asymmetry Rule.

- **Fails the pre-screen** → the hypothesis is retired. Record the refutation. No
  prospective cost is incurred. This is the cheap kill and it should catch most ideas.
- **Passes the pre-screen** → **nothing has been established.** The result is recorded as
  `pre_screen_survived` and carries *zero* evidential weight into P5. It may not be cited,
  aggregated, or reported as support.

The pre-screen must additionally run the **cost-first check** that F5 says we skipped: if
the hypothesis is already negative after the registered cost model at frictionless-best
assumptions, it dies here. Every one of the six EDGE candidates would have died at this
step, weeks earlier and for free.

Time-consistent splitting (§6) applies to the pre-screen. A pre-screen that leaks is
*more* favourable to the hypothesis, which under the Asymmetry Rule makes a failure even
more damning — but a leaky pre-screen that *passes* tells you nothing you did not already
know, so the splitting discipline is still worth its cost.

### 5.3 P2 — Registration (the commitment)

The registration record (§9) is completed and cryptographically committed **before any
prospective data exists**. The commitment covers hypothesis, typed population predicates,
universe, horizons, primary metric, cost model, sizing rule, sample floor, stopping rule,
multiplicity family, and evaluation date.

`start_time` is assigned by the registry, never by the author — this is already enforced
(`experiment_registry.py` rejects author-supplied `start_time`) and it is the single most
important anti-gaming property in the current implementation. Registration also pins the
digests of the evaluation code, the metric code, the predicate schema, and the field
registry, so that a later change to how a metric is computed is *detectable* rather than
silent.

**Only members whose decision timestamp is strictly after `start_time` are eligible.**
This is enforced by the `gte_registration_time` predicate operator, not by a filter an
analyst remembers to apply.

### 5.4 P3 — Collection (dark, and genuinely no looks)

Data accumulates. **The interim-look budget is zero by default.**

If interim looks are required (for an operational reason, e.g. confirming the collector
is alive), they are declared *at registration* with a count and a purpose, and they may
return only **liveness** fields: arrival count, coverage fraction, error states. They may
**not** return the primary metric, any secondary metric, any per-arm breakdown, or any
quantity from which those are recoverable. An implementation that cannot enforce that
separation must return nothing.

This is where F9's lesson bites hardest. The failure was not that someone peeked; it was
that the decision moment was chosen by a human who had seen the data. Making looks
metric-blind removes the ability to choose the moment.

### 5.5 P4 — Maturation

The experiment transitions to `matured` when the declared stopping condition is met —
evaluated **by the evaluator against the clock and the data**, not asserted by the author.

A live defect must be fixed before this protocol is trusted: today the evaluator permits
evaluation from `collecting` and even `registered`, recording it merely as a deviation
(`experiment_results.py:831`), which contradicts both the 001 design ("Evaluation is
refused before the `matured` state") and `status()`'s own
`evaluation_permitted = state in (MATURED, EVALUATED)`. **The protocol requires the strict
form: evaluation from a non-matured state is refused, not annotated.**

**Metric admissibility (from F6).** A primary metric must be a bounded, unit-declared
quantity with a defined aggregation. An unbounded ratio of a signed quantity to a
selected-small denominator — the `closure` metric — is **not admissible as a primary
metric** in any form. It may be reported as descriptive.

### 5.6 P5 — Evaluation (one run, one verdict, evaluator-computed)

The evaluator recomputes everything from the committed record. The author supplies only
`experiment_id`, `confirm`, and operator notes — no population, count, metric, end time,
or verdict. The existing implementation already has this signature lock and it should be
preserved exactly.

Evaluation order, with verdict precedence running top to bottom (integrity beats
arithmetic, always):

1. **Chain integrity** — event chain and result chain verified; broken chain → refuse.
2. **Code and canon drift** — pinned digests recompared. Unknown drift is material.
   *(Live gap: `canon_digests` are pinned but never compared, and `SAFETY_BOUNDARIES.md`
   has already drifted undetected. §10 requires this closed.)*
3. **Population reconstruction** — membership rebuilt from forecast-time fields only,
   independently recomputed, and refused on disagreement.
4. **Composition / representativeness** (F8) — evaluated sample composition vs registered
   universe composition. Material drift blocks a favourable verdict.
5. **Attrition accounting** (F7) — disposition buckets must sum to the enrolled count.
6. **Stopping-rule satisfaction** (F9) — window bounds checked against the real clock;
   evaluation outside the declared window is a deviation that downgrades **in both
   directions**.
7. **Sample floors** (F3) — total and per-arm, evaluator-computed. Below floor →
   `inconclusive_sample_floor`, and `supports_hypothesis` is unreachable.
8. **Cost application** (F5) — net computed before gross is displayed (§7).
9. **The number, with its interval** (F2, F6) — cluster bootstrap, multiplicity-corrected
   (§11).
10. **Control check** (F4) — each anomaly condition evaluated and **reported separately**,
    combined as a conjunction. The control must be mechanism-independent and specified
    without reference to any in-sample ranking.

The **first terminal verdict is pinned** and never moves; re-evaluation is permitted only
with an explicit recorded reason and is stamped `superseded_by_protocol`. This closes the
peek-until-favourable route and is already implemented.

### 5.7 P6 — Replication

**A single passing experiment is not a finding.** F4 established why: the entire
eight-cohort ranking on one window was noise, and the prereg's own trigger required
"successive" windows. A confirmatory claim requires a **second, independent, separately
registered window** with the same committed record, a non-overlapping population, and its
own evaluation.

The replication carries no multiplicity discount of its own — it is one pre-declared test
of one hypothesis — but its registration must cite the first experiment's digest, so the
family is traceable (§11).

### 5.8 P7 — Modeled paper P&L

Only after P6. Governed entirely by `docs/SAFETY_BOUNDARIES.md`: modeled fills only, every
artifact carrying its model identifier and modeled-vs-observed basis, no real fills,
orders, positions, or capital, and no dollar EV. Note that this phase is *also* gated by
`ADR-004` sequencing, which this document does not amend.

### 5.9 P8 — Capital gate

See §13. Not opened by this document.

---

## §6. Time-consistent validation: purge, embargo, walk-forward

This section governs the **P1 pre-screen** and **cost-model fitting**. Per §4 it is not a
route to confirmation, and no arrangement of splits converts a backtest into confirmatory
evidence. It exists because a leaky pre-screen wastes the one cheap kill we get.

### 6.1 Why naive k-fold is invalid here — four reasons, not one

The usual statement is "time series are ordered". That is the weakest of the four:

1. **Label overlap.** A label formed over `[t, t+H]` shares its formation window with
   every neighbouring label within `H`. Random folds put a training label and a test label
   that were computed from *the same future price path* on opposite sides of the split.
   The test set is then partly a function of the training labels.
2. **Serial correlation of features.** Adjacent snapshots are near-duplicates. Random
   splitting scatters near-duplicates across folds, so the test fold measures memorization,
   not generalization.
3. **Entity clustering.** This is the one our data punishes hardest and the one k-fold
   ignores entirely. Dozens of Kalshi markets belong to **one game**; many observations
   belong to **one token**. The independent unit is the *event*, not the row. Our discovery
   window was "all `sports_baseball`" with persistence "mostly 1 snapshot (203 of 217)" —
   effective sample size far below nominal.
4. **Regime non-stationarity.** Prevalence itself moved 0.3743 → 0.4258 across one drain,
   and the baseline follow-through "swung positive with no system change". k-fold averages
   across regimes and reports the average as if it were stable.

### 6.2 Purge and embargo, with lengths for our horizons

**Purge** removes from the training set every observation whose *label formation window*
overlaps the test window — it addresses reason 1. Purge length = the full label horizon.

**Embargo** removes an additional buffer *after* the test window before training resumes —
it addresses reasons 2 and 4, and the general fact that information propagates for longer
than the label takes to form.

**Both are applied at the cluster level, not the row level** (reason 3). Purging a row
while its sibling markets on the same game remain in training accomplishes nothing.

| Lane | Label horizon `H` | Purge | Embargo | Cluster unit | Total gap |
|---|---|---|---|---|---|
| Kalshi intraday follow-through | 60 min | 60 min | **24 h** | **game / event** | ~25 h |
| Kalshi market resolution (calibration) | to settlement (hours–days) | full declared max horizon | **7 d** | **event** | horizon + 7 d |
| Solana survival, short bands | 15 min / 1 h | = band | **6 h** | **token** | ≤ 7 h |
| Solana survival, long bands | 6 h / 24 h | = band | **48 h** | **token + launch-hour cohort** | ≤ 72 h |

**Rationale for the embargoes, which are deliberately longer than the textbook default:**

- *Kalshi 24h.* The natural clustering unit is a slate. Markets on a given day share
  weather, lineups, injury news, and one liquidity regime. An embargo shorter than a full
  daily cycle leaves same-slate contamination intact. The 60-minute label horizon is a red
  herring for embargo purposes.
- *Kalshi calibration 7d.* Resolution-horizon labels can settle days later, and the
  research packet feeding a forecast draws on a news window that is itself days wide.
- *Solana 48h on long bands.* Launch cohorts are correlated: tokens born in the same hours
  share a market regime, and frequently a deployer. The cluster unit therefore includes the
  **launch-hour cohort**, not just the token. 48h is two full 24h label spans, which is the
  minimum that guarantees no test-window token's label window touches a training token's.

**Where a length is uncertain, the protocol requires the longer value.** Under the
Asymmetry Rule an over-long embargo costs sample (making a kill harder to achieve, i.e.
conservative in the direction of *not* killing) while a too-short one manufactures false
survival. The asymmetry of the error justifies the asymmetry of the default.

### 6.3 Walk-forward

Use **rolling-origin** (fixed-width training window) rather than anchored/expanding for
anything microstructural, because reason 4 dominates: an expanding window increasingly
averages over dead regimes. Use **anchored/expanding** for calibration measurement, where
the quantity of interest is a long-run property and more data genuinely helps.

Each fold reports its own n, its own composition, and its own interval. **A walk-forward
result is reported as the vector of fold results plus the worst fold — never as the mean
across folds alone.** A mean across folds is exactly how "one fleeting 48h window printed
`blocked: False`" became a candidate: the aggregate hid that the result lived in one fold.

### 6.4 The combinatorial-purging caution

Combinatorially recombining purged folds raises the number of effective trials, and every
one of those trials is a look at the data. If it is used, the trial count enters the
declared family size for §11. This is F1 in a new costume: a method that multiplies looks
is a search, and a search must be declared.

---

## §7. Transaction-cost realism: gross, net, and the headline rule

### 7.1 The rule

> **Net is the only headline number.** Gross is reported adjacent to it, always, and never
> alone. A result stated gross-only, or a summary that carries gross without net, is out
> of protocol and is not produced.

This is a direct structural fix for F5, and it is reinforced by both citations: the field
audit found **1/19** studies disclosing any cost model, and PolyBench's celebrated
"2 of 7 profitable" is a **no-fee, five-levels-deep, small-size** figure whose own authors
report that profitability "violently contracts" with size. Our own history is more
pointed: after fees, `cohorts_positive_after_costs: NONE`.

The requirement that costs be *charged from a model of the actual book or route* rather
than a flat fee is not pedantry. A flat fee is a constant; it shifts every result equally
and preserves the ranking. A depth-aware cost is a *function of the position*, and it
reorders results — small-edge/deep-book opportunities survive it while
large-edge/thin-book ones do not. Since our crypto lane's median observed pool is **$2,860**
and the pre-registered notional ladder's third rung is $150 (~5% of that pool), the cost
term is not a rounding correction. It is the dominant term.

### 7.2 The cost model is a registered object

`cost_model` is a required field of the registration record (§9), committed with the
hypothesis and pinned by digest. It declares, per venue, every cost term and the
**basis** of each — `OBSERVED`, `MODELED`, or `BOUNDED` — inheriting the typed-absence
doctrine already established for route quotes: "`0`, `0.0`, `""`, `"unknown"`, `-1`, and
'the previous pass's value' are all **forbidden** stand-ins. Zero is an affirmative claim;
absence is not a claim."

**Kalshi** (the machinery for this already exists in `app/services/edge_cost.py`):

| Term | Basis | Note |
|---|---|---|
| Half-spread | OBSERVED | from the snapshot spread, tick fallback |
| Taker fee | MODELED | round trip at both entry and horizon; no maker rebates, no rounding down |
| Executable touch | OBSERVED | ask→bid or bid→ask by side; rows without usable touch quotes are **uncovered, never guessed** |
| Queue position, partial fill, market impact | **absent** | must be declared as a BOUNDED omission, not silently zero |

**Solana** (from the route-quote design; note that most of this is *not yet built*):

| Term | Basis | Note |
|---|---|---|
| Price impact at notional | OBSERVED | from the quote response, never recomputed from our own mid |
| Route/pool fees | OBSERVED where the venue reports it | "No default fee, no assumed bps, no per-dex fee table" |
| Priority fee, base fee | **BOUNDED — non-closable** | fetching a priority fee is *forbidden* by the safety boundary |
| Token-2022 transfer fee / hooks | **BOUNDED — non-observable** | "If present and unaccounted, every quote is wrong by an unknown multiplicative factor" |
| Realized slippage, landing probability, MEV | **BOUNDED — non-observable** | require submitting a transaction |

### 7.3 Non-closable cost terms get a declared adverse bound, never a zero

Several Solana terms cannot be measured within our boundary and never will be. The
protocol's answer is neither to ignore them nor to abandon the lane:

> Every non-observable cost term carries a **declared adverse bound**, registered before
> data is seen. Net is computed **twice**: at the observed/modeled terms alone
> (`net_partial`) and with every bounded term charged at its adverse bound
> (`net_conservative`). **`net_conservative` is the headline.**

This converts an unknown into a stated worst case, which is a claim that can be
falsified later, rather than into a silent zero, which cannot.

### 7.4 The cost-kill multiple

Because a meaningful part of the cost stack is bounded rather than measured, a single net
number under-states the fragility. Every result therefore reports one robustness scalar:

> **κ (cost-kill multiple)** = the multiple of the total modeled cost at which the net
> result crosses zero.

Interpretation, and a hard gate:

- **κ < 1** — already dead. The result is negative at the modeled cost.
- **1 ≤ κ < 2** — **not robust; may not support a confirmatory claim.** Our cost stack has
  non-closable terms and a 25h-window-old liquidity estimate; a result that dies if costs
  are twice the model has not survived our own measurement error.
- **κ ≥ 2** — reportable, with κ stated on the artifact.

κ is cheap to compute, hard to game (the evaluator computes it), and it is exactly the
number that would have made the EDGE candidates' fragility legible before the cost model
was written: at frictionless +0.10..+0.30 going to −0.03..−0.21 under one fee assumption,
their κ was well below 1.

### 7.5 Provenance

Every net number is a modeled fill and therefore carries, on the artifact itself, its
model identifier and its modeled-vs-observed basis, per `docs/SAFETY_BOUNDARIES.md`. §0
states why this protocol treats that as a measurement requirement: **a net number whose
cost basis is untraceable is not a weaker result, it is not a result.** And per the same
boundary, size is a stated *input*, never an output — nothing in this protocol may derive,
optimize, rank, or recommend a size from a modeled result.

---

## §8. Universe construction and survivorship

Severity ordering: this is the most dangerous section for the Solana lane and the section
where our own worst measurement error (F8) actually happened.

### 8.1 The decision-time rule

> The universe is fixed at a **single declared decision timestamp** per member, using
> **only information observable at or before that timestamp**, and membership is decided
> by a **closed typed predicate** over an allowlisted set of fields each carrying
> `available_at`. No prose rule, no analyst filter, no post-hoc exclusion.

This is already the right shape in-repo and should be preserved rather than reinvented:
13 allowlisted fields, every one carrying `available_at="forecast_creation"`,
`immutable_after_forecast=True`, and an evaluator that reconstructs membership
independently and refuses on disagreement. Anything absent from the field registry is
**refused**, so the guard fails closed against fields nobody thought to blacklist.

For Solana the decision timestamp is **enrollment, anchored on `first_evidence_at`, with
no fallback** — deliberately not `observed_at`, which is the tape run time rather than a
birth time. The available information is birth-event fields only. Critically, the sparse
lane applies **no liquidity, volume, risk, venue, or boost filter** within the eligible
population, precisely to keep the denominator honest, and its cap is applied by
deterministic id order and "**never** by any property of the token".

### 8.2 The denominator is stated against 41.4%, on the face of every result

**VERIFIED:** of 411 births in a 25h window, 411 had `first_evidence_at` but only **170
(41.4%)** had `initial_liquidity_usd > 0`. The binding constraint is that **58.6% of
births never acquire an `initial_liquidity_usd` at all** — which makes them
enrollment-*ineligible*, not observation-failures: "those births are not missed, not
retried, and not recoverable by observing harder."

Two rules follow:

1. **Every rate is reported against 41.4% of births, never 100%, and the ceiling appears
   on the face of the report rather than in a footnote.** A "90% coverage" claim over a
   denominator that has already discarded 58.6% of births is a 37% claim.
2. **The 58.6% exclusion is itself a selection event and must be characterized, not just
   disclosed.** Tokens that never acquire a liquidity reading are unlikely to be a random
   sample of births — they plausibly skew toward the shortest-lived and least liquid. This
   biases the enrolled population toward *survivors* before enrollment even begins. Its
   stability is currently **UNMEASURED beyond one 25h window and one provider**, and any
   confirmatory Solana claim must treat that as a declared limitation.

### 8.3 Dead members stay in the denominator — the disposition ledger

> Once enrolled, a member **never leaves the denominator**. It moves to a typed
> disposition, and the dispositions must **sum exactly to the enrolled count**. The
> evaluator computes the sum and refuses the result if it does not balance.

This is the structural fix for F7 — the silent drop of markets that stopped quoting — and
it is the single mechanical check that makes survivorship bias visible instead of
invisible.

Disposition buckets (extending the existing survival vocabulary):

| Disposition | Meaning |
|---|---|
| `observed_at_horizon` | A real in-band observation exists. The only bucket that produces a label. |
| `died_liquidity_removed` | Liquidity fell below `0.3 ×` initial (`SURVIVAL_LIQUIDITY_FRACTION`) |
| `died_volume` | 24h volume below `$500` at ≥6h after birth (`DEAD_VOLUME_24H_USD`) |
| `graduated_or_migrated` | Moved off the launchpad DEX set |
| `never_observed_evidence_gap` | We failed to observe. **Our failure, not the token's outcome.** |
| `horizon_unreached_censored` | Still alive, horizon not yet due — right-censored |

**The critical distinction, already correct in our code and worth protecting: NULL is not
death.** "NULL = not yet measurable or gap, never a guess." Collapsing
`never_observed_evidence_gap` into `died_*` would let an observation failure masquerade as
a finding — and given our measured 24h coverage of **4.6%**, that collapse would
manufacture a ~95% "death rate" out of a monitoring gap.

**Consequence that must be stated plainly:** at 4.6% coverage at 24h, a 24h-horizon
Solana experiment is currently **not evaluable**, and P0 (§5.1) should refuse to register
it. The honest 24h denominator is not the enrolled cohort; it is the 54 of 1,182 finalized
outcomes carrying a real 24h observation. This protocol's job here is to make that refusal
automatic rather than to let an under-powered experiment run and produce a number.

### 8.4 Representativeness is a computed, blocking check

The structural fix for F8. Every result carries a composition statement comparing the
**evaluated sample** against the **registered universe** across every declared
stratification field, and the evaluator **blocks a favourable verdict on material drift**.

Three specific tests, each drawn from a real failure:

- **Prevalence drift.** Report `ō` for the evaluated sample and for the universe. Brier's
  irreducible term is `ō(1−ō)`, so a prevalence shift moves the headline for a reason that
  has nothing to do with skill — this is exactly the 0.1800 → 0.1908 move. **Any Brier-like
  metric must be reported alongside its base-rate baseline, and skill-vs-baseline is the
  comparable quantity, never raw Brier.**
- **Composition/domain share.** Our scored sample is ~85.3% baseball, so "overall Brier" is
  approximately "baseball Brier", and daily cohorts run `top_domain_share` 0.99–1.00 —
  "baseball-only comparisons wearing a date label". Per-stratum n and share are mandatory.
- **Degenerate strata.** Soccer's Brier of 0.003297 on n=34 with 97% "no" outcomes "was
  never a measurement". A stratum whose prevalence is within 5% of 0 or 1, or below its
  declared floor, is reported as `inconclusive` and **may not contribute to a headline**.

The instruments for this already exist in-repo (`SCORED_SAMPLE_IS_NOT_REPRESENTATIVE`,
`RELIABILITY_SAMPLE_NOT_REPRESENTATIVE`, `COMPOSITION_SHIFT_DOMINATES`). They were
branch-only and undeployed during the EDGE episode. **Deploying them is a precondition of
this protocol, not an enhancement to it.**

### 8.5 No cross-population comparison

"**The new aggregate must not be compared with the old one directly.**" A metric computed
over a different population is a different quantity, not a better estimate of the same
one. Longitudinal claims must re-baseline, because prevalence is non-stationary
(0.3743 → 0.4258 over one drain). The evaluator enforces this by refusing to compare
results whose `population_digest` differs.

## §9. The preregistration record schema

### 9.1 The governing principle — and the general fix for paraphrase bypass

Our registry's original leakage guard was a prose blocklist, and the review defeated it
with a synonym: `"include forecasts in the cohort that beat the benchmark"` was
**accepted**. Worse, our own tennis manifest shipped a phrasing semantically identical to
one the tests assert is rejected. The correct diagnosis was recorded at the time and is
worth restating because it generalizes: "A blocklist rejects *spellings*; membership needs
a *decision procedure*."

That fix was applied to membership. It was not applied as a *rule*, and the same mistake
recurred one layer down — 002B's `universe.selection_method` prose scan accepted
`"hand picked after looking at results"`, and had to be re-fixed as a typed enum. A defect
that recurs after being fixed is a missing invariant, not a missing patch. So:

> **The Operative-Field Invariant.** Every field the evaluator reads is **typed and
> closed** — an enum, a number, a timestamp, or a predicate over an allowlisted field
> registry. Every prose field is **non-operative and provably unread**, enforced by an AST
> test asserting no evaluator branch depends on it.
>
> **A field that is required but unread is forbidden.** It is a recorded promise, and
> recorded promises are how "the registry is a good filing cabinet with a strong lock and
> no inspector" happens.

The precedent exists in-repo: operator notes are bounded, secret-scanned, and covered by
an AST test asserting no branch reads them. That pattern becomes the rule for all prose.

The third clause is not cosmetic. Our current manifest has **eight required fields that no
evaluator code reads**: `domain_sample_floors`, `evaluation_horizons`,
`missing_data_policy`, `canceled_void_policy`, `conflict_policy`, `stale_score_policy`,
`invalidating_conditions`, and `multiple_testing_policy`. Each must become typed-and-read
or be demoted to explicit non-operative rationale. There is no third option, because the
middle state is precisely what lets an author believe a promise is binding when it is not.

### 9.2 The record

`QDK-PREREG-1`. Fields marked **[R]** are read by the evaluator and must be typed;
**[N]** are non-operative rationale; **[A]** are registry-assigned and author-supplied
values are rejected.

#### Identity and classification

| Field | Type | Notes |
|---|---|---|
| `experiment_id` **[R]** | `^[a-z0-9][a-z0-9\-]{2,63}$` | |
| `experiment_version` **[R]** | int ≥ 1 | Must be *created and validated*; today it is required but no code enforces it |
| `mode` **[R]** | enum `exploratory` \| `confirmatory` | Only `confirmatory` can support a claim |
| `experiment_class` **[R]** | closed enum | Existing seven classes; execution-cost research needs a new class, never a widened one |
| `decision_function_contains_llm` **[R]** | bool | **New and load-bearing.** If true, §4 forbids historical confirmation and the evaluator refuses a confirmatory verdict sourced from resolved-historical data |

#### Hypothesis

| Field | Type | Notes |
|---|---|---|
| `hypothesis` **[N]** | prose | Human-readable statement |
| `null_hypothesis` **[N]** | prose | |
| `predicted_effect` **[R]** | `{direction: enum(gt\|lt), magnitude: number, unit: string}` | **Typed, not prose.** Must exceed `minimum_detectable_effect` |
| `mechanism` **[N]** | prose | Required by the "mechanism-first, not search-first" doctrine from our own retirement doc |

#### Population and universe

| Field | Type | Notes |
|---|---|---|
| `population` **[R]** | typed predicate doc | Existing 002A schema: `{schema_version, all, none, window_end, universe, rationale}`; ≤32 predicates; fields allowlisted; `window_end` required |
| `universe` **[R]** | `{selection_method: closed enum, artifact_digest: sha256, created_at: ts}` | `selection_method` ∈ `exhaustive_series` \| `exhaustive_event` \| `scheduled_fixtures` \| `random_sample_seeded`. Artifact must be **resolved**, and **created before registration** — a check that is currently unreachable from the register path (§10) |
| `decision_timestamp_field` **[R]** | enum of allowlisted timestamp fields | The single moment membership is fixed (§8.1) |
| `denominator_basis` **[R]** | `{ceiling_fraction: number, ceiling_rationale_digest: sha256}` | Forces the 41.4%-not-100% statement onto the artifact |
| `stratification_fields` **[R]** | list of allowlisted fields | Drives the §8.4 representativeness check |

#### Horizon, metric, and cost

| Field | Type | Notes |
|---|---|---|
| `evaluation_horizons` **[R]** | list of durations | Must become read — today it is an unread promise |
| `primary_metric` **[R]** | `{name: closed enum, unit: string, aggregation: closed enum, bounded: true}` | Exactly one. Unbounded ratios inadmissible (§5.5, F6) |
| `declared_baselines` **[R]** | closed enum list | Skill-vs-baseline is the comparable quantity, never raw Brier (§8.4) |
| `secondary_metrics` **[R]** | closed enum list | Computed into a **sealed** section the verdict function provably cannot read (§11.5) |
| `cost_model` **[R]** | `{model_id: str, version: str, terms: [{name, venue, basis: enum(OBSERVED\|MODELED\|BOUNDED), adverse_bound: number?}]}` | Required. Registration is **refused** without it (F5) |
| `kappa_floor` **[R]** | number ≥ 2 | The cost-kill multiple gate (§7.4) |
| `sizing_rule` **[R]** | `{notional_ladder: [ints], units: str, basis: "declared_input"}` | Size is an **input**, never an output |

#### Power, floors, and stopping

| Field | Type | Notes |
|---|---|---|
| `sample_floor` **[R]** | int > 0 | Total scoreable members |
| `per_arm_sample_floor` **[R]** | int > 0 | **New.** F3: per-arm n was never even recorded |
| `domain_sample_floors` **[R]** | map field→int | Must become read |
| `minimum_matured_fraction` **[R]** | 0..1 | Required for confirmatory |
| `minimum_detectable_effect` **[R]** | number | From P0. Must be < `predicted_effect.magnitude` |
| `stopping_rule` **[R]** | `{kind: closed enum, minimum_sample, minimum_matured_fraction, not_before, not_after}` | Only the typed form; the prose duplicate is removed |
| `interim_look_budget` **[R]** | `{count: int (default 0), fields: closed enum list}` | Metric-blind by construction (§5.4) |
| `evaluation_date` **[R]** | timestamp | Bounded by `not_before`/`not_after` |

#### Multiplicity and control

| Field | Type | Notes |
|---|---|---|
| `family_id` **[R]** | str | All confirmatory tests over one population in one epoch |
| `family_size_declared` **[R]** | int ≥ 1 | Registered confirmatory hypotheses in the family |
| `search_history` **[R]** | `{variants_evaluated: int, overlapping_data: bool, prior_milestone_ids: [str]}` | **The direct fix for F1.** Transitive count of variants evaluated on any overlapping data |
| `correction_method` **[R]** | closed enum `holm` \| `benjamini_hochberg` | Typed, replacing today's free-text `multiple_testing_policy` |
| `alpha` **[R]** | number | Family-wise, default 0.05 |
| `control_spec` **[R]** | `{kind: enum(mechanism_independent_null\|permutation\|synthetic), derived_from_in_sample_ranking: false}` | **F4.** The literal `false` is a validated constant; a control derived from an in-sample ranking is rejected at registration |
| `anomaly_conditions` **[R]** | list of typed conditions | Combined as a **conjunction**, each reported separately (F4) |

#### Registry-assigned

`registered_at` **[A]**, `start_time` **[A]**, `registration_commit` **[A]**,
`manifest_digest` **[A]**, `state` **[A]**, `immutable_references` **[A]**.

### 9.3 The commitment

Preserve what exists — it is the strongest part of the current implementation:

- `manifest_digest` = SHA-256 over canonical JSON of the manifest **minus** registry-assigned
  fields, so the author's declaration is verifiable before confirmation.
- Pinned at registration: population predicate digest, predicate schema and field-registry
  versions, source digests of the predicate/population/metric/evaluation modules, baseline
  and CI policy versions, sub-object digests, repository commit, and canon document digests.
- Append-only hash chain (`prev`, `seq`) with `head.json` pinning event count and terminal
  hash; a parallel result chain that re-opens every result file and recomputes its digest;
  the **first** terminal verdict pinned and immovable.

Three specific hardenings this protocol requires:

1. **External timestamping.** `registered_at` is currently `datetime.now()` — self-asserted.
   The only external corroboration is the git commit. For a record whose entire value is
   "this predates the data", add an **external timestamp anchor** (RFC-3161 token or
   equivalent) at registration. Without it, the commitment's strength rests on the honesty
   of the party it is meant to bind.
2. **Dirty-tree refusal.** There is no working-tree check, so the pinned commit need not
   describe the code that ran. Refuse registration from a dirty tree.
3. **Registration clock skew.** Evaluation guards against clock skew (±300s); registration
   accepts a caller-supplied `now` with no such guard. Apply the same bound.

Honest framing, already correct in-repo and worth keeping verbatim: "This is not
tamper-PROOF — an attacker with write access can rewrite both files. It is tamper-EVIDENT,
and the evidence lands in a git diff."

---

## §10. Evaluator-enforced floors

The 001 design named the gap precisely: "nothing connects the registry to evaluation…
`sample_floor`, `stopping_rule` and `primary_metric` are recorded promises that no code
checks… the registry is a good filing cabinet with a strong lock and no inspector."
002B built the inspector. This section specifies what the inspector must additionally
enforce before the protocol is trustworthy.

### 10.1 The enforcement principle

> **The evaluator computes; the author confirms.** The author supplies only
> `experiment_id`, `confirm`, and non-operative notes. Every quantity that could change a
> verdict — population, membership, n, metric, cost, interval, window satisfaction — is
> recomputed by the evaluator from the committed record. **A value an author can supply is
> a value an author can choose.**

The existing signature lock already implements this and must be preserved exactly.

### 10.2 Already correct — preserve

Independent membership recomputation with refusal on disagreement; verdict precedence
running integrity → drift → data quality → stopping → floor → number; deterministic
cluster bootstrap by market with a fixed seed (an evaluator must not be able to reroll);
degenerate-prevalence invalidation; first-terminal-verdict pinning; protocol deviations
downgrading a favourable verdict; amendments restricted to non-semantic reasons via a
hash-chained record.

### 10.3 Live defects that must be closed first

These are not hypothetical; they were found in the current implementation and each is a
route to an unearned verdict.

| # | Defect | Why it matters |
|---|---|---|
| D1 | Evaluation permitted from `collecting` and `registered`, recorded only as a deviation (`experiment_results.py:831`) | Contradicts the 001 design and `status()`'s own `evaluation_permitted`. **Nothing forces maturation before a terminal verdict is pinned** — this is the peek-and-lock route reopened |
| D2 | `canon_digests` pinned but **never compared** | Verified live: baseball pinned `SAFETY_BOUNDARIES.md` at `d6c38783…`; it now hashes to `c5cb2936…`. **Real, present, undetected drift** in the document that defines the safety boundary |
| D3 | `check_identifier_cohort` called without `universe_base`/`registered_at`, so the "universe created before registration" check is skipped at its only call site, and paths resolve against `cwd` | 002C's central universe guarantee is unreachable from the register path |
| D4 | `_evaluation_code_drift` uses `repo_root or Path.cwd()` | `status()` raises `FileNotFoundError` from another directory instead of reporting drift |
| D5 | Dead branch `if False and primary not in SUPPORTED_PRIMARY_METRICS:` | A disabled check left in place; harmless today, load-bearing the moment registration validation changes |
| D6 | `record-result` text renderer raises `KeyError` on a renamed field; text is the default format and no test covers it | The default invocation of the enforcement command tracebacks |
| D7 | Eight required manifest fields never read (§9.1) | Recorded promises |
| D8 | `multiple_testing_policy` is free text, validated only as "non-empty and not 'none'" | No correction of any kind exists in `app/` (§11) |
| D9 | Exceeding `MAX_EVENTS`/`MAX_RESULTS` makes `verify_event_chain` report "not intact" | Permanently bricks an experiment rather than refusing the append |
| D10 | `primary_metric.name` is never cross-checked against `primary_metric.definition` or the hypothesis | The registered baseball experiment declares `name: "mean_brier"` with a *skill* formula as its definition and a hypothesis stated in skill terms — three different quantities in one immutable record |

D10 deserves emphasis because it is already latent in a live registered experiment: the
evaluator will compute one quantity while the hypothesis asserts another, and the manifest
is immutable. **A registration whose metric name, metric definition, and hypothesis are
not mutually consistent must be rejected at registration**, because it cannot be fixed
afterwards.

### 10.4 New enforcement this protocol adds

1. **Per-arm floors.** Compute and report n per arm; refuse a supporting verdict if any
   arm is below `per_arm_sample_floor`. (F3)
2. **Disposition balance.** Sum the disposition ledger; refuse if it does not equal the
   enrolled count. (F7, §8.3)
3. **Representativeness block.** Compute composition drift against the registered universe
   across `stratification_fields`; material drift blocks a favourable verdict; degenerate
   strata are reported `inconclusive` and excluded from the headline. (F8, §8.4)
4. **Cost enforcement.** Compute `net_conservative` before displaying gross; refuse any
   artifact carrying gross without net; compute κ and refuse a confirmatory claim at
   κ < `kappa_floor`. (F5, §7)
5. **Window satisfaction, symmetric.** Determine stopping satisfaction from the clock and
   the data; stamp any out-of-window evaluation as a deviation **in both directions**. (F9)
6. **Control conjunction.** Evaluate each `anomaly_condition` separately, report each
   boolean, combine as AND. (F4)
7. **Multiplicity.** Apply `correction_method` over `family_size_declared + search_history.variants_evaluated`
   and report the adjusted interval as the headline. (F1, F2, §11)
8. **LLM-historical refusal.** If `decision_function_contains_llm` is true, refuse a
   confirmatory verdict whose evidence derives from resolved-historical data. (§4)
9. **Canon and code drift comparison.** Close D2 — compare what is pinned.

---

## §11. Multiple testing

### 11.1 The current state

There is **no multiple-testing correction of any kind in `app/`** — no Bonferroni, Holm,
Benjamini–Hochberg, alpha-spending, or family-wise term. `multiple_testing_policy` is a
free-text field validated only for non-emptiness. All three drafted manifests set it to
prose like `"one primary metric; ECE is descriptive only and cannot be promoted"`, and
**nothing enforces that non-promotion**.

Credit where due: the registry's *structural* multiplicity controls are real and
unusually good — exactly one primary metric (a list is rejected, because "multiple
primaries are how a null result becomes a positive one"), one decision rule, and a pinned
first terminal verdict. Those close the within-experiment looks. What is missing is
**across-experiment** and **across-search** multiplicity, which is precisely what killed
the EDGE lane: ~39+ variants over one window, six candidates each given an independent
shot at a fixed bar, no alpha anywhere.

### 11.2 Defining the family

> A **family** is the set of confirmatory tests evaluated against the same
> data-generating population within one declared epoch, **plus every variant evaluated on
> overlapping data during the search that produced them**.

The second clause is the whole point. A family defined only over registered experiments
counts six candidates and misses the 18-policy search that generated them, and the search
is where the selection bias lives. Hence:

```
m  =  family_size_declared  +  search_history.variants_evaluated
```

Both are registered fields (§9.2), and the evaluator reads `m` from the record rather than
from a human's recollection.

### 11.3 The correction regime

**Confirmatory claims → family-wise error rate, controlled by Holm–Bonferroni.**

- Rationale: a confirmatory claim is a gate toward capital. The cost of a false positive is
  strictly and severely asymmetric against the cost of a false negative — a false negative
  costs us an idea, a false positive costs us money and, worse, credibility in our own
  measurements. FWER is the right target.
- Holm rather than plain Bonferroni: uniformly more powerful, requires no independence
  assumption (our tests are positively correlated, sharing a population), and is trivial
  to implement deterministically.
- Default `alpha = 0.05` family-wise.

**Exploratory screening → false discovery rate, Benjamini–Hochberg at q = 0.10.**

- Used **only** to rank P1 pre-screen candidates for prospective registration. An
  FDR-surviving screen result is a *reason to register*, never a finding. Under the
  Asymmetry Rule it cannot be a finding anyway.

### 11.4 Reporting

Every confirmatory result reports, on the artifact:

| Field | Content |
|---|---|
| `m` | The family size actually used, and its two components |
| `alpha_raw`, `alpha_adjusted` | Nominal and Holm-adjusted |
| `interval_raw`, `interval_adjusted` | Cluster-bootstrap intervals |
| `headline` | **The adjusted interval.** The raw interval never appears without it |
| `family_members` | The digests of every registered experiment in the family |

The decision rule operates on `interval_adjusted`. This is a change in kind from the EDGE
gates, which compared a point estimate to a fixed threshold with no interval at all.

### 11.5 Looks, and the sealed secondary section

**Looks.** The interim-look budget defaults to zero and looks are metric-blind (§5.4). If
a design genuinely requires interim analysis, the number of looks and an alpha-spending
function (O'Brien–Fleming) are declared at registration, and the evaluator refuses a
verdict at a look index beyond the declared budget. **An undeclared look is a protocol
deviation that invalidates the experiment**, not one that downgrades it — because unlike
the deviations in §10.4(5), an undeclared look is unbounded in how much it can inflate the
result.

**The sealed secondary section.** Secondary metrics are computed and recorded, but written
into a section that the verdict function **provably cannot read**, enforced by an AST test
of exactly the kind already used for operator notes. This converts "ECE is descriptive
only and cannot be promoted" from a prose promise into a structural property. Without it,
a null primary and an interesting secondary is the most natural rationalization available,
and it is the one our registry's own founding document predicted we would reach for:
"…or if ECE were the primary metric instead of Brier skill."

### 11.6 Replication as the dominant control

The strongest multiplicity control here is not arithmetic — it is §5.7. Requiring an
independent, separately registered replication before a claim binds means a false positive
must survive two pre-registered tests on non-overlapping populations. At α = 0.05
family-wise per stage, the effective rate for a spurious claim reaching P7 is on the order
of α², and the replication requirement is robust to exactly the failure the correction is
not: **an undeclared prior search**. A search can inflate one window; it cannot easily
inflate two disjoint prospective windows in the same direction.

This is why §14 rates undeclared search as the protocol's most serious residual
vulnerability, and why replication is non-negotiable rather than a nice-to-have.

## §12. Structural fix table: failure mode to mechanism

*(to be filled)*

## §13. What evidence would justify real capital

*(to be filled)*

## §14. Known limitations of this protocol

*(to be filled)*

## §15. Open questions

*(to be filled)*
