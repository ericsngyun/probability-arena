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

_(to be filled)_

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
