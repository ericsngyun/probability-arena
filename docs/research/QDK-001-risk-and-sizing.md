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

### 4.1 Definitions

Let `L` be the loss (positive = bad) as a fraction of the modelled bankroll, and
`α` the tail probability.

```
VaR_α(L)  = inf { t : P(L ≤ t) ≥ 1−α }                    (a quantile; NOT coherent — not subadditive)
CVaR_α(L) = E[ L | L ≥ VaR_α(L) ]                          (coherent; Rockafellar–Uryasev)
          = min_t { t + (1/α)·E[(L − t)⁺] }                (the convex-program form)
EVaR_α(L) = inf_{z>0} (1/z)·ln( E[e^{zL}] / α )            (coherent; Ahmadi-Javid 2012)
```

Relations: `VaR_α ≤ CVaR_α ≤ EVaR_α`. EVaR is the tightest bound obtainable from
the Chernoff inequality, and its dual representation is a KL-divergence
(relative-entropy) ball — which is why it and the DRO framing of Section 3.5 are
the same object seen from two sides.

Why the Rockafellar–Uryasev form matters: `CVaR_α` is **jointly convex in the
position vector and `t`**, so a CVaR constraint over `K` positions is a convex
constraint and the resulting sizing problem is a convex program. So is EVaR's.
This is the technical reason both are usable and VaR is not.

### 4.2 Finding: **CVaR on a single position is degenerate and carries no information**

Simulated, 300,000 draws per row:

| position | P(loss) | α=0.01 | α=0.05 | α=0.20 | α=0.40 |
|---|---:|---|---|---|---|
| `p=0.55, q=0.50, f=0.1000` | 0.45 | CVaR = **0.1000** | 0.1000 | 0.1000 | 0.1000 |
| `p=0.93, q=0.90, f=0.3000` | 0.07 | CVaR = **0.3000** | 0.3000 | −0.0102 | −0.0102 |
| `p=0.13, q=0.10, f=0.0333` | 0.87 | CVaR = **0.0333** | 0.0333 | 0.0333 | 0.0333 |

The loss distribution of a single binary position is a **two-point** distribution:
`{ +f with prob 1−p ; −f·b with prob p }`. Therefore, for any `α ≤ 1−p`:

```
VaR_α = CVaR_α = f          exactly.
```

> **`f_CVaR` as a per-position term in the sizing formula reduces to `f ≤ CVaR_limit`
> — a flat position cap wearing a risk-measure costume.** It contains no
> information the cap did not already contain.

And it is worse than uninformative outside that region: at `p=0.93` with
`α = 0.20 > 1−p = 0.07`, the "tail" contains winning outcomes and `CVaR` goes
**negative** (−0.0102), i.e. the constraint reports the position as a source of
guaranteed profit and binds on nothing. **The sign of `α − (1−p)` silently
switches the constraint between a flat cap and a no-op**, and nothing in the
formula's notation warns you.

The same degeneracy applies to a long-only memecoin position: downside is bounded
by the stake and the fat tail is on the **upside**, where a loss-based measure
does not look. Simulated: for a payoff that is 90% total-stake-loss, 9.5% modest
gain, 0.5% Pareto(1.2) large gain, the estimated `CVaR95` is **0.0200 = the full
stake, with zero sampling variance at every n from 50 to 5,000**. Estimating it
is trivial and tells you nothing you did not know before placing it.

**Design consequence: drop `f_CVaR` from the per-position `min`.** The tail
constraint belongs at the **book and window level** and nowhere else. See
Section 9.

### 4.3 Where CVaR *does* carry information: the aggregate under a common factor

`K` positions each at `f = 0.02`, `p = 0.55`, `q = 0.50`, outcomes driven by a
latent Gaussian factor with pairwise correlation `ρ`. 300,000 draws per row.

| K | ρ | Σf | sd(L) | CVaR95 | CVaR99 | EVaR95 | **CVaR95 / Σf** | P(all lose) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 | 0.00 | 0.200 | 0.0629 | 0.0931 | 0.1300 | 0.1301 | **0.465** | 0.00039 |
| 10 | 0.10 | 0.200 | 0.0790 | 0.1386 | 0.1665 | 0.1596 | 0.693 | 0.00338 |
| 10 | 0.30 | 0.200 | 0.1041 | 0.1732 | 0.2000 | 0.1914 | 0.866 | 0.02458 |
| 10 | 0.60 | 0.200 | 0.1361 | 0.2000 | 0.2000 | 0.2017 | **1.000** | 0.09986 |
| 25 | 0.00 | 0.500 | 0.0993 | 0.1448 | 0.2020 | 0.1922 | **0.290** | 0.00000 |
| 25 | 0.10 | 0.500 | 0.1577 | 0.2678 | 0.3348 | 0.3161 | 0.536 | 0.00003 |
| 25 | 0.30 | 0.500 | 0.2359 | 0.3967 | 0.4728 | 0.4380 | 0.793 | 0.00400 |
| 25 | 0.60 | 0.500 | 0.3270 | 0.5000 | 0.5000 | 0.5000 | **1.000** | 0.05035 |
| 25 | 0.90 | 0.500 | 0.4232 | 0.5000 | 0.5000 | 0.5037 | 1.000 | 0.21857 |
| 50 | 0.30 | 1.000 | 0.4549 | 0.7849 | 0.9178 | 0.8447 | 0.785 | 0.00098 |
| 50 | 0.60 | 1.000 | 0.6455 | 0.9832 | 1.0000 | 0.9916 | 0.983 | 0.03011 |

Read the `CVaR95 / Σf` column. It is the **only** thing a tail constraint
actually measures in this setting: *what fraction of deployed capital the 5%
tail takes.*

- At `ρ = 0` and `K = 25`, it is 0.290 — diversification is real and the CVaR
  constraint is genuinely less binding than a gross-exposure cap.
- **At `ρ ≥ 0.6` it is exactly 1.000.** The 5% tail is "everything loses at
  once", so `CVaR_α = Σf` and **the tail constraint degenerates into a gross-
  exposure cap again.**

> **The entire informational content of `f_CVaR` is the estimate of `ρ`.** If
> correlation is high, CVaR tells you nothing a gross cap did not; if it is low,
> CVaR's value is exactly the diversification credit it grants — and granting a
> diversification credit on a mis-estimated `ρ` is the classic way to blow up.
> This makes Track 5 (correlation detection) a **prerequisite** for Track 3, not
> a parallel concern.

**On EVaR.** EVaR ≥ CVaR in every row, by 10–27% at small `K` and converging to
CVaR as the tail saturates. Its case is:

- ✅ Convex in the position vector, so the constrained sizing problem stays a
  convex program (same as CVaR).
- ✅ A **conservative** bound, which is the right direction of error for a
  constraint whose input (`ρ`) is the least reliable number in the system.
- ✅ Its dual is a KL ball, so an EVaR budget is literally a statement about how
  much the true distribution may differ from the estimated one in relative
  entropy — the honest way to price model error into a tail constraint.
- ❌ **It is not more robust to estimate.** The MGF `E[e^{zL}]` is exponentially
  dominated by the largest observations, so at the optimising `z` it is driven by
  the same handful of tail points that make CVaR noisy. Claims that EVaR "uses
  the whole sample and is therefore more stable" do not survive contact with the
  optimising `z`. Its advantages are conservatism and tractability, not
  estimation stability.

**Recommendation: use EVaR as the operational tail constraint, at the book level,
with `ρ` from Track 5 rather than from the return sample.**

### 4.4 Estimating a tail from few observations — the quiet failure

This is where such systems fail silently, so it gets numbers.

**First, the arithmetic that everyone skips.** A sample of `n` backs `CVaR_α`
with `⌈α·n⌉` observations:

| n | points behind CVaR95 | points behind CVaR99 |
|---:|---:|---:|
| 50 | 2 | 1 |
| 100 | 5 | 1 |
| 250 | 12 | 2 |
| 500 | 25 | 5 |
| 1,000 | 50 | 10 |
| 5,000 | 250 | 50 |

**A `CVaR99` computed from 250 observations is the average of two numbers.** It
will be reported to four decimal places.

**Second, sampling error and bias.** 800 bootstrap replicates per cell against a
1,000,000-draw ground truth, for the `K=25, ρ=0.3` book:

| n | CVaR95 bias | CVaR95 rel. sd | CVaR99 bias | CVaR99 rel. sd |
|---:|---:|---:|---:|---:|
| 50 | −3.7% | 12.5% | −8.1% | 11.1% |
| 100 | −0.3% | 8.3% | −2.8% | 7.8% |
| 250 | +0.1% | 5.5% | −2.9% | 5.2% |
| 500 | +1.1% | 4.1% | −1.5% | 3.6% |
| 1,000 | +1.3% | 3.0% | −1.1% | 2.8% |
| 5,000 | +0.5% | 1.6% | −0.1% | 1.1% |

The bias is **downward** at small `n` for `CVaR99` — the empirical estimator
understates the tail exactly when you have least data. That is the wrong
direction for a safety constraint.

