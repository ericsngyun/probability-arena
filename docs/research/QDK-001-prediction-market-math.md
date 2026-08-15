# QDK-001 — Belief → Trade Decision Mathematics for Prediction Markets

**Status:** RESEARCH ONLY. No production code, no trading, no live execution.
**Branch:** `QDK-001-prediction-market-math`
**Date opened:** 2026-08-14
**Scope:** Kalshi-facing binary markets (CLOB), with cross-venue extension.

## Thesis under test

> The naive pipeline `edge = p_agent − p_market` → Kelly-size is insufficient.

This document tries to establish *why*, with derivations, and to replace the naive
pipeline with a set of **competing candidates** subject to a prospective bake-off —
not with a new settled answer.

## Evidence labelling convention

Every substantive claim in this document carries one of:

- **[VERIFIED]** — read in the cited primary source; quote or equation checked.
- **[INFERRED]** — derived here, or a standard result reconstructed from memory of
  the field; correct as far as the derivation goes but not attributable to the cite.
- **[UNVERIFIED]** — asserted upstream, not confirmed. Do not build on it.
- **[REFUTED]** — checked and found wrong as stated.

---

## 1. Citation audit

| Cite | Claimed | Status |
|---|---|---|
| arXiv 2607.06166 | "When do prophets profit in prediction markets?" — profit decomposition, proper betting | **[VERIFIED]** exists; decomposition and strategy confirmed *verbatim* |
| arXiv 2602.19520 | ~353M Kalshi/Polymarket trades, conditional calibration | **[VERIFIED]** exists — see §6 |
| arXiv 2604.18576 | hierarchical calibration across heterogeneous sources | **[VERIFIED]** exists — see §5 |

### 1.1 arXiv 2607.06166 — VERIFIED, and stronger than the brief suggested

- URL: https://arxiv.org/abs/2607.06166 · HTML: https://arxiv.org/html/2607.06166v1
- Authors: Anri Gu, Nicole Kagan, Alec Sun, Jibang Wu, Haifeng Xu (U. Chicago). Submitted 2026-07-07, revised 2026-07-11.

Every element of the decomposition handed to us checks out **verbatim**, including the
notation. Theorem 8, Equation (1):

> π(**s**\*, **p**\*) = [ S(**p**;**p**\*) − S(**q**;**p**\*) ]  +  [ D_G(**q**,**p**) ]  −  [ L_ρ(**s**\*;**q**) ]
> &nbsp;&nbsp;&nbsp;&nbsp;score gap &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; Bregman divergence &nbsp;&nbsp;&nbsp; liquidity loss

Definition 7:

> Under a proper scoring rule S with potential function G, the **proper betting strategy**
> for a forecast **p** and a market price **q** is the position vector
> **s**_G(**p**,**q**) := ∇G(**p**) − ∇G(**q**).

