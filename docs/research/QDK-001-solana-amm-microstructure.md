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
  *No claim in this document ended up carrying this label.* Where something was
  merely plausible it was either verified, demoted to an explicitly open
  question, or recorded in §12.4/§12.5 as a citation that did not check out.

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

Fix the convention for the rest of this document: **\(x\) is the quote reserve
(what you spend), \(y\) is the token reserve (what you receive), and every price
is quoted in quote-units per token.** Let \(\tau \equiv \Delta x / x\), the trade
size as a fraction of the **quote-side reserve**.

| price | definition | formula | ratio to \(p_0\) |
|---|---|---|---|
| **spot / marginal, pre-trade** \(p_0\) | the fee-free price of an infinitesimal trade | \(p_0 = x/y\) | 1 |
| **effective execution price** \(p_{\text{exec}}\) | what you actually pay, averaged over the whole trade | \(p_{\text{exec}} = \Delta x / \Delta y\) | \(\dfrac{1+\gamma\tau}{\gamma}\) |
| **marginal, post-trade** \(p_1\) | **the price a data provider will report after your trade** | \(p_1 = (x+\Delta x)/(y-\Delta y)\) | \((1+\tau)(1+\gamma\tau)\) |

Both ratios follow directly from §2.1 by substituting
\(y - \Delta y = xy/(x+\gamma\Delta x)\).

**Note the ordering, which §2.6 turns into a warning:** for any \(\tau>0\),
\((1+\tau)(1+\gamma\tau) > (1+\gamma\tau)/\gamma\) whenever \(\gamma(1+\tau)>1\)
— i.e. for all but vanishingly small trades, **the reported price moves further
than your execution price did.**

In terms of value received versus value at spot, the **total entry cost** (fee
plus impact, as a fraction of notional) is

$$\boxed{\;S(\tau, f) \;=\; 1 - \frac{p_0}{p_{\text{exec}}} \;=\; 1 - \frac{\gamma}{1+\gamma\tau} \;=\; \frac{f + \gamma\tau}{1 + \gamma\tau}\;}$$

and its leading-order expansion is

$$S(\tau, f) \;\approx\; f + \tau \qquad\text{for small } \tau .$$

**This approximation is startlingly accurate.** Numerically checked against the
exact expression at our own measured pool sizes: at \(L=\$2{,}860,
n=\$500\) the exact cost is 35.216% and \(f+\tau\) gives 35.215%; at
\(L=\$67{,}119, n=\$10\) both give 0.280%. So the practical rule is:

> **Entry cost ≈ the fee, plus the trade size as a fraction of the quote-side
> reserve.** Nothing more sophisticated is needed to size a probe.

Worked check of the post-trade formula against §2.6's headline number: at
\(L=\$2{,}860\) (so \(x=\$1{,}430\)), \(n=\$500\), \(f=0.25\%\), we get
\(\tau = 0.34965\), \(\gamma\tau = 0.34878\), hence
\(p_1/p_0 = (1.34965)(1.34878) = 1.8204\) — **a reported price move of +82.0%**
— against \(p_{\text{exec}}/p_0 = 1.34878/0.9975 = 1.3522\), an execution price
only 35.2% above spot. Both match the exact computations in §2.4 and §2.6.

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

**To be fair to that document, it does not make this error and explicitly warns
against it**: §4.2 states that "every percentage above is a notional-to-TVL
*ratio*, not a predicted price impact," and that converting one to the other
"requires a curve model, and this milestone deliberately does not carry one."
This section supplies exactly that missing curve model, and the factor of 2 is
the first thing it contributes. The warning is aimed at a **future reader** who
sees "17%" beside a rung and takes it for a cost — which is a very easy mistake
to make and, at N4, a 17-point one.

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

**A dating trap worth naming, because the word "greedy" means two different
things in two eras.** Widely-circulated secondary descriptions — e.g. Helius,
"Each thread operates its own queue, prioritizing packets independently without
knowledge of the packets being processed by the other threads" and "the current
implementation of the scheduler does not guarantee that transactions with higher
priority fees will be included in a given block" — describe the **pre-v1.18**
multi-threaded scheduler, in which each banking thread had an *independent*
queue and no global view. The **central scheduler** introduced in Agave v1.18
(May 2024) uses a single scheduling thread *with* a global view. The current
default, `CentralSchedulerGreedy` (P10, P11), is "greedy" in the sense of taking
transactions in priority order as they become schedulable — **not** in the old
sense of independent per-thread queues.

> **Any claim about Solana ordering must be dated.** A pre-2024 description and
> a 2026 one disagree about something as basic as whether a global priority view
> exists. This document's §4.1 conclusion rests on reading current Agave master
> (P10, P11) rather than on any secondary source. **We did not verify whether
> `CentralSchedulerGreedy` is default-on in every deployed configuration in
> 2026, nor the exact intra-slot determinism guarantee** — both should be
> checked against current release notes before anything depends on them.

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

