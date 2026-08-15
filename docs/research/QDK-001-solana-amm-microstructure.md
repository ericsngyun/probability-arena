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