**Third — and this is the real failure mode — the regime you have not sampled.**
Take the same book, plus a rare common-shock regime (frequency 0.6%: a venue-wide
resolution shock, a SOL crash, a launchpad-wide rug) in which every position
loses. True `CVaR95` rises 0.3971 → 0.4278 and true `CVaR99` to 0.5000. Measured:

| n | CVaR95 bias | CVaR99 bias | **P(sample contains zero shock events)** |
|---:|---:|---:|---:|
| 50 | −7.3% | −10.1% | **0.590** |
| 100 | −3.8% | −4.4% | 0.361 |
| 250 | −3.4% | −3.7% | 0.092 |
| 500 | −2.4% | −2.3% | 0.003 |
| 1,000 | −2.2% | −2.2% | 0.000 |
| 5,000 | −1.6% | −1.8% | 0.000 |

At `n = 50`, **59% of samples contain no instance of the regime that defines the
tail**, and those samples produce a confident, low-variance, systematically-too-
small CVaR. The bias does not vanish even at `n = 5,000`.

Generally, `P(no instance of a regime of frequency π in n draws) = (1−π)ⁿ`:

| π (regime freq.) | n=100 | n=250 | n=500 | n=1,000 | n=5,000 |
|---|---:|---:|---:|---:|---:|
| 1/50 (2%) | 0.133 | 0.006 | 0.000 | 0.000 | 0.000 |
| 1/100 (1%) | 0.366 | 0.081 | 0.007 | 0.000 | 0.000 |
| 1/250 (0.4%) | 0.670 | 0.368 | 0.135 | 0.018 | 0.000 |
| 1/1,000 (0.1%) | 0.905 | 0.779 | 0.606 | 0.368 | 0.007 |

**A once-a-year event in a system that produces 250 observations a year is absent
from the sample 37% of the time.** No estimator fixes this. It is an
identification problem, not an efficiency problem.

### 4.5 What to do instead

Given 4.2–4.4, the honest treatment of the tail term:

1. **Never estimate the tail purely empirically at these sample sizes.** Use a
   **scenario floor**: an explicitly enumerated, hand-specified set of common-mode
   scenarios (all positions in a correlated cluster lose; the venue voids a
   series; the shared underlying resolves against the book) with **assigned**
   probabilities, unioned with the empirical distribution. The tail constraint
   binds on `max(empirical, scenario)`. This converts an unestimable quantity
   into a stated assumption, which can at least be argued about.
2. **Report the number of tail observations alongside every tail estimate.** A
   `CVaR99` backed by 2 points must be labelled as such, on the artefact, in the
   same spirit as the `PAPER_SIMULATION` modeled-vs-observed basis requirement.
   A tail number without its supporting count is the same category of error the
   safety boundary already forbids for modeled P&L.
3. **Prefer EVaR** for conservatism and convexity (4.3), and set its budget from
   the drawdown statement (Section 5), not from the sample.
4. **Gate on regime coverage, not on the estimate.** If the prospective sample
   for a regime is below the size at which a `π = 1/250` event would be expected
   to appear, the abstention code is `TAIL_UNVERIFIED_FOR_REGIME` — the tail
   constraint is not "satisfied", it is **unmeasured**, and those are different.

## 5. Track 4 — Drawdown and ruin

### 5.1 The two "drawdowns" are not the same statement, and one of them is degenerate

Under the continuous (diffusion) approximation, betting `λ` times the full-Kelly
fraction gives log-wealth `X_t = a·t + b·B_t` with `a = (μ²/σ²)(λ − λ²/2)` and
`b = λμ/σ`, so `2a/b² = 2/λ − 1` and

```
P( W ever falls to α·W₀ )  =  α^(2/λ − 1)          ── barrier from INITIAL wealth
```

That is the constraint Busseti–Ryu–Boyd formalise, and they define it on initial
wealth deliberately. The reason is that the *other* drawdown — peak-to-trough —
is degenerate:

> The peak-relative drawdown process `Y_t = max_{s≤t} X_s − X_t` is a **reflected
> Brownian motion with negative drift**. Such a process is positive-recurrent and
> therefore **visits arbitrarily high levels with probability 1**. Over an
> unbounded horizon, `P(max peak-to-trough drawdown > L) = 1` for every `L`, at
> every `λ > 0`.
>
> **There is no `λ` that bounds peak-to-trough drawdown. A drawdown constraint
> can only be stated against a fixed reference level.**

Simulated confirmation (`q=0.50, p=0.55`, 12,000 paths, `α = 0.5`):

| λ | formula `α^(2/λ−1)` | (a) hits 0.5·W₀, N=250 | N=1,000 | N=5,000 | (b) peak-DD>50%, N=250 | N=1,000 | N=5,000 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.10 | 0.00000 | 0.00000 | 0.00000 | 0.00000 | 0.00000 | 0.00000 | 0.00000 |
| 0.14 | 0.00010 | 0.00000 | 0.00008 | 0.00008 | 0.00000 | 0.00125 | 0.00625 |
| 0.25 | 0.00781 | 0.00350 | 0.00633 | **0.00642** | 0.00983 | 0.08258 | **0.39692** |
| 0.333 | 0.03112 | 0.01892 | 0.02850 | **0.02867** | 0.06942 | 0.32750 | **0.88317** |
| 0.50 | 0.12500 | 0.08600 | 0.11150 | **0.11267** | 0.34467 | 0.85333 | **1.00000** |
| 1.00 | 0.50000 | 0.40200 | 0.46883 | **0.47683** | 0.96658 | 1.00000 | **1.00000** |

Column (a) converges to the formula and then **stops growing** — the barrier
probability saturates, exactly as the closed form says. Column (b) **keeps
growing toward 1** at every `λ`, confirming the recurrence argument. And median
peak drawdown grows without bound in horizon:

| λ | N=100 med / p95 | N=250 | N=1,000 | N=5,000 |
|---:|---|---|---|---|
| 0.10 | 0.072 / 0.142 | 0.105 / 0.191 | 0.155 / 0.249 | 0.219 / 0.315 |
| 0.14 | 0.100 / 0.193 | 0.145 / 0.257 | **0.215** / 0.337 | 0.296 / 0.418 |
| 0.25 | 0.175 / 0.322 | 0.247 / 0.414 | 0.357 / 0.534 | 0.478 / 0.639 |
| 0.50 | 0.338 / 0.562 | 0.447 / 0.684 | 0.609 / 0.813 | 0.762 / 0.897 |
| 1.00 | 0.586 / 0.833 | 0.738 / 0.930 | 0.893 / 0.984 | 0.973 / 0.998 |

**Design consequence.** The tolerable-drawdown statement must be written against
a fixed reference — the starting bankroll of a declared epoch — and paired with
a **halt rule** at that level. "I will not lose more than 20% from any peak" is
not a satisfiable requirement. "I will halt if the modelled bankroll reaches 80%
of the epoch's starting value, and I accept a 5% chance of that" is.

### 5.2 Converting a max-tolerable-drawdown statement into a per-trade constraint

Invert `α^(2/λ−1) = ε`:

```
λ_dd = 2 / ( 1 + ln ε / ln α )
```

| max drawdown | α | ε=0.20 | ε=0.10 | ε=0.05 | ε=0.01 |
|---:|---:|---:|---:|---:|---:|
| 10% | 0.90 | 0.123 | 0.088 | 0.068 | 0.045 |
| 20% | 0.80 | 0.244 | 0.177 | **0.139** | 0.092 |
| 30% | 0.70 | 0.363 | 0.268 | 0.213 | 0.144 |
| 40% | 0.60 | 0.482 | 0.363 | 0.291 | 0.200 |
| 50% | 0.50 | 0.602 | 0.463 | 0.376 | 0.262 |

> **"A 20% maximum drawdown, at no more than 5% probability" implies
> `λ_dd = 0.139` — about one-seventh Kelly.**

Sanity-check that against Section 2.6: `λ = 0.14` earns **26.0%** of the optimal
growth rate. That is the price. It is not negotiable downward without either
accepting a larger drawdown or a higher probability of it — those are the only
two knobs, and the table prices both.

(A pleasing consistency: `λ = 0.14` also produces a *median peak* drawdown of
21.5% over 1,000 bets in the table above. Coincidental — the two are different
statements — but it means the number is not an artefact of choosing the
convenient definition.)

### 5.3 Risk of ruin, and why "there is no ruin under proportional betting" is false here

The standard reassurance is that proportional betting on infinitely divisible
wealth can never reach zero. Three things break that here:

1. **Integer contracts and a minimum lot.** Below the minimum tradeable size the
   strategy is no longer proportional; there is a real absorbing region. This is
   the mechanism by which the "no ruin" theorem stops applying, and it applies at
   a bankroll level far above zero.
2. **Per-contract fees.** Kalshi's fee is `0.07·C·P·(1−P)` (taker), which is
   proportional to contracts, so it scales with position — but the *minimum* is
   one cent, rounded up, which is a fixed cost at small sizes and becomes
   arbitrarily large as a fraction of a shrinking bankroll.
