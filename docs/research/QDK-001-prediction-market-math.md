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

*(to be filled)*

## 6. Track 5 — Conditional calibration

*(to be filled)*

## 7. Track 6 — Coherence / arbitrage engine (Probability Graph)

*(to be filled)*

## 8. Where the handed premise is wrong

*(to be filled)*

## 9. Open questions and recommended sequencing

*(to be filled)*