**SIMD-0525 — the 400 ms constant has a scheduled expiry.** VERIFIED
(https://github.com/solana-foundation/solana-improvement-documents/blob/main/proposals/0525-reduce-slot-times.md),
**status: Draft.** It proposes reducing slot time 400 ms → 200 ms in stages
(350/300/250/200) and halving leader windows from 1.6 s to 0.8 s. Its stated
motivation includes achieving "a smaller slot-time **quantization error**."

> **Two things follow, and the second is the important one.** First, any model
> whose parameters are fitted on a 400 ms lattice has a known expiry date, so
> the lattice constant should be a configuration value and never a hard-coded
> assumption. Second — **the platform's own design documents describe slot time
> as quantization error.** That is precisely §6.2's framing, stated by the
> people who build the scheduler. The discreteness this document treats as a
> modelling obstacle is understood as an obstacle by Solana itself.

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
> process on the slot lattice.

**This concern is not speculative — the identification failure it describes has
been demonstrated in print.** Filimonov & Sornette (arXiv:1308.6756,
*Quantitative Finance* 15(8)) show, VERIFIED from the fetched abstract, that

> "the calibration of the Hawkes process on mixtures of pure Poisson process
> with changes of regime leads to completely spurious apparent critical values
> for the branching ratio (n~1) while the true value is actually n=0."

They add that regime shifts "systematically lead to a significant upward bias in
the estimation of the branching ratio," and — directly on point for us — they
flag "**grouping of messages to packets by the stock exchange**" as a
timestamp-quality hazard.

> **Read those two sentences against a memecoin tape.** A memecoin's life *is* a
> sequence of regime changes: launch burst → decay → pump → rug (§7.2's S0–S5).
> A process with **zero** self-excitation but changing regimes calibrates to a
> near-critical Hawkes. **So the single most likely outcome of fitting a Hawkes
> model to memecoin swap flow is a large, stable, entirely spurious branching
> ratio** — and Solana slots are precisely the message-grouping hazard they
> name, industrialised.

For calibration on how much confidence any branching ratio deserves: Hardiman,
Bercot & Bouchaud (arXiv:1302.1405) report near-critical \(n \approx 1\) for
E-mini futures; Filimonov & Sornette explicitly reject that conclusion.
**The field does not agree on the branching ratio of the most-studied futures
contract in the world.** That is the right prior for what one estimated on a
two-hour-old memecoin would mean.

### 6.5 The empirical evidence, such as it is — and it points the wrong way

Two verified findings, both discovered after the structural argument above was
written, and both of which strengthen it.

**(a) The one study of AMM swap inter-arrival times found same-side arrivals
approximately EXPONENTIAL.** Jaimungal, Saporito, Souza & Thamsten
(arXiv:2304.02180) — note the abstract does not mention Hawkes at all; this is
from §2.2 of the body, "Cross-exciting nature of swap flow", VERIFIED by
fetching the HTML:

> "buy swaps tend to arrive more rapidly after sell swaps than after buy swaps
> suggesting the cross-exciting nature of swap arrivals" … "The inter-arrival
> times for buy-buy and sell-sell swaps are approximately exponentially
> distributed, while the inter-arrival times for buy-sell and sell-buy swaps
> exhibit heavier tails."

Reported intensities: buy→buy 0.037, sell→buy 0.070, buy→sell 0.085, sell→sell
0.034.

> **This is close to the opposite of the equity-LOB stylised fact that motivates
> Hawkes modelling in the first place.** Same-side arrivals look Poisson — *no
> self-excitation*. What excitation exists is **cross**-side, and the authors
> attribute it to arbitrage rebalancing rather than to informed clustering.
> One paper, one Ethereum pool, so treat it as a hypothesis rather than a
> settled result — **but it is the only direct evidence that exists, and it
> points against the model.**

**(b) Eight state-of-the-art temporal point-process models could not predict
on-chain AMM event timing usably.** Jia, You, Luo, Liu & Sun (arXiv:2604.20374),
8.9M on-chain events across Uniswap V3, Aave, Morpho and Pendle, Jan 2024–Sep
2025. VERIFIED from the fetched body:

- They measure time in **block heights**, not wall-clock, because that
  "guarantees proper order of executions and evades anomalies of wall-clock
  time."
- **Uniswap V3's median inter-event interval is 1 block.** The modal observation
  is therefore a **tie** — the timestamp carries almost no within-block
  information, which is §6.2's objection appearing in real data.
- Concurrency was severe enough that they abandoned per-type modelling and
  represented "each unique co-occurrence as a single categorical label, yielding
  31 distinct event-type combinations."
- Benchmarking NHP, RMTPP, SAHP, THP, AttNHP, IntensityFree, FullyNN and
  ODETPP, standard training "produces large block height errors… severely limits
  usability."

> **That is on Ethereum, at 12-second blocks. Solana's 400 ms slots make the tie
> problem worse relative to typical memecoin inter-arrival times, not better.**

**(c) The estimation theory agrees that binning breaks the standard estimator.**
Shlomovich, Cohen, Adams & Patel (arXiv:2001.07160, published in *JCGS*) state
existing methods are "capable of producing severely biased and highly variable
parameter estimates" on binned data; the multivariate follow-up
(arXiv:2108.12357, *Statistics and Computing*) is explicitly motivated by
"synchronous events due to data aggregation or rounding" and shows "competing
methodologies produce substantially biased results." Chen, Kwan & Stindl
(arXiv:2401.11075) treat discretisation as an incomplete-data problem requiring
a particle filter to recover an unbiased likelihood.

**A verified negative that is itself a finding.** A targeted search found **no
published Hawkes fit to DEX or AMM swap arrivals, none to Solana, pump.fun or
memecoin trade arrivals, no marked/multivariate Hawkes carrying
liquidity-add/liquidity-remove marks on an AMM, and no paper analysing whether
Solana's slot batching breaks point-process estimation.** The entire
Hawkes-in-crypto literature sits on *centralised exchange* tick data (Fabre &
Muni Toke, arXiv:2401.09361; Mark, Sila & Weber) or on *block* arrivals
(Luo, Krishnamurthy & Blasch, arXiv:2203.16666 — Bitcoin PoW block arrivals,
which do **not** transfer to Solana's deterministic leader schedule).

> **Anyone fitting a Hawkes model to Solana swap flow is not standing on a
> literature. They are first, with all that implies** — and §6.4 says the first
> result they get is likely to be a spurious near-critical branching ratio.

### 6.6 The mitigating fact — for most of our tokens, arrivals are sparse

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
> **§6.3's objection therefore survives §6.6 intact**, and §6.5(a)'s finding
> that same-side AMM arrivals look exponential removes much of the motivation
> for reaching for a self-exciting model at all.

### 6.7 Recommendation

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
5. **Add a second, harder null: a REGIME-SWITCHING Poisson process.** This is
   the direct lesson of Filimonov & Sornette (§6.4) and it is not optional here,
   because memecoin lifecycles are regime changes by definition. A branching
   ratio that does not survive comparison against a regime-switching Poisson
   null is not evidence of self-excitation — it is evidence of §7's lifecycle,
   which we already know about and can measure far more cheaply.
6. **Recognise that none of this is estimable by us today.** Every input —
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

### 7.5 External base rates — and what they say about *our* population

The first draft of this document cited no external base rates and relied
entirely on our own 25-hour window. Verified literature now fills that gap, and
it does two things: it supplies brutal population-level base rates, and it
reveals that **our observed population is not the launch population at all.**

**Published base rates (each with its quality flag):**

| quantity | value | source | quality |
|---|---|---|---|
| pump.fun 24h graduation rate | **0.198%** pooled, 0.207% steady-state | Kamat, arXiv:2607.02823 (832,941 launches, May–Jun 2026) | **unrefereed, single author, no affiliation** — data released on Zenodo |
| pump.fun graduation rate | **0.63%** (4,338 of 655,770) | Marino, Naviglio, Tarantelli & Lillo, arXiv:2602.14860 | preprint, but **Lillo is a first-rank microstructure academic**; these figures are from a secondary summary, **not fetched from the body** |
| tokens migrating to major DEXes | **<2%** | Mancino, arXiv:2512.11850, **IEEE ISCC 2025 (peer-reviewed)** | strongest provenance in this table |
| new tokens flagged as rug pulls | **76.4%** (76,469 of 100,063, H1 2025) at a manually audited **0.26% FPR** | Chen et al., arXiv:2603.24625 | preprint; established blockchain-data authors |
| tokens showing rug-pull patterns | 22,195 (from 3.69B transactions, 2021–24) | Alhaidari et al., arXiv:2504.07132, **ACM CODASPY 2025** | peer-reviewed |
| token supply held by coordinated accounts | **36.5% on average** | Hu et al. "MELT", arXiv:2602.13480 (Georgia Tech) | preprint; note the title changed from "MemeTrans" at v2 |

**The three estimates of graduation disagree by 3.2×** (0.198% vs 0.63%), across
different windows and possibly different definitions. Treat the **order of
magnitude — a few tenths of one percent — as established, and the level as
unsettled.**

**Now the part that matters more, and it is a finding about our own data.**
Kamat's window implies roughly **25,200 pump.fun launches per day**;
Marino/Lillo's implies roughly **10,800/day**. We record **~395 births/day**.

> **VERIFIED (arithmetic over published launch rates and our own M13 figure): we
> observe between 1.6% and 3.7% of pump.fun launches.** Our 411 births/25h are
> not a sample of token launches. They are a sample of **tokens DexScreener
> chose to surface**, which is a population already filtered by roughly 30–60×.

Three consequences, and they sharpen rather than undermine the rest of §7:

1. **§7.3's 58.6% is conditional on an earlier, much larger filter.** A token
   must first be surfaced (≈2–4%), and *then* 41.4% of those carry an
   `initial_liquidity_usd`. The compound selection is severe, and **our base
   rates cannot be compared to the published ones without saying so.**
2. **Our population is very likely far better than average**, because whatever
   makes DexScreener surface a token correlates with the token having real
   activity. That is *good* for a trading question and *bad* for any claim about
   memecoins in general. This document makes no such general claim.
3. **Graduation is rare enough to be a sampling problem, not just a modelling
   one.** At published rates our ~395 births/day would contain roughly **0.8 to
   2.5 eventual graduations per day** — so a study of graduation on our data
   accumulates positive cases at single digits per day, and a year is a
   few hundred events. §9.5 still argues graduation is the most timeable thing
   here; §7.5 is the reason that argument needs a multi-month horizon rather
   than a multi-week one.

**A final piece of context that explains the thinness of the literature.** The
IMC '25 authors (§4.3) record that the only prior comparable Solana measurement
study dates to **2022**, and that running a Solana archival node costs roughly
**$40,000 up front plus $3,000/month**.

> **The absence of contrary published evidence about Solana microstructure is a
> COST ARTIFACT, not a green light.** Nobody has checked most of these questions
> because checking them is expensive. That cuts against optimism and against
> pessimism equally — but it should be stated before anyone treats "no published
> refutation" as support.

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
asset that is rising, because arbitrageurs trade against a stale curve.

That cost has a name and a founding paper. **VERIFIED** — Milionis, Moallemi,
Roughgarden & Zhang, "Automated Market Making and Loss-Versus-Rebalancing"
(arXiv:2208.06046), abstract fetched verbatim:

> "Our central contribution is a 'Black-Scholes formula for AMMs'. We identify
> the main adverse selection cost incurred by LPs, which we call
> 'loss-versus-rebalancing' (LVR, pronounced 'lever'). LVR captures costs
> incurred by AMM LPs due to stale prices that are picked off by better informed
> arbitrageurs. We derive closed-form expressions for LVR applicable to all
> automated market makers."

**Why LVR is a better-posed object than VPIN, and this is the point of the
section.** VPIN and PIN are **latent-variable estimators** — they infer an
unobserved informed-trader intensity from observable counts through a
likelihood, and they inherit trade-classification error, boundary solutions and
numerical pathology (§10.2, §12.6). **LVR is a realized accounting quantity**:
you compute it by comparing an LP's realized P&L against a rebalancing portfolio
at observed prices. There is no classification step to corrupt and no likelihood
to get stuck in.

> **If you want an adverse-selection number on an AMM, LVR is strictly the
> better-founded construct — and the verified empirical picture it produces is
> uniformly bad for LPs.** Fritsch & Canidio (arXiv:2404.05803) find arbitrage
> losses "exceed the fees earned by liquidity providers across many of the
> largest AMM liquidity pools (on Uniswap)"; Cartea, Drissi & Monga
> (*Applied Mathematical Finance* 30(2)) find, on Uniswap v3 and Binance data,
> that "liquidity provision in CFMs is a loss-leading activity."
>
> **Note that this is a statement about the LP side, and we are on the taker
> side.** It does not translate into a taker edge — one party losing to
> arbitrageurs does not make a third party's round trip profitable. It is
> included because it identifies *who* the informed counterparty is (arbitrage
> flow against stale curves), which is what §8.2 needs.

One directional result worth carrying: Fritsch & Canidio report that moving from
12-second to 100 ms block times reduces arbitrage losses by **20–70%**, and
Milionis, Moallemi & Roughgarden (arXiv:2305.14604) find faster chains mean
smaller LP losses. **Solana's 400 ms slots are therefore a materially
lower-adverse-selection environment for LPs than Ethereum** — which is a point
in favour of the venue and against the assumption that everything about
memecoins is worse than everything about ordinary DeFi.

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
> **Three honest caveats.** F3 requires reading curve state, which is a
> capability we do not currently have and which would need its own scoped,
> approved milestone — this document does not authorise it and does not assume
> it. "The event is forecastable" is **not** the same as "the price move around
> it is forecastable"; the latter is unestablished and should not be assumed
> from the former. And **the base rate is punishing**: §7.5's verified
> graduation rates of 0.198%–0.63% mean our ~395 births/day contain roughly
> **0.8–2.5 eventual graduations**, so a study of graduation on our data
> accumulates positive cases at single digits per day. **This is a multi-month
> measurement, not a multi-week one**, and anyone proposing it should budget
> accordingly rather than discovering the sample-size problem later.

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

**This is not a hypothetical, and the size of the effect has been measured on
pump.fun itself.** Kamat (arXiv:2607.02795, ~1.58M buyer transactions across
166,098 launches) studied whether coordinated sniper cohorts drive early buyer
flow. The naive first-30-minute buyer-count lift is **+130.9%**. After
propensity-score matching and contamination adjustment — because *the cohorts'
own transactions are part of the flow being measured* — the estimate falls to
**+16.1%**, and the SOL-inflow lift to a negligible **+6.3%**.

> **A naive estimate overstated the matched one by roughly 8×, through exactly
> the mechanism §9.6 describes: the thing being counted was partly the thing
> doing the counting.** *(Quality flag: unrefereed, single author, no listed
> affiliation — cite the v3 title, which differs from v1. Treat the magnitude
> as indicative and the direction as the lesson.)*

**A second contamination, from a different direction.** Heimbach, Pahari &
Schertenleib (arXiv:2401.01622, **IEEE S&P 2024**) find that **more than a
quarter of the volume on Ethereum's five largest DEXes** is non-atomic
(CEX–DEX) arbitrage, and that **eleven searchers account for over 80% of it**
($132B).

> **INFERRED, and directly relevant to Block C: a large fraction of what looks
> like "order flow" on a DEX is a handful of arbitrage bots reacting to an
> off-chain price.** Any flow model fitted to raw swaps is substantially
> modelling those bots rather than a population of traders — and their behaviour
> is a function of the CEX price, which is not in our state vector at all. This
> is a further reason §5.1 marks C3–C7 as tier T3 rather than as an approximable
> gap: even with the feed, the signed flow would need decomposing before it
> meant what one wants it to mean.

**And the meta-warning, which changes how S-3 should be run.** Bailey, Borwein,
López de Prado & Zhu (*Notices of the AMS* 61(5)) prove that "high simulated
performance is easily achievable after backtesting a relatively small number of
alternative strategy configurations," and — the sentence that matters most —
that **"under memory effects, backtest overfitting leads to negative expected
returns out-of-sample, rather than zero performance."**

> **An overfit selection rule on a memory-bearing series is not neutral; it is
> systematically worse than random.** Combined with Harvey, Liu & Zhu's argument
> (NBER WP 20592 / *RFS* 29(1)) that a new factor needs a t-ratio above 3.0 and
> that "most claimed research findings in financial economics are likely false,"
> this is the concrete justification for §11.2's instruction to **pre-register
> S-3's feature set and split before looking** — and for treating a null result
> there as a genuine, publishable-internally outcome rather than a failure.

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

**Published work now partially answers the obvious objection.** The natural
caveat is that a sophisticated operator rotates addresses, defeating the whole
approach. Two verified results suggest persistence is nonetheless detectable at
scale:

- Kamat (arXiv:2607.02795) identifies **1,012 persistent co-firing wallet
  cohorts** across 166,098 launches in a two-week window — i.e. rings that
  recur, and are identifiable as recurring.
- Hu et al. "MELT" (arXiv:2602.13480, Georgia Tech) release bundle-level data
  "identifying multi-account single entities" and report **36.5% of token supply
  held by coordinated accounts on average**.

*(Both are preprints; the Kamat item is unrefereed and single-author. And note
§9.6: the same author's naive coordination estimate was 8× its matched value, so
read "1,012 cohorts exist" as the robust part and any effect size as the fragile
part.)*

*The caveats that remain, because this is the claim most likely to be
over-sold:* the above establishes that *trader* rings persist, which is not the
same as establishing that **creator** addresses repeat — the feature our schema
would key on. Clustering public addresses is also a technique whose reliability
degrades exactly against the adversaries it most wants to catch, and any
operator worth tracking has an obvious incentive to rotate.

> **So the first step is still not to build the feature — it is the one-line
> measurement of how often `creator_address` repeats across our 411 births/25h.**
> That is a single `GROUP BY` over a column we already persist. If reuse is
> rare, the whole line of work closes cheaply, and that is a good outcome. This
> should be added to §11.2 as a zero-cost study.

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
| **VPIN / order-flow toxicity as usually computed** | Three independent reasons, any one sufficient. (i) It requires signed volume (Block C, tier **T3**, out of bounds). (ii) Its volume-bucketing is calibrated to a market maker's inventory problem that no AMM LP faces. (iii) **It has been refuted in its home market** — see below. | **LVR** (§8.1), which is a *realized* quantity rather than a latent-variable estimate. |

**On (iii), because "refuted" is a strong word and it is the correct one.**
Andersen & Bondarenko, *Review of Finance* 19(1) (working paper CREATES RP
2013-43, fetched), using CME best-bid-offer files to construct near-perfect
trade classification, conclude verbatim:

> "when VPIN is constructed from accurate classification, it behaves in a
> diametrically opposite way to BVC-VPIN. We also find the latter to have
> forecast power for short-term volatility solely because it generates
> systematic classification errors that are correlated with trading volume and
> return volatility. When controlling for trading intensity and volatility, the
> BVC-VPIN measure has no incremental predictive power for future volatility.
> **We conclude that VPIN is not suitable for capturing order flow toxicity.**"

Their earlier *Journal of Financial Markets* 17(1) paper adds that VPIN "only
reached an all-time high **following** the flash crash" and that "its predictive
content stems from a mechanical relation with trading intensity." Easley, López
de Prado & O'Hara published a rejoinder (*JFM* 17(C): 47–52) disputing the
framing; the published exchange does not appear to contain an incremental-power
test controlling for volume and volatility.

> **Note the shape of the failure, because it is the same shape as §9.6's.** The
> metric appeared predictive because it was mechanically correlated with volume,
> and volume is correlated with volatility. Andersen & Bondarenko put it
> generally: "any variable correlated with volatility will, inevitably, possess
> non-trivial forecast power for future volatility… This merely confirms that
> volatility begets volatility." **That is exactly the trap §9.6 warns about in
> our own setting**, arrived at independently in a different market a decade
> earlier. It is the single best argument in this document for controlling on
> contemporaneous volume and liquidity in *every* study §11 proposes.

*(For completeness, PIN — VPIN's structural ancestor — carries its own
estimation pathologies: Lin & Ke (*JFM* 14) find "approximately 44% of PIN
estimates for recent stock market data may have been subject to a downward
bias"; Yan & Zhang (*JBF* 36(2)) document boundary-solution bias; Duarte & Young
(*JFE* 91(2)) find the information-asymmetry component of PIN is **not** priced
while the illiquidity component is — the asset-pricing analogue of the same
critique.)*

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

**S-0. Count the venue mix in our own `dex_id` column.**
*Why zeroth:* §3.5 shows three incompatible impact mathematics coexist in this
market — smooth constant product (Raydium AMM v4/CPMM, PumpSwap), stepped
constant-sum (Meteora DLMM), and concentrated liquidity (Orca Whirlpools,
Raydium CLMM). §2's formula is the **wrong functional form**, not a small
approximation, for the latter two. Until we know the mix, we do not know what
fraction of our own data §2 even applies to. *Cost:* one `GROUP BY`.
*Refutable claim:* the population is overwhelmingly uniform-CPMM, so §2 governs
nearly all of it. *What would refute it:* a material DLMM/CLMM share, which
would make `pool_kind` (A6) and the typed absence of A2 mandatory before any
impact number is computed, not optional.

**S-0b. Count `creator_address` repeats across births.**
*Why:* §9.7 argues creator/wallet clustering is the highest-value unbuilt
feature, and this single `GROUP BY` decides whether the line of work is open at
all. *Refutable claim:* creator addresses recur often enough to carry
cross-token information. *What would refute it:* near-total uniqueness, which
closes §9.7 cheaply and is a perfectly good outcome.

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

## 12. Bibliography, with verification status

**Verification convention for this section.** A source is **VERIFIED** only if
its primary artifact was fetched and the claim attributed to it was read in it.
Anything short of that bar is labelled, and where a widely repeated claim failed
verification it is recorded — protocol claims in **§12.4**, academic ones in
**§12.6** — rather than quietly dropped. Two sections are deliberately lists of
things this document does *not* rely on; that is the point of them.

**Peer-reviewed items are marked as such.** Several important sources are
unrefereed preprints, one class of which (A27, A28) is single-author with no
listed affiliation. They are used because their data is released and their
direction is corroborated elsewhere, **not** because they carry the weight of
the peer-reviewed entries, and every use of them says so.

### 12.1 Protocol and program sources — VERIFIED

| # | source | URL | what it establishes here |
|---|---|---|---|
| P1 | `UniswapV2Library.sol` (`getAmountOut` / `getAmountIn`) | https://github.com/Uniswap/v2-periphery/blob/master/contracts/libraries/UniswapV2Library.sol | The fee-on-input convention of §2.1. `getAmountIn` rounds **up** (`+1`); the exact-input path does not. |
| P2 | Uniswap v2 Core whitepaper (Adams, Zinsmeister, Robinson, 2020) | https://app.uniswap.org/whitepaper.pdf | 30 bps LP fee; the optional 5 bps protocol fee, initially off. |
| P3 | Raydium constant-product reference | https://docs.raydium.io/algorithms/constant-product.md | Raydium implements §2.1's exact form, with explicit rounding rules and a `k_after >= k_before` post-condition. |
| P4 | Raydium fee comparison | https://docs.raydium.io/reference/fee-comparison.md | §3.6's fee table, **and the x/10,000 vs x/1,000,000 encoding trap.** |
| P5 | Meteora DLMM formulas | https://docs.meteora.ag/core-products/dlmm/formulas.md | §3.5: discrete bins \(P_i=(1+s/10^4)^i\), **constant-sum within a bin**, volatility-scaled dynamic fee. |
| P6 | Orca Whirlpools spec | https://docs.orca.so/llms.txt | Fee tiers by tick spacing (0.01%–2.00%); 87/12/1 fee split; Q64.64 sqrt-price. |
| P7 | pump.fun program README | https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_PROGRAM_README.md | §3.1 curve constants; "based on Uniswap V2 … synthetic x and y reserves"; completion at `real_token_reserves == 0`. |
| P8 | pump.fun fee program README | https://github.com/pump-fun/pump-public-docs/blob/main/docs/FEE_PROGRAM_README.md | §3.4: **the mcap-tiered fee schedule, and that 1% is only the `feeConfig == null` fallback.** |
| P9 | PumpSwap README | https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_SWAP_README.md | §3.4: migration to PumpSwap, LP burn, disabled legacy `withdraw`-to-Raydium path. |
| P10 | Agave `validator.rs` | https://github.com/anza-xyz/agave/blob/master/core/src/validator.rs | §4.1: `CentralSchedulerGreedy` is the default; prio-graph `CentralScheduler` is **deprecated**. |
| P11 | Agave `greedy_scheduler.rs` | https://github.com/anza-xyz/agave/blob/master/core/src/banking_stage/transaction_scheduler/greedy_scheduler.rs | §4.1: priority-order greedy scheduling under `ThreadAwareAccountLocks`. |
| P12 | Solana SDK `clock/src/lib.rs` | https://github.com/anza-xyz/solana-sdk/blob/master/clock/src/lib.rs | §4.2: **400 ms slot as a compile-time assert**, 4 consecutive leader slots, forward-at-2, hold-20. |
| P13 | Jito low-latency transaction send docs | https://docs.jito.wtf/lowlatencytxnsend/ | §4.3: 5-transaction atomic bundles, **50 ms parallel auctions**, tip/CU-efficiency ordering, 1,000-lamport minimum tip. |
| P14 | Jupiter slippage docs | https://developers.jup.ag/docs/swap/advanced/slippage.md | §4.4: RTSE estimates **at order time, baked into the transaction**; Router default `slippageBps=50`; `otherAmountThreshold`. |
| P15 | Raydium swap user flow | https://docs.raydium.io/user-flows/swap.md | §4.4: `minimumAmountOut` formula, `ExceededSlippage` revert, the "2–5%" meme-token UI default. |
| P16 | SIMD-0191 (Activated) | https://github.com/solana-foundation/solana-improvement-documents/blob/main/proposals/0191-enable-transaction-loading-failure-fees.md | §4.4: **failed transactions are included in the block and still incur fees.** |
| P17 | Solana fees documentation | https://solana.com/docs/core/fees | §4.4: 5,000 lamports/signature base fee; the priority-fee formula. |
| P18 | Solana Alpenglow page | https://solana.com/upgrades/alpenglow | §4.2: ~150 ms finality target, Q3 2026 mainnet target, two prerequisites activated. **No slot-duration change stated.** |

### 12.2 Measurement literature — VERIFIED

| # | source | URL | what it establishes here |
|---|---|---|---|
| M1 | Gerzon, Weintraub, In, Mislove, Nita-Rotaru, *"Quantifying the Threat of Sandwiching MEV on Jito: A Measurement of Solana's Leading Validator Client"*, **ACM IMC '25** | DOI [10.1145/3730567.3764493](https://doi.org/10.1145/3730567.3764493) · PDF https://cnitarot.github.io/papers/imc26_solana.pdf | §4.3 in full: 521,903 sandwich instances costing >$7.7M (2025-02-09→2025-06-09); >$2.4M spent on defensive bundling; >86% of bundles single-transaction; 97% of top-500 validators Jito-compatible; Jito's mempool opened Aug 2022 and was suspended Mar 2024 without reducing activity. **The authors state their figures are lower bounds** (length-3 bundles only, ~2.77% of daily bundles, SOL-denominated trades only). |

### 12.3 Repository sources — the basis of every measured number here

| # | source | what it supplies |
|---|---|---|
| R1 | `docs/milestones/SOLANA-ROUTE-OBSERVATION-001.md` §4.2, §14.1 | The cohort-8 observation-time liquidity distribution (n=42) and the birth-time distribution (n=170); M12's 4.75× decay; M13's 41.4% enrolment ceiling; M14's zero death count; M15's single-window caveat. **These were supplied measurements — that document explicitly records that its author made no EVO measurement and no provider call, and neither did this one.** |
| R2 | `docs/milestones/SOLANA-ROUTE-OBSERVATION-001.md` §3.2, §5.3–§5.5, §8.1 | The typed-absence vocabulary reused in §5.1; the observable/unobservable split; the F9 per-trade-feed prohibition that defines tier T3. |
| R3 | `app/services/crypto_sparse_observation.py` | `SPARSE_HORIZONS`; enrolment eligibility; the 15m/1h/6h/24h coverage figures and the ~83-minute median last tick quoted in §7.4. |
| R4 | `app/models.py` | `CryptoPriceTick`, `CryptoTokenBirthEvent`, `CryptoHorizonObservation`, `CryptoTokenLifecycleSnapshot`, `CryptoTokenActorObservation`, `CryptoTokenSurvivalOutcome` — the columns Blocks A–F map onto. |
| R5 | `app/services/crypto_risk_engine.py` | The §8.3 thresholds: `max_top_holder_pct` 20.0, `max_sniper_pct` 20.0, `max_insider_pct` 15.0, `max_bundler_pct` 25.0, `min_liquidity_usd` 5000.0. |

### 12.4 CITATIONS THAT DID NOT CHECK OUT — do not repeat these

Included because the brief asked for it explicitly, and because several of these
are near-universal in secondary sources.

| claim | status | what is true instead |
|---|---|---|
| **pump.fun "graduates at ~$69,000 market cap"** | **FAILED VERIFICATION** — appears in no primary artifact we could locate | A SOL-price-dependent restatement of the 85 SOL constant, implying a SOL price that has not held for some time. **The invariant is 85.00536 SOL** (§3.2), token-denominated and exact. |
| **pump.fun trading fee is 1%** | **STALE** | 1% is `global.feeBasisPoints`, used **only** when the `FeeConfig` PDA is absent. Current bonding-curve total is **1.25%** (§3.4). |
| **PumpSwap launched March 2025** | **DATE UNVERIFIED** (substance verified) | That PumpSwap replaced Raydium as the migration destination is VERIFIED (P9). The date is not. |
| **Raydium CPMM has 4 fee tiers up to 4%; CLMM has 8 tiers including 2%; split is 84/12/4** | **CONTRADICTED** by Raydium's current docs (P4) | CPMM: 3 tiers, max 1%. CLMM: 4 tiers, max 1%. Split 88% LP / 12% protocol. Widely circulated but stale. |
| **Solana transaction ordering is deterministic FIFO** | **FALSE** (P10, P11) | Priority-ordered greedy scheduling with account-lock-aware deferral. Realized order depends on contention, not on arrival. |
| **Solana's priority score is fee-per-compute-unit; prio-graph look-ahead is N=256** | **UNVERIFIED** — the authoritative Anza write-up was unreachable from this environment | Read `transaction_priority_id.rs` before asserting either. Note also that the prio-graph scheduler is now the **deprecated** path. |
| **"No public mempool, therefore no sandwiching on Solana"** | **FALSE**, and measured to be false (M1) | Sandwiching proceeds via private orderflow into Jito bundles; the attacker never needs a mempool because the victim's transaction is routed to them. |
| **Alpenglow will change slot duration** | **NOT STATED** by the primary source (P18) | It targets ~150 ms *finality*. Secondary sources give contradicting mainnet dates; treat anything beyond "not on mainnet as of 2026-08-14" as unverified. |
| **Orca's tick price base is \(1.0001^i\)** | **UNVERIFIED** — Orca's own concepts page omits the formula | Read `programs/whirlpool/src/math/tick_math.rs` before asserting it. |
| **pump.fun `MAX_MIGRATE_FEES`** | **INTERNALLY INCONSISTENT IN THE PRIMARY SOURCE** — the README states `pool_migration_fee` is 15,000,001, "less than `MAX_MIGRATE_FEES == 15_000_000`", which is arithmetically false | Read the IDL before building on either number. This is an error in pump.fun's own documentation, recorded so the next reader does not spend time on it. |

### 12.5 Academic literature — VERIFIED

Each entry was fetched and the attributed claim read, unless a narrower status
is stated. **Quality flags are given because several key items are unrefereed
single-author preprints and should not be read as carrying the weight of the
peer-reviewed entries.**

**Point processes and the estimation problem (§6):**

| # | source | URL | status |
|---|---|---|---|
| A1 | Filimonov & Sornette, "Apparent criticality and calibration issues in the Hawkes self-excited point process model" (*Quantitative Finance* 15(8)) | https://arxiv.org/abs/1308.6756 | **VERIFIED** — the spurious-\(n\approx1\) result under regime switching, and the message-grouping hazard. §6.4's load-bearing citation. |
| A2 | Hardiman, Bercot & Bouchaud, "Critical reflexivity in financial markets" (*EPJ B* 86:442) | https://arxiv.org/abs/1302.1405 | **VERIFIED** — the near-critical E-mini claim that A1 rejects. Cited only to show the field disagrees. |
| A3 | Jaimungal, Saporito, Souza & Thamsten, "Optimal Trading in Automated Market Makers with Deep Learning" | https://arxiv.org/abs/2304.02180 | **VERIFIED from the body** (§2.2), not the abstract, which does not mention Hawkes. The exponential same-side inter-arrival finding. |
| A4 | Jia, You, Luo, Liu & Sun, "Towards Event-Aware Forecasting in DeFi" | https://arxiv.org/abs/2604.20374 | **VERIFIED** — median inter-event interval of 1 block; eight neural TPPs failing to reach usable timing accuracy. |
| A5 | Shlomovich, Cohen, Adams & Patel, "Parameter Estimation of Binned Hawkes Processes" (*JCGS*) | https://arxiv.org/abs/2001.07160 | **VERIFIED** — "severely biased and highly variable" estimates on binned data. |
| A6 | Shlomovich, Cohen & Adams, multivariate aggregated Hawkes (*Statistics and Computing*) | https://arxiv.org/abs/2108.12357 | **VERIFIED** — motivated by "synchronous events due to data aggregation or rounding". |
| A7 | Chen, Kwan & Stindl, "Estimating the Hawkes process from a discretely observed sample path" | https://arxiv.org/abs/2401.11075 | **VERIFIED** — discretisation as an incomplete-data problem. |
| A8 | Bacry, Mastromatteo & Muzy, "Hawkes processes in finance" | https://arxiv.org/abs/1502.04592 | **VERIFIED** — the standard review; background only. |
| A9 | Luo, Krishnamurthy & Blasch, "Hawkes Process Modeling of Block Arrivals in Bitcoin Blockchain" | https://arxiv.org/abs/2203.16666 | **VERIFIED** — and cited as **not transferring**: PoW block arrivals differ fundamentally from Solana's deterministic leader schedule. |
| A10 | SIMD-0525, "Reduce Slot Times" (**Draft**) | https://github.com/solana-foundation/solana-improvement-documents/blob/main/proposals/0525-reduce-slot-times.md | **VERIFIED** — 400→200 ms proposal; "quantization error" framing (§4.2). |

**AMM adverse selection (§8):**

| # | source | URL | status |
|---|---|---|---|
| A11 | Milionis, Moallemi, Roughgarden & Zhang, "Automated Market Making and Loss-Versus-Rebalancing" | https://arxiv.org/abs/2208.06046 | **VERIFIED, abstract fetched verbatim.** The LVR definition quoted in §8.1. **The "σ²/8" instantaneous-rate expression is NOT in the abstract and was NOT verified — this document does not use it.** |
| A12 | Milionis, Moallemi & Roughgarden, "…Arbitrage Profits in the Presence of Fees" (*FC* 2025) | https://arxiv.org/abs/2305.14604 | **VERIFIED** — faster chains ⇒ smaller LP losses. |
| A13 | Fritsch & Canidio, "Measuring Arbitrage Losses and Profitability of AMM Liquidity" | https://arxiv.org/abs/2404.05803 | **VERIFIED** — losses exceed fees on many large Uniswap pools; 100 ms vs 12 s blocks reduces losses 20–70%. |
| A14 | Cartea, Drissi & Monga, "Predictable Losses of Liquidity Provision…" (*Applied Mathematical Finance* 30(2)) | https://econpapers.repec.org/RePEc:taf:apmtfi:v:30:y:2023:i:2:p:69-93 | **VERIFIED** — "liquidity provision in CFMs is a loss-leading activity". Note "predictable loss" is a **distinct** decomposition from LVR, not a synonym. |
| A15 | Lehar, Parlour & Zoican, "Fragmentation and optimal liquidity supply on decentralized exchanges" | https://arxiv.org/abs/2307.13772 | **VERIFIED** — small LPs converge to high-fee pools to mitigate adverse selection. **This is Lehar–Parlour–Zoican on fragmentation, NOT the Lehar & Parlour AMM-vs-LOB paper**, which remains unverified (§12.6). |

**Toxicity metrics and their refutation (§10.2):**

| # | source | URL | status |
|---|---|---|---|
| A16 | Easley, López de Prado & O'Hara, "Flow Toxicity and Liquidity in a High Frequency World" (*RFS* 25(5)) | https://www.stern.nyu.edu/sites/default/files/assets/documents/con_035928.pdf | **VERIFIED** — the VPIN claim. *The same title page discloses that the authors have applied for a patent on VPIN and hold a financial interest in it.* |
| A17 | Andersen & Bondarenko, "VPIN and the flash crash" (*JFM* 17(1)) | https://ideas.repec.org/p/aah/create/2011-50.html | **VERIFIED via the CREATES working paper**; the published abstract was paywalled. |
| A18 | Andersen & Bondarenko, "Assessing Measures of Order Flow Toxicity…" (*Review of Finance* 19(1)) | https://pure.au.dk/ws/files/68359010/rp13_43.pdf | **VERIFIED, abstract read off the PDF.** The strongest form of the critique, quoted in §10.2. |
| A19 | Easley, López de Prado & O'Hara, "VPIN and the Flash Crash: A rejoinder" (*JFM* 17(C)) | https://ideas.repec.org/a/eee/finmar/v17y2014icp47-52.html | **VERIFIED** — cited so the dispute is represented from both sides. |
| A20 | Lin & Ke (*JFM* 14); Yan & Zhang (*JBF* 36(2)); Duarte & Young (*JFE* 91(2)) | RePEc listings | **VERIFIED (abstracts)** — PIN's floating-point, boundary-solution and pricing pathologies (§10.2 footnote). |

**Solana / memecoin empirics (§7.5, §9):**

| # | source | URL | status & quality |
|---|---|---|---|
| A21 | Gerzon et al., ACM IMC '25 — Jito sandwiching | https://cnitarot.github.io/papers/imc26_solana.pdf | **VERIFIED, pages 1–2 read. Peer-reviewed.** Also the source of the archival-node cost and the "only prior study dates to 2022" admission (§7.5). |
| A22 | Mancino, "The Memecoin Phenomenon" | https://arxiv.org/abs/2512.11850 | **VERIFIED. Peer-reviewed (IEEE ISCC 2025).** <2% migration; pump.fun = 40–67.4% of Solana DEX transactions. |
| A23 | Alhaidari et al., "SolRPDS" | https://arxiv.org/abs/2504.07132 | **VERIFIED. Peer-reviewed (ACM CODASPY 2025).** 3.69B transactions → 22,195 rug-pattern tokens. |
| A24 | Chen et al., "From Hype to Collapse: Rug Pull Scams on Solana" | https://arxiv.org/abs/2603.24625 | **VERIFIED.** Preprint; established authors. 76.4% flagged at 0.26% audited FPR. |
| A25 | Hu, Tekin, Xu & Liu, "MELT" (Georgia Tech) | https://arxiv.org/abs/2602.13480 | **VERIFIED.** Preprint. 36.5% of supply coordinated. **Title changed from "MemeTrans" at v2 — cite the v2 title.** |
| A26 | Marino, Naviglio, Tarantelli & Lillo, "Predicting the success of new crypto-tokens: the Pump.fun case" | https://arxiv.org/abs/2602.14860 | **ABSTRACT VERIFIED; the 655,770 / 4,338 / 0.63% figures are from a secondary summary and were NOT fetched from the body.** Preprint, but Lillo is a first-rank microstructure academic. |
| A27 | Kamat, "Pump.fun Graduation Regime Windows" | https://arxiv.org/abs/2607.02823 | **VERIFIED.** ⚠️ **Unrefereed, single author, no listed affiliation.** Data released on Zenodo (CC-BY-4.0), which is a point in its favour. 3.18× disagreement with A26 unexplained. |
| A28 | Kamat, "Coordinated Sniper Cohorts on Pump.fun" | https://arxiv.org/abs/2607.02795 | **VERIFIED (v3).** ⚠️ Same quality caveats as A27. The +130.9% → +16.1% contamination result in §9.6. **v1 carried a different title — cite v3.** |
| A29 | Heimbach, Pahari & Schertenleib, "Non-Atomic Arbitrage in Decentralized Finance" | https://arxiv.org/abs/2401.01622 | **VERIFIED. Peer-reviewed (IEEE S&P 2024).** >25% of top-5 DEX volume; 11 searchers ⇒ >80%; **$132B per the v3 abstract — secondary summaries saying $137B are wrong.** |

**Methodological skepticism (§9.6, §11.2):**

| # | source | URL | status |
|---|---|---|---|
| A30 | Bailey, Borwein, López de Prado & Zhu, "Pseudo-Mathematics and Financial Charlatanism" (*Notices of the AMS* 61(5)) | https://scholarworks.wmich.edu/math_pubs/40/ | **VERIFIED** — overfitting under memory effects gives **negative** expected out-of-sample returns. |
| A31 | Harvey, Liu & Zhu, "…and the Cross-Section of Expected Returns" (*RFS* 29(1); NBER WP 20592) | https://www.nber.org/papers/w20592 | **VERIFIED from the NBER title page.** t-ratio > 3.0; "most claimed research findings in financial economics are likely false." *(Authors are Harvey, Liu and **Heqing** Zhu; some listings misname the third author.)* |
| A32 | Bailey & López de Prado, "The Deflated Sharpe Ratio" (*JPM* 40(5)) | https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf | **VERIFIED** — selection bias under multiple testing. |
| A33 | Cong, Li, Tang & Yang, "Crypto Wash Trading" (*Management Science* 69(11)) | https://arxiv.org/abs/2108.10984 | **VERIFIED** — >70% of reported volume on unregulated exchanges. Cited as a warning about volume-based inputs generally; **it is a CEX finding and does not directly indict on-chain Solana data.** |
| A34 | Hudson & Urquhart, "Technical trading and cryptocurrencies" (*Annals of OR* 297) | https://centaur.reading.ac.uk/85715/8/Hudson-Urquhart2019_Article_TechnicalTradingAndCryptocurre.pdf | **VERIFIED** — ~15,000 rules with multiple-hypothesis correction; **no out-of-sample predictability for Bitcoin**, survivors are the smaller, less liquid coins where costs are worst. |
| A35 | Fieberg, Liedtke & Zaremba, "Cryptocurrency anomalies and economic constraints" (*IRFA* 94) | https://ideas.repec.org/a/eee/finana/v94y2024ics1057521924001509.html | **VERIFIED** — anomalies "originate from micro-cap coins of negligible economic importance"; alpha extracted "largely from short positions"; effects "fade over time". |

### 12.6 What remains unverified

**Named papers this document deliberately does NOT cite, because they were not
fetched.** Each was searched for and could not be confirmed. **Do not repeat any
of these on this document's authority.**

| paper | why it is absent |
|---|---|
| **Lehar & Parlour on AMMs vs limit order books** | Not fetched; co-authorship on the specific AMM-vs-LOB piece unconfirmed. A *different* paper — Lehar–Parlour–**Zoican** on fragmentation — **is** verified as A15 and is not a substitute. |
| **Barbon & Ranaldo**, "On The Quality Of Cryptocurrency Markets: Centralized Versus Decentralized Exchanges" | Not fetched. Authors, venue and claims unconfirmed. |
| **Capponi & Jia** on AMM adoption/economics | Not fetched. *(A different Capponi paper — Capponi & Zhu on latency auctions, arXiv:2512.10094 — was verified but is not used here.)* |
| **Angeris & Chitra**, "Improved Price Oracles: Constant Function Market Makers" | Not fetched. |

**Specific numbers deliberately not used, despite being easy to find:**

- **The LVR "σ²/8" instantaneous-rate expression.** The LVR paper is verified
  (A11) but that expression is not in its abstract and the body was not read.
  **This document states LVR's definition and never its closed form.**
- **"Over 60% of Solana block compute units consumed by arbitrage."** Relayed by
  vendor blogs from a claimed Jito figure; no primary Jito publication reached.
- **Deployer-funded sniping statistics** (~15,000 SOL/month extracted, 4,600
  sniper wallets, 87% of snipes profitable, 85% exiting within five minutes).
  Industry blogs only, no primary report. **These circulate widely and are
  unsourced.**
- **A Solana-specific Hawkes branching ratio.** A search summary attributed
  system-wide \(n=0.80\) figures to Fabre & Muni Toke (arXiv:2401.09361); the
  abstract contains no branching ratio and a full-text search found nothing
  extractable. **Do not cite this.**
- **Any academic measurement of the bot / wash-trade / MEV share of Solana DEX
  volume.** None found. The nearest verified proxies (A29 Heimbach, A33 Cong)
  are **off-Solana** and are labelled as such wherever used.

**Verified negatives — searched for, genuinely absent, and reported as findings
rather than as gaps** (§6.5): no published Hawkes fit to DEX or AMM swap
arrivals; none to Solana, pump.fun, or memecoin trade arrivals; no marked or
multivariate Hawkes carrying liquidity-add/remove marks on an AMM; and no
analysis of whether Solana's slot batching breaks point-process estimation.
§6.4's call for a batched-Poisson null therefore stands as **an unfilled gap in
the literature, not merely an unfilled gap in our reading** — though it remains,
strictly, a statement about what a bounded search did not find.

> **What the literature changed, and what it did not.** It **changed** §6, which
> now rests on demonstrated identification failure (A1) and direct contrary
> evidence on AMM inter-arrival times (A3) rather than on structural argument
> alone; it **changed** §7.5 and §9, which now carry external base rates and the
> finding that we observe only 1.6–3.7% of launches; and it **corroborated**
> §9.6's contamination warning from two independent directions (A28, A29).
>
> It did **not** change a single number in §2 or §3, which are derivations from
> verified protocol constants, nor in §2.4–§2.6, §7.1 or §9.2, which are
> arithmetic over our own measurements. **The load-bearing quantitative spine of
> this document never depended on the literature, and still does not.**

---

## Closing note on scope

This document is research. It contains no production code, defines no executable
path, and authorises nothing. Every capability it touches — bonding-curve
progress observation (§9.5), denser sampling (§11.3), any per-swap feed (§5.0
tier T3) — is named as requiring its own scoped, approved milestone, and none is
assumed.

The hard boundary of `AGENTS.md` and `docs/SAFETY_BOUNDARIES.md` applies
throughout: market and liquidity **observation** only. Nothing here is EV, a
side, a size, an order, a recommendation, or a trade direction. The notional
figures in §2, §3 and §9 are properties of a **measurement instrument** — probe
sizes chosen to match the depth of the thing being measured, in the same sense
as `SOLANA-ROUTE-OBSERVATION-001` §4.2 — and are explicitly not position sizes,
not sizing recommendations, and not derived from any signal, conviction, or
capital base.