3. **A non-replenished bankroll has no mean reversion in capacity.** With no
   external inflow, capacity is monotone in the worst path taken so far. Combined
   with 5.1's recurrence result, the operative statement is: **run long enough
   and the peak-to-trough drawdown will eventually exceed any bound; the only
   protection is a halt rule at a fixed level, and the halt is permanent unless a
   human re-capitalises.** That decision must be an explicit human act, not a
   parameter.

### 5.4 The halt rule, stated

```
epoch:      declared start, declared starting modelled bankroll W₀, declared λ
soft halt:  W ≤ 0.90·W₀   → new positions blocked; existing positions run off;
                             a mandatory review of β̂ and the residual-correlation
                             estimate before the epoch may resume
hard halt:  W ≤ 0.80·W₀   → programme stops; resumption requires an explicit
                             human decision and a NEW epoch with a NEW W₀
```

The soft halt exists because Section 6.2(c) says the most likely cause of a
drawdown is that model errors have become correlated — which is a *detectable*
condition, and the drawdown is the trigger to go and look.

### 5.5 The safety factor: with noisy `p̂` you pay `λ`'s risk and get `λ_eff`'s growth

The `λ_dd` table assumes `p` is known. It is not. Simulating `λ·f*(p̂)` with
`p̂ ~ N(p_true, s)` and mapping the realised growth back onto the `g(λ)` curve
(400,000 draws per cell):

| λ intended | s=0.00 | s=0.01 | s=0.02 | s=0.03 |
|---:|---:|---:|---:|---:|
| 0.14 | λ_eff 0.140 | 0.140 | 0.138 | 0.138 |
| 0.25 | 0.250 | 0.249 | 0.244 | 0.239 |
| 0.50 | 0.500 | 0.490 | 0.463 | **0.428** |
| 1.00 | 1.000 | 0.800 | 0.601 | **0.420** |

Read this correctly — it is **not** saying that noise makes you safer:

> **Noise gives you the risk profile of `λ` and the growth of `λ_eff < λ`.** At
> `λ = 1.0, s = 0.03` you take the drawdowns of full Kelly (median peak DD 89%
> over 1,000 bets) while earning what an honest `λ = 0.42` would have earned. The
> growth-per-unit-drawdown deteriorates by more than half.

The other reading is the useful one: **at small `λ` the noise sensitivity nearly
vanishes** (0.140 → 0.138 across the whole range of `s`). Low `λ` is not merely
safer; it is *robust*, in the specific sense that its performance stops depending
on a quantity we cannot measure. That robustness — not the drawdown table alone —
is the strongest argument for `λ ≈ 0.15`.

## 6. Track 5 — Correlation and concentration

### 6.1 The ceiling on diversification is `1/ρ`

For `K` equicorrelated positions of equal size, the variance of the average is
`σ²(1/K + (1−1/K)ρ)`, so the **effective number of independent positions** is

```
K_eff = K / (1 + (K−1)·ρ)        →   1/ρ   as K → ∞
```

| K | ρ=0.05 | ρ=0.1 | ρ=0.2 | ρ=0.3 | ρ=0.6 |
|---:|---:|---:|---:|---:|---:|
| 10 | 6.90 | 5.26 | 3.57 | 2.70 | 1.56 |
| 25 | 11.4 | 7.35 | 4.24 | 3.05 | 1.62 |
| 50 | 14.3 | 8.47 | 4.59 | 3.18 | 1.64 |
| 100 | 16.8 | 9.17 | 4.78 | 3.26 | 1.66 |
| ∞ (**ceiling**) | **20.0** | **10.0** | **5.0** | **3.33** | **1.67** |

> **At ρ = 0.3 you can never hold more than 3.33 independent bets, no matter how
> many markets you enter.** Entering 100 markets instead of 25 buys you 0.21 of
> an effective position.

This single fact governs everything downstream: the tail constraint (4.3), the
drawdown budget (Section 5), and — decisively — the required sample size
(Section 8.3), where it appears as the design effect `1 + (m−1)ρ`.

### 6.2 Three distinct correlation mechanisms, only one of which is usually instrumented

**(a) Logical / definitional.** Two markets are about the same proposition, or
one implies the other. "Team A wins" vs "Team A wins by more than 3". "Fed cuts
in March" vs "Fed cuts by June". Correlation is near ±1 and is *deterministic* —
it is not a statistical quantity to be estimated but a fact to be read off the
market definitions.

**(b) Statistical / common factor.** Shared drivers with no logical link. On the
memecoin side: SOL price, aggregate risk appetite, a single launchpad, a single
deployer cluster, one bridge, one RPC-visible liquidity source. On the
prediction-market side: one weather system across many event contracts, one
referee, one court ruling, one macro print. Estimable in principle, but 4.4's
sample-size arithmetic applies — and cluster identity changes faster than the
clusters can be measured.

**(c) Model-error correlation — the one nobody instruments.** Even if the *events*
are independent, **your P&L across them is correlated through your own error**.
If ten positions were taken because the same feature fired, and that feature is
mis-signed in the current regime, all ten lose together. The events were
independent; the losses were not.

This is both the most dangerous mechanism and, uniquely, the **cheapest to
measure**, because it needs no new data:

```
residual r_i = Y_i − p̂_i        (already available for every scored forecast)
ρ_model = corr( r_i , r_j )     across positions grouped by feature / model
                                 version / regime / time bucket
```

> **Recommendation: make residual correlation the primary correlation instrument.**
> It uses data this repository already holds (12,945 scored forecasts), it
> directly captures the failure that actually happens, and it does not require
> identifying the causal factor. Mechanisms (a) and (b) are then upper bounds and
> sanity checks on it.

There is direct local precedent for why (c) matters: per
`docs/EDGE_SELECTION_RETIREMENT_2026_07_10.md`, all six EDGE-SELECTION-001
candidates failed out-of-sample **together**, and the `spread_only` negative
control outperformed them. Six "independent" candidate policies producing one
correlated failure is mechanism (c) in its pure form.

### 6.3 Detecting semantically correlated markets that look independent

Ordered by reliability:

1. **Venue metadata (free, exact).** Same event/series ticker, same underlying,
   same resolution source, same settlement date. Catches most of (a) at zero cost.
   Never skip it in favour of something cleverer.
2. **The existing cross-venue normalizer.** POLY-002 / POLY-PRECISION-001 already
   produce `comparable_market_candidate` labels — "these two markets are about
   the same proposition" is *exactly* a correlation edge, and the precision
   hardening (outcome-side alignment, market-scope gates, entity-anchored
   scorelines) already solved the hard part. **This is a correlation detector
   that already exists in the repository and is currently used only for
   observation.** Its `unresolved_semantic_match` label is, for risk purposes,
   the most important output, not the least — see rule 5 below.
3. **Shared-entity / shared-resolution-source graph.** Named-entity overlap,
   identical resolution source, overlapping resolution windows. Cheap, high
   recall, moderate precision.
4. **Embedding similarity.** High recall, poor precision, and it will happily
   link markets that share vocabulary but not risk. Use it to **propose** edges
   only; never to accept one.
5. **The fail-closed rule, which is the whole design.**

   > **The null hypothesis for correlation is DEPENDENCE, not independence.**
   >
   > A pair whose relationship is unresolved is treated as **correlated**. This
   > inverts the statistical convention deliberately: in inference the cost of
   > wrongly assuming dependence is lost power; in risk the cost of wrongly
   > assuming independence is the blow-up. POLY-PRECISION-001 already applies
   > this logic in the opposite direction (ambiguity degrades to
   > `unresolved_semantic_match`, never to a forced match); the risk layer must
   > read that same label as *correlated until proven otherwise*.

### 6.4 Consuming the Probability Graph

A parallel track builds a Probability Graph of market relationships. What the
sizing layer needs from it, and the one rule that must not be broken:

