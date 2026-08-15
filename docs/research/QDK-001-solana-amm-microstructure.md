# QDK-001 — Solana AMM microstructure: the market-state model for memecoins

**Status:** RESEARCH ONLY. No production code, no live execution, no trading, no
provider call was made in the course of writing this document.

**Scope:** establish the correct state model for Solana memecoin markets, which
trade on bonding curves and automated market makers rather than on limit order
books. Establish what transfers from classical microstructure, what must be
discarded, and what is honestly timeable.

**Evidence labels used throughout.** Every substantive claim carries one:

- **VERIFIED** — confirmed against a primary source (protocol documentation,
  on-chain program source, a paper that was fetched and read) or against a
  measurement already recorded in this repository, with a citation.
- **INFERRED** — a deduction from VERIFIED facts, with the deduction stated.
- **SPECULATIVE** — plausible, unconfirmed, and load-bearing for nothing.

---

## Table of contents

1. [The central architectural point: there is no book](#1-the-central-architectural-point-there-is-no-book)
2. [Impact mathematics: constant product, with fees, derived](#2-impact-mathematics-constant-product-with-fees-derived)
3. [Impact mathematics: the pump.fun bonding curve](#3-impact-mathematics-the-pumpfun-bonding-curve)
4. [What is actually uncertain — the real research question](#4-what-is-actually-uncertain--the-real-research-question)
5. [The AMM state vector, as a typed feature schema](#5-the-amm-state-vector-as-a-typed-feature-schema)
6. [Flow as a point process: does Hawkes transfer?](#6-flow-as-a-point-process-does-hawkes-transfer)
7. [Lifecycle as the dominant regime variable](#7-lifecycle-as-the-dominant-regime-variable)
8. [Adverse selection and toxicity without a market maker](#8-adverse-selection-and-toxicity-without-a-market-maker)
9. [What is genuinely timeable — an honest assessment](#9-what-is-genuinely-timeable--an-honest-assessment)
10. [DISCARD list: classical CLOB constructs that do not transfer](#10-discard-list-classical-clob-constructs-that-do-not-transfer)
11. [What our data can and cannot support today](#11-what-our-data-can-and-cannot-support-today)
12. [Bibliography, with verification status](#12-bibliography-with-verification-status)

---

## 1. The central architectural point: there is no book

Classical market microstructure — the body of work that gives us order-flow
imbalance, queue imbalance, the microprice, cancellation intensity, and queue
position — is **limit-order-book mathematics**. Every one of those constructs is
defined in terms of objects that exist only in a book: resting limit orders,
price levels with a finite queue, a time-priority rule that assigns you a
position in that queue, and a cancellation that removes an order before it
trades.

Solana memecoins do not have any of those objects. They trade on:

- **Bonding curves** (pump.fun and its imitators) during the pre-graduation
  phase, and
- **Constant-product / concentrated-liquidity AMMs** (Raydium, Meteora, Orca,
  PumpSwap) after graduation.

In both cases the venue is a **deterministic pricing function of pool state**,
not a queue of competing intentions. There is no resting order to be in front
of, no queue to be at the head of, and no cancellation to detect. The single
most common analytical error in this domain is to port a CLOB feature set onto
an AMM and then wonder why the features have no predictive content. They have no
content because the objects they measure do not exist.

**The correct reframing, in one sentence:** in a CLOB the *price* is uncertain
and the *state* is observable; in an AMM the *price is a known function of
state* and what is uncertain is **which state your transaction will actually be
applied to**.

That inversion drives this entire document. It means:

- The thing classical microstructure spends most of its effort estimating —
  price impact — is here a **closed-form identity** (§2, §3), not a statistical
  quantity.
- The thing classical microstructure largely takes for granted — that your order
  interacts with the book you just observed — is here **the whole problem** (§4).

A full list of what to discard, and what replaces each item, is in §10.

---

## 2. Impact mathematics: constant product, with fees, derived

### 2.1 The invariant and the swap formula

**VERIFIED (derivation; the underlying convention is Uniswap-V2-style
fee-on-input, cited in §12).** A constant-product pool holds reserves
\(x\) of the input asset and \(y\) of the output asset, maintaining

$$x \cdot y = k .$$

A swap of \(\Delta x\) input, with a proportional fee \(f\) taken **on the
input** (so only \(\gamma \Delta x\) with \(\gamma \equiv 1-f\) participates in
the invariant, while the full \(\Delta x\) is added to the reserve and the fee
accrues to LPs), must leave the invariant non-decreasing. Setting it equal:

$$(x + \gamma\,\Delta x)\,(y - \Delta y) = x\,y$$

$$\boxed{\;\Delta y \;=\; \frac{\gamma\, y\, \Delta x}{\,x + \gamma\,\Delta x\,}\;}$$

This is exactly the `getAmountOut` of the Uniswap V2 library with
\(\gamma = 997/1000\), and it is the form Raydium's constant-product pools
implement with their own fee constant. **It is an identity, not an estimate.**
Given \((x, y, f)\) and \(\Delta x\), the output is known to the last base unit
before the transaction is sent.

### 2.2 The three prices, which are not the same number

Confusing these three is the source of most bad P&L in this domain.

| price | definition | formula |
|---|---|---|
| **spot / marginal, pre-trade** \(p_0\) | the price of an infinitesimal trade | \(p_0 = y/x\) |
| **effective execution price** \(p_{\text{exec}}\) | what you actually pay, averaged over the trade | \(p_{\text{exec}} = \Delta x / \Delta y\) |
| **marginal, post-trade** \(p_1\) | the price a data provider will report after your trade | \(p_1 = (y-\Delta y)/(x+\Delta x)\) |

Define \(\tau \equiv \Delta x / x\), the trade size as a fraction of the
**input-side reserve**. Then:

$$\frac{p_{\text{exec}}}{p_0} = \frac{1+\gamma\tau}{\gamma}, \qquad
\frac{p_1}{p_0} = \frac{1}{\;\gamma \cdot \text{(as re-expressed below)}\;}$$

More usefully, in terms of value received versus value at spot, the **total
entry cost** (fee plus impact, as a fraction of notional) is

$$\boxed{\;S(\tau, f) \;=\; 1 - \frac{\gamma}{1+\gamma\tau}\cdot\frac{1}{1}\;=\;\frac{f + \gamma\tau}{1 + \gamma\tau}\;}$$

and its leading-order expansion is

$$S(\tau, f) \;\approx\; f + \tau \qquad\text{for small } \tau .$$

**This approximation is startlingly accurate.** Numerically checked against the
exact expression at our own measured pool sizes: at \(L=\$2{,}860,
n=\$500\) the exact cost is 35.216% and \(f+\tau\) gives 35.215%; at
\(L=\$67{,}119, n=\$10\) both give 0.280%. So the practical rule is:

> **Entry cost ≈ the fee, plus the trade size as a fraction of the quote-side
> reserve.** Nothing more sophisticated is needed to size a probe.

The post-trade marginal price satisfies \(p_1/p_0 = (1+\tau)/(1 -
\gamma\tau/(1+\gamma\tau))^{-1}\), which simplifies to a strictly larger move
than the execution price experienced — see §2.4, which is where this matters.

### 2.3 Converting provider TVL into reserves — the load-bearing step

Our data does not carry reserves. `CryptoPriceTick.liquidity_usd` and
`CryptoHorizonObservation.liquidity_usd` carry a provider-computed **USD TVL
aggregate** (`docs/milestones/SOLANA-ROUTE-OBSERVATION-001.md` §8.1 row 2:
"OBSERVABLE only as a provider-computed USD aggregate; true reserves remain
unparsed and unverified").

**INFERRED, and this inference is load-bearing.** For a two-sided
constant-product pool, the invariant forces both sides to hold **equal value**
at the pool's own marginal price. Therefore for a pool with TVL \(L\):

$$\text{quote-side reserve value} = L/2 \qquad\Longrightarrow\qquad
\boxed{\;\tau = \frac{2 \cdot \text{notional}}{L}\;}$$

**The factor of 2 is the single most consequential line in this document.**
`SOLANA-ROUTE-OBSERVATION-001` §4.1 tabulates each rung as a percentage of pool
TVL — N4 $500 is "17% of the median pool". The quantity that actually enters the
impact formula is **twice that**: \(\tau = 35\%\). Reading the §4.1 table as if
it were \(\tau\) understates entry cost by roughly a factor of two at every
rung.

*Caveats on the inference, stated because they bound it:* it holds exactly for
uniform constant-product pools (Raydium AMM v4/CPMM, PumpSwap, Uniswap-V2-style
venues). It does **not** hold for concentrated-liquidity pools (Orca Whirlpools,
Raydium CLMM) or discrete-bin pools (Meteora DLMM), where TVL may sit far from
the active price and the depth *at* the price can be arbitrarily smaller or
larger than \(L/2\) implies. For those venues the \(L/2\) conversion is an
**unbounded-error approximation** and the milestone's decision to treat impact
as OBSERVED-from-quote rather than modeled is the correct one.

### 2.4 What this costs at *our* measured pool sizes

Computed exactly (arbitrary-precision, no floats) against the measured
observation-time liquidity distribution from `SOLANA-ROUTE-OBSERVATION-001`
§4.2 (cohort 8, n=42) and the frozen V2 notional ladder from §4.1.

**Entry cost \(S\), fee = 0.25%:**

| rung | notional | p25 $1,936 | p50 $2,860 | p75 $11,578 | p95 $67,119 |
|---|---|---|---|---|---|
| N1 | $10 | 1.28% | 0.95% | 0.42% | 0.28% |
| N2 | $50 | 5.42% | 3.75% | 1.11% | 0.40% |
| N3 | $150 | 15.75% | 10.74% | 2.84% | 0.70% |
| N4 | $500 | **51.90%** | **35.22%** | 8.89% | 1.74% |

**Inverting the question — the largest notional that keeps total entry cost
under a budget:**

| entry-cost budget | p25 | p50 | p75 | p95 |
|---|---|---|---|---|
| 1% | **$7** | **$11** | $44 | $255 |
| 2% | $17 | $26 | $104 | $601 |
| 5% | $49 | $72 | $290 | $1,682 |

> **VERIFIED (arithmetic over our own measured distribution): at the median
> observed pool, an entry costing under 1% is an ELEVEN DOLLAR trade.** This is
> the hardest constraint in the entire problem and it is prior to any question
> of signal. A strategy that needs $500 clips does not exist at the median of
> this population; it exists only in the p95 tail, which is 2 tokens in 42.

### 2.5 Two results that overturn the intuitive cost picture

**(a) An *instantaneous* round trip costs only \(2f\), independent of size.**

| pool | notional | round-trip return | \(2f\) |
|---|---|---|---|
| $1,936 | $10 | −0.494% | 0.500% |
| $1,936 | $500 | −0.330% | 0.500% |
| $67,119 | $500 | −0.492% | 0.500% |

**VERIFIED (exact computation).** This is a genuine property of the constant
product invariant: buying moves you up the curve and selling immediately moves
you back down the *same* curve, so the impact is fully recovered. Only the fee
is lost — twice, and slightly less than twice because the second fee is charged
on a smaller base.

The naive reading of §2.4 — "$500 into the median pool costs you 35%" — is
therefore **wrong as a statement about round-trip cost** in the no-intervening-
flow case. Entry slippage is not a loss; it is a position on the curve that you
recover on exit. This matters enormously, because it means the cost of trading
thin AMM pools is *not* the impact figure everyone quotes.

**(b) So where does the money actually go? Into liquidity decay.** The impact
cancels only if you exit against **the same curve**. Our own data says you will
not. `SOLANA-ROUTE-OBSERVATION-001` §14.1 M12 measures median liquidity falling
from $13,586 at birth to $2,860 at observation — **4.75×**. Modelling that
honestly (enter at birth-time liquidity, exit at horizon liquidity, **true price
held constant**, both reserves rescaled by the decay so the price does not
move):

| notional | entry slip | exit slip | **net round trip** |
|---|---|---|---|
| $10 | 0.40% | 0.94% | −1.04% |
| $50 | 0.99% | 3.63% | −3.16% |
| $150 | 2.46% | 9.87% | **−8.11%** |
| $500 | 7.61% | 27.38% | **−22.23%** |

> **VERIFIED (exact computation over our own measured decay factor). At the
> median, a $500 round trip loses 22% of notional to microstructure alone, with
> the token's true price completely unchanged.** The loss is not the entry
> impact — it is the *asymmetry* between a fat entry curve and a thin exit
> curve. Liquidity decay is the dominant execution cost in this asset class, and
> it is a **lifecycle** variable, not a market-impact variable. This is the
> single strongest argument in this document for §7's claim that lifecycle stage
> is a first-order state variable.

### 2.6 The mark-to-market illusion — a direct warning for paper P&L

The price a data provider reports after a swap is the **post-trade marginal
price** \(p_1\), not your execution price \(p_{\text{exec}}\). In a thin pool
these diverge violently, and \(p_1 > p_{\text{exec}}\) always for a buy.

| pool TVL | notional | your avg exec vs spot | reported marginal move | **phantom gain if marked at \(p_1\)** |
|---|---|---|---|---|
| $1,936 | $500 | +51.90% | **+129.79%** | **+51.27%** |
| $2,860 | $500 | +35.22% | **+82.04%** | **+34.63%** |
| $2,860 | $150 | +10.74% | +22.05% | +10.21% |
| $11,578 | $500 | +8.89% | +18.00% | +8.37% |
| $67,119 | $500 | +1.74% | +3.00% | +1.24% |

> **VERIFIED (exact computation). A $500 buy into the median observed pool
> raises the reported price by 82% and, if the resulting position is marked at
> the reported price, shows an instantaneous 35% "profit" that is entirely the
> trade's own footprint.**

This is a concrete, quantified hazard for the shared paper ledger described in
the project's roadmap. Any `PaperFill` → `Position` → `RealizedPaperPnL` chain
that marks positions using `CryptoPriceTick.price_usd` **will manufacture
profits out of its own simulated impact** unless (i) the simulated trade's
effect on the pool is applied to the mark, or (ii) positions are marked at an
*exit-side executable quote* rather than at a mid price. This is an
implementation requirement, stated here as a research finding rather than as
code.

---