Notation (paper's, which we adopt): **p** = forecaster's prediction, **q** = market price,
**p**\* = unobservable ground-truth distribution, G = strictly convex potential, L_ρ =
integrated loss from the price-impact function ρ (for a CLOB, ρ is piecewise constant and
read directly off the order book).

**The brief's framing needs one correction.** The brief presents the decomposition as the
explanation for *why an accurate forecaster loses money*. It is not. Read the signs:
D_G(**q**,**p**) ≥ 0 always (Bregman), and the score gap is > 0 exactly when the forecast
beats the market. So under **proper betting**, the decomposition is a *positive* result —
the only route to a loss with a genuine accuracy edge is L_ρ exceeding the sum of the
other two terms. The paper's own condition is exactly that: **[VERIFIED]**

> Therefore, if D_G(**q**,**p**) ≥ L_ρ(**s**\*,**q**) — i.e. the divergence offsets liquidity
> loss — then proper betting is robustly profitable.

The "accurate forecaster loses money" result is a *separate* set of counterexamples about
*other* strategies (§2.1), not a consequence of Equation (1).

### 1.2 Supporting results in the same paper (all [VERIFIED])

- **Lemma 9** (the engine; holds pointwise for realized outcome *y*, and for scoring rules
  that need not even be proper):
  **s**_G(**p**,**q**) · (**1**_y − **q**) = [S(**p**,y) − S(**q**,y)] + D_G(**q**,**p**).
  Because it is pointwise, it averages over realized outcomes — so the guarantee transfers
  from unobservable expected scores to **empirical** scores (Corollary 17).
- **Theorem 13**: proper betting is *essentially the only* robustly profitable strategy
  (up to positive rescaling λ**s**_G and constant shift **s**_G + λ**1**).
- **Proposition 14**: a scoring rule is proper **iff** its associated betting strategy is
  robustly profitable. A genuine characterization, not just an implication.
- **Corollary 15**: in an AMM, D_G(**q**,**p**) and L_ρ *cancel exactly*, recovering the
  classical Hanson result. This is the paper's stated reason the classical theory
  mispredicts CLOB venues like Kalshi.
- **Corollary 19** (bid-ask): with ask q⁺_k and NO-ask q⁻_k, and q⁺_k + q⁻_k ≥ 1, proper
  betting **buys YES when p_k > q⁺_k, buys NO when p_k < 1 − q⁻_k, and does nothing when
  1 − q⁻_k ≤ p_k ≤ q⁺_k.** The abstain band falls out of the theory; we do not have to
  invent it.
- **Corollaries 21/22** (multi-period): a *fundamental* strategy needs a per-round edge
  against the unobservable **p**\*; a *momentum* strategy needs an edge only against the
  next market price **q**^{t+1} — which the paper notes is "an operationally observable
  condition."

### 1.3 What in 2607.06166 deserves skepticism

- **The headline live result is n=1.** $200 of capital, 26 days (Apr/May 2026), 236 orders
  across 129 markets, one model (Gemini 3), one rule (Brier), no control arm.
  A Sharpe of 3.35 computed on ~26 daily returns has an enormous standard error
  (SE ≈ √(1/26) ≈ 0.2 in daily units → roughly ±3 annualized). **Treat +80.33% as an
  existence proof that the pipeline runs, not as an effect size.** **[INFERRED]**
- The live agent used an **ex-ante eligibility filter** excluding "contracts with ambiguous
  resolution criteria and settings known to impair LLM forecasting performance." The
  returns are therefore conditional on a selection step the paper does not fully specify.
- Offline ROI came from **zero-price-impact simulation** (L_ρ = 0). The whole difficulty in
  a CLOB is L_ρ. Table 2's numbers are upper bounds. **[INFERRED]**
- Table 2's Kelly column (−99.9% for four of five models) uses **leverage when the Kelly
  ratio exceeds 1** (Appendix D.2). That is levered Kelly, which is not the Kelly most
  people mean. It overstates the case against unlevered Kelly. Flagging this because the
  brief invites us to write Kelly off, and this particular number does not earn that.

---

## 2. Track 1 — Forecasting accuracy is not trade profitability

### 2.1 The claim is TRUE, and both directions fail

The link between accuracy and profit breaks in **both** directions. Verbatim from the paper:

**Example 4 — accurate forecaster + Kelly can lose. [VERIFIED]**
**p** = (0.64, 0.32, 0.04), **q** = (0.80, 0.10, 0.10), **p**\* = (0.10, 0.10, 0.80).
Under the quadratic rule ‖**p**\*−**p**‖ < ‖**p**\*−**q**‖, so the forecaster *beats the
market*. Many-outcome Kelly spends p_i to buy p_i/q_i shares of contract i, giving
expected profit (**p** ⊘ **q**) · (**p**\* − **q**) < 0. The paper's diagnosis:

> The intrinsic reason of this loss is that the Kelly criterion is derived by assuming its
> forecast is the ground truth, which in practice can be very off from the forecast.

**Example 5 — accurate forecaster + max-margin can lose. [VERIFIED]**
**p** = (0.61, 0.39, 0.00), **q** = (0.10, 0.89, 0.01), **p**\* = (0.05, 0.10, 0.85).
Forecaster beats the market; betting the largest |p_k − q_k| yields negative profit.
*This is exactly the naive `edge = p_agent − p_market` rule in the brief.*

**Example 6 — inaccurate forecaster can profit. [VERIFIED]**
**p** = (0.9, 0.1), **q** = (0.5, 0.5), **p**\* = (0.6, 0.4). The forecaster is *worse* than
the market under the quadratic rule, yet betting outcome 1 has positive expected profit.

So the brief's thesis survives contact with the literature. Note the crucial extra content
of Example 6: **positive P&L is not evidence of forecasting skill.** Any evaluation that
scores our agent on realized P&L alone can be fooled by a directional-beta strategy with no
edge at all. This is an argument for measuring ΔS and D separately, which the decomposition
lets us do.

### 2.2 The intuition, stated correctly

Rearranging Lemma 9: profit under proper betting is driven by being **sufficiently
different from the market while remaining sufficiently accurate**. The paper says exactly
this:

> a profitable forecast is not merely about being accurate (i.e., having big score gap),
> but is about sufficiently different from the market yet still remains sufficiently accurate.

Table 3 shows this empirically and it is startling. Under the Brier rule, GPT-5.2 (Base)
posts ΔS = **−108.4** (far *worse* than the market) and D = **+112.7**, netting **+4.3% ROI**.
Grok 4.1 Fast under Log: ΔS = −20.3, D = +23.8, ROI **+3.5%**. **[VERIFIED]**

The architectural consequence for us is uncomfortable and important:

> **A large fraction of proper-betting P&L can come from the divergence term, which is
> pure disagreement with the market and carries no accuracy content whatsoever.**

D_G(**q**,**p**) is a *convexity rent* — it is positive whenever **p** ≠ **q**, regardless of
whether **p** is any good. It is the CLOB analogue of the AMM's market-maker subsidy, and in
an AMM it is exactly cancelled by L_ρ (Corollary 15). In a CLOB with a thin book, L_ρ can
exceed it. **Any backtest we run at L_ρ = 0 will therefore report a divergence rent that
does not exist in live trading.** This is the single largest backtest-to-live gap in the
design. **[INFERRED, from Corollary 15 + the paper's own zero-impact caveat]**

### 2.3 The theorem is an accounting identity, not a signal

The load-bearing limitation, which the paper is honest about:

> In practice the ground truth **p**\* is unobservable, therefore a forecaster's advantage
> over the market on a single event in expectation can never be validated.

Theorem 8 is *conditional*: **if** you have an edge, proper betting converts it to profit.
It tells you nothing about **whether** you have one. Equation (1) is an ex-post accounting
identity over an unobservable term. Concretely:

- Proper betting is the right **transform**. It is not a **filter**.
- It cannot be used to decide *whether* to trade a market, only *how* to size and direct a
  trade once you have decided. (The one exception is the bid-ask abstain band of
  Corollary 19, which *is* a filter and which we should adopt.)
- Everything about *whether* we have an edge is pushed onto Tracks 4–6: calibration,
  conditional calibration, and coherence. **Proper betting does not reduce our need for
  those; it increases it**, because it will faithfully convert a *negative* edge into a loss
  just as reliably (Table 4, Regime B: −20% to −83% ROI across all personas and rules).

### 2.4 Verdict on Track 1

**The premise is correct as stated, with the sign correction in §1.1.** Naive
`edge = p − q` → Kelly is not merely suboptimal; it is one of the specific strategies with a
published counterexample where a genuinely superior forecaster loses money.

---

## 3. Track 2 — Proper scoring rules and Bregman geometry

### 3.1 The correspondence

**[VERIFIED]** (Proposition 3, attributed to McCarthy 1956): S is (strictly) proper **iff**
there is a (strictly) convex G : Δ_K → ℝ with

&nbsp;&nbsp;&nbsp;&nbsp;S(**p**, y) = G(**p**) + ∇G(**p**) · (**1**_y − **p**)

G is the *expected score of a truthful report*: G(**p**) = S(**p**;**p**). It is the convex
conjugate of the AMM cost function C (Corollary 15's proof). The Bregman divergence is
D_G(**q**,**p**) := G(**q**) − G(**p**) − ∇G(**p**)·(**q**−**p**) ≥ 0, with equality iff **p** = **q**.

Note the argument order: **D_G(q, p)** — the market price is the *first* argument, the
forecast is the *base point*. For the log rule this is KL(**q** ‖ **p**), the *reverse* of
the usual KL(true ‖ model). Getting this backwards flips a sizing rule; it is worth a unit
test when this is ever implemented.

### 3.2 The three rules, and their binary specializations

Paper's Table 1 **[VERIFIED]**, with the K=2 reductions derived here **[INFERRED]**:

| Rule | S(**p**,y) | G(**p**) | D_G(**q**,**p**) | **s**_G(**p**,**q**) |
|---|---|---|---|---|
| Quadratic (Brier) | −‖**1**_y − **p**‖² | ‖**p**‖² − 1 | ‖**q** − **p**‖² | 2(**p** − **q**) |
| Logarithmic | log p_y | Σ p_k log p_k | Σ q_k log(q_k/p_k) | log(**p**) − log(**q**) |
| Spherical | p_y / ‖**p**‖ | ‖**p**‖ | ‖**q**‖ − **p**·**q**/‖**p**‖ | **p**/‖**p**‖ − **q**/‖**q**‖ |

Now specialize to a binary market, **p** = (p, 1−p), **q** = (q, 1−q). Recall from
Theorem 13 that a constant shift **s** → **s** + λ**1** is free (it costs λ·Σq_k = λ and pays
λ, netting zero for **q** in the simplex), so we may always reduce a binary position to a
net YES exposure.

**Brier.** **s**_G = 2(p−q)·(1, −1) → net long **2(p − q)** YES contracts.
Position is **linear in the margin**. D_G = 2(p−q)².

**Log.** **s**_G = (log(p/q), log((1−p)/(1−q))). Shift by λ = log((1−p)/(1−q)):

&nbsp;&nbsp;&nbsp;&nbsp;**net YES exposure = logit(p) − logit(q)**

This is a genuinely important identity for our architecture. The log-rule proper bet in a
binary market *is the logit difference between belief and price*. It means the natural
sizing variable and the natural belief-representation variable (Track 4 aggregates in logit
space) are **the same coordinate**. If we represent beliefs as logits — which Track 4 argues
for on independent grounds — the log-rule position size is available with no transform at
all. D_G = KL(q‖p) = q log(q/p) + (1−q) log((1−q)/(1−p)).

**Weighting behavior.** Brier weights **linearly** in |p−q|; Log weights **sub-linearly**
near the middle but **explodes** as q → 0 or 1 (logit q → ±∞). Spherical sits between and
was the worst performer in every table in the paper. **[VERIFIED from Tables 2–4]**

### 3.3 Which rule should be our internal objective?

The paper's persona experiment (Table 4) gives a sharp, and somewhat awkward, answer
**[VERIFIED]**:

> **Brier is optimal in Regime A** (we beat the market) **for all four personas, while
> Log is optimal in Regime B** (we lose to the market).

Regime A ROI: Brier +37.7 to +48.8 across personas. Regime B ROI: Log −20.7 to −71.5,
consistently the least-bad. The mechanism: with a positive score gap, expected return is
positive in every margin bin, so linear scaling in |p−q| is right; with a negative gap,
losses concentrate in high-margin bets, so Log's sub-linear attenuation limits the damage.

**This is a decision-theoretic trap and we should name it.** The rule that maximizes upside
is the rule that maximizes downside, and choosing between them requires knowing the sign of
a quantity (ΔS against **p**\*) that §2.3 established is unobservable. Selecting Brier is a
bet that we beat the market. Recommendation:

1. **Start on Log**, not Brier. Log is the minimax-regret choice under sign uncertainty
   about our own edge, and every honest prior about an unproven pipeline should put
   substantial mass on Regime B. Our own repo evidence supports this pessimism: the tennis
   lane shows *negative* forecasting skill and soccer's apparent edge was an artifact
   (`outcome-post-drain-baseline`).
2. **Cap the logit.** Log's blow-up as q → 0/1 is unacceptable at Kalshi's penny ticks.
   Clip |logit(p) − logit(q)| at some L_max, and additionally hard-exclude q outside
   [q_min, 1−q_min]. Choosing L_max is a real parameter, not a detail.
3. **Switch to Brier only on measured evidence of a positive empirical score gap**, per
   domain, with a pre-registered threshold. Corollary 17 legitimizes using empirical scores
   for exactly this.
4. **Never use Spherical.** Dominated in all three tables.
5. Note that the rule choice is *not* separable from accuracy: "a model's score under a
   given scoring rule does not by itself determine which rule yields the best returns, since
   divergence is rule-dependent." **[VERIFIED]** The bake-off in §4 must therefore sweep the
   rule, not fix it.

### 3.4 The scale-freedom point — proper betting is NOT a sizing rule

This deserves its own heading because it changes the shape of the design and the brief
does not anticipate it. From Theorem 13's setup **[VERIFIED]**:

> trivial transformations such as rescaling the strategy (i.e., λ**s**_G(**p**,**q**)) or
> adding a constant shift ... will not change the profitability nature of the strategy.

**s**_G is determined only up to a positive scalar λ. The theory fixes the **direction** of
each trade and the **relative weights across markets** — it says nothing about total
exposure. The paper's own experiments make this explicit: each strategy "determine[s] a set
of weights — how to allocate a **fixed budget** and on what direction." The budget is
exogenous to the theory.

Therefore:

> **Proper betting and Kelly are not competitors on the same axis.** Proper betting is an
> *allocator* (direction + relative weight). Kelly is a *scale* (what fraction of bankroll
> the whole book represents). The brief's instinct — "Kelly as a sizing CEILING, not the
> allocator" — is not just a reasonable hedge; it is the structurally correct reading of
> the theory.

This resolves the bake-off's design: the candidate space is not one-dimensional. It is
(allocator) × (scale rule), and the two can be varied independently.

## 4. Track 3 — Belief-to-trade transforms: the bake-off

### 4.1 Why Kelly on a point estimate is dangerous — concretely

Classical binary Kelly at YES price q with belief p: **f\* = (p − q)/(1 − q)**, the fraction
of bankroll staked. Expected log-growth under the *true* p\*:

&nbsp;&nbsp;&nbsp;&nbsp;g(f) = p\*·log(1 − f + f/q) + (1 − p\*)·log(1 − f)

Two properties do the damage. **[INFERRED — standard results, numerically verified below]**

**(a) Sensitivity explodes at extreme prices.** ∂f\*/∂p = 1/(1 − q). A *fixed* miscalibration
in p becomes an unbounded error in stake as q → 1:

| q | error in f\* per 0.01 error in p |
|---|---|
| 0.50 | 0.020 of bankroll |
| 0.90 | 0.100 of bankroll |
| 0.98 | 0.500 of bankroll |

Concretely, with **no true edge at all** (p\* = q) and a constant +0.05 overconfidence:

| q | claimed p | f\* | true log-growth per bet | wealth after 200 bets |
|---|---|---|---|---|
| 0.50 | 0.55 | 0.100 | −0.00503 | ×0.37 |
| 0.70 | 0.75 | 0.167 | −0.00640 | ×0.28 |
| 0.90 | 0.95 | **0.500** | **−0.02065** | **×0.016** |
| 0.95 | 1.00 | **1.000** | ruin | 0 |
| 0.98 | 1.03 | **2.500** | ruin (levered) | 0 |

A five-point overstatement — well inside the noise of any LLM forecast — stakes **half the
bankroll** on a market with zero edge at q = 0.90, and destroys 98.4% of capital over 200
such bets. At q ≥ 0.95 the same overstatement calls for the entire bankroll or leverage.
This is the mechanism behind the paper's −99.9% Kelly column, and it explains why it appears
for *four of five* models rather than only the bad ones.

**(b) The tolerance for overstatement is a factor of exactly 2.** g(f) = 0 at f = 0 and again
at f ≈ 2f\*. Verified numerically at q = 0.50, true edge 2¢, claimed 5¢:

| stake | true log-growth |
|---|---|
| 1.0 × f\*_true | +0.000800 |
| 1.9 × | +0.000150 |
| **2.0 ×** | **−0.000003** ← zero-growth boundary |
| 2.5 × | −0.001012 |
| 3.0 × | −0.002429 |

> **If our stated edge is more than twice our true edge, full Kelly has negative growth.**
> Not sub-optimal growth — negative. An agent claiming 5¢ of edge where 2¢ exists is already
> past the cliff. No LLM forecasting pipeline should be assumed to be within 2× on edge.

**(c) Fractional Kelly is the direct antidote, with a clean rule.** Since the zero-growth
boundary is at 2×, **α-fractional Kelly tolerates overstatement up to a factor of 2/α**:

| α | survives claimed/true edge up to | at 2.5× overstatement (q=0.5, ex. above) |
|---|---|---|
| 1.00 (full) | 2.0× | g = −0.001012 (losing) |
| 0.50 | 4.0× | g = +0.000750 |
| 0.25 | 8.0× | g = +0.000688 |
| 0.10 | 20.0× | g = +0.000350 |

Note the shape: going from full Kelly to half-Kelly turns a loss into ~94% of the *optimal*
growth. Going from half to quarter costs almost nothing more. **The cost of caution is
second-order; the cost of overconfidence is first-order.** Given the repo's own track record
of edges evaporating on out-of-sample review (`edge-auto-observation-plan`,
`outcome-post-drain-baseline`), α ≤ 0.25 is the defensible starting point.

### 4.2 The candidate space is two-dimensional (three, with gating)

Per §3.4, allocation and scale are separable. Enumerating them as one flat list — which the
brief does — mixes incommensurable things. Correct factorization:

**Axis A — allocator** (direction and *relative* weight across simultaneous markets):
| ID | Rule | Net YES exposure, binary |
|---|---|---|
| `A-brier` | proper, quadratic | ∝ 2(p − q) |
| `A-log` | proper, logarithmic | ∝ logit(p) − logit(q), clipped at L_max |
| `A-sph` | proper, spherical | ∝ p/‖**p**‖ − q/‖**q**‖ |
| `A-margin` | raw margin (the naive baseline under test) | ∝ (p − q) |
| `A-maxmargin` | all capital on the single largest \|p − q\| | — |
| `A-invmargin` | inverse margin | ∝ 1/\|p − q\| |
| `A-uniform` | equal weight on every market passing the gate | 1 |
| `A-tadj` | time-adjusted EV: margin ÷ days-to-resolution | ∝ (p − q)/T |

`A-brier` and `A-margin` differ only by the constant 2 and are *the same allocator* once
normalized to a budget. That is worth stating plainly: **the brief lists "proper-scoring-rule
betting" and "raw margin" as competing candidates, but for the Brier rule in a binary market
they are identical.** The genuine contrast between proper and naive is (i) Log vs. linear
weighting, and (ii) multi-outcome events, where the naive rule has no principled way to
weight across the K legs and the proper rule does.

**Axis B — scale** (total bankroll fraction the whole book represents):
| ID | Rule |
|---|---|
| `B-fixed` | fixed fraction of bankroll per period (control) |
| `B-kelly` | full Kelly on the point estimate |
| `B-frac(α)` | α-fractional Kelly, α ∈ {0.5, 0.25, 0.1} |
| `B-cons(γ)` | **conservative Kelly**: Kelly on the γ-quantile of the belief distribution, pulled toward q |
| `B-cap` | any of the above, then hard-capped at c% of bankroll per market and d% per correlated cluster |

**Axis C — gate** (whether to trade at all):
| ID | Rule |
|---|---|
| `C-none` | trade everything |
| `C-spread` | **Corollary 19 abstain band**: no trade when 1 − q⁻ ≤ p ≤ q⁺ |
| `C-calib` | conditional-calibration gate (§6) |
| `C-cohere` | coherence-violation gate (§7) |
| `C-abstain` | ABSTAIN always — the null arm |

`C-spread` is not optional. It falls directly out of the theory once bid-ask is admitted
(Corollary 19) and it is the *only* candidate here that is a theorem rather than a heuristic.
It should be a floor under every arm, not an arm.

### 4.3 Conservative Kelly deserves top billing

`B-cons(γ)` is the most promising candidate in the whole set and the brief is right to name
it. Given a belief *distribution* rather than a point (Track 4), let p_γ be the γ-quantile on
the side facing the price:

&nbsp;&nbsp;&nbsp;&nbsp;p_γ = Q_γ(P) if trading YES (γ small, e.g. 0.25); p_γ = Q_{1−γ}(P) if trading NO
&nbsp;&nbsp;&nbsp;&nbsp;f = max(0, (p_γ − q)/(1 − q))

Three things fall out at once **[INFERRED]**:

1. **Sizing and abstention unify.** If the credible interval straddles q, then p_γ < q, f
   clips to 0, and the trade is declined *automatically*. No separate abstain threshold to
   tune.
2. **Uncertainty is priced, not discarded.** A wide belief with a large nominal margin gets
   sized down relative to a tight belief with a small margin — the opposite of what
   `A-margin` does, and the correct direction.
3. **It degrades gracefully into fractional Kelly.** For a symmetric belief distribution with
   scale σ, p_γ ≈ p − z_γ·σ, so f ≈ f\* − z_γ·σ/(1−q): an *additive* haircut proportional to
   uncertainty, rather than fractional Kelly's *multiplicative* one. Additive is better
   behaved here because it bites hardest exactly where §4.1(a) says the danger is — it does
   not shrink a confident small-margin bet as aggressively as α-Kelly does.

Its weakness: it is only as good as the calibration of the belief *spread*, which is far
harder to validate than the calibration of the mean. An LLM ensemble's inter-trial variance
is a measure of prompt sensitivity, not of epistemic uncertainty, and is typically far too
small. **`B-cons` must not ship until the spread itself has been calibrated out-of-sample**
(§5.5). Until then it is overconfidence with a safety-blanket.

### 4.4 The evaluation metric

Realized P&L alone is **disqualified as the primary metric** by Example 6 (§2.1): a strategy
with no accuracy edge can post positive expected profit. A bake-off scored on P&L will
sometimes crown a directional-beta arm and we will not be able to tell.

**Primary metric.** Net paper P&L per unit of capital-at-risk-day, after Kalshi fees and
modeled slippage against the recorded book, evaluated on prospectively-generated forecasts
only.

**Mandatory co-reported decomposition** (Corollary 17 makes this computable from realized
outcomes without knowing **p**\*):

&nbsp;&nbsp;&nbsp;&nbsp;ROI = ΔS + D − L_ρ,&nbsp;&nbsp; ΔS = Σ_i [S(p_i, y_i) − S(q_i, y_i)] / Σ_i cost_i,&nbsp;&nbsp; D = Σ_i D_G(q_i, p_i) / Σ_i cost_i

An arm is only **promotable** if **ΔS > 0**. An arm with ROI > 0 driven entirely by D is
harvesting convexity rent, not skill, and per §2.2 that rent is precisely what L_ρ eats in a
real CLOB. **This single rule is the most valuable thing the paper gives us**, because it
converts an unfalsifiable claim ("we have edge") into a measurable, pre-registerable one.

**Secondary / guardrail metrics:**
- Max drawdown, and time-to-recovery.
- **Concentration**: share of total P&L from the top 5 trades. An arm whose P&L is one lucky
  resolution is not an arm.
- Realized L_ρ: fill price vs. mid at decision time. This is the backtest-to-live gap and
  must be measured, not assumed.
- Abstention rate. `C-abstain` returns exactly 0 with zero variance and **must be in the
  comparison table**, otherwise the bake-off can only rank ways of losing money.

### 4.5 What would make each candidate win

| Candidate | Wins if… | Loses if… |
|---|---|---|
| `A-log` | ΔS < 0 or noisy; losses concentrated at high margin | we have a real, stable edge (leaves money on the table) |
| `A-brier` | ΔS > 0 and stable across margin bins | ΔS < 0 — it *amplifies* the loss (Table 4 Regime B) |
| `A-margin` | never distinguishable from `A-brier` in binary markets | — (it is `A-brier`) |
| `A-maxmargin` | never; published counterexample (Example 5) + worst offline column | — |
| `A-tadj` | capital turnover is the binding constraint, not edge | edge is scarce; it will chase short-dated noise |
| `B-kelly` | our forecasts are within 2× on edge | they are not (assume they are not) |
| `B-frac(0.25)` | edge exists but magnitude is untrustworthy | edge is precisely known (rare) |
| `B-cons(γ)` | belief *spreads* are calibrated | spreads are miscalibrated → false confidence |
| `C-abstain` | ΔS ≤ 0 in every domain | ΔS > 0 somewhere |

**The honest prior is that `C-abstain` wins the first round.** The repo's forecasting track
record (`outcome-post-drain-baseline`: Brier worsened to 0.1908 on the representative sample;
tennis negative skill; soccer's edge an artifact) contains no demonstrated positive score gap
against *any* benchmark, let alone against a market price. A bake-off that cannot return
"none of these" is not an experiment.

### 4.6 How much data does the bake-off need?

Power at 80%, α = 0.05 two-sided, N = number of resolved trades **[INFERRED]**:

*Absolute test — is one arm's mean per-trade ROI different from zero?*

| true effect | sd=0.30 | sd=0.50 | sd=0.80 | sd=1.00 |
|---|---|---|---|---|
| 2% | 1,767 | 4,906 | 12,559 | 19,623 |
| 5% | 283 | 785 | 2,010 | 3,140 |
| 10% | 71 | 197 | 503 | 785 |

Per-trade return sd for binary contracts is brutal: near q = 0.5 a YES buy pays −100%/+100%,
so sd ≈ 1.0. Near q = 0.9 it is ≈ 0.33. **Realistically sd ∈ [0.5, 1.0], so detecting a
genuine 5% per-trade edge needs on the order of 1,000–3,000 resolved trades.** At a plausible
tens-of-trades-per-day cadence this is months, not weeks. Anyone promising a verdict in two
weeks is promising noise.

*Paired test — is allocator X better than allocator Y on the **same** forecasts?* Far cheaper,
because the forecast noise cancels:

| effect | sd_diff=0.10 | sd_diff=0.20 | sd_diff=0.40 |
|---|---|---|---|
| 2% | 197 | 785 | 3,140 |
| 5% | 32 | 126 | 503 |
| 10% | 8 | 32 | 126 |

> **Design consequence: run every arm on one shared forecast stream and compare pairwise.**
> The bake-off should be a *paired* design over identical forecasts — which is exactly what
> the brief specified — precisely because it cuts the required N by roughly an order of
> magnitude. Do not run arms on separate forecast streams.

Corollary: the **ΔS** channel is cheaper to measure than the P&L channel, because scores are
bounded and lower-variance than returns. We can establish whether a score gap exists long
before we can establish whether an arm is profitable. **Sequence the work that way**: prove
ΔS > 0 first, on paper, and only then bake off allocators.

### 4.7 Registration

This experiment is precisely what `PROSPECTIVE-EXPERIMENT-REGISTRY-002A`'s typed predicate
schema exists for: pre-register the arm list, the promotion rule (ΔS > 0 *and* net P&L > 0),
both window ends, and the minimum-N floor from §4.6, before the first trade. Note the standing
blocker: registration is deferred pending 002B, and nothing yet enforces floors at evaluation
time. **Fitting that enforcement is a prerequisite of this bake-off, not a follow-up.**

## 5. Track 4 — Forecasts as distributions

### 5.1 Citation check: paper VERIFIED, but the brief's update equation is its REJECTED branch

- https://arxiv.org/html/2604.18576 (v4, 12 Jul 2026) — Kevin Murphy, UBC.
  "Agentic Forecasting using Sequential Bayesian Updating of Linguistic Beliefs" (BLF).

The brief proposes evaluating the update form
`logit p_t = logit p_{t−1} + Σ_i w_i log BF_i`. **[REFUTED as a description of the paper.]**
That form appears only in **Appendix J**, as an alternative the authors built, evaluated, and
**rejected**. The shipped BLF update is an LLM forward pass. Verbatim (§C.2):

> The process is structured to resemble sequential Bayesian updating and decision making for
> a POMDP, but **we are not actually doing Bayesian inference in any technical sense**: the
> update rule (a_t, b_t) = LLM(m_{t−1}) is an LLM forward pass with **no explicit likelihood,
> no marginalisation, and no formal posterior**.

And the rejected form is Eq. 24, with a **single global** tempering coefficient α — not the
per-source w_i the brief assumes:

&nbsp;&nbsp;&nbsp;&nbsp;logit b_T = logit b_0 + α · Σ_{t=1..T} λ_t,&nbsp;&nbsp; λ_t = log[ p(o_t | s=1) / p(o_t | s=0) ]

The paper's verdict: *"The gap is therefore the mechanical scalar update itself —
accumulating one-dimensional log-likelihood-ratios cannot match weighing heterogeneous,
dependent evidence holistically in a single pass."* (Brier 0.123 for the best explicit
variant vs 0.088 for BLF.)

**We should nonetheless take the rejected branch seriously**, for a reason the paper did not
have: it is **auditable and deterministic**. An LLM forward pass that "does whatever it does"
cannot be unit-tested, replayed, or reasoned about; a log-odds accumulator can. Given this
repo's standing commitments to determinism and typed intermediate artifacts, a 3.5-point
Brier penalty may be a price worth paying — but that is a tradeoff to make explicitly, not by
default. And per §5.2 the rejected branch produced the single most useful number in the paper.

### 5.2 Correlated evidence — the measured redundancy discount

The brief calls correlated-evidence double-counting the most likely failure mode of an LLM
research pipeline and asks for it to be treated as central. The paper supplies a hard number,
and it is worse than expected.

**The problem, named verbatim (Appendix J):**

> The α term is a tempering coefficient... This is needed because **with conditionally
> dependent observations, the naive product over-counts evidence.** If we set α < 1, it
> down-weights each likelihood factor to restore approximately calibrated precision (a
> Gibbs / power-posterior correction). We estimate α using a validation set.

**The measured optimal tempering (Table 26, n=80 questions, 154 events):**

| Conditioning of each λ_t | α\* | Brier | ECE |
|---|---|---|---|
| none (`uncond`) | **0.05** | 0.136 | 0.114 |
| on the action a_t (`acond`) | **0.05** | 0.136 | 0.112 |
| on raw history (`hcond`) | **0.00** | 0.136 | 0.117 |
| on belief summary b_{t−1}.h (`bcond`) | **0.35** | 0.123 | 0.097 |
| summary + action + history (`sahbcond`) | 0.35 | **0.117** | 0.103 |
| *(BLF, linguistic)* | — | *0.088* | *0.083* |

> **Read α\* as the empirical redundancy discount. Even with best-case conditioning, each
> elicited log-Bayes-factor is worth about ONE THIRD of its face value. Without conditioning,
> α\* collapses to 0.00–0.05 — the naively-multiplied evidence stream is net worthless or
> actively harmful.**

This is the strongest available confirmation that the brief's concern is the right one. A
pipeline that accumulates LLM-elicited log-BFs at face value is not slightly overconfident;
it is *twenty times* overconfident relative to the fitted optimum.

**The principled cure is conditioning, not tempering.** Verbatim:

> λ_t from p(o_t | s, a_t, b_{t−1}.h), additionally conditioning on the belief summary
> b_{t−1}.h of the evidence gathered so far (**the belief state b_{t−1}, with its probability
> removed so the likelihood is not contaminated by the running posterior**). This makes the
> additive form Eq. 24 a **chain-rule decomposition rather than an independence
> approximation**: evidence already implied by b_{t−1}.h contributes λ_t ≈ 0, **the principled
> cure for the over-counting that tempering only blunts.**

Three design rules follow directly **[VERIFIED source, INFERRED application]**:

1. **Score each new piece of evidence against what is already believed, not in isolation.**
   The prompt-level realization is one sentence: *"Judge only what the NEW evidence adds
   beyond what is already known."* This is cheap and should be non-negotiable.
2. **Strip the running probability out of the conditioning context.** Otherwise the likelihood
   is contaminated by the posterior and the update becomes self-confirming. This is a subtle
   bug that is easy to write and almost impossible to notice from outputs.
3. **Bound the elicitation scale.** The paper elicits typicality on an **integer 0–10 scale**
   under each hypothesis, λ_t = log((r¹+0.5)/(r⁰+0.5)), which caps |λ_t| at ≈ log(10.5/0.5) ≈ 3.04.
   Verbatim: asking the model directly for the log-ratio *"produces inflated, frequently
   wrong-signed weights and was uniformly worse (under that elicitation the optimal tempering
   collapsed to α\* = 0, i.e. the evidence was **net-harmful and best discarded entirely**)."*
   **Never ask an LLM for a log-likelihood-ratio directly.** Ask for a bounded ordinal rating
   and compute the ratio ourselves.

Also noted and declined by the paper: *dependence-aware evidence pooling* (cluster documents
by underlying claim, one λ per cluster). They skip it because conditioning already targets the
same over-counting. **For us, clustering is worth doing anyway** — it is deterministic,
inspectable, and provides the *evidence-cluster* unit that §5.4's schema needs regardless.

**What the shipped BLF does about correlated evidence: nothing explicit.** No dedup, no
redundancy penalty, no learning rate, no per-step movement cap. The claim is that
overwrite-a-summary semantics is roughly idempotent where multiply-in-a-likelihood is not.
There is also **no cross-trial independence correction** — the K trials share one base model
and one search engine, and ensembling across *different* models gave **no gains** because
*"the component models are highly correlated — they receive the same evidence."*

> **Correlated evidence is not only a within-trial problem. Our agents share retrieval
> infrastructure, so inter-agent disagreement systematically understates true uncertainty.
> §5.3 quantifies how badly.**

### 5.3 Forecasts as distributions — the citation does NOT support this, and that matters

The brief asks for a forecast object carrying mean + uncertainty + inter-agent disagreement.
The cited paper does not deliver one, and the reasons are instructive.

**(a) The posterior variance is computed and then discarded.** The shrinkage model
(Eqs. 7–10) is a clean hierarchical Normal: y_qk = logit(p_qk), y_qk | θ ~ N(θ_q, σ_q²),
θ_q ~ N(μ_q, τ²), giving

&nbsp;&nbsp;&nbsp;&nbsp;**α_q = K τ² / (K τ² + σ_q²)**,&nbsp;&nbsp; p̂_q = σ( α_q · ȳ_q + (1 − α_q) · μ_q )

with σ_q² the sample variance of the K trial logits, μ_q = logit(market price) where one
exists, and **τ² estimated by LOO-CV** rather than marginal-likelihood empirical Bayes
("simpler, and more robust"). The posterior θ_q | y ~ N(m_q, v_q) is written down — and
**v_q never appears again**; Eq. 10 is an explicit "plugin approximation" using m_q only.
**The system emits a scalar.**

**(b) Inter-trial variance is a weak proxy for epistemic uncertainty.** Median across-trial
σ_q on ForecastBench is only **0.27 logits**, and the shrinkage was a measured **no-op: the
LOO grid selected α ≡ 1 in 791/791 folds.** It binds only on the higher-variance AIBQ2 set
(median σ_q = 0.50), worth ~+4 Metaculus score.

> This is the empirical form of the warning in §4.3. **Inter-trial spread measures prompt and
> sampling sensitivity, not epistemic uncertainty**, it is small, and on the benchmark where
> it was tested it carried no usable information at all. **`B-cons(γ)` — conservative Kelly on
> a belief quantile — therefore has no validated source of spread today.** It remains the most
> attractive sizing rule in §4.3 and it is the least ready to ship. Building the spread is
> prerequisite work, not a detail of the sizing rule.

**(c) Logit-space averaging is empirical, not proven.** The paper proves arithmetic-mean
averaging improves Brier by Jensen, then notes the proof does **not** transfer: BS_y(y) =
(σ(y) − o)² *"is not globally convex in y... Our preference for logit-space averaging on FB is
therefore empirical, not formal."* Two credited mechanisms: it preserves extremity, and it
degrades gracefully — *"For a confident prediction p ≈ 0.95, one dead trial in five costs the
arithmetic mean ~9 percentage points but the logit mean only ~4."* Use logit space, but know
it is a heuristic with a failure-tolerance rationale, not a theorem.

**(d) Trial count is the only component that reliably helps.** Ablation (Table 14, ΔBI vs
5 trials): logit:1 → **−1.2\*\*\***; averaging space → −0.2 (n.s.); shrinkage → **0.0 (n.s.)**.
Caption verbatim: *"More trials reliably improve BI; averaging-space and shrinkage choices give
differences within bootstrap noise on FB."* **Spend on K, not on aggregation cleverness.**

### 5.4 Hierarchical Platt calibration

**[VERIFIED]** Global: p̂_cal = σ(a · logit p̂ + b). Hierarchical (Eq. 13):

&nbsp;&nbsp;&nbsp;&nbsp;**p̂_cal = σ(a · logit p̂ + b + δ_s)** — shared slope, shared intercept, **per-source offset δ_s only** (no per-source slope)

Prior on δ_s is an L2 penalty λ Σ δ_s², i.e. a zero-mean Gaussian, with **λ = 1.0 fixed and
never tuned**. Fit by minimizing log loss under **leave-one-question-out CV**; 9 sources over
~400 questions / 791 events, so roughly **45 questions per source offset**. The paper states no
minimum sample size and runs no sample-size sweep. **[FLAG: the brief's question "how many
calibration points are needed" has no answer in this source.]**

**What "over-shrinking extreme predictions for skewed base rates" means concretely.** Source
empirical priors range from 0.00 (ACLED 10× spike; Wikipedia vaccine) to 0.99 (Wikipedia
swimming world record), while FRED ≈ 0.42 and yfinance ≈ 0.58. A single global slope a < 1,
fitted to fix genuine overconfidence on Polymarket, drags the legitimately-near-0/near-1
sources back toward the pooled centre and destroys real skill. Per-source δ_s lets each source
sit at its own operating point.

**Measured effect (Table 15, ΔBI):**

| Setting | global Platt | hierarchical Platt |
|---|---|---|
| Zero-shot baseline, overall | +0.2 (n.s.) | **+3.5\*\*\*** |
| Zero-shot, dataset | +0.5\*\*\* | **+5.5\*\*\*** |
| **Full BLF, overall** | +0.4 (n.s.) | **+0.4 (n.s.)** |

Verbatim: *"**Calibration has limited effect on the full BLF system**, but hierarchical
calibration is crucial for the ZS baseline... Global Platt hurts the ZS baseline because it
over-shrinks predictions from the empirical prior."*

> **Calibration is a crutch for a weak forecaster, not an amplifier for a strong one.** If our
> forecaster is weak, hierarchical Platt is worth ~3.5 BI. If it becomes strong, calibration
> stops mattering. Either way, **global Platt is worse than nothing** on heterogeneous sources —
> and "heterogeneous sources" describes our repo exactly (separate baseball / tennis / soccer
> forecasters plus a template baseline, with documented per-lane skill differences). If we
> calibrate at all, it must be hierarchical from day one.

### 5.5 The proposed forecast object

Synthesizing §5.1–5.4 plus the Track 5 finding that the contemporaneous price is not recorded:

```
Forecast                                   # replaces a bare `estimated_probability`
  # --- identity ---
  market_ticker, proposition_id, forecaster_name, forecaster_version,
  model_name, prompt_version, created_at

  # --- the belief itself ---
  p_mean                    float          # logit-space mean of K trials, post-shrinkage
  p_logit_mean, p_logit_sd  float          # CARRY THE SPREAD FORWARD (BLF discards it)
  n_trials K                int
  trial_logits              [float]        # raw, for re-aggregation without a re-run
  shrinkage_alpha           float          # K*tau^2/(K*tau^2+sigma^2); 1.0 => no shrinkage
  prior_anchor              {kind: market|base_rate|uninformative, value}

  # --- calibration state ---
  p_calibrated              float          # sigma(a*logit(p) + b + delta_source)
  calibrator_id, calibrator_fit_through    # WHICH calibrator, fit on data through WHEN
  is_extrapolation          bool           # p outside the calibrator's fitted support

  # --- market context (MISSING TODAY; see 6.4 reason 3) ---
  market_price_at_forecast  float          # q. Without this, Delta-S is not computable.
  yes_bid, yes_ask, spread, liquidity_proxy, quote_age_ms
  price_source              tick|rest|absent

  # --- evidence, CLUSTERED not listed ---
  evidence_clusters: [
    { cluster_id, claim_summary,
      members: [ {source_id, url, retrieved_at, publisher, query_that_found_it} ],
      independence_class: primary | syndicated | derivative | unknown,
      lambda_raw            float,         # bounded-ordinal-derived log-BF for the CLUSTER
      lambda_tempered       float } ]      # after alpha; alpha recorded, not assumed
  tempering_alpha           float
  novelty_conditioned       bool           # was lambda scored against the running summary?

  # --- provenance & audit ---
  evidence_cutoff_at        datetime       # leakage guard
  research_packet_id, resolution_assessment_id
```

Five design rules embedded above, each traceable to a finding:

1. **`market_price_at_forecast` is the highest-value single field.** Without it, ΔS is not
   computable and Track 1 is unmeasurable (§6.4 reason 3).
2. **Evidence is stored as CLUSTERS, not items.** One λ per underlying claim, never one per
   document. Three articles syndicating one wire story are one piece of evidence. This is the
   deduplication the BLF authors declined to test and which we should do anyway, because it is
   deterministic and inspectable where their conditioning cure is not.
3. **`independence_class` is explicit and defaults to `unknown`.** `unknown` must be treated as
   `syndicated` (maximally correlated) by any consumer. Fail closed.
4. **`lambda_raw` comes from a bounded ordinal elicitation**, never from asking the model for a
   log-ratio (§5.2 rule 3), and `tempering_alpha` is **stored, not assumed** — so a
   re-estimation of α can be replayed over historical forecasts without re-running the agents.
5. **The spread is carried forward.** `p_logit_sd` and `trial_logits` are what `B-cons(γ)`
   needs, and BLF's discarding of them is the specific gap that blocks that sizing rule.
   Storing raw trial logits also lets us re-aggregate later without paying for new inference.

### 5.6 The finding that should reset expectations for the whole project

The most consequential number in this paper is not in the brief's list of questions.

BLF's baseline `Crowd+emp` **is the market price** (ForecastBench freeze value:
Polymarket/Manifold/RFI price, Metaculus community prediction). Against it **[VERIFIED]**:

| Method | Market questions | Dataset questions | All |
|---|---|---|---|
| **BLF (Gemini Pro)** | **+2.3 BI (n.s.)** | +4.5\*\*\* | +3.4\*\* |
| Cassi | +0.6 (n.s.) | +1.3 (n.s.) | +1.0 (n.s.) |
| GPT-5 | −1.1 (n.s.) | +1.8\* | +0.3 (n.s.) |
| Grok 4.20 | −1.9 (n.s.) | +4.7\*\*\* | +1.4 (n.s.) |
| Foresight-32B | −0.6 (n.s.) | −2.3 (n.s.) | −1.5 (n.s.) |

> **On MARKET questions — the only ones we can trade — the state-of-the-art agentic
> forecaster beats the market price by +2.3 BI, and that is NOT statistically significant
> (n = 200 events). Every other system is at or below the market. All of BLF's significant
> edge comes from DATASET questions, where there is no market at all**, and substantially
> from non-LLM machinery (a KNN model that "bypasses the LLM entirely" on DBnomics; linear
> trend models on FRED/yfinance).

Chain this to Track 1. Theorem 8 says profit = ΔS + D − L_ρ. If the best published agentic
forecaster cannot demonstrate ΔS > 0 against market prices, then our realistic prior is
**ΔS ≈ 0**, leaving profit ≈ D − L_ρ: pure divergence rent against pure execution cost. In a
CLOB with thin books that is a coin flip at best, and Corollary 15 says the two terms cancel
exactly in the idealized case.

Two independent corroborations, from arXiv 2607.03015 (Raven-Agent, verified to exist and read;
treat its own ROI figures as anecdotal — n = 42–58 trades, single deployment):
- **Prediction Arena**: six frontier LLMs run as end-to-end Kalshi agents, \$10k each, 57 days.
  **All six lost money (−16% to −31%).** Only hard-coded constraints held; prompt-level risk
  guidance was *"frequently ignored."*
- **RLVR-style accuracy optimization improved Brier while *worsening* return** (best-accuracy
  agent: −14.8%). This is Track 1's thesis appearing again, from a completely different
  direction: optimizing the forecast does not optimize the trade.

Raven-Agent's own controlled replay (forecaster held fixed, all policies see identical
probabilities) is the cleanest available evidence for §4's design:

| Policy | ROI |
|---|---|
| **Edge-proportional sizing** | **−55.5%** |
| Forecast-only | −10.7% |
| Edge-filter | −9.3% |
| Raven fixed-stake | −4.7% |
| Raven full (select → quarter-Kelly → hard caps) | +15.9% |

> **Sizing proportional to edge, without a selection gate, was five times worse than flat
> stakes** — it concentrates capital on confidently-wrong high-edge forecasts. This is the
> naive `edge = p − q` rule of the brief's thesis, measured. It also settles an ordering
> question for §4: **selection must precede sizing**, and the risk constraints must live in
> deterministic code outside the prompt, not in instructions the model can ignore.

## 6. Track 5 — Conditional calibration

### 6.1 Citation check: VERIFIED, but PIN THE VERSION

- v1: https://arxiv.org/html/2602.19520v1 — 23 Feb 2026
- v2: https://arxiv.org/html/2602.19520v2 — **04 Aug 2026, post-referee, substantially revised**
- Nam Anh Le, "Decomposing Crowd Wisdom: Domain-Specific Calibration Dynamics in Prediction Markets"

**Every headline figure in the brief is a v1 figure, and v2 changed all of them.** This is a
live sourcing hazard, so it is recorded in full:

| | v1 | v2 |
|---|---|---|
| Dataset | 292M trades / 327k contracts | **353M / 429k** (the brief's number) |
| Components | four | **five** (a common size main effect κ(s) split out) |
| Polymarket size effect | +0.113 [−0.151, +0.395], n.s. | **+0.281 [+0.026, +0.542], significant** |
| Posterior predictive coverage | 96.3% (208/216) | 99.5% (215/216) |
| Structural share of variance | not reported | **45.6% [31%, 62%]** |

The brief's "~353M Kalshi/Polymarket trades" is v2's headline. Note it is inflated relative
to what is analyzed: **58.7M Kalshi + 135.6M Polymarket = 194.3M trades enter the calibration
analysis after price filtering, and every headline result is Kalshi-only (58.7M).** Polymarket
is a replication check on 3 of 6 domains. The dataset changed between versions only because
the Polymarket snapshot was regenerated with a larger long tail of bespoke markets.

### 6.2 Does the claim hold? Yes — with a much smaller actionable core than advertised

**[VERIFIED]** Calibration is *not* a universal `price = probability` map. Kalshi
recalibration slopes by domain × time-to-resolution (b > 1 = underconfidence, prices
compressed toward 50%; b < 1 = overconfidence):

| Domain | 0–1h | 6–12h | 24–48h | 2d–1w | 1w–1mo | 1mo+ |
|---|---|---|---|---|---|---|
| **Politics** | 1.34 | 1.55 | 1.52 | **1.83** | **1.83** | 1.73 |
| Sports | 1.10 | 1.01 | 1.08 | 1.04 | 1.24 | 1.74 |
| Crypto | 0.99 | 1.01 | 1.21 | 1.12 | 1.09 | 1.36 |
| Finance | 0.96 | 0.97 | **0.82** | 1.07 | 1.42 | 1.20 |
| **Weather** | **0.69** | 0.87 | 0.97 | 1.20 | 1.20 | 1.37 |
| Entertainment | 0.81 | 0.92 | 0.84 | 1.07 | 1.11 | 0.96 |

The effect is real, domain-specific, horizon-dependent, and **not monotone** — Politics dips
to 0.93 at 1–3h, Finance to 0.82 at 24–48h. The paper shows the Politics dip is a **Simpson's
paradox** from subcategory mix (Trump Administration, 63% of trades at slope 1.08, dilutes
Electoral College at 1.81 and Other Politics at 2.17), not a regime shift.

**But now the finding that reframes everything.** The model-free reliability decomposition
(v2 Table 6, contract-weighted):

| Domain | ECE | **Reliability** | Brier |
|---|---|---|---|
| **Politics** | **0.117** | **0.024** | 0.119 |
| Entertainment | 0.022 | 0.001 | 0.160 |
| Finance | 0.016 | 0.000 | 0.156 |
| Weather | 0.016 | 0.000 | 0.172 |
| Sports | 0.008 | 0.000 | 0.185 |
| Crypto | 0.007 | 0.000 | 0.174 |

> **Outside Politics, every domain's reliability component rounds to 0.000–0.001. There is
> essentially nothing to recalibrate.** The "conditional calibration" story, stripped of the
> logit-slope framing that magnifies small deviations, is really a single-domain story:
> **Kalshi political markets are underconfident; everything else is well calibrated.**

Two further deflations from v2, both important and both absent from any summary of the paper:

1. **Only ~46% of the observed slope variation is structural signal.** *"the event-clustered
   measurement variance accounts for roughly half of the raw cell-to-cell dispersion...
   the four structural components explain 45.6% of the total observed variance (95% CrI
   [31%, 62%])."* The famous 87.3% is the share of *observed* variation captured, noise
   included.
2. **A permutation null gets R² = 0.329** (95th pct 0.406, max 0.531 over 5,000 permutations)
   from randomly reassigning slopes across cells. A 72-parameter model on 216 points is
   expected to explain a third of the variance from nothing at all.

### 6.3 The brief's proposed table has a design error — adopt the paper's design instead

The brief proposes learning
`P(resolve YES | market price, domain, time-to-resolution, liquidity regime, price region)`.
Two problems:

1. **"market price" and "price region" are the same variable.**
2. More substantively: **the paper has no price dimension in its cell grid at all**, and its
   reason is the right one. The grid is domain (6) × horizon (9) × size (4) = 216 cells, and
   within each cell **price enters as a continuous regressor in a 2-parameter logistic**:

&nbsp;&nbsp;&nbsp;&nbsp;**Stage 1:** logit P(y_i = 1) = a + b · logit(p_i)&nbsp;&nbsp;(per cell, L2 with C = 10)
&nbsp;&nbsp;&nbsp;&nbsp;**Stage 2:** θ(d,τ,s) = μ(τ) + α_d + κ(s) + β_d(τ) + γ_d(s) + ε&nbsp;&nbsp;(θ = the stage-1 slope b̂)
&nbsp;&nbsp;&nbsp;&nbsp;**Recalibration map for consumers (v2 Eq. 18):** **p\* = σ(â + b̂ · logit p)**

Binning price would require populating a histogram; the logistic fits **two parameters**.
That is why 200 observations can suffice per cell where a binned table would need thousands.
**Take the parametric form.** Note v2 explicitly *retracts* v1's slope-only variant
`p^θ/(p^θ+(1−p)^θ)` as a "slope-only illustration" and insists the intercept be retained —
intercepts are not negligible (Crypto mean |a| = 0.41, max 1.667).

Sample floor, verbatim: *"excludes markets with fewer than 10 trades, requires at least 200
trades per analysis cell."* All 216 Kalshi cells cleared it; none were dropped.

### 6.4 Can our data fit this? No — and sample size is only the third-biggest reason

The brief cites 17,073 forecasts / 24,500 scored records. **[UNVERIFIED locally — no database
is present in this worktree; taken as given.]** Assessing feasibility:

**Reason 1 — domain mix (fatal).** Our forecasting lanes are sports: dedicated
`baseball_forecasting`, `tennis_forecasting`, `soccer_forecasting` services. **Sports is the
domain the paper measures as essentially perfectly calibrated** (ECE 0.008, reliability
0.000, slopes 0.90–1.10 out to a week). We would be fitting a recalibration table in the one
place there is nothing to recalibrate, and would correctly recover b ≈ 1. **Infinite data
does not fix this.** The actionable domain — Politics — is the one where our repo has no
forecaster at all. This is a *coverage* problem, not a statistics problem, and it should be
the headline of any feasibility answer.

**Reason 2 — our forecasts are anchored to the market midpoint (structural).**
`TemplateBaselineForecaster` in `app/services/forecasting.py:166`, by its own docstring:

> Deterministic neutral prior. Anchors to the market midpoint when a two-sided quote exists
> (public consensus as prior), otherwise 0.50. **Adds no independent information** — and says
> so in its own skeptic notes.

with `probability = round((yes_bid + yes_ask)/2/100, 4)` clipped to `[0.02, 0.98]`.
`LLMForecaster` **falls back to this baseline on any failure** (line ~508) and is gated behind
`ENABLE_LLM_FORECASTING`. So an unknown but likely large share of the 17,073 forecasts satisfy
**p ≡ q**. Consequences, and they cut both ways:

- **Bad for edge measurement:** where p ≡ q, the score gap ΔS is identically zero and the
  proper-betting position **s**_G is identically zero. Those records contain *no information
  about our forecasting skill* and must be excluded from any ΔS estimate. Any historical
  Brier number computed over them is a measurement of the market, attributed to us.
- **Good for market calibration:** where p ≡ q, fitting `logit(y) = a + b·logit(p)` over our
  records *is* fitting the **market's** calibration curve. These rows are a free
  market-calibration dataset. The `[0.02, 0.98]` clip censors the extremes — tolerable, since
  the paper filters to 5–95¢ anyway.

**This split must be made before any number is quoted.** `forecaster_name`,
`forecaster_version` and `calibration_tags` (the baseline writes `anchored_to_market_mid`)
make it mechanical.

**Reason 3 — the price at forecast time is not recorded.** `MarketForecastRecord` (models.py:214)
stores `estimated_probability`, `confidence`, and rich reasoning fields, but **no contemporaneous
market price**; the docstring is explicit that the table "carries no EV, sizing, or
trade-recommendation fields by design." `ForecastScoreRecord` scores against the outcome, not
against the market. So **ΔS = S(p,y) − S(q,y) is not computable from the existing corpus**
without joining `MarketPriceTick` by (ticker, time) — and per
`paper-execution-ledger-and-ranked-roadmap` the Probability lane has **no live tape writer**,
so that coverage is unlikely to exist historically. Adding a `market_price_at_forecast` column
is the single cheapest high-value change identified in this document, and it is a prerequisite
for measuring anything in Track 1.

**Reason 4 — no trade-size dimension.** The paper's size axis is contract count
(Single / 2–10 / 11–100 / >100). We observe `volume_24h` and `liquidity_proxy` on ticks, not
per-trade size. The size axis is unavailable in the paper's form; a liquidity-regime
substitute is *not* the same construct and its findings would not transfer.

**Reason 5 — sample size, and effective N is far below nominal N.** Only now the count:
- 24,500 ÷ 216 cells ≈ **113 records/cell, below the paper's own 200 floor** before any
  discount.
- The paper's smallest realized cell is 472 trades (Weather); Sports is 22,518 — **one of its
  cells is comparable to our entire corpus.**
- The binding constraint is clustering, not raw count: *"they are on the order of **50 times
  the naive Fisher errors** that assume independent trades."* Politics has **850 event
  clusters** in 64.7M trades. Our records cluster the same way — repeated forecasts on one
  ticker, and both sides of one event, are near-perfectly dependent. Effective N for us is
  the number of **distinct resolved events**, which is a small fraction of 17,073.

### 6.5 What we should actually build

Given the above, the defensible design is far smaller than the brief's:

1. **Fit ONE global 2-parameter recalibration first**: p\* = σ(â + b̂ · logit q) over all
   resolved markets, using midpoint-anchored records as market-price observations. Report b̂
   with **event-clustered** standard errors, never naive ones.
2. **Add at most 3 coarse horizon buckets** (`<24h`, `1d–1w`, `>1w`) and **at most 2 domain
   groups**. That is ≤ 6 cells, not 216. The paper's own leave-one-domain and leave-one-size
   checks "perform poorly, so the decomposition summarizes the observed grid rather than
   extrapolating" — so cells we cannot populate must not be interpolated.
3. **Report the permutation null alongside every R².** If our R² does not clearly exceed a
   shuffled baseline, we have nothing.
4. **Hard gate: train-early / test-late.** The paper has *no* time-split anywhere — its only
   statement on stability is *"whether these patterns are stable over time is an open
   question."* Its Politics result is drawn from a window dominated by the 2024 US election
   cycle and may be a one-cycle estimate. **A calibration table that is not validated
   out-of-time is the exact failure mode this repo already recorded once**
   (`edge-auto-observation-plan`: prereg candidates failed out-of-sample, inverted, and lost
   to the negative control). Out-of-time validation is a gate, not a follow-up.
5. **Do not add a price-bin dimension.** Use the continuous logistic.

### 6.6 The most promising testable hypothesis in this document

Follow the logic through. If the market price q is miscalibrated with a known (â, b̂), then

&nbsp;&nbsp;&nbsp;&nbsp;p_recal = σ(â + b̂ · logit q)

is a **forecast derived from the price alone** — no research, no LLM, no evidence pipeline.
Feed p_recal into the Track 3 machinery and you have a complete strategy with no forecasting
dependency whatsoever. On the paper's own numbers, a 70¢ political contract one week out maps
to ≈83%: **~13 percentage points of gross edge against a mean fee of ~1.2–1.3¢/contract.**

This deserves to be flagged as the highest expected-value item here **and** hedged hard:

- The paper is **purely descriptive**. It contains no backtest, no P&L, no spread modelling,
  no execution assumptions. Fees appear once, only to rule them out as an *explanation* of the
  calibration differences — not to assess profitability.
- The 13pp figure is **my arithmetic on the paper's in-sample fit**, not a result the paper
  states. **[INFERRED — treat as a ceiling on an untested hypothesis.]**
- It is in-sample, over a window dominated by one election cycle, with no out-of-time check.
- It requires **Politics** markets — the domain we currently do not forecast — and 94% of
  Kalshi political volume comes from large trades, so the price we can actually hit may not
  be the price in the dataset.
- If it were both real and this easy, its persistence needs an explanation. The paper does not
  offer one.

**Recommendation: test it on paper, prospectively, out-of-time, as its own arm in the §4
bake-off — before building any LLM forecasting apparatus for Kalshi.** It is cheap, it needs
no research pipeline, and it is falsifiable in a way the rest of the stack is not. If it fails
out-of-time, that is strong evidence that the conditional-calibration structure does not
survive, which should change our priors about the whole enterprise.

### 6.7 Consequence for Tracks 1–3: recalibration RAISES our bar

One asymmetry worth stating explicitly. If p_recal is a better forecast than q, then the
relevant benchmark for our agent is no longer the raw price. Theorem 8's guarantee is stated
against **q**, the price we trade at — that part is unchanged. But the *question of whether we
have an edge worth pursuing* becomes: **do we beat σ(â + b̂ · logit q), which is free?**

If the answer is no, the correct strategy is to trade p_recal and skip the forecaster
entirely. **Conditional calibration is not a bonus layered on top of our forecasts; it is a
competitor to them**, and it must appear in the bake-off as an arm with its own ΔS.

## 7. Track 6 — Coherence / arbitrage engine (Probability Graph)

### 7.1 The one structural decision: nodes are propositions, not markets

The single most important design choice, and the one that determines whether this engine
makes or loses money:

> **A node is a PROPOSITION with a resolution procedure. A market is an EDGE-LESS ATTACHMENT
> to a node, and the attachment is itself uncertain.**

Modelling markets as nodes silently asserts that two markets attached to "the same"
proposition must agree. They need not — they can *both settle correctly and disagree*,
because their resolution procedures differ. Every loss in coherence trading traces back to
collapsing this distinction. Making the attachment an explicit, typed, *confidence-weighted*
object is what lets us price that risk instead of assuming it away.

```
Proposition (node)
  id, canonical_statement
  resolution_procedure: { source, criteria_text, criteria_hash, timezone,
                          settlement_lag, dispute_process }
  resolution_time: { earliest, expected, latest }        # NOT a point
  domain, subdomain

MarketBinding (proposition -> venue market)   # the uncertain attachment
  proposition_id, venue, market_ticker, side_convention
  equivalence_class: exact | strong | weak | heuristic
  binding_confidence: float                    # P(this market settles iff the proposition holds)
  divergence_scenarios: [text]                 # enumerated ways they can come apart
  established_by: manual | rule | llm | cross-venue-matcher
  reviewed_at, reviewed_by
```

`binding_confidence` and `divergence_scenarios` are not documentation. §7.5 shows they are
the dominant term in the sizing formula.

### 7.2 Edge types and their constraints

Constraints stated on **true probabilities**. §7.3 converts them to executable form.

| Edge | Meaning | Constraint |
|---|---|---|
| `complement` | B = ¬A | P(A) + P(B) = 1 |
| `partition` (hyperedge) | {A_i} exhaustive & exclusive | Σ_i P(A_i) = 1 |
| `mutual_exclusion` (hyperedge) | {A_i} exclusive, maybe not exhaustive | Σ_i P(A_i) ≤ 1 |
| `implication` | A ⇒ B | P(A) ≤ P(B) |
| `equivalence` | A ⇔ B (same venue) | P(A) = P(B) |
| `cross_venue_equivalence` | A ⇔ B, different venues | P(A) = P(B), **conditional on binding** |
| `conditional_dependence` | A, B dependent, structure unknown | Fréchet bounds only |

For `conditional_dependence` the only constraints available without a dependence model are
the **Fréchet–Hoeffding bounds**:

&nbsp;&nbsp;&nbsp;&nbsp;max(0, P(A) + P(B) − 1) ≤ P(A ∧ B) ≤ min(P(A), P(B))
&nbsp;&nbsp;&nbsp;&nbsp;max(P(A), P(B)) ≤ P(A ∨ B) ≤ min(1, P(A) + P(B))

These are *wide*. A "violation" of a Fréchet bound is a genuine arbitrage; anything inside
the bounds is a view, not an inconsistency. **Do not let a dependence assumption smuggle a
view into the arbitrage engine** — that is how a coherence desk becomes a directional desk
without anyone deciding to.

Note that `implication` and `partition` subsume most of Kalshi's structured families. The
ladder markets the prophets paper mentions ("price at least \$7K", "at least \$8K", …) are a
pure implication chain: P(≥8K) ≤ P(≥7K), monotone in the threshold. That chain is the
cheapest, highest-confidence source of constraints on the whole venue — same ticker family,
same settlement source, same resolution time, no semantic risk at all. **Start there.**

### 7.3 Constraints must be evaluated on EXECUTABLE prices, never midpoints

This is where most naive coherence engines die. A constraint on probabilities is not a
constraint on quotes. To *capture* a violation you must buy every leg at its **ask** and sell
at its **bid**. So:

- **Complement pair.** Buy YES at ask a, buy NO at ask b. Guaranteed payoff 1. Tradable iff
  **a + b + fees < 1**.
- **N-leg partition.** Buy every leg at its ask. Payoff exactly 1. Tradable iff
  **Σ_i ask_i + fees < 1**.
- **Implication A ⇒ B violated.** Requires ask(A) < bid(B): buy A, sell B. Payoff ≥ 0 always,
  > 0 when A false and B true.

Evaluating any of these on midpoints manufactures phantom edge equal to half the summed
spread. On an N-leg partition that is N/2 spreads of pure fiction. **The engine must read
the book, not the mid.** This is a hard architectural requirement and the repo does not yet
have the input: `MarketPriceTick` stores `yes_bid`/`yes_ask` but there is no depth beyond
top-of-book and — per `paper-execution-ledger-and-ranked-roadmap` — **no live tape writer on
the Probability lane at all.** The coherence engine cannot be built before that lands.

Pleasingly, the prophets paper covers this case within its own framework **[VERIFIED]**:

> Notably, Theorem 8 holds even when **q** ∉ Δ_K. As an interesting application, we can also
> apply it when Q := Σ q_k < 1, i.e., there is trivial arbitrage opportunity by buying
> **s** = **1** at cost Q(<1) with a deterministic return 1 … By choosing G(**q**) = (Σ q_k)²,
> we can verify that the proper betting **s**_G = 2[Σ(p_k − q_k)]·**1**, leading to
> deterministic profit 2(1−Q)² for any **p**\*.

So coherence arbitrage is not a separate mathematics — it is Theorem 8 at a degenerate
potential, and the same ROI decomposition applies. It is the special case where the entire
profit is the divergence term and the score gap is irrelevant.

### 7.4 The fee hurdle is large, and it is largest exactly where the markets are

Kalshi's taker fee is **ceil(0.07 · C · P · (1−P))** per trade, maker ≈ 0.0175 · C · P · (1−P);
there is no settlement fee. **[VERIFIED — Kalshi fee schedule, secondary sources; confirm
against the primary PDF before use]** The fee is *maximal at P = 0.50* and decays to zero at
both extremes. Consequences, computed:

**Minimum mispricing required for a complement pair to break even on fees alone:**

| leg prices | taker hurdle | maker hurdle |
|---|---|---|
| 0.50 / 0.50 | **3.50¢** | 0.88¢ |
| 0.70 / 0.30 | 2.94¢ | 0.74¢ |
| 0.90 / 0.10 | 1.26¢ | 0.32¢ |
| 0.99 / 0.01 | 0.14¢ | 0.03¢ |

**N-leg exhaustive partition, legs at 1/N (taker):**

| N | fee hurdle per \$1 payoff |
|---|---|
| 2 | 3.50¢ |
| 3 | 4.67¢ |
| 4 | 5.25¢ |
| 5 | 5.60¢ |
| 10 | **6.30¢** |

Three conclusions **[INFERRED]**:

1. **At the money you need ~3.5¢ of genuine violation before a two-leg arb is break-even,
   before spread.** Sub-penny coherence violations at mid-price are not opportunities; they
   are noise. A very large fraction of what a naive detector flags will be inside this band.
2. **The fee hurdle grows with leg count** (roughly ∝ N · (1/N)(1−1/N) = 1 − 1/N), so wide
   partitions are *worse*, not better, despite offering more apparent violations.
3. **Maker execution is a 4× improvement.** Since coherence arbs are not time-critical in the
   way a latency arb is, resting limit orders on the legs is the correct execution mode. But
   resting orders reintroduce **leg risk** (§7.5), and that trade-off — 4× cheaper vs. partial
   fills — is the central execution question for this engine. It should be pre-decided, not
   discovered live.

### 7.5 Why an apparent violation is NOT tradable — the part that matters

The brief asks for real weight here. It deserves it: **essentially every dollar lost by a
coherence engine is lost to this list**, not to the detection math, which is trivial.

**(1) Fees.** §7.4. Disqualifies most flagged violations outright.

**(2) Spread.** You cross it on every leg. An N-leg basket crosses N spreads. On thin Kalshi
markets a 3–5¢ spread per leg is ordinary, which alone exceeds the entire fee hurdle.

**(3) Depth.** Top-of-book size is often a handful of contracts. An arb that exists for 10
contracts and evaporates for 100 is not a strategy, it is a rounding error with operational
overhead. **Size must be computed from the book, and the opportunity's size is
min over legs of available depth at the required price** — the *worst* leg governs.

**(4) Leg risk / non-atomicity.** There is no atomic multi-leg execution. You fill leg 1,
the market moves, leg 2 is gone, and you are now holding a naked directional position you
never wanted, in a market you have no view on. **A partially-filled arbitrage is a random
directional bet.** Mitigations — leg into the least-liquid leg first, hard timeout, immediate
unwind at market on timeout — all cost money, and the unwind cost must be budgeted *into the
opportunity's expected value*, not treated as an exception.

**(5) Resolution-criteria mismatch — the killer.** Two markets that read identically can
settle differently:
- *Different source of truth.* "Will CPI exceed 3%?" — initial print vs. revised figure.
- *Different timezone or cutoff.* An event on the boundary of a UTC vs. ET day.
- *Different treatment of the degenerate case.* Ties, cancellations, postponements,
  withdrawal of a candidate, a game suspended and completed the next day. Venue A voids;
  venue B settles NO. Both are correct per their own rules; your "arbitrage" loses a full leg.
- *Different dispute/oracle process.* A Polymarket UMA resolution and a Kalshi exchange
  determination are simply different mechanisms with different failure modes.

**(6) Semantic near-equivalence.** The LLM-shaped failure. A matcher that scores
"Will Candidate X win the nomination?" against "Will Candidate X be the party's nominee?" as
equivalent is right ~almost always — and the residual is exactly the scenario where they
diverge (nominee withdraws after winning; brokered convention). **Near-equivalence is
correlated with the payoff**: the cases where the propositions come apart are precisely the
weird cases, and weird cases are when prices move. The residual risk is not independent noise,
it is adversarially selected.

**(7) Correlated resolution risk.** Venue-level events — a delisting, an exchange
determination reversal, a regulatory halt, a settlement-source outage — hit all your legs at
once. Diversifying across many arbs does not diversify this.

**(8) Capital lockup.** Prediction-market arbs are held to resolution; there is no
mark-to-market exit at a fair price in a thin book. So the correct metric is
**annualized return on capital locked until the *latest* leg resolves**:

| captured edge | 7d | 30d | 90d | 180d | 365d |
|---|---|---|---|---|---|
| 1¢ | 68.9% | 13.0% | 4.2% | 2.1% | 1.0% |
| 2¢ | 186.7% | 27.9% | 8.5% | 4.2% | 2.0% |
| 5¢ | 1350.6% | 86.7% | 23.1% | 11.0% | 5.3% |

A 2¢ arb on a one-year market annualizes to **2.0%** — worse than T-bills, for
unbounded tail risk and full operational cost. Note the resolution time must be the
**latest** across legs, and it must be the pessimistic tail of the distribution, not the
expected value; markets extend, sports get postponed, elections get contested.

**(9) The quantified punchline.** Let ε = P(the legs fail to offset) from causes (5)–(7).
For a 2¢ arb on a \$0.98 basket:

| ε | EV per \$0.98 staked |
|---|---|
| 0.000 | +2.00¢ |
| 0.005 | +1.50¢ |
| 0.010 | +1.00¢ |
| **0.020** | **0.00¢** |
| 0.050 | −3.00¢ |

> **Break-even ε = edge / (edge + basket cost) = 0.02 / 1.00 = 2%.**
> A 2-cent coherence arbitrage is destroyed by a **2% chance the legs don't offset.**
> No LLM semantic matcher should be credited with 98% precision on the adversarially-selected
> tail cases described in (6). **Therefore: cross-venue and LLM-established equivalences are
> not tradable at ordinary edge sizes, full stop.** Only `exact` bindings — same venue, same
> ticker family, same settlement source, same resolution timestamp, mechanically verified —
> clear this bar.

This is the central finding of Track 6 and it inverts the usual enthusiasm: **the coherence
engine's value is almost entirely in the intra-venue structured families (complement pairs,
threshold ladders, exhaustive partitions within one event), and almost none of it is in the
exciting cross-venue semantic matching.**

### 7.6 Continuous coherence checking

Design **[INFERRED]**:

- **Incremental, event-driven.** Maintain `constraints_by_market: ticker -> [constraint_id]`.
  A tick on ticker *m* re-evaluates only constraints touching *m*. Cost is O(degree(m)), not
  O(|constraints|). This is the only form that keeps up with a live tape.
- **Two-tier evaluation.** Tier 1: cheap mid-price screen to shortlist. Tier 2: full
  executable-price + depth + fee + ε evaluation on the shortlist only. Never emit from Tier 1.
- **Staleness is a first-class input.** A constraint is only evaluable if *every* leg has a
  quote fresher than τ. A stale leg is not a violation, it is missing data. Given the
  observation-cliff history in this repo (`stage1-denominator-baseline`: 24h coverage 4.57%),
  assume staleness is the common case and fail closed.
- **Persistence filter.** A violation visible in one tick is probably a crossed/stale book.
  Require it to persist over k consecutive independent observations spanning ≥ Δt before it
  is even a candidate.
- **Hysteresis on emission** so a violation oscillating around the threshold does not emit a
  stream of duplicate opportunities.

### 7.7 From violation to typed, sized opportunity

```
CoherenceOpportunity
  id, detected_at, constraint_id, constraint_type
  legs: [ { ticker, side, executable_price, price_source: ask|bid,
            depth_available, quote_age_ms } ]

  # gross
  raw_violation           # e.g. 1 - sum(ask_i)
  gross_edge_per_unit

  # deductions, each explicit and separately auditable
  fee_estimate            # ceil(0.07*C*P*(1-P)) per leg, taker or maker
  spread_cost             # already in executable_price; recorded for attribution
  slippage_estimate       # depth-walked, not assumed
  net_edge_per_unit

  # risk
  binding_confidence_min  # min over legs
  epsilon_leg_failure     # P(legs do not offset) from bindings + divergence_scenarios
  risk_adjusted_ev        # (1-eps)*net_edge - eps*basket_cost
  breakeven_epsilon       # net_edge / (net_edge + basket_cost)   <- report ALWAYS

  # sizing
  max_size_from_depth     # min over legs
  max_size_from_risk      # from the Track 3 scale rule
  recommended_size        # min of the two

  # time
  latest_leg_resolution_p90
  annualized_return_on_locked_capital

  # gating
  tradable: bool
  rejection_reasons: [ fee_hurdle | spread | depth | staleness | binding_confidence
                     | epsilon_too_high | horizon_too_long | not_persistent ]
```

Two rules on this schema:

1. **`breakeven_epsilon` is always reported**, tradable or not. It is the single number that
   makes the (6)/(7) risk legible: "this arb requires our semantic matcher to be better than
   98% accurate on hard cases" is a sentence a human can rule on. Nothing else in the record
   forces that question to be asked.
2. **`rejection_reasons` is a list, not a first-match.** We want the distribution of *why*
   things fail, because that tells us whether the engine is fee-bound (→ pursue maker
   execution), depth-bound (→ it will never scale), or staleness-bound (→ fix the tape first).
   Guessing which one is the binding constraint is exactly the mistake
   `crypto-reconciliation-capacity-001` records six sessions of.

### 7.8 Build order

1. **Intra-venue complement pairs** on the same ticker family. `exact` binding, ε ≈ 0,
   mechanically verifiable, no semantics. Measure how often the fee hurdle is cleared. If it
   never is, **stop here** — that result alone is worth having and saves the rest of the work.
2. **Threshold ladders** (implication chains). Same settlement source, monotone constraint.
3. **Exhaustive partitions** within one event.
4. Mutual exclusion / Fréchet bounds — only if 1–3 show tradable frequency.
5. **Cross-venue equivalence: do not build.** §7.5(9) says it cannot clear the bar at
   realistic edge sizes. Revisit only with a measured, out-of-sample estimate of ε from
   *observed* historical divergences between paired markets — and note that estimating a 2%
   tail rate to useful precision itself needs hundreds of paired resolutions.

## 8. Where the handed premise is wrong

The central thesis — *naive `edge = p − q` then Kelly-size is insufficient* — **survives
fully**, and is better supported than the brief knew (Examples 4 and 5 are published
counterexamples; Raven-Agent measured edge-proportional sizing at −55.5%). The following are
corrections to the surrounding scaffolding, ordered by how much they change the design.

**1. Proper betting is an allocator, not a sizing rule — so it is not Kelly's competitor.**
**s**_G is defined only up to positive rescaling (Theorem 13). The theory fixes direction and
relative weights; total exposure is exogenous. The brief's candidate list puts
"proper-scoring-rule betting" and "full/fractional/conservative Kelly" in one flat set, but
they occupy different axes and compose. The bake-off must be a grid — allocator × scale × gate
— not a list. (§3.4, §4.2)

**2. The decomposition does not explain how an accurate forecaster loses money.** The brief
presents `Profit = ScoreGap + D_G − L_ρ` as the mechanism of loss. It is the opposite: under
proper betting D_G ≥ 0 always, so a positive score gap yields positive profit unless L_ρ
swamps both terms. The losses in the brief's framing come from *other strategies* (Kelly,
max-margin) via separate counterexamples. Building architecture on the misread would lead us
to look for the loss in the wrong term. (§1.1)

**3. "Raw margin" and "Brier proper betting" are the same allocator in binary markets.**
**s**_Brier = 2(p − q). Two of the brief's competing candidates are one candidate. The real
contrast is Log-vs-linear weighting and multi-outcome events. (§4.2)

**4. The forecast-object citation does not support forecasts-as-distributions.** BLF computes
a posterior variance and discards it; its output is a scalar. Inter-trial spread is small
(0.27 logits median) and was a measured no-op in 791/791 folds — it captures prompt
sensitivity, not epistemic uncertainty. The brief's most attractive sizing rule, conservative
Kelly on a belief quantile, therefore has **no validated source of spread**. It is not a
detail to be filled in; it is a prerequisite research task. (§5.3)

**5. The update equation handed to us is the cited paper's rejected branch.** Not the shipped
method, and it uses one global tempering coefficient rather than per-source w_i. Adopting it
is still defensible — on determinism grounds, which the paper did not weigh — but that must be
a deliberate choice against a measured 3.5-point Brier penalty. (§5.1)

**6. The conditional-calibration table as specified is mis-designed.** It lists "market price"
and "price region" as separate conditioning variables (they are one), and it proposes binning
price where the cited paper deliberately uses a continuous 2-parameter logistic per cell —
which is precisely why 200 observations can suffice. (§6.3)

**7. The conditional-calibration effect is one domain, not a general phenomenon.** By model-free
ECE, only Politics is meaningfully miscalibrated; every other domain's reliability component
rounds to 0.000–0.001. Roughly 46% of the slope variance is structural signal and a permutation
null already reaches R² = 0.329. The framing "market calibration is conditional" is true but
oversells a single-domain, possibly single-election-cycle result. (§6.2)

**8. Our own repo cannot fit that table, and sample size is the third-ranked reason.** The
binding constraints are (i) our lanes are sports, the domain the paper finds already
calibrated, and (ii) `MarketForecastRecord` records no contemporaneous market price, so ΔS is
not computable at all today. **We currently cannot measure whether we have an edge, by any
definition.** (§6.4)

**9. Coherence "arbitrage" is mostly not tradable, and the brief under-weights how mostly.**
Break-even ε = edge/(edge + basket cost) means a 2¢ arb dies at a **2%** chance the legs fail
to offset. The at-the-money taker fee hurdle alone is 3.5¢ for two legs and 6.3¢ for ten.
Cross-venue semantic equivalence cannot clear this bar at realistic edge sizes; it should be
marked do-not-build, not deferred. (§7.5)

**10. The biggest omission: the premise assumes we have an edge to transform.** Every track is
about *converting* forecasting skill into P&L. But the best published agentic forecaster beats
market prices by +2.3 BI **non-significantly**; every other system is at or below the market;
six frontier LLMs trading Kalshi with real money all lost 16–31%. If ΔS ≈ 0, then
profit ≈ D − L_ρ, and Corollary 15 shows those cancel exactly in the idealized case. **The
decision mathematics is not the binding constraint. The existence of an edge is.** (§5.6)

**11. A methodological caution, not a correction.** The brief supplied three arXiv IDs with
confident paraphrases. All three papers exist and two of the three paraphrases were materially
wrong in ways that would have propagated into design: the update equation was the rejected
branch, and every calibration figure was v1 of a paper whose v2 changed all of them. The brief
was right to ask for verification. **This should be the standing rule, not a one-off**: a
citation is not usable until someone has read the equation.

---

## 9. Open questions and recommended sequencing

### 9.1 The one-paragraph summary

Proper betting (∇G(**p**) − ∇G(**q**)) is the correct, and provably unique, way to turn a
forecasting edge into a position — but it is an **allocator**, it needs a separate **scale**
rule (fractional Kelly as a ceiling, conservative Kelly once spreads are calibrated), it needs
a **gate** (Corollary 19's abstain band, free from the theory), and it converts a *negative*
edge into a loss just as faithfully. Meanwhile the evidence that we — or anyone — has a
positive edge against Kalshi prices is absent. **The recommended posture is: build the
measurement apparatus first, prove ΔS > 0 on paper second, and only then argue about
allocators.**

### 9.2 Sequencing

**Phase 0 — make edge measurable (blocking everything else).**
1. Add `market_price_at_forecast` (plus bid/ask/spread/quote-age) to `MarketForecastRecord`.
   Nothing in Tracks 1–5 is measurable without it. Cheapest high-value change in this document.
2. Segment the historical corpus by `forecaster_name` / `calibration_tags` and quantify how
   many of the 17,073 forecasts are midpoint-anchored (p ≡ q). Any historical Brier computed
   over those rows measures the market, not us.
3. Land the **live tape writer** on the Probability lane. It gates coherence checking (§7.3),
   L_ρ measurement, and the backfill of q.

**Phase 1 — measure ΔS, cheaply, before any trading machinery.**
4. Compute empirical ΔS = mean[S(p,y) − S(q,y)] per domain, with **event-clustered** standard
   errors, under Corollary 17. This is far lower-variance than P&L (§4.6) and answers the only
   question that matters.
5. Pre-register via `PROSPECTIVE-EXPERIMENT-REGISTRY-002A`, with an out-of-time split as a hard
   gate. Note that 002B and floor-enforcement at evaluation are prerequisites, not follow-ups.
6. **If ΔS ≤ 0 everywhere, stop.** That is a legitimate and valuable outcome, and it is what
   the external evidence predicts.

**Phase 2 — the cheapest candidate strategy, which needs no forecaster.**
7. Fit the global 2-parameter recalibration p\* = σ(â + b̂ · logit q) and test **trading the
   recalibration itself**, prospectively and out-of-time (§6.6). It requires no LLM, no research
   pipeline, and is falsifiable. It is also a **competitor** to our forecasting stack: if we do
   not beat a free price transform, we should trade the transform.

**Phase 3 — the bake-off, paired, on one shared forecast stream.**
8. Grid: allocator × scale × gate, with `C-abstain` as a real arm and Corollary 19's band as a
   floor under all arms. Start on **Log**, not Brier (§3.3). Cap α ≤ 0.25 on any Kelly arm
   (§4.1). Selection gate before sizing function (§5.6).
9. Promotion requires **ΔS > 0 and net P&L > 0**, never P&L alone (Example 6).

**Phase 4 — coherence, narrowest first.**
10. Intra-venue complement pairs only. Measure how often the 3.5¢ fee hurdle is cleared on
    executable prices. **If it never is, stop there** — that result is worth having.
11. Threshold ladders, then partitions. Cross-venue equivalence: do not build.

### 9.3 Open questions this document could not close

- **What fraction of the 17,073 forecasts are midpoint-anchored?** Not answerable from source;
  needs a database query. It determines whether we have any edge history at all.
- **What is our effective N** — the count of *distinct resolved events*, not records? The
  ~50× SE inflation from event clustering makes this the real sample size.
- **What is L_ρ on Kalshi at our size?** Every offline result in the prophets paper assumes
  L_ρ = 0, and Corollary 15 says D_G and L_ρ cancel exactly in the idealized case. Unmeasured,
  and it is the whole backtest-to-live gap.
- **How do we calibrate a belief *spread*?** Blocks conservative Kelly. Inter-trial variance is
  demonstrably not it.
- **What is L_max**, the logit clip on the Log allocator? A real parameter, currently unchosen.
- **Does the Politics calibration effect survive out-of-time?** The paper has no time split at
  all, and its window is dominated by one election cycle.
- **Confirm the Kalshi fee formula against the primary fee schedule PDF.** Sourced here from
  secondary references plus a corroborating quote in arXiv 2602.19520 (§9.2: "0.07 C p(1−p)
  (rounded up)"); the primary PDF returned HTTP 429 and was not read.

### 9.4 Sources

- Gu, Kagan, Sun, Wu, Xu — *When do prophets profit in prediction markets?*
  https://arxiv.org/abs/2607.06166 · https://arxiv.org/html/2607.06166v1
- Le — *Decomposing Crowd Wisdom: Domain-Specific Calibration Dynamics in Prediction Markets*
  https://arxiv.org/html/2602.19520v1 (v1) · https://arxiv.org/html/2602.19520v2 (v2, cite this one)
- Murphy — *Agentic Forecasting using Sequential Bayesian Updating of Linguistic Beliefs*
  https://arxiv.org/html/2604.18576
- *Beyond Forecasting: The Belief-to-Trade Layer in Prediction-Market Agents*
  https://arxiv.org/html/2607.03015v1
- Kalshi fee schedule (secondary; primary PDF not retrieved) https://kalshi.com/docs/kalshi-fee-schedule.pdf