**Required edge types** (typed, not free-text — the same discipline as
REGISTRY-002A's typed predicate schema):

| edge | meaning | correlation treatment |
|---|---|---|
| `IMPLIES(A→B)` | A true ⇒ B true | treat as one position |
| `MUTUALLY_EXCLUSIVE(A,B)` | at most one true | negative correlation; **may reduce risk, but see the asymmetry rule** |
| `SAME_UNDERLYING(A,B)` | same event, different framing | one position |
| `SHARES_RESOLUTION_SOURCE(A,B)` | same oracle/source/adjudicator | correlated *operationally* even if logically independent |
| `SHARES_FACTOR(A,B,f)` | common driver `f` | ρ from the factor loading |
| `UNRESOLVED_RELATION(A,B)` | a candidate relation the graph could not type | **counts as correlated** |

**Consumption:**

1. **Cluster** = connected component under any edge with confidence ≥ threshold,
   **including `UNRESOLVED_RELATION`**. Clusters, not positions, are the unit the
   book budgets.
2. **Cluster budget.** Each cluster receives a fraction of the total risk budget;
   positions inside it share that fraction. This is what replaces `f_concentration`
   in the `min` (Section 9).
3. **Coverage is a first-class output.** The graph must report, for each market,
   whether it was *analysed and found unrelated* or *not analysed*. These are
   different and the difference is the whole safety property. Absence of an edge
   is not evidence of independence unless coverage says the market was examined.
   Unexamined ⇒ abstention code `GRAPH_COVERAGE_UNKNOWN`.
4. **THE ASYMMETRY RULE — the single most important line in this section:**

   > **The Probability Graph may only ever REDUCE allowed size. It may never
   > increase it.**
   >
   > A graph edge that reveals correlation shrinks the budget. A graph *silence*
   > — or even a positive `INDEPENDENT` finding, or a `MUTUALLY_EXCLUSIVE` edge
   > that mathematically justifies a hedging credit — must **not** be permitted to
   > grow any position. Section 4.3 showed that the entire value of a tail
   > constraint is the diversification credit it grants, and Section 4.4 showed
   > that the correlation estimate behind that credit is the least reliable
   > number in the system. A one-directional graph is wrong in the direction that
   > costs growth; a two-directional graph is wrong in the direction that costs
   > the bankroll.

5. **Version and freshness.** A cluster assignment carries the graph version and
   its computation time. A stale graph is `STALE_STATE`, not a usable graph — the
   memecoin lane's clusters (one launchpad, one deployer) change on a timescale of
   hours.

### 6.5 The aggregate constraint, concretely

```
per cluster c:     Σ_{i ∈ c} f_i  ≤  B_c
book level:        Σ_i f_i        ≤  B_total
                   EVaR_α(book)   ≤  D_max        (Section 4.3, Section 5)
K_eff(book)        ≥  K_min                        (6.1; refuse a book that is
                                                    secretly one position)
```

with `B_c` set so that a total loss of the cluster is survivable under the
Section 5 drawdown statement. Given `λ_dd ≈ 0.14` (5.2) and `K_eff` capped at
`1/ρ`, the arithmetic is unforgiving: **at ρ = 0.3, a book of any size behaves
like 3.33 positions, so a cluster budget of `B_c ≈ λ_dd/K_eff ≈ 0.14/3.33 ≈ 4%`
of the modelled bankroll is the order of magnitude the constraint permits** — not
the 20–25% of correlated exposure that trading folklore suggests.

---

## 7. Track 6 — Abstention as a first-class action

### 7.1 The default

```
DECISION := NO_TRADE  unless every gate below returns PASS.
```

`NO_TRADE` is the initial value, not the else-branch. Every gate must
affirmatively pass; a gate that errors, times out, or cannot evaluate returns
`ABSTAIN`, never `PASS`. This is the same fail-closed discipline the repository
already applies elsewhere — `CryptoDiscoveryService.scan_once` raises
`MissingPolicyError` rather than proceeding without a provider policy, and
CRYPTO-COVERAGE-REPAIR-002 records a band that closed unobserved as a permanent
reported miss rather than interpolating. The sizing layer must be built the same
way.

### 7.2 The reason-code set

Every abstention emits a **typed** code plus its evidence. Free-text reasons are
forbidden for the same reason REGISTRY-002A replaced prose leakage guards with a
closed typed predicate schema: prose is paraphrase-bypassable and cannot be
aggregated.

| code | condition | why it is not negotiable |
|---|---|---|
| `CALIBRATION_UNKNOWN_FOR_REGIME` | fewer than 500 scored forecasts in this regime cell, or `β̂` CI half-width > 0.2 | 3.2/3.7 — without `β̂` you cannot compute `λ_calib`, and Section 2.3 shows unbiased noise alone flips the sign of growth |
| `CALIBRATION_FAILED_FOR_REGIME` | `β̂` significantly < 0 or the regime shows negative skill | the repository has a live instance: tennis (`OUTCOME_SYNC_POST_DRAIN_BASELINE_2026_08`) |
| `LIQUIDITY_BELOW_THRESHOLD` | resting size at the touch < the size implied by `f_actual`, or depth unmeasured | a size you cannot fill is a modelled fill, and a modelled fill presented as an edge is precisely the failure `PAPER_SIMULATION` guards against |
| `EXECUTION_COST_EXCEEDS_EDGE` | `e_net = e − half_spread − fee(q) ≤ 0` | Section 8.2: at `q=0.50` the round-trip wedge is ~3.5–4.75 pp, larger than most real edges |
| `EDGE_BELOW_MINIMUM` | `e_net` below the smallest edge the sample size can ever validate | an edge you cannot measure is an edge you cannot claim; Section 8.4 |
| `TAIL_CONSTRAINT_BINDING` | book EVaR at limit | 4.3 |
| `TAIL_UNVERIFIED_FOR_REGIME` | prospective sample too small to have seen a `π=1/250` regime | 4.4 — "unmeasured" ≠ "satisfied" |
| `CORRELATED_EXPOSURE_AT_LIMIT` | cluster budget `B_c` exhausted | 6.5 |
| `GRAPH_COVERAGE_UNKNOWN` | Probability Graph did not analyse this market | 6.4 rule 3 — absence of an edge is not evidence of independence |
| `STALE_STATE` | quote, forecast, graph, or calibration older than its regime's freshness bound | the repository's own `FOLLOWTHROUGH-001` found stale-or-chasing measurement to be a real, named failure mechanism |
| `MODEL_VERSION_UNTESTED_IN_REGIME` | the model version has no prospective record in this regime | a model validated elsewhere is an untested model here; the EDGE-SELECTION-001 retirement is what this looks like when it is skipped |
| `SIZE_ROUNDS_ABOVE_LIMIT` | integer/lot rounding would push realised fraction above `f_actual` | 2.4 — rounding up is systematically risk-increasing |
| `RESOLUTION_CRITERIA_AMBIGUOUS` | the market's resolution rule is not machine-checkable, or the resolution source is disputed | model-misspecification risk lives entirely outside `p` |
| `REGIME_SHIFT_SUSPECTED` | recent residual distribution differs from the calibration window | 6.2(c): your errors have become correlated |
| `BUDGET_EXHAUSTED` | book-level `B_total` at limit | — |
| `KILL_SWITCH_ACTIVE` | manual halt, or drawdown stop from 5.4 triggered | — |

### 7.3 Properties the abstention layer must have

1. **Abstention is not a failure to be minimised.** The rate of `NO_TRADE` is not
   a KPI to drive down. A system abstaining on 99% of candidates is behaving
   correctly if 99% of candidates fail a gate. Any pressure to "increase
   coverage" must go through changing a threshold explicitly, with the change
   recorded — never through weakening a gate's evaluation.
2. **Abstentions must be counted, typed, and reported.** The distribution of
   reason codes is the single most informative diagnostic the system produces.
   If `EXECUTION_COST_EXCEEDS_EDGE` is 95% of abstentions, the programme's
   problem is the cost model, not the forecaster — and that is worth knowing
   before spending two years collecting trades.
3. **Abstention must be recorded prospectively and count toward the denominator.**
   A prospective record that only contains taken trades is a selected sample and
   cannot support the Section 8 acceptance test. Every candidate — abstained or
   not — is an observation.
4. **Reason codes must be ordered and the first binding one reported**, with all
   binding codes retained. Order: `KILL_SWITCH_ACTIVE` → `STALE_STATE` →
   `CALIBRATION_*` → `MODEL_VERSION_UNTESTED_IN_REGIME` → `GRAPH_COVERAGE_UNKNOWN`
   → `EXECUTION_COST_EXCEEDS_EDGE` → `EDGE_BELOW_MINIMUM` → `LIQUIDITY_*` →
   `TAIL_*` → `CORRELATED_EXPOSURE_AT_LIMIT` → `BUDGET_EXHAUSTED` →
   `SIZE_ROUNDS_ABOVE_LIMIT`. Cheapest and most fundamental first.
5. **No gate may be bypassed by a flag.** A "force" path is how every one of
   these systems eventually fails. If a gate must be relaxed, the threshold
   changes and the change is versioned into the prospective record — which
   invalidates the prior sample for the acceptance test, as it should.

## 8. Track 7 — The definition of ready, and the sample-size answer

The stated bar: *"demonstrable positive expectancy after realistic execution
costs, prospectively measured, with calibrated uncertainty and bounded
probability of ruin."* Four clauses, four gates, and one uncomfortable number.

### 8.1 First, a correction to what the existing evidence shows

`docs/OUTCOME_SYNC_POST_DRAIN_BASELINE_2026_08.md` reports, on all 12,945 scored
forecasts: `baseball_evidence:v1` n=7,983, Brier 0.1868, **skill +0.2286**;
`soccer_evidence:v1` skill +0.2434; overall coverage 100% and the sample
representative.

Those are **skill against the base rate**, not against the market price. Section
2.1's identity is unambiguous about which one matters:

```
achievable growth = KL(p ‖ q)          q = the MARKET price
```

Beating a base rate is nearly free and is worth exactly zero. **None of the
repository's existing skill numbers bear on whether an edge exists.** The one
measurement that *is* against the market — edge-precheck gap follow-through — is
negative, and `docs/EDGE_SELECTION_RETIREMENT_2026_07_10.md` retired all six
pre-registered candidates after they failed out-of-sample while the `spread_only`
negative control outperformed them.

**The prior going into this programme should therefore be that `e_net ≤ 0`.**
That prior is what makes the sample size large: you need enough data to
distinguish a small positive number from zero, starting from a belief that it is
zero.

And a companion number that puts the existing evidence in scale. The MVP-005A
gate crossed on a **paired n = 36**. The minimum edge detectable at n=36
(one-sided 5%, 80% power, `q=0.50`) is:

```
MDE = (z₀.₉₅ + z₀.₈₀)·√(p(1−p)) / √n = 2.4866 × 0.5 / 6 = 0.207
```

> **A sample of 36 can only detect a 20.7 percentage-point edge.** Every
> conclusion so far has been drawn at a resolution roughly 20× coarser than the
> effect being looked for.

### 8.2 Gate 1 — "after realistic execution costs": the wedge

Kalshi taker fee is `0.07 · C · P · (1−P)`, rounded up to the cent; maker is 25%
of that. Combined with a half-spread `s`:

| q | fee/contract | fee (pp) | one-way with `s`=1¢ | gross edge needed for `e_net`=1pp (round-trip) | for 2pp |
|---:|---:|---:|---:|---:|---:|
| 0.10 | $0.0063 | 0.63 | 1.63 | 2.63 | 3.63 |
| 0.25 | $0.0131 | 1.31 | 2.31 | 3.31 | 4.31 |
| **0.50** | **$0.0175** | **1.75** | **2.75** | **3.75** | **4.75** |
| 0.75 | $0.0131 | 1.31 | 2.31 | 3.31 | 4.31 |
| 0.90 | $0.0063 | 0.63 | 1.63 | 2.63 | 3.63 |

(Consistent with the repository's own `kalshi_fee_rate_assumption = 0.07` charged
round-trip at both measurement ends in COST-MODEL-001.)

**At `q = 0.50` you must be right by 3.75 percentage points more often than the
market to net 1 pp.** A 5 pp gross edge — enormous — nets 1.25 pp. This alone
justifies treating `e_net ∈ [0.5 pp, 3 pp]` as the plausible design range, and it
makes `EXECUTION_COST_EXCEEDS_EDGE` the abstention code that will fire most often.

The 1¢ half-spread is optimistic. Where the spread is 3–4¢ (common outside the
most liquid Kalshi series), the wedge exceeds any realistic edge and the correct
answer is that **the venue is untradeable for this strategy**, which is a finding,
not a failure.

### 8.3 Gate 1, continued — how many trades

For a fixed unit stake buying YES at `q` with true probability `p`, the per-trade
return on stake is `+b` with probability `p` and `−1` otherwise. Then

```
mean = (p−q)/q          sd = √(p(1−p))/q          SR = (p−q)/√(p(1−p))
```

The `q` cancels — **per-trade Sharpe depends only on the edge and the outcome
variance.** Required `n` for a one-sided test at α=0.05:

| q | e_net | SR/trade | n @80% | n @90% | n @80%, Bonferroni-20 |
|---:|---:|---:|---:|---:|---:|
| 0.50 | 0.5 pp | 0.01000 | 61,820 | 85,630 | 133,114 |
| 0.50 | **1 pp** | 0.02000 | **15,451** | 21,402 | 33,269 |
| 0.50 | 2 pp | 0.04003 | 3,858 | 5,344 | 8,308 |
| 0.50 | 3 pp | 0.06011 | 1,712 | 2,371 | 3,685 |
| 0.50 | 5 pp | 0.10050 | 613 | 848 | 1,318 |
| 0.90 | 1 pp | 0.03494 | 5,064 | 7,014 | 10,904 |
| 0.90 | 2 pp | 0.07372 | 1,138 | 1,576 | 2,450 |
| 0.10 | 1 pp | 0.03196 | 6,053 | 8,385 | 13,034 |
| 0.25 | 1 pp | 0.02280 | 11,896 | 16,477 | 25,614 |
| 0.75 | 1 pp | 0.02341 | 11,277 | 15,621 | 24,283 |

Extreme prices are *cheaper* to validate for the same absolute edge (less outcome
variance) — but 8.2 shows they also have the smallest fee wedge, so this is the
one place where the two effects point the same way. Mid-price markets are the
worst of both.

**Then the two corrections that are not optional.**

**Correlation.** Section 6.1: `DEFF = 1 + (m−1)ρ` where `m` is cluster size and
`ρ` the within-cluster correlation of *P&L*, which by 6.2(c) is driven by model
error, not by event correlation.

| m | ρ=0.1 | ρ=0.2 | ρ=0.3 | ρ=0.5 |
|---:|---:|---:|---:|---:|
| 5 | 1.40 | 1.80 | **2.20** | 3.00 |
| 10 | 1.90 | 2.80 | 3.70 | 5.50 |
| 20 | 2.90 | 4.80 | 6.70 | 10.50 |

**Multiplicity.** If `k` strategies/regimes/thresholds were examined, the test
level is `α/k`. Twenty hypotheses is conservative for a research programme; the
EDGE-SELECTION-001 prereg alone carried 6 candidates + baseline + control.

**Composite** (`q=0.50`, clusters of 5 at ρ=0.3, 20 hypotheses):

| e_net | base n | × DEFF 2.2 | × DEFF **and** Bonferroni-20 |
|---:|---:|---:|---:|
| 0.5 pp | 61,820 | 136,003 | 292,850 |
| **1 pp** | 15,451 | 33,991 | **73,191** |
| 2 pp | 3,858 | 8,488 | 18,276 |
| 3 pp | 1,712 | 3,765 | 8,107 |

### 8.4 **The sample-size answer**

> ### 15,000 prospective trades to detect a 1 pp net edge under the most favourable assumptions; **30,000–75,000** once correlation and multiplicity are handled honestly; **3,800–18,000** if the true net edge is 2 pp.
>
> **Plan on 30,000–75,000 prospectively-recorded executable decisions.** Anything
> below ~4,000 cannot distinguish a 2 pp edge from noise, and anything below ~600
> cannot distinguish a 5 pp edge — an edge larger than any that has ever survived
> out-of-sample here.

The **minimum detectable edge** table is the more useful planning instrument,
because it answers "what can I conclude from the sample I will actually have?"
(`q=0.50`, one-sided 5%, 80% power, no corrections — so these are optimistic
floors):

| n | MDE (probability points) | interpretation |
|---:|---:|---|
| 36 | **20.7 pp** | the MVP-005A paired sample. Detects nothing real. |
| 100 | 12.4 pp | — |
| 500 | 5.6 pp | larger than any plausible gross edge |
| 1,000 | 3.9 pp | ≈ the round-trip cost wedge at q=0.50 |
| 2,000 | 2.8 pp | |
| 5,000 | 1.8 pp | |
| 10,000 | 1.24 pp | |
| 15,451 | 1.00 pp | |
| 50,000 | 0.56 pp | |
| 100,000 | 0.39 pp | |

**Read the n=1,000 row against 8.2.** A thousand prospective trades can only
detect an edge roughly equal to the round-trip cost of trading. Below about
`n = 2,000` the experiment cannot see anything smaller than its own friction.

### 8.5 The acceptance test, written out

**Preconditions (must all hold before the counting window opens):**

| # | requirement | threshold |
|---|---|---|
| P1 | Pre-registration in the existing registry, with a locked timestamp, typed predicates, and both window ends pinned | per REGISTRY-002A |
| P2 | Regime cells declared **in advance**, with the model version fixed | no post-hoc slicing; `k` for the multiplicity correction is declared here and is binding |
| P3 | Calibration slope `β̂` estimated per regime cell | n ≥ 500 per cell, CI half-width ≤ 0.2 |
| P4 | Cost model fixed and stated: fee formula, half-spread source, fill assumption | every artefact carries the model identifier + modeled-vs-observed basis required by `PAPER_SIMULATION` |
| P5 | `λ` fixed in advance from `λ_calib × λ_dd`; `α_quantile` fixed; halt levels fixed | changing any of them starts a new window |
| P6 | Abstention denominator instrumented | every candidate recorded, typed, whether taken or not |

**Gate 1 — positive expectancy after realistic costs.**

- Primary statistic: mean per-trade net return on stake, `r̄`.
- `n ≥ n_required` from 8.3 at the pre-registered `e_net` and the declared `k`
  and `DEFF` — **`DEFF` estimated from the realised residual correlation, not
  assumed.** If realised `DEFF` exceeds the pre-registered value, `n_required`
  rises and the window extends.
- Reject `H₀: E[r] ≤ 0` at one-sided `α/k`, with a **cluster-robust** standard
  error (clusters = Probability-Graph components, Section 6.4), not an i.i.d. one.
- **And** the point estimate must exceed a pre-registered economically-meaningful
  floor, not merely be significantly positive. Significance at `n = 75,000` is
  compatible with an edge too small to survive a 1¢ change in the spread.

**Gate 2 — prospectively measured.**

- All observations after the registry lock. Zero in-sample observations admitted.
- Abstained candidates counted; the reason-code distribution reported.
- No mid-window parameter change. Any change → new window, prior data discarded
  for this test (retained for exploration only).

**Gate 3 — calibrated uncertainty.**

- Per regime cell: `β̂ ∈ [0.85, 1.15]` with CI half-width ≤ 0.2, **on the
  prospective sample**, not the fitting sample.
- Reliability: ECE ≤ 0.05 with ≥ 8 populated bins per cell.
- One-sided coverage check on `p_conservative` (3.7 rule 4): realised frequency
  exceeds `p_α` at least `1−α` of the time, within binomial CI.
- **Skill measured against the market price `q`, never against a base rate.** The
  headline statistic is mean `KL(p ‖ q)` realised as log-score advantage, and it
  must be positive with the same significance treatment as Gate 1 — it is the
  same quantity (2.1), so consistency between Gate 1 and Gate 3 is itself a
  validity check. **Disagreement between them means the cost model is wrong.**

**Gate 4 — bounded probability of ruin.**

- Declared max drawdown `1−α_dd` and tolerance `ε`; `λ_dd` derived from 5.2, not
  chosen.
- Realised max drawdown from the epoch's `W₀` within the predicted distribution
  (a one-sided check against `α_dd^(2/λ−1)`).
- Book-level EVaR within budget over the whole window, with **the count of tail
  observations reported** (4.5 rule 2).
- `K_eff ≥ K_min` realised, computed from residual correlation.
- No halt breached; if the soft halt fired, the window is void.

**All four gates, or the answer is not ready.** There is no partial credit and no
"promising" state — that vocabulary is already prohibited from authorising
anything elsewhere in this repository, and the same applies here.

### 8.6 Feasibility — the honest arithmetic

Days to accumulate `n` qualifying decisions:

| qualifying trades/day | n=3,858 (2 pp, no corr.) | n=15,451 (1 pp) | n=33,991 (1 pp + DEFF) | n=73,191 (full) |
|---:|---:|---:|---:|---:|
| 1 | 10.6 yr | 42 yr | 93 yr | 200 yr |
| 5 | 2.1 yr | 8.5 yr | 18.6 yr | 40 yr |
| 10 | 1.1 yr | 4.2 yr | 9.3 yr | 20 yr |
| 25 | 154 d | 1.7 yr | 3.7 yr | 8.0 yr |
| 50 | 77 d | 309 d | 1.9 yr | 4.0 yr |
| 100 | 39 d | 155 d | 340 d | 2.0 yr |

And the qualifying rate is *after* abstention. If the gates in Section 7 abstain
on 95% of candidates — a reasonable expectation given that
`EXECUTION_COST_EXCEEDS_EDGE` alone will remove most of them — then **50
qualifying trades per day requires observing 1,000 candidates per day.**

> **The honest verdict: on a single venue at a realistic candidate rate, with a
> 1 pp net edge, the acceptance test takes years. It is feasible in months only
> if (a) the true net edge is ≥ 2 pp, or (b) candidate throughput is in the
> hundreds per day, or (c) both.**
>
> This is not a reason to abandon the programme. It is a reason to **stop
> treating "build the trading system" as the milestone** and start treating
> "raise the candidate rate and shrink the confidence interval" as the milestone,
> because those are what the timeline is actually made of.

### 8.7 What this implies about sequencing

The numbers point at a specific, cheaper order of operations.

1. **Measure skill against the market price now, on the 12,945 forecasts already
   scored.** Compute mean `KL(p ‖ q)` where a contemporaneous `q` exists. This
   costs no new data, needs no trading, and Section 2.1 says it is *the same
   quantity* as growth. If it is not positive, nothing downstream can be.
2. **Fit `β̂` per regime** (3.2). At n ≈ 1,000 per cell you get ±0.16. This is
   ~15× cheaper in sample than the P&L test and it produces `λ_calib` directly.
3. **Measure residual correlation** (6.2c) on the same existing data. It yields
   `DEFF`, which determines the sample size of everything after it — so measuring
   it *first* is the difference between planning for 15,000 and discovering at
   trade 15,000 that you needed 73,000.
4. **Measure the cost wedge and the abstention distribution** before any trade.
   If `EXECUTION_COST_EXCEEDS_EDGE` fires on 99% of candidates, the programme's
   binding constraint is venue selection and the whole sizing layer is premature.
5. **Only then** open a prospective window, with `n_required` computed from
   measured `DEFF` and a pre-registered `e_net` — not assumed.

**On sequential testing.** SPRT / anytime-valid confidence sequences (e-values)
permit continuous monitoring without inflating type-I error, and reduce *expected*
sample size under a strong alternative by roughly 30–50%. They do **not** change
the order of magnitude here, and under a weak alternative — which the negative
prior of 8.1 makes the relevant case — they require **more** samples than the
fixed-sample test, because the price of optional stopping is paid up front. Use
them for the *safety* property (the ability to stop early on evidence of harm),
not as a way to make the number smaller.

**On the `PAPER_SIMULATION` requirement.** The boundary's demand that every
modeled fill carry a model identifier and a modeled-vs-observed basis is not
bureaucratic overhead — at these sample sizes it is the only thing that makes the
programme meaningful. Thirty thousand modeled fills built on an optimistic fill
assumption measure the assumption, not the edge, and would produce a confident
positive result. The requirement that the basis travels *with the number* is
precisely what stops 30,000 such observations from being mistaken for evidence.

## 9. Challenge to the formula

```
f_actual = min( λ·f_Kelly(p_conservative), f_liquidity, f_CVaR, f_concentration, f_drawdown )
```

The form is **60% right**. Two of the five terms do not belong where they are,
one is a duplicate, and the combinator cannot express the most important output.

### 9.1 `min(...)` can never return `NO_TRADE` — the structural gap

A `min` over positive quantities is positive. The formula as written has **no way
to abstain**, yet Section 7 argues abstention is the default and the most
frequently correct action. Every term would have to be capable of returning 0,
and "the liquidity cap happened to be 0" is a poor way to encode "calibration is
unknown for this regime, so we decline".

**Fix: an explicit gate in front, returning a typed reason-code set, evaluated
before any sizing arithmetic runs.** Sizing is only reached if the gate passes.

### 9.2 `f_CVaR` does not belong in a per-position `min`

Section 4.2: the loss distribution of a single binary position is two-point, so
`CVaR_α = f` exactly for any `α ≤ 1−p`, and **negative** (constraint vacuous) for
`α > 1−p`. As a per-position term it is a flat cap with a misleading name, and
its behaviour flips on the sign of `α − (1−p)` with no warning in the notation.

**Fix: remove it from the per-position `min`; impose `EVaR_α(book) ≤ D_max` as a
book-level constraint** (4.3 for why EVaR).

### 9.3 `f_concentration` is not a cap at all — it is a joint allocation

`min` takes a function of one position. "These five positions together must not
exceed `B_c`" is not expressible that way. Any per-position translation
(`B_c / 5`, say) is either wasteful when the positions have different edges or
wrong when a sixth arrives.

**Fix: a joint allocation step over clusters** — a small convex program, not a
`min`. Section 9.6.

### 9.4 `f_drawdown` is already inside `λ` — either a duplicate or double-counting

`λ = λ_calib × λ_dd` (3.8), and `λ_dd` **is** the drawdown constraint (5.2). A
separate `f_drawdown` term applies it twice, or means something different from
what it says.

There is, however, a genuinely distinct drawdown term the formula is missing: a
**state-dependent** multiplier that tightens as wealth approaches the halt floor.
This is the Grossman–Zhou insight — size on the *excess over the stop level*, not
on total wealth:

```
κ(W) = clamp( (W − W_halt) / (W₀ − W_halt), 0, 1 )
```

`κ = 1` at the epoch start, `κ = 0` at the hard halt. This is a real, non-
redundant term and it is what `f_drawdown` should have been.

### 9.5 Three terms the formula omits entirely

**(a) A minimum size, `f_min`.** Below it, discretisation and fees dominate and
the correct action is `NO_TRADE`, not a tiny position (2.4). Without `f_min` the
system takes thousands of trades whose fees exceed their edge — and, worse,
those trades enter the Section 8 denominator and drag the measured expectancy
toward the cost wedge.

**(b) Time.** `g` is per *resolution*, not per unit time. An edge of 0.005 nats
that resolves in an hour and one that resolves in six months are different
propositions by a factor of ~4,000. Capital is committed for the duration, so
the quantity to be ranked and budgeted is:

```
growth rate  =  g(f) / Δt        Δt = expected time to resolution
```

Its omission is the single largest *economic* gap in the stated form. It also
changes the answer to "which position gets the cluster budget" completely, and it
interacts with Section 8: a strategy with a 2 pp edge on six-month markets cannot
accumulate 30,000 observations in any human timeframe, so long-horizon markets
are **unvalidatable** regardless of their edge.

**(c) Target vs increment.** The formula is silent on whether `f_actual` is a
target position or an order size. It must be a **target**, with a **no-trade
band** around it — every rebalance pays the full cost wedge of 8.2, and a system
that chases a moving target pays it repeatedly for no edge.

### 9.6 The revised form

```
0. GATE          codes ← evaluate_gates(market, forecast, book, graph, state)
                 if codes ≠ ∅:  return NO_TRADE(codes)

1. BELIEF        p_cal  ← σ( â_r + β̂_r · logit(p̂) )        per-regime recalibration (3.2)
                 p_con  ← Q_α( dispersion from bootstrap / ensemble / residuals )   (3.4, 3.6)

2. CEILING       λ      ← β̂_r × λ_dd × κ(W)                  (3.8, 5.2, 9.4)
                 f_ceil ← min( λ · (p_con − q)/(1 − q),
                               f_liquidity,       # fillable depth at the touch
                               f_position_cap )   # a stated flat cap

3. ALLOCATE      choose { f_i } to maximise   Σ_i  g(f_i; p_con,i, q_i) / Δt_i
                 subject to   0 ≤ f_i ≤ f_ceil,i
                              Σ_{i∈c} f_i ≤ B_c      ∀ clusters c        (6.4, 6.5)
                              Σ_i f_i     ≤ B_total
                              EVaR_α(book) ≤ D_max                        (4.3)
                              K_eff(book) ≥ K_min                         (6.1)
                 — convex: objective concave, EVaR convex, rest linear.

4. DISCRETISE    if realised_fraction(round(f_i)) > f_i:  NO_TRADE(SIZE_ROUNDS_ABOVE_LIMIT)
                 if f_i < f_min:                          NO_TRADE(EDGE_BELOW_MINIMUM)
                 if |f_i − f_current,i| < no_trade_band:   HOLD

5. EMIT          f_i, the binding constraint, slack on every other constraint,
                 λ's three factors separately, p_con and its dispersion source,
                 Δt_i, cluster id + graph version, model identifier,
                 and the modeled-vs-observed basis for every input.
```

Step 5 is not decoration. A size with no record of *why* it is that size cannot
be audited, cannot be debugged when the drawdown comes, and — under the
`PAPER_SIMULATION` amendment — **may not legally be produced at all** without the
model identifier and modeled-vs-observed basis travelling with the number.

### 9.7 The deepest challenge: the whole layer may be premature

Sections 8.1 and 8.6 together say something uncomfortable. The prior is that
`e_net ≤ 0`; the measurement that would move that prior needs 30,000–75,000
prospective observations; and none of that measurement requires a sizing layer.
**A sizing layer is the machinery for exploiting an edge whose existence has not
been established, and building it first inverts the dependency.**

The defensible reasons to design it now anyway — and they are real — are: it
tells you what to *instrument* (residual correlation, abstention codes, cluster
identity, `Δt`) before the prospective window opens rather than after; it
produces the `λ` and `n_required` numbers that determine whether the programme is
feasible at all; and it establishes that `Δt` and `DEFF` must be measured up
front, which is the difference between a two-year window and a twenty-year one.

Those are design-ahead reasons. They are not build reasons, and this document is
not a build authorisation.

---

## 10. Consolidated term reference

| term | concrete definition | value / source | status |
|---|---|---|---|
| `f_Kelly(p,q)` | `(p−q)/(1−q)` | derived §2.1 | ceiling only, never the allocator (§2.7) |
| `p_conservative` | `Q_α(π)` of a **measured** dispersion (bootstrap refit / ensemble disagreement / regime residuals), applied after log-odds recalibration | `α ∈ [0.10, 0.25]`; `α` = P(overbet vs true Kelly), exactly (§3.6) | dispersion source must be measurable — self-reported posterior width is unfalsifiable (§3.4) |
| `λ_calib` | Cox calibration slope `β̂`, per regime, in log-odds space | needs n ≥ 500/cell for ±0.22; ±0.16 at n=1,000 (§3.2) | learned |
| `λ_dd` | `2 / (1 + ln ε / ln α_dd)` | **0.139** for "20% max DD at 5%" (§5.2) | derived from a stated tolerance |
| `κ(W)` | `clamp((W − W_halt)/(W₀ − W_halt), 0, 1)` | Grossman–Zhou form (§9.4) | state-dependent |
| `λ` | `λ_calib × λ_dd × κ(W)` | ≈ 0.14 at epoch start with the above | validated on a **lower confidence bound** of realised growth, never the point estimate (§3.8) |
| `f_liquidity` | fillable fraction at the touch within the assumed slippage | venue depth; unmeasured depth ⇒ abstain | hard cap |
| `f_CVaR` | **removed from per-position.** Book-level `EVaR_α(book) ≤ D_max` | §4.2, §4.3 | relocated |
| `f_concentration` | **replaced by** cluster budgets `Σ_{i∈c} f_i ≤ B_c` | `B_c ≈ λ_dd / K_eff ≈ 4%` at ρ=0.3 (§6.5) | joint allocation, not a cap |
| `f_drawdown` | **absorbed into `λ_dd`**; the non-redundant part is `κ(W)` | §9.4 | de-duplicated |
| `f_min` | smallest size where fees + discretisation do not dominate | new term (§9.5a) | gate, not a cap |
| `Δt` | expected time to resolution; objective is `g/Δt` | new term (§9.5b) | the largest economic omission |
| `NO_TRADE` | typed reason-code set, evaluated **before** sizing | 16 codes (§7.2) | the default |

**The three numbers to remember:**

| | |
|---|---|
| `λ ≈ 0.14` | one-seventh Kelly, for a 20% max drawdown at 5% probability — and it earns 26% of the optimal growth rate |
| `K_eff ≤ 1/ρ` | at ρ=0.3, no book ever holds more than 3.33 independent bets |
| **n ≈ 30,000–75,000** | prospective decisions to demonstrate a 1 pp net edge honestly |

---

## 11. Evidence ledger — VERIFIED vs INFERRED

### 11.1 VERIFIED — derived or computed in this document, reproducible

| claim | how |
|---|---|
| `f* = (p−q)/(1−q)` | algebraic derivation, §2.1 |
| `g(f*) = KL(p‖q)`, i.e. max growth = log-score advantage over the market | algebra + numerical agreement to 6 dp in all 6 parameter blocks, §2.1 |
| 2× Kelly ⇒ zero growth; the bias producing it is `δ = e` | simulation, §2.2 |
| At `q=0.90`, unbiased noise `s=0.03` gives **negative** expected growth on a real +3 pp edge | 400,000-draw MC, §2.3 |
| Estimation-error penalty carries a `1/(1−q)²` factor | second-order expansion + the 100× gap between q=0.50 and q=0.90 rows, §2.3 |
| `λ=0.5` → 74.9% of growth at 50% of log-return volatility | 20,000 paths × 1,000 bets, §2.6 (agrees with published MacLean–Ziemba–Blazenko) |
| Full Kelly: median peak drawdown 89.4%, `P(DD>50%) = 1.0000` across 20,000 paths, with `p` known exactly | simulation, §2.6 |
| One-period Bayesian Kelly ≡ Kelly at the posterior mean, for a binary contract | algebra (`g` is linear in `p`), §3.1 |
| `P(f(p_α) > f*(p_true)) = α` exactly, because `f*` is linear in `p` | algebra, §3.6 |
| Calibration-slope CIs: ±0.16 at n=1,000; ±0.045 at n=12,945 | Fisher-information simulation, §3.2 |
| `VaR_α = CVaR_α = f` for a single binary whenever `α ≤ 1−p`; CVaR goes **negative** when `α > 1−p` | analytic + 300,000-draw MC, §4.2 |
| `CVaR95 / Σf → 1.000` as ρ rises; 0.290 at ρ=0, K=25 | 300,000-draw MC, §4.3 |
| EVaR ≥ CVaR in every simulated case, by 10–27% | §4.3 |
| CVaR99 at n=250 rests on 2 observations; small-`n` bias is **downward** (−8.1% at n=50) | 800 bootstrap replicates vs 10⁶-draw truth, §4.4 |
| With a 0.6% common-shock regime, 59% of n=50 samples contain zero instances of it | §4.4 |
| `α^(2/λ−1)` matches the simulated **initial-wealth** barrier and saturates | 12,000 paths at N=250/1,000/5,000, §5.1 |
| Peak-to-trough drawdown is unbounded at every `λ` (reflected BM with negative drift is positive-recurrent); simulated `P(DD>50%)` → 1.000 at λ=0.5 | argument + simulation, §5.1 |
| `λ_dd = 0.139` for "20% max DD at ε=0.05" | inversion of the closed form, §5.2 |
| With noisy `p̂` you take `λ`'s risk and earn `λ_eff < λ`'s growth; at small `λ` the sensitivity nearly vanishes | 400,000-draw MC, §5.5 |
| `K_eff = K/(1+(K−1)ρ)`, ceiling `1/ρ` | algebra, §6.1 |
| Per-trade Sharpe `= (p−q)/√(p(1−p))`; `q` cancels | algebra, §8.3 |
| `n = 15,451` (1 pp, 80%, one-sided 5%); 33,991 with DEFF 2.2; 73,191 with Bonferroni-20 | standard power formula, §8.3–8.4 |
| MDE at n=36 is **20.7 pp**; at n=1,000 it is 3.9 pp ≈ the round-trip cost wedge | §8.4 |

### 11.2 VERIFIED against repository documents

| claim | source |
|---|---|
| 12,945 forecasts, all scored, coverage 100%, sample representative | `docs/OUTCOME_SYNC_POST_DRAIN_BASELINE_2026_08.md` (lines 115–127) |
| Reported skill figures are **against the base rate**: baseball +0.2286 (n=7,983), soccer +0.2434, tennis **negative** and `credible_current_finding` | same, results tables |
| Soccer's earlier +0.8845 skill was `contradicted_by_expanded_sample` — a 34-observation artefact | same |
| All six EDGE-SELECTION-001 candidates retired after out-of-sample failure; `spread_only` negative control outperformed | `docs/EDGE_SELECTION_RETIREMENT_2026_07_10.md` |
| MVP-005A gate crossed on paired n=36 | `docs/SAFETY_BOUNDARIES.md`, EV-calculation row |
| `kalshi_fee_rate_assumption = 0.07`, charged round-trip at both measurement ends | `docs/SAFETY_BOUNDARIES.md`, COST-MODEL-001 bullet |
| Portfolio sizing / dollar EV / trade recommendations / order placement all forbidden with no implementation surface | `docs/SAFETY_BOUNDARIES.md` |
| `PAPER_SIMULATION` requires a model identifier **and** a modeled-vs-observed basis on every artefact, not satisfiable by a header or docstring | `docs/SAFETY_BOUNDARIES.md`, SAFETY-BOUNDARY-ROUTE-QUOTE-001 |
| `BANNED_IDENTIFIER_FRAGMENTS` includes `kelly`, `position_siz`, `portfolio`, `expected_value`, `paper_trad`; the audit is deliberately not amended by the boundary doc | same |
| POLY-002 / POLY-PRECISION-001 produce `comparable_market_candidate` and degrade ambiguity to `unresolved_semantic_match` rather than forcing a match | `docs/SAFETY_BOUNDARIES.md`, POLY bullets |
| REGISTRY-002A replaced prose leakage guards with a closed typed predicate schema | `docs/PROSPECTIVE_EXPERIMENT_REGISTRY_002A.md` |

### 11.3 VERIFIED via external sources (primary where available)

| claim | source |
|---|---|
| Risk-constrained Kelly; drawdown risk defined as `P(wealth ever drops to a fraction of its **initial** value)`; convex bound; single risk-aversion parameter | Busseti, Ryu & Boyd, *Risk-Constrained Kelly Gambling* — https://arxiv.org/abs/1603.06183 · https://web.stanford.edu/~boyd/papers/pdf/kelly.pdf |
| EVaR: coherent, tightest Chernoff bound on VaR and CVaR, KL-divergence dual, tractable where CVaR is not | Ahmadi-Javid (2012), *JOTA* 155(3):1105–1123 — https://link.springer.com/article/10.1007/s10957-011-9968-2 · commentary: https://arxiv.org/pdf/1504.00640 |
| CVaR for general loss distributions; the `min_t { t + (1/α)E[(L−t)⁺] }` convex form | Rockafellar & Uryasev — https://sites.math.washington.edu/~rtr/papers/rtr187-CVaR2.pdf |
| Half-Kelly ≈ 75% of growth at ~50% of volatility (MacLean–Ziemba–Blazenko 1992); Chopra–Ziemba 20:2:1 error sensitivity (means dominate); a 10% error in the mean can produce ~50% overbetting; overbetting penalty far exceeds underbetting | Ziemba, *Using the Kelly Criterion for Investing* — https://webhomes.maths.ed.ac.uk/mckinnon/blackouts/StochOptFinanceAndEnergySpringer/Chap1_KellyZiemba.pdf |
| Full vs fractional Kelly medium-term simulations | Thorp — http://www.edwardothorp.com/wp-content/uploads/2016/11/KellySimulationsNew.pdf |
| Estimation risk in Kelly investing | https://arxiv.org/html/2508.18868v1 |
| Modified Kelly criteria under parameter uncertainty | Chu, Wu & Swartz — https://www.sfu.ca/~tswartz/papers/kelly.pdf |
| Kelly generalisation under temporal correlation | https://arxiv.org/pdf/2003.02743 |
| Grossman–Zhou: size on the excess over the moving stop level; Klass & Nowicki (2005) show it is not always optimal in discrete time | https://perso.math.u-pem.fr/elie.romuald/elie_files/et06.pdf · https://www.sciencedirect.com/science/article/abs/pii/S0167715205001641 · Kelly with a stop-loss rule: https://arxiv.org/pdf/1311.2550 |
| Drawdown-constrained Kelly under parameter uncertainty (Bayesian Grossman–Zhou) | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6942459 |
| Empirical CVaR estimator is biased though consistent; performs materially worse than VaR under fat tails where tail observations are sparse | https://arxiv.org/pdf/1908.07232 · https://kramer.ucsd.edu/img/pubs/Conditional-Value-at-Risk-Estimation-Reduced-Order-Models-Heinkenschloss-Kramer-Takhtaganov-Willcox-2018.pdf |
| g-entropic / entropy-based risk measures, class containing both CVaR and EVaR | https://arxiv.org/pdf/1801.07220 |
| CFTC Core Principle 5 position limits; aggregation across similar event contracts with the same underlying reference | https://www.federalregister.gov/documents/2026/03/16/2026-05105/prediction-markets |

### 11.4 VERIFIED only against **secondary** sources — treat as provisional

| claim | caveat |
|---|---|
| Kalshi taker fee `= 0.07 · C · P · (1−P)`, rounded up to the cent; maker fee = 25% of taker; max 1.75¢/contract at P=0.50 | Consistent across three independent secondary sources — https://marketmath.io/platforms/kalshi · https://pm.wiki/learn/kalshi-fees-explained · https://whirligigbear.substack.com/p/makertaker-math-on-kalshi — and consistent with the repository's own `kalshi_fee_rate_assumption = 0.07`. **NOT verified against Kalshi's official fee schedule in this session** (the official PDF returned HTTP 429). Every cost figure in §8.2 inherits this caveat. Verify before the acceptance test's cost model is locked. |
| "Limit total correlated exposure to 20–25% of capital" as practitioner guidance | https://www.predictengine.ai/blog/common-market-making-mistakes-on-prediction-markets-explained — low-quality source, quoted only to note that §6.5's arithmetic gives **~4%**, an order of magnitude tighter than folklore |

### 11.5 INFERRED — judgment, assumption, or extrapolation. Not established.

| claim | basis | how to settle it |
|---|---|---|
| Plausible net edge range `e_net ∈ [0.5, 3] pp` | cost wedge (§8.2) minus the repository's negative follow-through evidence (§8.1) | it *is* the thing the acceptance test measures |
| Design values `m = 5, ρ = 0.3` ⇒ `DEFF = 2.2` | illustrative; every sample size in §8.4 scales linearly with this | **measure residual correlation on the existing 12,945 forecasts** — cheapest high-value action in the document |
| `k = 20` hypotheses for the multiplicity correction | conservative guess | fixed by pre-registration (P2) |
| ~95% abstention rate | from the cost wedge exceeding most edges | measurable before any trade (§8.7 step 4) |
| `α_quantile ∈ [0.10, 0.25]` | judgment, from the over/under-betting asymmetry of §2.6 | coverage check of §3.7 rule 4 |
| `β̂ ∈ [0.85, 1.15]` acceptance band; `n ≥ 500` per regime cell | judgment, anchored to the §3.2 CI table | — |
| **EVaR is *not* more robust to estimate than CVaR** | mechanism argument (the MGF at the optimising `z` is dominated by the same tail points); **not simulated here** | a bootstrap comparison of EVaR vs CVaR estimation error — a genuine gap in this document |
| Sequential tests cut expected `n` by 30–50% under a strong alternative and cost more under a weak one | general SPRT/e-value theory; not computed for this payoff | compute for the actual payoff before relying on it |
| `B_c ≈ 4%` of bankroll per cluster | `λ_dd / K_eff` at ρ=0.3 — arithmetic is exact, the inputs are the assumptions above | follows from measuring ρ |
| That `Δt` (§9.5b) is the largest economic omission | reasoning, not measurement | measure the realised distribution of time-to-resolution per venue |
| Soft/hard halt at 0.90/0.80 `W₀` | judgment; the *form* is forced by §5.1, the levels are not | a stated human risk tolerance, per §5.2's table |

### 11.6 Known gaps in this document

1. **No multi-asset / continuous-payoff Kelly.** The memecoin lane needs
   `E[R/(1+f·R)] = 0` over a full return distribution; §2.4 argues Kelly should
   not be used there at all, but does not develop the alternative beyond "flat
   cap + tail constraint".
2. **EVaR estimation error is asserted, not measured** (11.5).
3. **`Δt` is identified as a missing term but not developed** into a ranking rule.
4. **The joint allocation of §9.6 step 3 is specified but not solved** for any
   concrete book; its convexity is stated from the structure of the constraints,
   not verified on an instance.
5. **The Kalshi fee formula is secondary-sourced** (11.4) and every cost number
   depends on it.
6. **Nothing here is validated against live data**, because doing so would
   require capabilities the safety boundary forbids. This document is
   design-ahead research and its numbers are properties of models, not
   measurements of markets.
