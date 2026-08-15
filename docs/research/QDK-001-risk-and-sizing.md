# QDK-001 — Risk and position-sizing layer (DESIGN-AHEAD RESEARCH ONLY)

> **STATUS: RESEARCH DOCUMENT. NOT A BUILD AUTHORIZATION. NOT A DESIGN THAT MAY BE
> IMPLEMENTED.**
>
> This document designs a layer that **does not exist and is not authorized to be
> built**. Per `docs/SAFETY_BOUNDARIES.md`, as of 2026-08-14:
>
> - **Portfolio sizing** — ❌ no implementation surface. Gate: "post-paper-trading
>   milestone with explicit human acceptance."
> - **EV calculation (dollar EV)** — ❌ "remains forbidden with no unlocking
>   milestone defined."
> - **Trade recommendations** — ❌ no implementation surface.
> - **Order placement / live trading / autonomous trading** — ❌ no implementation
>   surface.
> - **Real capital, real orders, real positions, real fills** — ❌ forbidden under
>   every mode including `PAPER_SIMULATION`.
>
> Every quantity in this document is a **mathematical object in a modeled
> context**. `f` is a dimensionless fraction of a hypothetical modeled bankroll,
> never a dollar amount, never a contract count, never a side, and never an
> instruction. The only place any of this may ever operate is a
> `PAPER_SIMULATION` context that carries an explicit model identifier and a
> modeled-vs-observed basis on every artifact — and that mode is itself
> unimplemented.
>
> **This document also does not authorize its own implementation.** Writing code
> that names these concepts will fail the AST safety audit
> (`BANNED_IDENTIFIER_FRAGMENTS` contains `kelly`, `position_siz`, `portfolio`,
> `expected_value`, `paper_trad`), and per the SAFETY-BOUNDARY-ROUTE-QUOTE-001
> amendment that failure is the **correct** outcome. This file is markdown under
> `docs/research/`; the canonical safety grep is scoped to `app/ --include="*.py"`
> and is unaffected.

## Table of contents

