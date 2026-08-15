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

*(to be filled)*

## 5. Track 4 — Forecasts as distributions

*(to be filled)*

## 6. Track 5 — Conditional calibration

*(to be filled)*

## 7. Track 6 — Coherence / arbitrage engine (Probability Graph)

*(to be filled)*

## 8. Where the handed premise is wrong

*(to be filled)*

## 9. Open questions and recommended sequencing

*(to be filled)*
