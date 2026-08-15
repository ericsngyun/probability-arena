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

## 3. Impact mathematics: the pump.fun bonding curve

### 3.1 The curve is a constant product over *virtual* reserves

**VERIFIED** against pump.fun's own public documentation repository
(https://github.com/pump-fun/pump-public-docs), which states the curve "is based
on Uniswap V2 and uses synthetic x and y reserves". Program ID
`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`. Constants read from the
documented on-chain `Global` account
`4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf`:

| field | raw value | human |
|---|---|---|
| `initial_virtual_sol_reserves` | 30,000,000,000 | **30 SOL** |
| `initial_virtual_token_reserves` | 1,073,000,000,000,000 | **1,073,000,000 tokens** |
| `initial_real_token_reserves` | 793,100,000,000,000 | **793,100,000 tokens** |
| `token_total_supply` | 1,000,000,000,000,000 | 1,000,000,000 tokens |
| `fee_basis_points` | 100 | 1% — **legacy fallback only, see §3.4** |
| `pool_migration_fee` | 15,000,001 | lamports |

So the swap mathematics is **identical in form to §2**, with \((x,y)\) replaced
by virtual reserves \((S, T)\):

$$\Delta T = \frac{\gamma\,T\,\Delta S}{S + \gamma\,\Delta S}, \qquad k = S\cdot T = 30 \times 1{,}073{,}000{,}000 = 3.219\times10^{10}.$$

**The entire §2 apparatus therefore applies unchanged to the bonding-curve
phase** — including the \(S \approx f + \tau\) rule — with one crucial
substitution: \(\tau\) is measured against the **virtual** SOL reserve, not
against any real balance. That substitution is what makes the early curve far
less brutal than its real deposits would suggest (§3.3).

### 3.2 Graduation is token-denominated, and the "85 SOL" figure is exact

**VERIFIED:** completion is set "at the end of a `buy` instruction, when
`real_token_reserves == 0`" — i.e. graduation triggers when the 793,100,000
saleable tokens are exhausted, **not** when a SOL or market-cap target is hit.
The SOL figure is a *consequence*, and it is exactly computable:

$$T_{\text{end}} = 1{,}073{,}000{,}000 - 793{,}100{,}000 = 279{,}900{,}000$$
$$S_{\text{end}} = k / T_{\text{end}} = 115.00536\ \text{SOL} \;\Longrightarrow\; \text{real SOL raised} = 85.00536\ \text{SOL}$$

**DERIVED (exact arithmetic on VERIFIED constants): the widely quoted "~85 SOL
graduation" is 85.00536 SOL, and it is a deterministic property of the
constants, not a policy target.**

The price move across the whole curve is likewise exact:

$$\frac{p_{\text{end}}}{p_{\text{start}}} = \left(\frac{T_0}{T_{\text{end}}}\right)^{2} = \left(\frac{1{,}073}{279.9}\right)^{2} = \mathbf{14.6958\times}$$

> **DERIVED, and worth internalising: a pump.fun token that completes its curve
> rises exactly 14.70× from the first buy to graduation. Not approximately —
> exactly, by construction.** Any narrative about a pre-graduation token "going
> up 100×" is describing either a different venue, a post-graduation move, or
> nothing at all. This is one of the few places in this entire domain where a
> hard ceiling can be stated with certainty, and it is a useful falsifier for
> claims encountered in the wild.

**FLAGGED — a citation that does not check out.** The commonly repeated
"graduates at ~$69,000 market cap" figure appears in **no primary artifact** we
could locate. It is a SOL-price-dependent restatement of the 85 SOL constant and
implies a SOL price that has not held for some time. **Do not treat it as a
protocol constant.** The 85.00536 SOL figure is the real invariant.

### 3.3 Depth rises along the curve, then falls after graduation

Because \(\tau = \Delta S / S\) and the virtual SOL reserve *grows* from 30 to
115 as the curve fills, **price impact for a fixed notional falls monotonically
as the curve progresses.** Computed exactly, at the current 1.25% bonding-curve
fee (§3.4):

| curve progress | virtual SOL reserve | cost of a 0.1 SOL buy | 1 SOL buy | 5 SOL buy |
|---|---|---|---|---|
| 0% | 30.00 | 1.57% | 4.40% | 15.21% |
| 25% | 36.80 | 1.51% | 3.83% | 12.93% |
| 50% | 47.59 | 1.45% | 3.26% | 10.53% |
| 75% | 67.32 | 1.39% | 2.68% | 8.00% |
| 99% | 111.84 | 1.34% | 2.11% | 5.43% |

Now place that beside the post-graduation pools we actually measure. Putting
both on one scale needs a SOL price; $150 is used **purely as an illustrative
scale factor and nothing below depends on it** except the two dollar columns.

**Cost of a $150 entry (the N3 rung) across the lifecycle:**

| lifecycle point | effective quote reserve | \(\tau\) | entry cost |
|---|---|---|---|
| pump.fun curve, at launch | 30 SOL ≈ $4,500 (virtual) | 3.33% | 4.40% |
| pump.fun curve, at graduation | 115 SOL ≈ $17,251 (virtual) | 0.87% | **2.09%** |
| graduated pool, **birth** TVL $13,586 | $6,793 | 2.21% | 2.40% |
| graduated pool, **horizon** TVL $2,860 | $1,430 | 10.49% | **9.70%** |

> **DERIVED, and it is the structural result of this section: effective depth is
> NON-MONOTONIC across the memecoin lifecycle. It rises through the bonding
> curve, peaks at or shortly after graduation, and then decays.** The decayed
> post-graduation pool at our measured median ($1,430 quote reserve) is
> **shallower than the pump.fun curve was on its very first buy** ($4,500
> virtual). A token that has "made it" — graduated to a real AMM — is, at the
> horizon where we observe it, a worse venue to transact than the launchpad it
> escaped.

This is independent corroboration of §7's central claim from a completely
different direction: the lifecycle stage variable is not a control, it is the
dominant determinant of execution cost, and it moves in both directions.

### 3.4 The fee is 1.25%, not 1% — a correction worth propagating

**VERIFIED** (https://github.com/pump-fun/pump-public-docs,
`docs/FEE_PROGRAM_README.md`): pump.fun replaced the flat fee with a
**market-cap-tiered dynamic fee**. `computeFeesBps()` reads a `FeeConfig` PDA
and selects a tier by market cap; **only when `feeConfig == null` does it fall
back to `global.feeBasisPoints`, which is the legacy 100 bps.** That fallback is
the origin of the "1%" figure now circulating everywhere.

The documented tier table runs from **1.25% total on the bonding curve** (0.300%
creator + 0.950% protocol + 0% LP) monotonically down to **0.30%** for
$20M+ market caps.

> **Any model using 1% for the bonding-curve phase under-costs by 25 bps —
> a 25% relative error in the fee term.** At the N1 control rung, where §2.4
> shows the fee *is* essentially the entire cost, that error is most of the
> measurement.

Two further mechanics that a naive integration gets wrong, both VERIFIED:

- **Graduation now migrates to PumpSwap, not Raydium.** `migrate()` moves a
  completed curve's liquidity to the PumpSwap AMM
  (`pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`, "a constant-product AMM"); it
  is permissionless and idempotent, and LP tokens are **burnt**. The legacy
  `withdraw`-to-Raydium path is disabled. Any `dex_id`-based lifecycle logic
  keyed on a Raydium destination is now wrong. *(The commonly cited March 2025
  date for this change we could **not** verify; the substance is verified, the
  date is not.)*
- **Not every coin is SOL-quoted any more.** `buy_v2` documents USDC-paired
  coins. A SOL-only price model is wrong for some tokens.

### 3.5 Meteora DLMM breaks the constant-product assumption entirely

**VERIFIED** (https://docs.meteora.ag/core-products/dlmm/formulas.md), and this
is important enough to sit in the impact section rather than a footnote:

Meteora's DLMM prices in **discrete bins**, \(P_i = (1 + \text{bin\_step}/10^4)^i\),
and **within a bin the invariant is constant-SUM**, \(L = P\cdot x + y\).

> **The consequence is stark: price impact inside a DLMM bin is exactly ZERO.**
> The price does not move at all until the active bin's output side is
> exhausted, at which point it jumps to the next bin. Swap output is literally
> \(\lfloor \text{amount\_in} \cdot P \rfloor\).

So DLMM impact is a **step function**, not the smooth convex curve of §2.
Applying §2's \(S \approx f + \tau\) to a Meteora pool is not an approximation
with a small error — it is the wrong functional form. DLMM also carries a
**volatility-dependent dynamic fee** (`total_fee = base + variable`, where the
variable component scales with the square of a decaying volatility accumulator),
so even the fee term is not a constant.

Similarly, **Orca Whirlpools and Raydium CLMM are concentrated-liquidity**
venues where TVL may sit far from the active price, so §2.3's \(L/2\) conversion
has unbounded error in both directions.

> **This is the concrete justification for the `pool_kind` feature (A6) and for
> making `quote_reserve_usd_est` (A2) typed-absent on non-CPMM venues.** Three
> distinct impact mathematics coexist in this market — smooth constant product,
> stepped constant sum, and concentrated liquidity — and a state model that
> applies one formula everywhere is silently wrong on an unknown fraction of the
> population. **Measuring the venue mix in our own `dex_id` column is a free T0
> study that nothing in §11 currently proposes, and it should be S-0.**

### 3.6 Verified fee reference (for whoever builds A4)

Collected here because §5.1's A4 explicitly forbids a guessed per-dex fee table
— this is the *verified* one, and even it must be read from the pool, not
assumed.

| venue | tiers | default | note |
|---|---|---|---|
| **Raydium AMM v4** | 0.25% only | 0.25% | fee encoded as x/10,000 |
| **Raydium CPMM** | 0.01% / 0.25% / 1% | 0.25% | **encoded as x/1,000,000** |
| **Raydium CLMM** | 0.01% / 0.05% / 0.25% / 1% | 0.25% | encoded as x/1,000,000 |
| **Orca Whirlpools** | 0.01%–2.00% across 9 tick spacings | per-pool | 87% LP / 12% DAO / 1% Climate |
| **Meteora DLMM** | base + variable, cap 10% | per-pool | dynamic, volatility-scaled |
| **pump.fun curve** | tiered by mcap | **1.25%** | 1% is the legacy fallback |
| **PumpSwap (non-canonical)** | 20 bps LP + 5 bps protocol | 0.25% | canonical pools use the tier table |

> **A trap worth naming: Raydium AMM v4 encodes its fee as x/10,000 while CPMM
> and CLMM encode as x/1,000,000.** Reading one with the other's denominator is
> a **100× error** in the fee term. This is exactly the kind of silent numeric
> wrongness that `SOLANA-ROUTE-OBSERVATION-001` §5.6 built its no-float,
> canonical-decimal discipline to catch.

---

## 4. What is actually uncertain — the real research question

§2 and §3 establish that impact is a **closed form**. This section is therefore
the actual research frontier: given that the pricing function is known, what
stops you from knowing your fill?

Five sources, ordered by how much they matter at our sizes.

### 4.1 Ordering within a block is priority-greedy, not FIFO — and not deterministic from your side

**VERIFIED** from Agave master
(https://github.com/anza-xyz/agave, `core/src/validator.rs`): the default block
production method is now `CentralSchedulerGreedy`; the older prio-graph
`CentralScheduler` is **deprecated** ("will be removed in a future release").
The greedy scheduler's own doc comment describes it as scheduling "in priority
order, scheduling anything that can be immediately scheduled, up to the limits",
with `target_scheduled_cus = MAX_BLOCK_UNITS / 4` and
`max_scanned_transactions_per_scheduling_pass = 100_000`.

Critically, it assigns work across threads under `ThreadAwareAccountLocks`, and
a transaction that cannot take its account locks is deferred to
`unschedulables` and retried.

> **INFERRED, and it is the correct mental model: your position in the block is
> a function of your fee AND of contention on the specific accounts your swap
> touches.** For a hot memecoin pool, *every* swap contends on the same pool
> accounts, so the account-lock path is the binding constraint precisely in the
> situation where ordering matters most. You cannot compute your own ordering
> ex ante, and paying more does not straightforwardly fix it.

**FLAGGED AS UNVERIFIED:** the exact priority-score formula (widely stated as
fee-per-compute-unit) and the prio-graph look-ahead depth. The authoritative
Anza write-up was not reachable from our environment. **Do not assert either
without reading `transaction_priority_id.rs`.**

### 4.2 Concurrent swaps in the same slot are the dominant fill uncertainty

**VERIFIED:** Solana's slot target is 400 ms — and this is not folklore, it is a
compile-time assertion in the SDK
(https://github.com/anza-xyz/solana-sdk, `clock/src/lib.rs`):

```rust
pub const DEFAULT_TICKS_PER_SECOND: u64 = 160;
pub const DEFAULT_TICKS_PER_SLOT: u64 = 64;
static_assertions::const_assert_eq!(DEFAULT_MS_PER_SLOT, 400);
pub const NUM_CONSECUTIVE_LEADER_SLOTS: u64 = 4;
pub const FORWARD_TRANSACTIONS_TO_LEADER_AT_SLOT_OFFSET: u64 = 2;
pub const HOLD_TRANSACTIONS_SLOT_OFFSET: u64 = 20;
```

Two consequences that are directly load-bearing:

1. **The pool state you quoted against is at best one slot stale, and clients
   forward to the leader 2 slots ahead and may hold up to 20.** So the gap
   between "state I priced against" and "state my swap is applied to" is on the
   order of **0.8–8 seconds**, not milliseconds.
2. **Every swap that lands ahead of yours in the same block moves the curve you
   execute against, deterministically, by the §2 formula.** This is not noise
   with zero mean. On a token with directional flow it is systematically against
   you.

> **This is the single largest genuine uncertainty at our notional sizes, and it
> is quantifiable in principle.** §2 gives the exact impact of any preceding
> swap. So the fill distribution is fully determined by the distribution of
> *preceding same-slot flow* — which is precisely feature C4 (`net_signed_flow`)
> at tier **T3**. **The one thing we would most need to model fills is the one
> thing our capability boundary excludes.** That is a clean, honest statement of
> where the wall is.

**ALPENGLOW — VERIFIED status, and it does not change this.** The Alpenglow
upgrade (https://solana.com/upgrades/alpenglow) targets ~150 ms finality versus
today's ~400 ms pre-confirmation, with mainnet activation targeted Q3 2026 and
two prerequisites already activated (BLS Pubkey Management, 2026-07-08;
Validator Admission Ticket, 2026-07-22). **The page describes no change to slot
duration.** Faster finality does not reduce same-slot concurrency, which is the
mechanism above. *(Secondary sources give contradicting mainnet dates; treat
anything beyond "not on mainnet as of 2026-08-14" as unverified.)*

### 4.3 MEV and sandwiching — no public mempool, and it happens anyway

This is the part most commonly reasoned about wrongly. The premise "Solana has
no public mempool, therefore sandwiching is impossible" is **false**, and there
is a peer-reviewed measurement of exactly how false.

**VERIFIED — primary source:** Gerzon, Weintraub, In, Mislove, Nita-Rotaru,
*"Quantifying the Threat of Sandwiching MEV on Jito: A Measurement of Solana's
Leading Validator Client"*, **ACM IMC '25**, DOI
[10.1145/3730567.3764493](https://doi.org/10.1145/3730567.3764493), PDF:
https://cnitarot.github.io/papers/imc26_solana.pdf

Findings, from the paper:

- Solana's original design "lacks a *public mempool*… making MEV impossible for
  non-validator users, since only the validators are privy to the information
  necessary to conduct MEV attacks."
- Jito opened a public mempool in August 2022 and **suspended it in March 2024**
  citing "negative externalities impacting users on Solana."
- **Suspending it did not work.** Tips per day and Jito network utilisation
  "have only increased since then," and the authors report speculation that some
  validators "re-created the mempool by privately collaborating."
- Measurement window 2025-02-09 → 2025-06-09: **521,903 sandwiching instances,
  costing users over $7.7M.**
- Mechanism: the victim transaction "is instead **included in a Jito bundle**
  surrounded by an attacker's transactions." **The attacker does not need a
  mempool because the victim's transaction is routed to them.**
- **Over 86% of Jito bundles contain a single transaction** with tips too small
  for priority placement — i.e. they are purely **defensive**, costing users over
  **$2.4M** in the period "that provide little benefit beyond preventing
  Sandwiching."
- As of Sept 2025, **97% of the top 500 validators run a Jito-compatible
  client**, including the entire super-minority.
- **The authors state these are lower bounds** — they analyse only length-3
  bundles (~2.77% of daily bundles) and exclude non-SOL-denominated trades.

Supporting mechanics, **VERIFIED** from https://docs.jito.wtf/lowlatencytxnsend/:
bundles are **max 5 transactions**, executed "sequentially and atomically",
priced in a **priority auction** where "parallel auctions are run at 50 ms
ticks" — roughly **8 auction rounds per 400 ms slot** — ordered by "requested
tip/cus-requested efficiency", minimum tip 1,000 lamports.

> **INFERRED, and it is the operationally useful conclusion: sandwich exposure
> on Solana is a function of your ORDERFLOW ROUTING, not of mempool visibility.**
> If your transaction reaches a leader through a path that also reaches a
> searcher, you are exposed regardless of how private the network looks. This
> means sandwich risk is not a property of the token or the pool — the state
> variables of §5 cannot predict it — it is a property of **how you submit**.
> It therefore belongs in an execution-policy model, not in the market-state
> model this document specifies.

### 4.4 Slippage-tolerance failures cost money and return nothing

**VERIFIED — Raydium** (https://docs.raydium.io/user-flows/swap.md):
`minimumAmountOut = expectedAmountOut × (1 − slippage)`; "if actual output would
be less, the tx reverts with `ExceededSlippage`." UI defaults are 0.5% general
and **"2–5% for meme tokens"**, which is itself a useful piece of evidence about
what practitioners expect.

**VERIFIED — Jupiter** (https://developers.jup.ag/docs/swap/advanced/slippage.md):
the Real-Time Slippage Estimator "estimates slippage at **order time** (when you
call `/order` or `/build`), **not at execution time**. The estimated slippage is
baked into the transaction." The Router path defaults to a fixed
`slippageBps=50` (0.5%); RTSE is opt-in. The enforced floor surfaces as
`otherAmountThreshold`.

**VERIFIED — and this is the expensive part.** SIMD-0191 ("Relax Transaction
Loading Constraints", **Activated**,
https://github.com/solana-foundation/solana-improvement-documents/blob/main/proposals/0191-enable-transaction-loading-failure-fees.md)
defines a Runtime Transaction Error as one that "results in a failed
transaction, and may be included in the block. **These transactions still incur
transaction fees, and nonce advancements.**"

> **A slippage revert is included in the block, charged the base fee (5,000
> lamports/signature) and the full priority fee, and returns no fill.** So the
> cost of a failed attempt is not zero, and at our N1 $10 control rung a few
> failed attempts can exceed the notional. Any honest fill model must carry a
> **failure branch with a non-zero cost**, not a retry-until-success loop.

Note also that Jupiter's RTSE bakes a slippage estimate in at *order* time —
which means the tolerance itself is computed against the same stale state as the
quote, compounding §4.2 rather than mitigating it.

### 4.5 Liquidity changing between quote and execution

The fifth source, and the one our own data speaks to most directly. §7.1 puts
the median pool half-life at **2.7–10.7 hours**. Over a quote-to-execution gap
of a few seconds (§4.2) that is negligible.

> **INFERRED: at the timescale of a single transaction, liquidity change is the
> SMALLEST of the five uncertainty sources. At the timescale of a holding
> period, §2.5(b) shows it is the LARGEST cost in the entire system.** The same
> variable is nearly irrelevant at 1 second and dominant at 6 hours. Any model
> that carries one "liquidity risk" number for both horizons is wrong at one of
> them.

### 4.6 Summary — where the uncertainty actually is

| source | magnitude at our sizes | observable to us? |
|---|---|---|
| Same-slot concurrent flow (§4.2) | **largest** per-transaction | **No** — needs C4 at tier T3 |
| Intra-block ordering / account contention (§4.1) | large, coupled to the above | No |
| Sandwich extraction (§4.3) | material, measured at scale industry-wide | No — and it is a routing property, not a state property |
| Slippage-revert cost (§4.4) | small per event, non-zero, asymmetric | Partially — it is a modelling choice we control |
| Liquidity change quote→execution (§4.5) | negligible at 1s, **dominant at 6h** | **Yes** — F6, and it is the one we can actually measure |

> **The through-line: of the five things that make a fill uncertain, exactly one
> is observable inside our boundary — and it happens to be the one that
> dominates at the holding horizons we actually care about.** That is a much
> better position than it first appears, and it argues strongly for spending
> effort on §11's lifecycle studies rather than on execution modelling we cannot
> validate.

---

## 5. The AMM state vector, as a typed feature schema

### 5.0 The observability tiers, declared first

Every feature below is tagged with the tier that *actually supplies it*, because
the difference between "this is the right feature" and "we can compute this" is
where most research plans quietly fail.

| tier | source | in bounds today? | marginal cost |
|---|---|---|---|
| **T0** | already-persisted rows (`crypto_price_ticks`, `crypto_token_birth_events`, `crypto_token_risk_assessments`, `crypto_token_lifecycle_snapshots`) | **yes** — pure SQL, zero provider calls | ~0 |
| **T1** | one additional DexScreener read per token per pass, via the existing read-only adapter | **yes**, bounded by `OBSERVE_MAX_CALLS` and the sparse lane's cadence | 1 provider call |
| **T2** | route/quote endpoint response | **pending** — the subject of `SOLANA-ROUTE-OBSERVATION-001`, currently at CP-0 with M4 unverified | 2 calls per rung-direction |
| **T3** | per-swap on-chain feed (swap events with signer, direction, size, slot) | **NO — explicitly out of bounds.** `SOLANA-ROUTE-OBSERVATION-001` §3.2 F9 forbids the paid per-trade feed; §8.1 row 6 records realized slippage as consequently unobservable | n/a |

**The single most important line in this section:** almost every feature that
classical microstructure would call "flow" lives in **T3**, and T3 is closed.
That is a declared capability boundary, not an engineering gap. The schema below
is therefore explicit about which parts of the state vector are **structurally
unavailable to us today** rather than merely unimplemented.

### 5.1 The schema

Absence vocabulary reuses the closed set frozen in
`SOLANA-ROUTE-OBSERVATION-001` §5.4 (`not_returned_by_venue`,
`venue_returned_null`, `venue_returned_unparseable`, `derivation_input_absent`,
`no_response`, `request_not_issued`, `population_truncated`), extended with
three this schema needs and that lane does not: **`feed_not_available`** (the
quantity requires T3), **`insufficient_history`** (a window statistic whose
window is not yet full), and **`pre_graduation`** (the quantity is undefined
while the token is still on a bonding curve).

#### Block A — pool state (the AMM's actual state variables)

| # | feature | definition | tier | update freq | cost | typed absence |
|---|---|---|---|---|---|---|
| A1 | `pool_tvl_usd` | provider-computed pool TVL, \(L\) | T0/T1 | per observation | ~0 / 1 call | `venue_returned_null` |
| A2 | `quote_reserve_usd_est` | \(L/2\) — DERIVED, valid **only** for uniform CPMM (§2.3) | T0 | per observation | ~0 | `derivation_input_absent`; **must be absent for CLMM/DLMM venues** |
| A3 | `reserve_base`, `reserve_quote` | true on-chain reserves, base units | **T3** | per slot | n/a | `feed_not_available` — **never estimated into this column** |
| A4 | `fee_bps` | the pool's fee tier | T1 | static per pool | ~0 | `not_returned_by_venue`; **never a per-dex default table** — that is precisely the fabrication `SOLANA-ROUTE-OBSERVATION-001` §5.3 forbids |
| A5 | `dex_id` | venue label, verbatim and unmapped | T0 | static | ~0 | `venue_returned_null` |
| A6 | `pool_kind` | `cpmm` / `clmm` / `dlmm` / `bonding_curve` / `unknown` | INFERRED from A5 | static | ~0 | `unknown` is a first-class value, not an absence |
| A7 | `pool_age_seconds` | now − `pair_created_at` | T0 | continuous | ~0 | `derivation_input_absent` |
| A8 | `tvl_log_return_Δ` | \(\ln L_t - \ln L_{t-\Delta}\) | T0 | per observation pair | ~0 | `insufficient_history` |
| A9 | `tvl_drawdown_from_peak` | \(1 - L_t/\max_{s\le t} L_s\) over persisted ticks | T0 | per observation | ~0 | `insufficient_history` |

**A2 and A6 together carry the §2.3 caveat.** A2 must be typed-absent whenever
A6 is not `cpmm`, because for concentrated-liquidity venues the \(L/2\)
conversion has unbounded error. Emitting A2 for an Orca Whirlpool would be
exactly the class of silent wrongness this repository has spent several
milestones removing.

#### Block B — price and volatility

| # | feature | definition | tier | update freq | cost | typed absence |
|---|---|---|---|---|---|---|
| B1 | `price_usd` | provider mid | T0/T1 | per observation | ~0 | `venue_returned_null` |
| B2 | `log_return_Δ` | \(\ln p_t - \ln p_{t-\Delta}\) | T0 | per pair | ~0 | `insufficient_history` |
| B3 | `price_churn_window` | stdev of B2 over a window — **deliberately not named `realized_vol`** | T0 | per window | ~0 | `insufficient_history` |
| B4 | `price_change_5m`, `price_change_1h` | provider-reported | T0 | per observation | ~0 | `venue_returned_null` |
| B5 | `market_cap`, `fdv` | provider-reported | T0 | per observation | ~0 | `venue_returned_null` |
| B6 | `fdv_to_tvl` | B5 / A1 — how much notional claim sits above how much real depth | T0 | per observation | ~0 | `derivation_input_absent` |

> **B3 carries a hard warning, and the name change is the warning.** Realized
> volatility computed from a provider mid on a thin AMM is **not** the
> volatility of an efficient price. §2.6 shows a single $500 swap moves the
> median pool's marginal price by 82%. What B3 measures at these pool sizes is
> predominantly **the impact footprint of individual swaps**, not information
> arrival. It remains a legitimate feature — it proxies "how big are trades
> relative to the pool" — but calling it volatility imports every classical
> volatility intuition, all of which are wrong here.

#### Block C — flow (the block where our position is weakest)

| # | feature | definition | tier | update freq | cost | typed absence |
|---|---|---|---|---|---|---|
| C1 | `volume_5m/1h/24h_usd` | provider-reported, **unsigned aggregate** | T0/T1 | per observation | ~0 | `venue_returned_null` |
| C2 | `volume_to_tvl` | C1 / A1 — turnover; the best T0 flow proxy we have | T0 | per observation | ~0 | `derivation_input_absent` |
| C3 | `swap_count_by_direction` | buys and sells counted separately | **T3** | per slot | n/a | `feed_not_available` |
| C4 | `net_signed_flow` | \(\sum\) buy notional − \(\sum\) sell notional | **T3** | per slot | n/a | `feed_not_available` |
| C5 | `trade_size_distribution` | quantiles of per-swap notional | **T3** | per window | n/a | `feed_not_available` |
| C6 | `unique_signers_window` | distinct swapping addresses | **T3** | per window | n/a | `feed_not_available` |
| C7 | `liquidity_add_events`, `liquidity_remove_events` | LP mint/burn events, signed and sized | **T3** | per slot | n/a | `feed_not_available` |
| C8 | `tvl_jump_unexplained` | a change in A1 too large to be explained by C1's volume — a **detected but unattributed** liquidity event | T0 | per observation pair | ~0 | `insufficient_history` |

**C8 is the honest T0 substitute for C7, and it is worth building.** We cannot
see LP burn events, but a pool whose TVL falls 60% across an interval in which
reported volume was $200 did not lose that TVL to trading — the arithmetic does
not close. That is a **liquidity-removal detector built entirely from data we
already persist**, and it is the closest thing to a rug signal available inside
our current boundary. It is a *detector*, not an *attribution*: it cannot
distinguish an LP withdrawal from a single enormous swap the provider
under-reported, and the feature must carry that ambiguity rather than resolve it.

#### Block D — venue dispersion

| # | feature | definition | tier | update freq | cost | typed absence |
|---|---|---|---|---|---|---|
| D1 | `pair_count` | number of pools for the token | T0 | per observation | ~0 | `venue_returned_null` |
| D2 | `single_venue` | D1 == 1 | T0 | per observation | ~0 | `derivation_input_absent` |
| D3 | `tvl_herfindahl` | \(\sum_i (L_i/\sum_j L_j)^2\) across pools | T1 | per observation | 1 call | `derivation_input_absent` |
| D4 | `best_pair_share` | largest pool's share of total TVL | T1 | per observation | 1 call | `derivation_input_absent` |
| D5 | `route_hops`, `route_split` | from the quote response | **T2** | per quote | 1 quote | `route_not_returned` |

`CryptoTokenLifecycleSnapshot` already carries D1, D2 and `best_pair_address`,
so D3/D4 extend existing persisted structure rather than adding capability.

#### Block E — holder and actor structure

All of Block E is **already modelled** in `CryptoTokenLifecycleSnapshot` and
`CryptoTokenActorObservation`. Tier T1, from persisted risk-assessment rows.
Coverage is the open question; the schema is not.

| # | feature | note |
|---|---|---|
| E1 | `top10_holder_pct` | concentration; the standard rug prior |
| E2 | `creator_pct` / `creator_holding_pct` | the creator's remaining claim on the float |
| E3 | `sniper_pct`, `bundler_pct`, `insider_pct` | **provider-labelled cohorts. Their definitions are the provider's, not ours, and must never be treated as ground truth** — they are a vendor's heuristic with an unpublished threshold |
| E4 | `holder_count` and its growth rate | the only genuine *adoption* series available at T0/T1 |
| E5 | `known_creator_cluster_ref`, `repeated_cohort_ref` | **currently placeholders with no behavior** (`app/models.py`: "placeholders for later cross-token cohort analysis (no behavior today)"). §9 argues this is the highest-value unbuilt feature in the schema. |

#### Block F — lifecycle state (see §7 for why this block dominates)

| # | feature | definition | tier | typed absence |
|---|---|---|---|---|
| F1 | `lifecycle_stage` | the regime label of §7 | INFERRED from A/B/C | `unknown` is first-class |
| F2 | `bonding_curve_state` | already a column on `CryptoTokenBirthEvent` | T1 | `venue_returned_null` |
| F3 | `curve_progress_pct` | fraction of the bonding curve sold | T1/T3 | `not_applicable` post-graduation |
| F4 | `graduated_or_migrated` | already a column on `CryptoTokenSurvivalOutcome` | T0 | NULL = not yet measurable |
| F5 | `token_age_seconds` | now − `first_evidence_at` | T0 | `derivation_input_absent` |
| F6 | `tvl_decay_ratio` | \(L_{\text{birth}} / L_{t}\) — **the variable §2.5(b) proves dominates execution cost** | T0 | `derivation_input_absent` |
| F7 | `mint_authority_enabled`, `freeze_authority_enabled` | already on `CryptoTokenBirthEvent` | T0 | NULL-honest |

**F6 deserves its own line.** It is computable today from two columns we already
persist (`CryptoTokenBirthEvent.initial_liquidity_usd` and
`CryptoHorizonObservation.liquidity_usd`), it costs one division, and §2.5(b)
shows it drives realized execution cost more strongly than any impact term.
**It is the highest value-per-unit-effort feature in this schema.**

### 5.2 What the schema says about our current observation design

Blocks A, B, D, E and F are substantially computable from data we already hold.
Block C is not — and the sparse observation lane's design makes Block C harder
in a specific way worth stating plainly.

`app/services/crypto_sparse_observation.py` buys **exactly two observations per
token, at 6h and 24h** (`SPARSE_HORIZONS`), because the coverage cliff it exists
to repair sits there (15m/1h coverage was already 80.9%/81.1%). That is the
right decision *for repairing a survival-label denominator*. It is close to the
worst possible sampling design *for microstructure*, which needs many
observations close together, not two observations eighteen hours apart.

**This is not a criticism of the lane — it is the correct read of what the lane
is for.** But the consequence is sharp:

> **INFERRED, and load-bearing for sequencing: no amount of analysis over the
> sparse lane's output will produce a flow model.** Two points per token support
> lifecycle and decay features (A8, A9, F6) very well and support nothing in
> Block C at all. A flow model needs a different acquisition design, and the
> honest first question is whether the T0/T1 lifecycle features alone carry
> enough signal to justify building one. §9 argues they might, and that this is
> the correct order of work.

---

## 6. Flow as a point process: does Hawkes transfer?

The brief asks specifically whether marked/multivariate Hawkes self-excitation is
the right model for Solana swap arrivals, and instructs that the continuous-time
formalism not be assumed to transfer. This section takes that instruction
seriously and reaches a **qualified no**.

### 6.1 What would have to be true

A univariate Hawkes process has conditional intensity

$$\lambda(t) = \mu + \sum_{t_i < t} \phi(t - t_i)$$

and its standard maximum-likelihood estimator assumes a **simple** point process:
event times are distinct, observed on a continuum, and the excitation kernel
\(\phi\) is resolvable at the lags where it carries mass. The multivariate/marked
extension — the version actually wanted here, with types {buy, sell, liquidity
add, liquidity remove} and marks for size — inherits all of those assumptions
per component and adds cross-excitation terms.

Three of those assumptions are under pressure on Solana.

### 6.2 Pressure point 1 — event times are tied at the slot lattice

**VERIFIED (§4.2):** Solana's slot target is 400 ms, asserted at compile time in
the SDK. Every transaction in a block shares that block's time. There is no
sub-slot timestamp available from ordinary block data; the only intra-block
information is **position in the block**, and §4.1 establishes that position is
set by a priority-greedy scheduler under account-lock contention, not by arrival
order.

> **INFERRED, and it is the crux: intra-slot arrival order does not recover
> arrival TIME.** Two swaps in the same block are simultaneous as far as any
> observable clock is concerned, and their *order* reflects fee and contention
> rather than when they were sent. So the finest honest time resolution for
> Solana swap arrivals is **one slot**, and the data are tied counts on a
> lattice, not a simple point process.

### 6.3 Pressure point 2 — the interesting kernel mass sits inside one slot

This is the argument that actually decides the question, and it is not about
data hygiene.

The self-excitation one wants to capture here is **bot response**: a sniper or
copy-trader reacting to a visible buy. That response is mediated by software
racing to get into the *next* block, or the same one. So the excitation kernel's
mass is concentrated at lags of **roughly one slot or less** — precisely the
region the lattice cannot resolve.

> **A Hawkes fit on slot-stamped Solana data would be estimating a kernel across
> the exact interval where all of its structure lives, using a clock too coarse
> to see it.** The fitted decay parameter would be dominated by the
> discretisation, and one would be measuring the block time, not the behaviour.

This is a different and more serious objection than "ties are inconvenient."
Ties can be jittered or handled with a tie-aware likelihood. A kernel that is
entirely sub-resolution cannot be recovered by any amount of estimator care.

### 6.4 Pressure point 3 — batching manufactures clustering that is not behavioural

Hawkes is attractive here because swap arrivals visibly cluster. But block
batching **produces clustering mechanically**: independent Poisson arrivals,
observed only through 400 ms batches, appear as bursts at batch boundaries. A
Hawkes model fitted to slot-aggregated data will happily attribute that
batching artifact to self-excitation and report a large branching ratio.

> **Any claimed self-excitation on Solana swap data must first be shown to
> exceed what the batching alone produces.** The correct null is not a
> homogeneous Poisson process in continuous time; it is a **batched** Poisson
> process on the slot lattice. We are not aware of published work establishing
> that comparison for Solana, and §12 records that as an unfilled gap rather
> than as a claim.

### 6.5 The mitigating fact — for most of our tokens, arrivals are sparse

The above is the case against. Here is the case that it may not bind, and it
comes from our own numbers.

Ties become material when the expected count per slot is non-negligible. That
threshold sits at:

| events per slot | events/sec | swaps/day |
|---|---|---|
| 0.01 | 0.025 | 2,160 |
| 0.10 | 0.250 | 21,600 |
| 1.00 | 2.500 | 216,000 |

Our tokens are nowhere near the top of that table. At a median observed TVL of
$2,860 and plausible average swap sizes of 1–5% of the quote reserve, a token
would need **$500,000 of daily volume** to reach even 0.4 events/sec, and at a
more typical $50,000 of daily volume the rate is **0.008–0.04 events/sec** —
about **one swap every 25 to 125 seconds**, or one event per 60–300 slots.

> **INFERRED: for the median token at the horizons we observe, slot
> discretisation is NOT the binding problem — arrivals are far too sparse for
> ties to matter.** The 400 ms lattice is fine resolution for a process that
> fires once a minute.

But this rescue is narrower than it looks, and the reason is exactly why one
wanted Hawkes in the first place:

> **Hawkes is a model of CLUSTERS, and the clusters are precisely where the
> local rate is high.** A token averaging one swap per minute may fire twenty
> swaps in four seconds during a burst. The average rate says the lattice is
> fine; the *conditional* rate during the events of interest says it is not.
> **§6.3's objection therefore survives §6.5 intact.**

### 6.6 Recommendation

1. **Do not adopt continuous-time Hawkes as the default formalism for Solana
   swap flow.** It is the right instinct and the wrong clock.
2. **Prefer a discrete-time model on the slot lattice** — an
   autoregressive count model over slot-indexed, direction-typed counts
   (integer-valued autoregression / observation-driven Poisson autoregression).
   This is honest about the resolution actually available, it handles ties by
   construction, and it does not require inventing sub-slot times.
3. **Choose the aggregation window explicitly and defend it.** If the analysis
   window is one second or one minute rather than one slot, most of §6.2–§6.4
   dissolves — and so does any hope of capturing bot-reaction dynamics. That is
   a real trade-off to be made deliberately, not by default.
4. **Whatever is fitted, benchmark it against a batched-Poisson null** (§6.4),
   not against a continuous-time Poisson null.
5. **Recognise that none of this is estimable by us today.** Every input —
   direction-typed swap counts (C3), signed flow (C4), sizes (C5), liquidity
   events (C7) — sits at tier **T3**, outside our declared capability boundary
   (§5.0). **This section is therefore a design conclusion, not a research
   plan.** It records what we would do, and what we would avoid, if the boundary
   ever changed — and it deliberately does not motivate changing it, because
   §11's zero-cost lifecycle studies are the better next move.

---

## 7. Lifecycle as the dominant regime variable

In equity or FX microstructure, "regime" is a slow-moving nuisance parameter —
something you control for. Here it is the **fastest-moving and largest-magnitude
variable in the system**, and treating it as a control rather than as state is
the second-biggest modelling error available in this domain (the first is §1).

### 7.1 The evidence that lifecycle dominates

Three measurements from our own production lane, all recorded in
`docs/milestones/SOLANA-ROUTE-OBSERVATION-001.md` §14.1:

**(1) Liquidity decays by 4.75× at the median between birth and horizon** (M12,
`crypto_token_birth_events` vs cohort 8, 25h window). §2.5(b) then shows this
single variable costs a $500 round trip 22% of notional with the price
unchanged — **larger than every other execution cost combined**.

**(2) The decay is strongly non-uniform, and it fans the cross-section out.**

| percentile | birth | observation | decay |
|---|---|---|---|
| p25 | $10,059 | $1,936 | **5.20×** |
| p50 | $13,586 | $2,860 | **4.75×** |
| p75 | $21,712 | $11,578 | 1.88× |
| p90 | $33,392 | $17,655 | 1.89× |
| p100 | $348,926 | $167,041 | 2.09× |

Reading the *dispersion* rather than the levels gives the sharper result:

| dispersion measure | at birth | at observation | change |
|---|---|---|---|
| p75 / p25 | **2.16×** | **5.98×** | **2.77× wider** |
| p90 / p25 | 3.32× | 9.12× | 2.75× wider |

> **VERIFIED (arithmetic over the M12 measurement). At birth these tokens look
> nearly alike — the interquartile liquidity ratio is 2.16×. By the 6h/24h
> horizon they have separated to 5.98×.** The lifecycle is not a uniform decay
> applied to a population; it is a **sorting process** that takes a
> homogeneous-looking birth cohort and fans it out by roughly 2.8× in
> dispersion.

This reframes the predictive question, and it is the most useful thing in this
document for deciding what to work on:

> **The research question is not "what will the price do?" It is "can the sort
> be predicted at birth, before the fan-out has happened?"** That is a
> classification problem over the Block E/F features, on a population we already
> enrol, with an outcome (`tvl_decay_ratio`, F6) we can already compute. It is
> falsifiable, it needs no new capability, and it requires not one Block C flow
> feature.

**(3) An implied median liquidity half-life of 2.7–10.7 hours.** If decay is
approximately exponential, a 4.75× median fall implies a half-life of 2.67 h had
every observation sat at the 6h mark, and 10.68 h had every one sat at 24h.
Cohort 8 mixes both horizons, so the honest statement is a **bracket, not a
point estimate**:

> **INFERRED: median pool liquidity half-life is between roughly 2.7 and 10.7
> hours.** The bracket is wide because the sample (n=42) mixes two horizons and
> the exponential form is assumed rather than fitted. Narrowing it is cheap — it
> needs only that observations be attributed to their horizon, which
> `CryptoHorizonObservation.horizon` already records — and it would be one of
> the more valuable small measurements available to this project.

### 7.2 The stage model

Six stages. The boundaries are chosen to be **observable transitions**, not
narrative ones.

| stage | definition | observable marker | our coverage |
|---|---|---|---|
| **S0 — pre-liquidity** | token exists; no pool state ever observed | `initial_liquidity_usd` NULL or ≤ 0 | **58.6% of births** — see §7.3 |
| **S1 — bonding curve** | trading against the launchpad curve, pre-graduation | `bonding_curve_state`, `first_dex_id` = launchpad | F2/F3; coverage unmeasured |
| **S2 — graduation** | curve completes; liquidity migrates to an AMM pool | `graduated_or_migrated`; a new `pair_address` under a different `dex_id` | F4 exists as a column |
| **S3 — post-graduation peak** | the AMM pool at maximum TVL | \(\arg\max_s L_s\) over persisted ticks | A9 computes it |
| **S4 — decay** | sustained TVL decline from peak | A8 < 0 sustained; F6 rising | **where our 6h/24h observations almost all sit** |
| **S5 — death or removal** | TVL collapses, or LP is withdrawn | `no_liquidity_state`, `provider_no_pair`, or C8 firing | M14: **0 occurrences** in cohort 8 to date |

**S4 is where our data lives, and that is a sampling fact worth naming.** The
sparse lane observes at 6h and 24h; §7.1(3) puts the median half-life at 2.7–10.7
hours. So **our observations are taken between roughly one and nine half-lives
after birth** — deep in S4, well past the S3 peak, and long after whatever sort
determined the outcome had already happened. We are measuring the *result* of
the sorting process, not the process.

### 7.3 The 58.6% that never enter the model at all

`SOLANA-ROUTE-OBSERVATION-001` §14.1 M13: of 411 births in 25 hours, all 411
carry `first_evidence_at`, but only **170 (41.4%)** carry an
`initial_liquidity_usd > 0` — the binding enrolment filter in
`crypto_sparse_observation.enrolment_rejection_reason`.

The milestone records this as coverage finding **F-1**, a limitation. This
document takes a different view of the same number:

> **INFERRED — a reframe rather than a disagreement: the 58.6% is not only a
> coverage gap, it is the largest single lifecycle observation in the dataset.**
> A token that never acquires an observable liquidity state is an S0 token that
> never reached S1/S2 in any way a provider surfaced. That is an *outcome*, and
> it is the **modal** outcome. Treating it purely as missing data discards the
> majority class of the very lifecycle this section argues is the dominant
> variable.

Two caveats that keep this from being over-claimed:

1. **It may be a provider artifact rather than a token fact.** The absence is in
   DexScreener's response, not on chain. A token that traded briefly and died
   between provider polls is indistinguishable from one that never traded at
   all. Separating them needs either denser polling (T1 cost) or chain data (T3,
   out of bounds).
2. **M15 explicitly flags the 58.6% as measured over a single 25-hour window**
   and therefore possibly a one-day artifact. The *existence* of the ceiling is
   established; its *magnitude* is not.

The direction is still clear enough to state: **any lifecycle model built only
on the 41.4% is conditioned on having already survived the largest filter in the
system**, and its base rates will be optimistic by an amount nobody has yet
measured.

### 7.4 The observation cliff is itself lifecycle evidence

The `crypto_sparse_observation.py` module docstring records the production
coverage that motivated the lane: **15m 80.9%, 1h 81.1%, 6h 16.8%, 24h 4.6%**,
with the median token's *last* tick arriving ~83 minutes after birth.

That was diagnosed — correctly, for the lane's purposes — as an **observation**
failure: the background scout stops ticking aged tokens. The same numbers admit
a second reading that matters here:

> **INFERRED: an ~83-minute median last tick, set against a 2.7–10.7 hour median
> liquidity half-life, is also consistent with the scout losing interest at
> roughly the moment the token stops being interesting.** The two explanations —
> "we stopped observing" and "there was progressively less to observe" — are not
> mutually exclusive, and no measurement we currently hold separates them.

This is a genuine confound in every coverage number the project reports. It is
worth naming because the sparse lane's design **resolves it**: the lane observes
on a schedule anchored to birth, independent of whether the token still looks
interesting. Comparing the sparse lane's 6h/24h answer rate against the scout's
is therefore a clean natural experiment separating observation failure from
token death — and M14's finding of **0 `no_liquidity_state` and 0
`provider_no_pair` across all cohort-8 observations to date** is the first data
point. It currently favours "we stopped observing" over "the tokens died."

---

## 9. What is genuinely timeable — an honest assessment

This domain is saturated with unfalsifiable claims, so this section applies a
fixed four-part test to each and reports the results without softening them. A
pattern is tradeable only if it (a) **exists**, (b) is **predictable** at a
usable horizon, (c) **survives costs**, and (d) **survives competition**.

Most candidates fail at (c), and the reason is arithmetic rather than opinion.

### 9.1 The cost hurdle comes first, because it eliminates most of the field

From §2.5(b), the round-trip microstructure cost along the *measured* median
decay path:

| clip | round-trip cost | ⇒ minimum gross edge to break even |
|---|---|---|
| $10 | 1.04% | **1.04%** |
| $50 | 3.16% | **3.16%** |
| $150 | 8.11% | **8.11%** |
| $500 | 22.23% | **22.23%** |

> **Any claimed pattern must predict a move larger than this, reliably, after
> the fact, out of sample.** Note the hurdle is *not* a fixed transaction cost
> to be amortised — it scales with clip size, so it cannot be outrun by trading
> bigger. It is the reverse: **trading bigger makes the hurdle worse faster than
> it makes the profit better.**

### 9.2 The capacity arithmetic, which is the real verdict

Take the strongest version of the opportunity: **all 170 eligible births per 25
hours**, every one traded, along the median decay path.

**Net dollars per day at various assumed true gross edges:**

| clip | deployed/day | 5% edge | 10% edge | 20% edge | 50% edge |
|---|---|---|---|---|---|
| $10 | $1,700 | $67 | $152 | $322 | $832 |
| $50 | $8,500 | $156 | $581 | $1,431 | $3,981 |
| $150 | $25,500 | **−$793** | $482 | $3,032 | $10,682 |
| $500 | $85,000 | **−$14,646** | **−$10,396** | **−$1,896** | $23,604 |

Three things fall out, and they matter more than any signal question:

1. **There is an interior optimum, and it is small.** Around $50–150 per token.
   Below it you leave money on the table; above it the decay-asymmetry cost
   overwhelms the edge. **At a 10% gross edge, the $500 clip loses $10,396/day
   while the $50 clip makes $581/day** — same signal, same population, opposite
   sign, purely from sizing.
2. **A $500 clip is unprofitable even at a 20% gross edge.** This is the
   quantitative vindication of `SOLANA-ROUTE-OBSERVATION-001` §4.1's decision to
   cap the ladder at $500 and of its instinct that V1's $2,000 rung was not a
   quote at all.
3. **The whole opportunity is small.** Even a *10% sustained gross edge* — which
   would be extraordinary — yields several hundred dollars a day at the optimal
   clip. That is a real number, but it is not a number that justifies unbounded
   engineering, and it should be stated before, not after, the work is done.

> **The honest headline: the binding constraint on this asset class is not
> signal, it is CAPACITY. The pools are too thin to absorb size, and the
> population is too small to compensate with breadth.** Anyone reasoning about
> Solana memecoins should establish this arithmetic first, because it determines
> whether any signal question is worth asking.

### 9.3 The claims, assessed

| # | claim | exists? | predictable? | survives cost? | verdict |
|---|---|---|---|---|---|
| 1 | Sniping the launch block | **yes** | n/a — it is a race | yes, if you win | **REAL, but not a statistical edge** — see §9.4 |
| 2 | Bonding-curve graduation is a forecastable event | **yes, VERIFIED** | **yes** — deterministic in curve progress | plausible | **MOST PROMISING** — §9.5 |
| 3 | Liquidity decay / the birth-to-outcome sort is predictable | **yes, measured** | **open, testable today** | depends on effect size | **BEST NEXT STUDY** (S-3) |
| 4 | Holder concentration predicts failure | plausible mechanism (§8.4) | open, testable today | plausible | **WORTH TESTING** |
| 5 | Short-horizon price momentum / mean reversion | unclear | doubtful | **no** | **REJECT** — §9.6 |
| 6 | Volume spikes predict price moves | **partly mechanical** | contaminated | no | **REJECT as stated** — §9.6 |
| 7 | Sniper/bundler percentages predict failure | vendor-defined | open | plausible | **TEST, but audit the labels first** (E3) |
| 8 | Social/boost attention predicts moves | unclear | unclear | doubtful | **LOW PRIORITY** |
| 9 | Following profitable creator/wallet clusters | plausible | **unbuilt** | plausible | **HIGHEST-VALUE UNBUILT** — §9.7 |
| 10 | Avoiding sandwiches is alpha | it is a cost, not a signal | n/a | n/a | **CATEGORY ERROR** — §4.3 |

### 9.4 Sniping: real, and a worse trade than it looks

Launch sniping demonstrably exists and is industrially contested. Two facts from
this document make it a worse proposition than its reputation suggests, both
**DERIVED from §3**:

- **The upside is capped at exactly 14.6958×**, and only for a token that
  completes its curve. That is a hard ceiling (§3.2), not a distribution with a
  fat right tail. Any pitch describing 100× from a curve entry is describing
  post-graduation price action, which is a different and much less certain bet.
- **The launch instant is the WORST execution point on the entire curve.** §3.3
  shows a $150 buy costs 4.40% at 0% progress and 2.09% at 99%. Impact falls
  monotonically as the curve fills. **The sniper pays the highest impact on the
  curve for the privilege of being early.**

It is a latency and infrastructure contest against well-capitalised specialists,
with a capped payoff and the worst fill on the curve. **It is not a statistical
edge and it is not what this project is built to do.**

### 9.5 Graduation: the most genuinely timeable event in the asset class

This is the one place where the four-part test comes out clean, and it is worth
stating why.

- **It exists and is exactly defined.** VERIFIED (§3.2): completion fires when
  `real_token_reserves == 0`. It is not a fuzzy regime label; it is a program
  state transition.
- **It is predictable in a strong sense.** Curve progress is a deterministic
  function of tokens sold. Given progress and recent fill rate, the *time* to
  graduation is forecastable in a way essentially nothing else in this document
  is.
- **It is a discrete, dated, structural event** — liquidity migrates to
  PumpSwap, LP tokens are burnt (§3.4) — which is exactly the shape of thing this
  project's forecasting machinery is built around, as opposed to a continuous
  price prediction.
- **Depth is best there.** §3.3: the curve is at its deepest near graduation
  (2.09% for $150 at 99% progress), so it is also the cheapest point on the
  lifecycle at which to transact.

> **If one thing in this document deserves follow-up capability work, it is
> observing bonding-curve progress (feature F3).** It converts the problem from
> "predict a price" into "forecast a dated state transition," which is a
> qualitatively easier and better-posed question, and it is the only candidate
> here where predictability, depth, and event structure all point the same way.
>
> **Two honest caveats.** F3 requires reading curve state, which is a capability
> we do not currently have and which would need its own scoped, approved
> milestone — this document does not authorise it and does not assume it.
> And "the event is forecastable" is **not** the same as "the price move around
> it is forecastable"; the latter is unestablished and should not be assumed
> from the former.

### 9.6 What to reject, and why the rejection is stronger than usual

**Claims 5 and 6 — short-horizon momentum and volume-spike signals — should be
rejected, and §2.6 gives a mechanical reason rather than an empirical one.**

At our pool sizes, the provider-reported price change *is largely the impact
footprint of individual swaps*. A $500 buy into the median pool moves the
reported price 82% (§2.6). So:

- **`price_change_5m` and `volume_5m_usd` are not two independent observations.**
  They are two views of the same swaps, mechanically linked through the curve.
  Any regression of one on the other recovers the AMM formula, not a behavioural
  relationship.
- **A "volume spike predicts a price move" finding is very nearly guaranteed a
  priori**, because on a constant-product curve volume *is* price movement. It
  will look like a strong result and mean nothing.

> **This is the highest-risk analytical trap in the whole domain, because the
> spurious result is large, stable, out-of-sample-robust, and completely
> useless.** It will survive every validation a naive pipeline applies. The only
> defence is knowing the mechanism in advance — which is the entire reason §2
> is in this document.

### 9.7 The highest-value unbuilt feature

`app/models.py` carries `repeated_cohort_ref` and `known_creator_cluster_ref` on
`CryptoTokenActorObservation`, explicitly annotated as "placeholders for later
cross-token cohort analysis (no behavior today)."

**These are the most valuable unbuilt things in the schema**, for a reason
specific to this asset class:

> **Individual memecoins have no history — that is the defining difficulty. But
> the PEOPLE who create and trade them do.** A creator address that has launched
> eleven tokens has a track record even though each of its tokens has none. This
> is the only mechanism available for transferring information *across* the
> lifecycle barrier that makes every other prediction problem hard.

It is also, unlike almost everything in §5's Block C, **buildable from public
addresses we already persist** (`CryptoTokenBirthEvent.creator_address`), and it
requires no new provider and no per-swap feed.

*The caveats, because this is the claim most likely to be over-sold:*
creator-address reuse may be low (an operator who rotates addresses defeats it
entirely, and any operator sophisticated enough to be worth tracking has an
obvious incentive to rotate); the repository has no measurement of reuse rate;
and clustering public addresses is a technique whose reliability degrades exactly
against the adversaries it most wants to catch. **The first step is not to build
the feature — it is the one-line measurement of how often
`creator_address` repeats across our 411 births/25h.** If reuse is rare, the
whole line of work is closed cheaply and that is a good outcome.

### 9.8 The summary judgement

> 1. **Capacity, not signal, is the binding constraint** (§9.2). Establish the
>    cost arithmetic before asking any predictive question.
> 2. **The clip size is $50–150 and the optimum is interior.** Bigger is
>    strictly worse, and the arithmetic changes sign between $150 and $500.
> 3. **The most timeable thing is graduation** (§9.5), because it is a dated
>    structural event rather than a price — but observing it needs capability we
>    do not have.
> 4. **The best study we can run today costs nothing**: the birth-to-outcome
>    sort, S-3 (§11.2), on data already in the database.
> 5. **Reject short-horizon price/volume signals on mechanical grounds** (§9.6),
>    before wasting a pipeline on them.
> 6. **The domain's own folklore is systematically biased toward the two things
>    that are least accessible** — sniping and MEV — and away from the two that
>    are most measurable — decay and concentration. That misallocation is itself
>    the opportunity, such as it is.

---

## 10. DISCARD list: classical CLOB constructs that do not transfer

This section exists because the failure mode it prevents is specific and
expensive: importing a CLOB feature library, computing 40 features, finding no
signal, and concluding the market is efficient — when in fact 30 of the features
were measuring objects that do not exist.

Each row states what the construct is, **why** it fails here, and what — if
anything — replaces it.

### 10.1 DISCARD ENTIRELY — the object does not exist

| construct | why it does not transfer | replacement |
|---|---|---|
| **Queue position / time priority** | There is no queue. A swap is applied to the pool's state function; there is nothing to be in front of. Within a slot, ordering is set by the leader's scheduler and by fee-based prioritization, not by arrival time at a matching engine. | **Nothing structural.** The nearest analogue is *transaction landing*, which is a §4 uncertainty, not a state variable. |
| **Queue imbalance** (bid depth vs ask depth at the touch) | Requires two sides with independent depths. A CPMM has **one** state \((x,y)\) that serves both directions; "bid depth" and "ask depth" are not free parameters — they are both determined by the same reserves. Any computed imbalance is an artifact of the computation. | **Nothing.** Depth is symmetric by construction: A1/A2. |
| **Cancellation rate / order-book flickering** | There are no resting orders, hence no cancellations. "Spoofing" in the CLOB sense is not expressible. | **Nothing.** The nearest economically similar behaviour is **liquidity add/remove** (C7, T3), which is a genuinely different phenomenon: it is capital movement, not intent signalling. |
| **The microprice** \((p_b q_a + p_a q_b)/(q_a+q_b)\) | Defined as a depth-weighted blend of the two best quotes. With one reserve pair there is one marginal price, and the weights collapse. Computing it returns the mid by construction. | **The marginal price** \(y/x\) is already the correct "fair" price. For an execution-relevant price, use the **quote-derived executable price** (T2, `executable_price_equiv`), which is a genuinely different and better number. |
| **Bid-ask spread** | There is no spread. The venue quotes one curve; the "spread" a trader experiences is \(2f\) plus the round-trip impact, which §2.5(a) shows is \(\approx 2f\) instantaneously. | **The fee, doubled** — plus the decay asymmetry of §2.5(b), which is where the real cost is and which has no CLOB analogue at all. |
| **Order-book slope / depth-at-k-levels** | Levels do not exist. | **The curve itself.** Depth at any size is the closed form of §2 — strictly more informative than a level ladder, because it is exact and continuous. |
| **Effective/realized spread, price improvement** | All defined relative to a quoted spread that does not exist. | **Nothing.** Do not compute these. |

### 10.2 DISCARD AS COMPUTED — the concept survives, the estimator does not

| construct | why the estimator fails | replacement |
|---|---|---|
| **Kyle's λ, and every regression-estimated price-impact coefficient** | λ estimates \(\partial p/\partial(\text{signed volume})\) statistically, because in a CLOB impact is not observable ex ante. **Here it is a closed form.** Regressing price changes on volume to recover a coefficient we can compute exactly from \((x,y,f)\) is strictly worse: it adds estimation error to an identity, and it will absorb liquidity-decay effects into what looks like an impact coefficient. | **§2's \(S(\tau,f)\) directly.** The exact identity, evaluated at the pool state. |
| **Amihud illiquidity** \(|r|/\text{volume}\) | Same objection: a proxy for a quantity we can compute. It is also badly contaminated here — §2.6 shows the reported \(|r|\) is largely *self-generated impact*, so Amihud partly measures the trade-size distribution rather than illiquidity. | **A1/A2 (TVL and implied reserve) and \(\tau\).** These are the actual illiquidity. |
| **Roll's implied spread from autocovariance** | Assumes bid-ask bounce between two quotes. There is no bounce; consecutive prints walk a deterministic curve. The estimator will return noise, or a negative variance, and mean something different from what it means in a CLOB. | **\(2f\).** It is known exactly. |
| **Realized volatility from provider mid** | Not wrong so much as *misnamed*: at our pool sizes it predominantly measures the impact footprint of individual swaps (§2.6). | **B3, renamed `price_churn`.** Keep the number, drop the interpretation. |
| **VPIN / order-flow toxicity as usually computed** | Requires signed volume (Block C, tier **T3**, out of bounds) and volume-bucketing calibrated to a market maker's inventory problem that no AMM LP faces in the same form. See §8. | **LVR-style reasoning** and the AMM-native adverse-selection framing of §8. |

### 10.3 TRANSFERS, BUT ONLY WITH A CHANGED DEFINITION

| construct | what changes |
|---|---|
| **Order-flow imbalance (OFI)** | The *concept* — net signed pressure — is exactly right and is arguably the single most valuable feature in the whole schema (C4). What changes is that it must be built from **swap direction against the pool**, not from book updates, and that this requires T3. **We cannot compute it today.** It should not be quietly approximated from unsigned `volume_24h_usd`; an unsigned aggregate carries no directional information whatsoever, and pretending otherwise is fabrication of exactly the kind this repository's absence vocabulary exists to prevent. |
| **Trade arrival intensity / clustering** | Survives as a concept (§6), but the continuous-time formalism must be re-examined against Solana's discrete slotting before it is adopted. |
| **Adverse selection** | Survives, but with a different victim and a different mechanism (§8). |
| **Volume-synchronised time / trade clocks** | Survives, and may be *more* natural here than calendar time given the extreme lifecycle compression of §7 — but requires T3 to construct properly. A crude T0 version can be built from cumulative `volume_24h_usd`. |

### 10.4 The one-line summary

> **Discard everything that presumes a queue. Keep everything that presumes
> flow. Replace every statistical impact estimator with the closed form, and
> spend the effort saved on the two things that are genuinely uncertain: which
> state your transaction lands against (§4), and where the token is in its
> lifecycle (§7).**

---

## 8. Adverse selection and toxicity without a market maker

### 8.1 Why the classical framing does not apply unmodified

"Toxic flow" in classical microstructure means: flow that systematically arrives
on the informed side, so that a market maker who fills it loses on average and
must widen or withdraw. Every part of that sentence assumes a market maker who
**chooses** quotes, **observes** flow, and **can withdraw**.

An AMM LP does none of those things. The pool quotes a fixed curve, fills
everything, and cannot decline. It has no queue to defend and no inventory
policy to run. So the classical machinery — VPIN, order-flow-imbalance-based
toxicity scores, quote-fade detection — does not merely lack inputs here; the
decision it was built to inform does not exist.

**What survives is the economics, not the metric.** An LP is still adversely
selected: it systematically ends up long the asset that is falling and short the
asset that is rising, because arbitrageurs trade against a stale curve. That is
a real, measurable, AMM-native cost, and the literature's name for it is
**loss-versus-rebalancing (LVR)** — see §12.

### 8.2 The four toxicity channels that actually exist here

For a *taker* on a thin Solana memecoin pool — which is our position, not the
LP's — there are four distinct adverse-selection channels. They are genuinely
different mechanisms and conflating them is a modelling error.

**Channel 1 — Sandwich / MEV extraction.** An adversary observes your intent,
buys ahead of you, lets your trade push the price, and sells into it. Your
execution is worse by the amount extracted. *Status:* **unobservable
prospectively** (`SOLANA-ROUTE-OBSERVATION-001` §8.1 row 8: "NOT OBSERVABLE
WITHOUT SUBMITTING A TRANSACTION"). §4 covers the mechanism.

**Channel 2 — Stale-quote adverse selection.** You quote at \(t\), the pool
state changes, you execute against a different state. This is the taker-side
mirror of LVR and it is entirely a function of the gap between quote and
execution. *Status:* the quantity is **bounded but not measured** — see §4.

**Channel 3 — Liquidity-removal risk (the rug).** The LP withdraws, and your
exit curve ceases to exist. Note carefully that this is **not** a price move: it
is the disappearance of the function that converts your position into money.
Classical microstructure has no analogue at all, because a CLOB's liquidity
withdrawal leaves the venue intact. *Status:* partially detectable at T0 via C8
(§5.1); this is the highest-value detector we can build inside the boundary.

**Channel 4 — Structural liquidity decay.** The pool does not disappear; it
thins. §2.5(b) quantifies this at **−22% of notional on a $500 round trip at the
measured median decay, with the token's true price unchanged.**

> **The ranking is the finding.** For a taker in this asset class, ordered by
> measured or plausible magnitude:
>
> **Channel 4 (decay, ~22% at $500/median, MEASURED) > Channel 3 (rug, total
> loss but lower frequency, UNMEASURED) > Channel 1 (sandwich, UNMEASURABLE
> prospectively) > Channel 2 (stale quote, bounded and small at our sizes).**
>
> **The dominant adverse-selection cost in Solana memecoins is not MEV.** It is
> the slow structural thinning of the pool between entry and exit — a lifecycle
> phenomenon (§7), not a microstructure one. MEV gets the attention; decay takes
> the money. Every hour of effort spent on sandwich modelling before Channel 4
> is measured is misallocated.

### 8.3 What the repository already encodes, and how to read it

`app/services/crypto_risk_engine.py` already carries a normalized set of
adverse-selection proxies with explicit thresholds:

| config | default | category |
|---|---|---|
| `max_top_holder_pct` | 20.0 | `holder_concentration` |
| `max_sniper_pct` | 20.0 | `sniper_concentration` |
| `max_insider_pct` | 15.0 | `insider_concentration` |
| `max_bundler_pct` | 25.0 | `bundler_concentration` |
| `min_liquidity_usd` | 5000.0 | liquidity floor |
| — | — | `provider_rug_flag` (pass-through of a provider's own flag) |

Three observations about this set, offered as findings rather than criticism:

1. **The thresholds are declared policy, not fitted quantities.** They are round
   numbers (20/20/15/25) and nothing in the repository claims otherwise. That is
   honest, and it also means **their calibration is an entirely open empirical
   question** that S-3 (§11.2) would begin to answer as a side effect.
2. **`min_liquidity_usd = 5000.0` is above 62% of the observed population.**
   `SOLANA-ROUTE-OBSERVATION-001` §4.2 measured 62% of quoted-population pools
   below $5,000 and used exactly this observation to re-anchor its notional
   ladder. The same argument applies to the risk floor: a threshold that most of
   the population fails is not discriminating within the population, it is
   selecting a different one. **Whether that is intended should be an explicit
   decision, not an inherited constant.**
3. **`sniper_pct` / `insider_pct` / `bundler_pct` are provider-defined labels**
   (§5.1 Block E, E3). We consume a vendor's heuristic with an unpublished
   threshold. They may be excellent; we cannot know, and nothing in the schema
   currently records the vendor's definition alongside the value. Treating them
   as ground truth in any downstream model imports an unaudited dependency.

### 8.4 The concentration channel is the one with a real mechanism

Of everything in §8.3, holder concentration has the clearest causal story, and
it connects directly to §2:

**INFERRED, and it follows from §2 arithmetic rather than from folklore.** If a
single holder controls a fraction \(h\) of the float and the pool has TVL \(L\),
that holder's exit is a swap of size roughly \(h \cdot \text{FDV}\) against a
quote reserve of \(L/2\). Using §2's rule \(S \approx f + \tau\), that exit moves
the price by approximately

$$\tau_{\text{exit}} \;\approx\; \frac{2\,h\cdot \text{FDV}}{L}\;=\;2h \cdot \left(\frac{\text{FDV}}{L}\right).$$

So **concentration matters exactly in proportion to `fdv_to_tvl`** — feature B6
in §5.1. A 20% holder in a token whose FDV is 5× its TVL faces
\(\tau_{\text{exit}} \approx 2\), i.e. an exit that consumes the pool. A 20%
holder in a token whose FDV is 1.2× its TVL faces \(\tau \approx 0.48\) — still
enormous, but survivable.

> This gives a **composite risk feature with an actual derivation behind it**,
> rather than two independently-thresholded numbers:
> \(\;\texttt{exit\_pressure} = \texttt{top10\_holder\_pct} \times
> \texttt{fdv\_to\_tvl}\).
> Both inputs are already persisted (Block E, B6). It costs one multiplication
> and it is testable against the F6 decay outcome in study S-3. **This is the
> most concrete new feature proposed anywhere in this document.**

*The caveat that keeps it honest:* it assumes the holder exits into the same
pool in one transaction. A patient seller splitting across hours faces far less
impact — but a patient seller also does not produce the failure mode the risk
engine is trying to catch. It is a **worst-case** measure, and should be named
as one.

---

## 11. What our data can and cannot support today

This section converts the rest of the document into an inventory and a ranked
set of studies. Every study named here is **falsifiable, bounded, and stated
with the measurement that would refute it**.

### 11.1 The inventory

| we have | we do not have |
|---|---|
| ~411 births / 25h, all with `first_evidence_at` | signed swap flow (C3–C7) at any price |
| ~170 / 25h eligible births carrying `initial_liquidity_usd > 0` | true on-chain reserves (A3) |
| birth-time liquidity, price, volume, market cap, FDV | per-swap sizes, signers, slots |
| two observations per token (6h, 24h) with liquidity/price/volume | realized slippage (permanently — §8.1 row 6 of the milestone) |
| holder/actor structure where the risk provider supplied it (Block E) | landing probability, MEV extraction (unobservable without executing) |
| a survival-outcome table with per-horizon labels | executable quotes (T2, pending CP-0) |
| provenance, `missing_info`, and honest NULLs throughout | dense intraday sampling of any single token |

### 11.2 Studies runnable TODAY at zero provider cost

Ranked by value per unit of effort. All are pure SQL plus arithmetic over
already-persisted rows.

**S-1. Compute F6 (`tvl_decay_ratio`) for every enrolled token and describe its
distribution.**
*Why first:* §2.5(b) shows this variable dominates realized execution cost, and
it is two existing columns and one division. *Refutable claim:* the decay
distribution is unimodal and heavy-tailed with median ≈ 4.75×. *What would
refute it:* bimodality, which would mean "decays" and "rugs" are distinct
populations that must be modelled separately rather than as one distribution —
an outcome that would be **more** interesting than the expected one.

**S-2. Narrow the half-life bracket by attributing decay to horizon.**
*Why:* §7.1(3) reports 2.7–10.7 h only because cohort 8 mixes horizons.
`CryptoHorizonObservation.horizon` already records which is which. *Refutable
claim:* a single exponential fits both horizons. *What would refute it:* a 6h-
implied half-life materially shorter than the 24h-implied one, which would mean
decay **decelerates** — survivors stabilise — and that would make lifecycle
stage even more decision-relevant than §7 claims.

**S-3. The birth-to-outcome sort: can the fan-out be predicted?**
*Why:* §7.1(2) is the central finding of this document. *Design:* classify
tokens into decay quartiles at 6h/24h using **only** birth-time features
(initial liquidity, initial volume, FDV/TVL, mint & freeze authority, social
link count, and Block E holder structure where present). *Refutable claim:* the
classifier beats the base rate out of sample. *What would refute it:* it does
not — which is a genuinely useful negative result and would redirect effort to
§9's other candidates. **Pre-register the feature set and the split before
looking**, per this repository's existing prospective-experiment registry
discipline.

**S-4. Build and characterise C8 (`tvl_jump_unexplained`).**
*Why:* it is the only rug/liquidity-removal detector available inside our
current capability boundary. *Refutable claim:* a TVL fall not explicable by
reported volume identifies a distinct population. *What would refute it:* the
"unexplained" residual is dominated by provider volume-reporting error, in which
case the detector fires everywhere and means nothing. **This is the likeliest
outcome and should be checked first**, by looking at the residual on tokens
whose TVL is stable.

**S-5. Separate observation failure from token death.**
*Why:* §7.4's confound contaminates every coverage number the project reports.
*Design:* compare the sparse lane's birth-anchored 6h/24h answer rate against
the background scout's. *Refutable claim:* the scout's coverage cliff is
observational, not real. *Status:* M14's 0 `no_liquidity_state` / 0
`provider_no_pair` is the first data point and currently supports the claim, on
a very small sample.

### 11.3 Studies that need T1 (bounded extra provider calls)

**S-6. Denser sampling on a small deliberately-chosen sub-cohort.** Even hourly
sampling on 20 tokens for 24 hours would give the first real *trajectory* data
this project has ever had, and would let A8/A9/B3/C2 be computed as series
rather than as two-point differences. **Cost is the whole question** and must be
set against `OBSERVE_MAX_CALLS` and the existing lane's budget; this is a
proposal for Eric, not a plan.

**S-7. Fill Block D (`tvl_herfindahl`, `best_pair_share`).** Requires the
multi-pair response the adapter can already return.

### 11.4 What is closed, and will stay closed

- **All of Block C except C8** requires T3, a per-swap feed, which
  `SOLANA-ROUTE-OBSERVATION-001` §3.2 F9 forbids. Section 6's question about
  Hawkes processes is therefore, for us, **currently unanswerable with data** —
  it is a design question about what we would do *if* the boundary changed.
- **Realized slippage** is permanently unobservable within the boundary (§8.1
  row 6). There is no ground truth to validate a fill model against. This is
  stated in the milestone as "a permanent limitation of the result, not a gap
  more work will close," and this document agrees.
- **Landing probability and MEV extraction** are unobservable without submitting
  a transaction, which is out of scope by construction.

### 11.5 The sample-size reality

At ~170 eligible births per 25 hours, a 30-day accumulation gives on the order
of **5,000 labelled tokens** — comfortably enough for S-3's classification study
with a held-out split, and enough to estimate decay quantiles precisely.

The binding constraint is therefore **not sample size**. It is:

1. **Label coverage.** Historical 24h survival coverage was 4.6%; the sparse
   lane exists to fix this prospectively, and M1 measured `survived_24h` moving
   4 → 9 across two passes. Whether it reaches a usable rate is the open
   question, and it is already being measured.
2. **The 58.6% selection filter** (§7.3), which no amount of accumulation fixes.
3. **Feature depth**, not row count — we have many tokens observed twice, not
   few tokens observed many times, and §5.2 explains why that ordering is
   correct for the lane's purpose and wrong for microstructure.

---