1. [Scope, notation, and what a "size" means here](#1-scope-notation-and-what-a-size-means-here)
2. [Track 1 — Kelly and its failure modes](#2-track-1--kelly-and-its-failure-modes)
3. [Track 2 — Uncertainty-aware sizing](#3-track-2--uncertainty-aware-sizing)
4. [Track 3 — Tail risk: CVaR and EVaR as constraints](#4-track-3--tail-risk-cvar-and-evar-as-constraints)
5. [Track 4 — Drawdown and ruin](#5-track-4--drawdown-and-ruin)
6. [Track 5 — Correlation and concentration](#6-track-5--correlation-and-concentration)
7. [Track 6 — Abstention as a first-class action](#7-track-6--abstention-as-a-first-class-action)
8. [Track 7 — The definition of ready, and the sample-size answer](#8-track-7--the-definition-of-ready-and-the-sample-size-answer)
9. [Challenge to the formula](#9-challenge-to-the-formula)
10. [Consolidated term reference](#10-consolidated-term-reference)
11. [Evidence ledger — VERIFIED vs INFERRED](#11-evidence-ledger--verified-vs-inferred)

## 1. Scope, notation, and what a "size" means here

### 1.1 Notation

| symbol | meaning |
|---|---|
| `q` | the market's price for a binary contract that pays 1 on YES, 0 on NO. Treated as the market's implied probability. Dimensionless, in (0,1). |
| `p` | our believed probability of YES. |
| `e = p − q` | **edge, in probability points.** The only "edge" quantity this document uses. Never a dollar amount. |
| `b = (1−q)/q` | net odds received per unit staked on YES. |
| `f` | fraction of a **hypothetical modeled bankroll** committed. Dimensionless. Never a contract count, never a notional. |
| `g(f; p, q)` | expected log-growth per resolution = `p·ln(1+f·b) + (1−p)·ln(1−f)`. |
| `λ` | the fractional-Kelly multiplier, `λ ∈ (0,1]`. |

Everything is dimensionless by construction. This is not cosmetic: the safety
boundary forbids **dollar EV** and **portfolio sizing**, and a fraction-of-a-
modeled-bankroll is the largest object that can be reasoned about without
producing either. There is no step in this document that converts `f` into a
number of contracts, and no such step may be added without the gated milestone.

### 1.2 The target form, restated

```
f_actual = min( λ·f_Kelly(p_conservative), f_liquidity, f_CVaR, f_concentration, f_drawdown )
```

`min` over a set of caps, with an **abstention gate in front** (Section 7) that
can return `NO_TRADE` regardless of what the `min` evaluates to. Section 9
argues that the `min` form is right for four of the five terms and **wrong for
`f_concentration`**, which is not a per-position cap at all.

### 1.3 What is *not* in scope

Exit/stop rules, order-type selection, queue position, adverse-selection
detection at the microstructure level, and anything that touches a venue. The
sizing layer consumes a believed `p` and a market state; it emits a fraction or
a refusal. It never emits a side (the side falls out of the sign of `e`, and
even that is a downstream object this layer does not produce).

---

## 2. Track 1 — Kelly and its failure modes

### 2.1 Derivation for a binary contract at price `q`

Stake fraction `f` of bankroll `W` on YES at price `q`. You acquire `fW/q`
contracts. On YES: `W' = W(1−f) + fW/q = W(1 + f·b)` with `b = (1−q)/q`.
On NO: `W' = W(1−f)`.

```
g(f) = p·ln(1 + f·b) + (1−p)·ln(1 − f)
g'(f) = p·b/(1+f·b) − (1−p)/(1−f) = 0
  ⇒ p·b(1−f) = (1−p)(1+f·b)
  ⇒ f* = (p(1+b) − 1)/b
```

Substituting `1 + b = 1/q`:

```
f* = (p − q) / (1 − q)          ── the target form, confirmed
```

**Two identities that do a lot of work later.** At `f = f*`:

```
1 + f*·b = p/q          1 − f* = (1−p)/(1−q)
```

so

```
g(f*) = p·ln(p/q) + (1−p)·ln((1−p)/(1−q)) = KL(p ‖ q)
```

**The maximum achievable log-growth of a Kelly-sized binary position is exactly
the Kullback–Leibler divergence of your belief from the market price** — which
is also exactly your expected log-score advantage over the market. This was
confirmed numerically to 6 decimal places in every row of the table below.

This is not a curiosity. It means:

- **Growth and forecast skill are the same measurement.** There is no such thing
  as "a good forecaster with no edge" or "an edge without probabilistic skill"
  in this setting. Anything that improves the log-score against the market price
  improves growth by the identical amount.
- **The whole programme is bounded by KL, and KL is small.** At `q=0.50, p=0.55`
  — a 5 percentage-point edge, which is enormous for a liquid prediction market —
  `KL = 0.005008` nats per resolution. That is the *entire* growth budget before
  any fee, spread, correlation, or estimation error is subtracted. Section 8's
  uncomfortable sample size is a direct consequence of this number being small.

### 2.2 Failure mode 1 — overconfident `p` (bias)

`f*` is **linear in `p` with slope `1/(1−q)`**. So a fixed bias `δ` in `p`
produces an error in `f` of `δ/(1−q)` — amplified without limit as `q → 1`.
Meanwhile `g` is concave in `f`. Bias in `p` therefore does damage that grows
quadratically while the bias itself grows linearly.

**Numeric table** (`δ = p_believed − p_true`; growth is per-resolution log
growth realised under the *true* `p`; `% of g*` is the fraction of the optimal
growth actually earned):

`q = 0.50, p_true = 0.55` (f* = 0.1000, g* = 0.005008)

| δ | f used | ×Kelly | realised g | % of g* |
|---:|---:|---:|---:|---:|
| +0.000 | 0.1000 | 1.00 | 0.005008 | 100.0 |
| +0.010 | 0.1200 | 1.20 | 0.004806 | 96.0 |
| +0.020 | 0.1400 | 1.40 | 0.004195 | 83.8 |
| +0.030 | 0.1600 | 1.60 | 0.003172 | 63.3 |
| +0.050 | 0.2000 | 2.00 | **−0.000138** | **−2.8** |
| +0.075 | 0.2500 | 2.50 | −0.006728 | −134.3 |
| +0.100 | 0.3000 | 3.00 | −0.016203 | −323.5 |

`q = 0.90, p_true = 0.93` (f* = 0.3000, g* = 0.005527) — the favourite case,
where `1/(1−q) = 10`:

| δ | f used | ×Kelly | realised g | % of g* |
|---:|---:|---:|---:|---:|
| +0.000 | 0.3000 | 1.00 | 0.005527 | 100.0 |
| +0.010 | 0.4000 | 1.33 | 0.004683 | 84.7 |
| +0.020 | 0.5000 | 1.67 | 0.001762 | 31.9 |
| +0.030 | 0.6000 | 2.00 | −0.004120 | −74.5 |
| +0.050 | 0.8000 | 2.67 | −0.033464 | −605.4 |
| +0.075 | 0.9900 (capped) | 3.30 | −0.225307 | −4076.3 |

`q = 0.10, p_true = 0.13` (f* = 0.0333, g* = 0.004613):

| δ | f used | ×Kelly | realised g | % of g* |
|---:|---:|---:|---:|---:|
| +0.010 | 0.0444 | 1.33 | 0.004189 | 90.8 |
| +0.020 | 0.0556 | 1.67 | 0.002983 | 64.7 |
| +0.030 | 0.0667 | 2.00 | 0.001077 | 23.3 |
| +0.050 | 0.0889 | 2.67 | −0.004576 | −99.2 |

**The three facts to take away:**

1. **2× Kelly = zero growth, exactly.** Visible in every block: `δ` that doubles
   `f` lands growth at ~0. This is a known general property of log-optimal
   betting, not an artefact of these numbers. Past 2×, growth is negative — you
   lose money *while holding a genuine edge*.
2. **The `δ` that produces 2× Kelly is `δ = e`.** Doubling `f` requires doubling
   the *edge estimate*, i.e. `δ = p_true − q`. Since realistic net edges are
   1–3 pp (Section 8.2), **a 2 pp forecasting bias is enough to zero out the
   growth of a genuine 2 pp edge.** A forecaster calibrated to ±2 pp is
   considered *excellent*. This is the central practical problem.
3. **Favourites are the trap.** At `q=0.90` a 3 pp bias — inside anyone's
   calibration error — turns +0.0055 into −0.0041 and demands 60% of bankroll on
   one contract. The `1/(1−q)` amplification is where an unconstrained Kelly
   implementation blows up first.

### 2.3 Failure mode 2 — estimation error with **no bias at all**

This is the more insidious one, because a team that has verified its forecaster
is unbiased will believe it is safe. It is not. `g` is concave, so by Jensen
`E[g(f(p̂))] < g(f(E[p̂]))` even when `E[p̂] = p_true`.

Monte Carlo, `p̂ ~ N(p_true, s)`, 400,000 draws, `f` clipped at 0 (negative `f`
treated as abstention, which is the *charitable* assumption — it removes the
wrong-side bets entirely):

| q | p_true | s = sd(p̂) | E[g] | % of g* | P(abstain) |
|---:|---:|---:|---:|---:|---:|
| 0.50 | 0.550 | 0.005 | 0.004958 | 99.0 | 0.000 |
| 0.50 | 0.550 | 0.010 | 0.004806 | 96.0 | 0.000 |
| 0.50 | 0.550 | 0.020 | 0.004209 | 84.0 | 0.006 |
| 0.50 | 0.550 | 0.030 | 0.003321 | 66.3 | 0.048 |
| 0.50 | 0.550 | 0.050 | 0.001089 | **21.7** | 0.159 |
| 0.90 | 0.930 | 0.010 | 0.004738 | 85.7 | 0.001 |
| 0.90 | 0.930 | 0.020 | 0.002057 | 37.2 | 0.067 |
| 0.90 | 0.930 | 0.030 | **−0.007516** | **−136.0** | 0.160 |
| 0.90 | 0.930 | 0.050 | −0.051247 | −927.2 | 0.274 |
| 0.10 | 0.130 | 0.030 | 0.001769 | 38.4 | 0.158 |
| 0.10 | 0.130 | 0.050 | −0.001249 | −27.1 | 0.274 |

**At `q=0.90`, an unbiased forecaster with 3 pp of noise turns a real +3 pp edge
into negative growth.** No overconfidence, no bias, no model error — just
ordinary sampling noise on an honest estimate. The expansion is:

```
E[g] ≈ g(f*) + ½·g''(f*)·Var(f̂)   with   Var(f̂) = Var(p̂)/(1−q)²
```

so the penalty carries a `1/(1−q)²` factor. The 100× amplification between
`q=0.50` and `q=0.90` in the table is exactly that factor.

**Consequence for the design: noise in `p` must be an input to the sizing layer,
not an afterthought.** This is the honest justification for Track 2 — not a
Bayesian one (Section 3.2 shows the Bayesian argument does *not* work).

### 2.4 Failure mode 3 — fat tails and discreteness

The binary payoff is not fat-tailed *per position*; it is bounded on both sides.
Three real problems replace fat tails:

- **Discreteness.** Contracts are integers with a minimum of 1 and prices are
  cent-quantised. `f = 0.004` of a modelled bankroll may round to 0 or to a size
  many times larger than intended. **Rounding is systematically upward-biased
  in risk** if implemented as `max(1, round(...))`, which is the obvious
  implementation. The correct handling is: if `f` rounds to a size whose realised
  fraction exceeds `f_actual`, the outcome is `NO_TRADE`, not a bigger bet.
- **Resolution risk is not a Bernoulli.** Prediction markets void, resolve
  ambiguously, resolve late, or resolve against the plain reading of the rule.
  These are model-misspecification events living entirely outside `p`. They are
  fat-tailed in the sense that matters: a rare event that takes the whole
  position and is not in the model.
- **Memecoins genuinely are fat-tailed and are not binary.** A memecoin position
  is a continuous payoff with a heavy right tail and a near-certain left absorb
  at −100%. Kelly for that payoff is *not* `(p−q)/(1−q)`; it is the solution of
  `E[R/(1+f·R)] = 0` over the full return distribution, and its input is the
  whole distribution — precisely the thing that cannot be estimated from a few
  hundred observations (Section 4.3). **Kelly should not be used at all on the
  memecoin lane**; a flat cap plus the tail constraint is the honest instrument.

### 2.5 Failure mode 4 — correlated simultaneous positions

Kelly for `K` simultaneous positions is a joint optimisation over the joint
outcome distribution, not `K` independent applications of `f*`. Applying `f*`
independently and summing is equivalent to assuming `ρ = 0`. With `K` positions
each at fraction `f` and pairwise outcome correlation `ρ`, the variance of the
aggregate stake is `K·f²·(1 + (K−1)ρ)` rather than `K·f²` — the effective single
position size is inflated by `√(1 + (K−1)ρ)`.

At `K = 10, ρ = 0.3` that is `√3.7 = 1.92`: **a portfolio built from ten
"individually half-Kelly" positions is running at roughly full Kelly in
aggregate** — the exact regime the tables above show as maximally dangerous.
Section 6 handles this properly; the point here is that the failure is silent
and that each individual position passes its own check.

### 2.6 Fractional Kelly: the growth/variance trade-off, measured

Simulation: `q=0.50, p=0.55`, 1,000 resolutions, 20,000 paths, betting `λ·f*`.

| λ (×Kelly) | f | g / bet | % of g* | sd(log ret) | median maxDD | p95 maxDD | P(DD>50%) | P(end < start) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.10 | 0.0100 | 0.000950 | 19.0 | 0.00995 | 0.157 | 0.253 | 0.0000 | 0.0013 |
| 0.14 | 0.0140 | 0.001302 | 26.0 | 0.01393 | 0.214 | 0.339 | 0.0008 | 0.0016 |
| 0.25 | 0.0250 | 0.002188 | 43.7 | 0.02488 | 0.357 | 0.537 | 0.0876 | 0.0031 |
| 0.333 | 0.0333 | 0.002776 | 55.4 | 0.03315 | 0.452 | 0.653 | 0.3308 | 0.0050 |
| 0.50 | 0.0500 | 0.003753 | **74.9** | 0.04979 | 0.614 | 0.817 | 0.8576 | 0.0097 |
| 0.75 | 0.0750 | 0.004694 | 93.7 | 0.07476 | 0.786 | 0.941 | 0.9990 | 0.0237 |
| 1.00 | 0.1000 | 0.005008 | 100.0 | 0.09983 | **0.894** | 0.985 | **1.0000** | 0.0625 |
| 1.25 | 0.1250 | 0.004692 | 93.7 | 0.12503 | 0.954 | 0.997 | 1.0000 | 0.1203 |
| 1.50 | 0.1500 | 0.003736 | 74.6 | 0.15038 | 0.984 | 1.000 | 1.0000 | 0.2146 |
| 2.00 | 0.2000 | −0.000138 | −2.8 | 0.20172 | 0.999 | 1.000 | 1.0000 | 0.5063 |

Three results:

- **`λ = ½` gives 74.9% of the growth at 50% of the log-return volatility.** This
  reproduces the MacLean–Ziemba–Blazenko "half Kelly ≈ 75% of growth, 50% of
  volatility" result to within a decimal, from first principles on this payoff.
  The general small-edge approximation is `g(λf*) ≈ λ(2−λ)·g(f*)`; at `λ=0.5`
  that is 0.75, and the simulation gives 0.749.
- **The growth curve is symmetric about `λ=1`, the risk curve is not.** `λ=0.75`
  and `λ=1.25` both earn 93.7% of `g*` — but `λ=1.25` has a 12.0% chance of
  ending below where it started against 2.4%, and a median max drawdown of 95%
  against 79%. **Underbetting is nearly free; overbetting is not.** This
  asymmetry, plus the fact that every error source in Sections 2.2–2.5 pushes
  `f` *upward*, is the entire argument for `λ < 1`.
- **Full Kelly's drawdowns are not survivable by a human.** Median maximum
  drawdown of **89.4%** over 1,000 bets, and `P(drawdown > 50%) = 1.0000` — every
  single one of 20,000 paths. This is with `p` known *exactly*. Full Kelly is
  not a strategy anyone can actually run.

### 2.7 Verdict: Kelly is a CEILING, not the allocator

**Kelly must be a ceiling.** The argument, in order of strength:

1. **Its optimality is conditional on a known `p`, and `p` is the one thing we
   do not have.** Sections 2.2–2.3 show the optimality is not robust: unbiased
   3 pp noise at `q=0.90` flips the sign of the growth rate. An optimality result
   whose sign flips inside the error bar of its own input is not an allocation
   rule.
2. **It optimises the wrong objective for a non-replenished bankroll.** `E[log W]`
   is asymptotic and indifferent to path. Section 5 shows the constraint that
   actually binds — tolerable drawdown — implies `λ` an order of magnitude below 1.
3. **It composes wrongly across positions** (2.5), so the per-position number is
   not the operative number anyway.
4. **The `min(...)` form only makes sense with Kelly as one of the caps.** A `min`
   over caps is an allocator that says "no more than this, for each of five
   independent reasons". Kelly's role there is "no more than the growth-optimal
   amount", which is the correct role: it is the only one of the five terms that
   is an *upper* bound derived from the edge itself.

What plays the allocator role instead: **a constant fraction `f_base`, capped
by `λ·f_Kelly`.** In the regime the numbers point to (`λ ≈ 0.15–0.25`, edges of
1–3 pp), `λ·f_Kelly` is a small number that varies with `e/(1−q)`, and the
variation is driven almost entirely by estimation noise rather than by real
signal. A flat fraction is more robust and gives up little; Kelly's job is to
stop it when the edge does not support even that.

**On `λ` being learned, not assumed** — Section 3.5 gives the two-factor
decomposition (`λ = λ_calib × λ_dd`) where both factors are estimated from data,
and Section 8.5 gives the validation procedure. The headline preview: the
drawdown factor alone lands at **λ_dd ≈ 0.14** for a "20% max drawdown at 5%
probability" statement (Section 5.2).

## 3. Track 2 — Uncertainty-aware sizing

A parallel track designs a forecast layer that emits a **distribution** `π` over
`p` rather than a point. The natural move is Kelly on a pessimistic quantile,
`p_conservative = Q_π(α)`. This section establishes when that is justified and
when it is theatre.

### 3.1 The negative result: Bayesian Kelly gives you nothing here

For a single binary contract, `g(f; p, q)` is **linear in `p`**:

```
E_π[g(f;p)] = E_π[ p·ln(1+f·b) + (1−p)·ln(1−f) ]
            = p̄·ln(1+f·b) + (1−p̄)·ln(1−f)
            = g(f; p̄, q)          where p̄ = E_π[p]
```

The objective under the posterior is *identical* to the objective at the
posterior mean. Therefore:

> **For a single binary contract, one-period Bayesian Kelly is exactly Kelly at
> the posterior mean. The width, skew, and shape of the posterior over `p` have
> literally zero effect on the Bayes-optimal log-growth bet.**

This is worth stating loudly because the intuition "I am uncertain, so I should
bet less" does **not** follow from Bayesian log-utility in this setting. Anyone
who implements "Bayesian Kelly" expecting it to shrink positions has implemented
Kelly-at-the-mean with extra steps. (The result is special to the binary payoff
and to one period — it fails for general payoffs, where `p` enters inside the
log, and it fails multi-period, see 3.3.)

So the sizing-down must be justified on other grounds. There are three good ones.

### 3.2 Justification A — shrinkage, and the identity that makes `λ_calib` measurable

Section 2.3 measured the damage from noisy `p̂`. Reconcile it with 3.1: there is
no contradiction, because in 2.3 the estimate `p̂` was plugged in **as if it were
the posterior mean**, and it is not. If `p̂` is a noisy signal about `p`, then
`E[p | p̂] ≠ p̂` — the posterior mean is `p̂` **shrunk toward the prior**.

The 2.3 disaster is therefore not "Bayes fails"; it is "we skipped the Bayesian
step". And the shrinkage has a directly estimable form. In log-odds space, with
`x = logit(p̂)`, the standard recalibration model is

```
logit P(Y=1 | x) = a + β·x
```

`β` is the **Cox calibration slope**. `β < 1` means the forecaster is
overconfident and its log-odds should be shrunk by exactly `β`; `β > 1` means
underconfident. Fitting it is a one-parameter logistic regression on already-
scored forecasts.

**This yields a genuinely learned, not assumed, first factor of `λ`:**

```
p_calibrated = σ( â + β̂ · logit(p̂) )        and       λ_calib ≡ β̂
```

Applied *before* any Kelly computation, not as a multiplier afterwards — because
shrinking log-odds and shrinking `f` are not the same operation, and the former
is the one with a probabilistic justification.

**How much data does `β̂` need?** Simulated Fisher information for the slope
(600 replicates per cell, `x = logit(p̂)` normal with the given sd):

| sd(logit p̂) | n = 200 | 500 | 1,000 | 2,000 | 5,000 | 12,945 |
|---|---:|---:|---:|---:|---:|---:|
| 0.5 (narrow forecasts) | ±0.603 | ±0.394 | ±0.270 | ±0.192 | ±0.122 | ±0.076 |
| 1.0 | ±0.362 | ±0.224 | ±0.162 | ±0.116 | ±0.073 | ±0.045 |
| 1.5 (bold forecasts) | ±0.306 | ±0.191 | ±0.138 | ±0.096 | ±0.060 | ±0.038 |

(95% CI half-widths on `β̂`.)

**This is the single most encouraging number in the document.** At `n ≈ 1,000`
scored forecasts you know your calibration slope to about ±0.16, and at the
12,945 forecasts this repository has already scored (per
`docs/OUTCOME_SYNC_POST_DRAIN_BASELINE_2026_08.md`) you would know it to ±0.045.
Contrast Section 8: establishing net positive expectancy needs **15,000+
executable trades**. Calibration is roughly **15× cheaper in sample than P&L**,
and it must be established first — not because it is easier, but because
Section 2.1's identity says it is the *same quantity*, measured with less noise.

### 3.3 Justification B — multi-period structure and the absorbing barrier

The 3.1 equivalence is one-period. Over a sequence with a **non-replenished**
bankroll, three things break it:

1. **Compounding is path-dependent.** `E[log W_T] = Σ E[g_t]` is true, but the
   *distribution* of `W_T` is not, and a drawdown constraint (Section 5) is a
   statement about the path, not the endpoint.
2. **Parameter uncertainty persists across bets.** If `p̂` is wrong for a *class*
   of markets, the error is repeated, not averaged out. The one-period posterior
   treats each bet's error as fresh; in reality model error is a common factor
   across the whole sequence (Section 6 treats this as a correlation problem,
   which is what it is).
3. **There is a real absorbing barrier.** Contracts are integer, there is a
   minimum size, and fees are charged per contract. Proportional betting on a
   continuum has no ruin; proportional betting with a minimum lot does.

### 3.4 Justification C — ambiguity, not risk

The decisive argument: **the posterior is itself a model output, and its width is
not identifiable from outcome data.** This deserves to be stated as a hard
limitation:

> `p` is **never observed**. Only the binary outcome `Y` is. The outcome data
> identifies the *mean* of the predictive distribution (that is what a
> calibration curve tests) and says essentially nothing about its *spread*. A
> forecast layer that reports a 90% credible interval of ±2 pp and one that
> reports ±15 pp produce identical likelihoods for every possible outcome
> sequence, provided their means agree.

Consequences for the design:

- A self-reported posterior width **must not** be trusted as the input to
  `p_conservative`. It is unfalsifiable by the only data available.
- The dispersion used for `p_conservative` must come from a **measurable**
  source. Three that are:
  1. **Bootstrap / refit dispersion** — resample the training window, refit,
     look at the spread of `p̂` for the same market. Measures estimation
     variance, which is real and is exactly the `s` of Section 2.3.
  2. **Ensemble disagreement** — spread across independent forecasters/seeds for
     the same market. Measures model-choice sensitivity.
  3. **Regime-conditional residual dispersion** — historical `|Y − p̂|` grouped
     by regime (sport, venue, time-to-resolution, liquidity band), which bounds
     how much worse than the pooled calibration curve a given cell can be.
- Whatever is not covered by those is **ambiguity**, not risk, and ambiguity is
  what `λ` and the abstention rules exist for.

### 3.5 The four candidates, compared

| approach | what it does on a binary contract | verdict |
|---|---|---|
| **Point Kelly** on `p̂` | `f = (p̂−q)/(1−q)` | ❌ Section 2.3 — negative growth at `q=0.9` with 3 pp of honest noise |
| **Bayesian Kelly** `argmax E_π[g]` | **provably identical to Kelly at `p̄`** (3.1) | ❌ not wrong, but does nothing. Provides no shrinkage. |
| **Robust / max-min Kelly** `argmax min_{p∈P} g` | with `P = [p_lo, p_hi]`, the min is at `p_lo`, so `f = (p_lo−q)/(1−q)` — **Kelly on the lower bound** | ✅ recommended |
| **DRO** over an ambiguity set of outcome distributions | for a two-point outcome space, any ambiguity set is an interval in `p`; max-min **reduces exactly to robust Kelly on `p_lo`** | ⚠️ mathematically identical to the row above *for binaries*; earns its keep only on the memecoin lane, where the payoff is continuous and the ambiguity set is over a real distribution |

**Recommendation: robust Kelly on a lower quantile, with the quantile taken from
a measurable dispersion, after log-odds recalibration.** Explicitly *not*
justified as Bayesian optimality — justified as ambiguity aversion against an
unidentifiable posterior width, which is an honest reason.

### 3.6 Choosing the quantile — an exact interpretation

Because `f*` is **linear** in `p`, the quantile has a clean and exact meaning:

```
P( f(p_α) > f*(p_true) ) = P( p_α > p_true ) = α        (if π is calibrated)
```

> **Choosing the α-quantile sets the probability of overbetting relative to true
> Kelly to exactly α.**

That is a real design knob, not a heuristic. Given Section 2.6's asymmetry
(overbetting is far more costly than underbetting), and given that `π`'s spread
is *not* verifiable (3.4):

- **`α ∈ [0.10, 0.25]`**, and toward the low end when the dispersion estimate
  comes from a single source rather than bootstrap + ensemble agreement.
- **Crucially, the quantile controls the *frequency* of overbetting, not its
  *magnitude*.** In the 15.9% of Section-2.3 draws that abstained, the
  remaining draws still overbet badly. So the quantile is **not a substitute for
  `λ`**; the two do different jobs and both are needed:
  - `p_conservative` at level α → handles **parameter uncertainty** in `p`.
  - `λ_dd` → handles **path risk** given `p` (Section 5).
  - `λ_calib = β̂` → handles **systematic overconfidence** in `p̂`.

### 3.7 Interaction with calibration quality — the gate

The quantile is only meaningful if `π` is calibrated in the mean and the
dispersion source has verified coverage. This produces a hard rule, which
becomes abstention reason code `CALIBRATION_UNKNOWN_FOR_REGIME` in Section 7:

1. **Recalibrate first.** Never let `p_conservative` compensate for a `β̂ ≠ 1`.
   Miscalibration is a bias; the quantile addresses variance. Using one to
   patch the other silently couples them and both stop being measurable.
2. **Fit `β̂` per regime, with a minimum cell size.** A pooled `β̂` is wrong in
   every cell if calibration varies by sport/venue/horizon — and this repository
   has direct evidence that it does: per
   `docs/OUTCOME_SYNC_POST_DRAIN_BASELINE_2026_08.md`, tennis shows **negative
   skill** while soccer's apparent edge was an artefact of a non-representative
   sample. A single pooled slope would have averaged those into a plausible-
   looking number.
3. **Minimum cell size ≈ 500 scored forecasts** for `β̂` to ±0.2 (table in 3.2 at
   sd(logit p̂) ≈ 1.0). Below that, the regime is `CALIBRATION_UNKNOWN` and the
   answer is `NO_TRADE` — not "use the pooled slope".
4. **Verify quantile coverage prospectively on the only testable implication.**
   You cannot test the spread directly (3.4), but you *can* test the induced
   statement: over markets where the layer claimed `p_conservative = p_α`, the
   realised outcome frequency should exceed `p_α` more than `1−α` of the time.
   That is a testable, one-sided coverage check — weak, but not vacuous, and it
   is the only honest validation available.

### 3.8 The composite `λ`, learned

```
λ = λ_calib × λ_dd
    λ_calib = β̂        estimated per regime (3.2), n ≥ 500 per cell
    λ_dd    = 2 / (1 + ln ε / ln α_dd)      from the drawdown statement (5.2)
```

with the final value **validated, not adopted**, by the out-of-sample procedure
in 8.5: choose `λ` that maximises a *lower confidence bound* on realised growth
over the prospective sample, never the point estimate — because the point
estimate of the optimal `λ` is itself upward-biased for exactly the reason
Section 2.3 describes.

## 4. Track 3 — Tail risk: CVaR and EVaR as constraints

_(to be filled)_

## 5. Track 4 — Drawdown and ruin

_(to be filled)_

## 6. Track 5 — Correlation and concentration

_(to be filled)_

## 7. Track 6 — Abstention as a first-class action

_(to be filled)_

## 8. Track 7 — The definition of ready, and the sample-size answer

_(to be filled)_

## 9. Challenge to the formula

_(to be filled)_

## 10. Consolidated term reference

_(to be filled)_

## 11. Evidence ledger — VERIFIED vs INFERRED

_(to be filled)_
