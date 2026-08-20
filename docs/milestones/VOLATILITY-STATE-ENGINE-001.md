# VOLATILITY-STATE-ENGINE-001

**Status: DESIGN, NOT IMPLEMENTED.** Written 2026-08-20, while
`PROD-ACTIVITY-PROFILE-001` was capturing and **without reading any of its
data**. Nothing here presupposes the outcome of `MARKET-MICROSTRUCTURE-EDGE-001`,
which has not run.

Consumes `MARKET-STATE-FABRIC-v1`. Adds no venue access, no capital path, no
agent in the synchronous market path.

---

## 1. Why prediction-market volatility is structurally different

An equity price is unbounded and has no scheduled terminal date. A Kalshi
contract has **both** constraints, and they dominate its second moment:

**The price is a bounded probability.** `p ∈ [0,1]`, and the variance of a
bounded variable is mechanically suppressed near its bounds — a contract at 0.02
*cannot* fall 50% and *cannot* be volatile in the way a 0.50 contract is. Any
model quoting a single σ across the price range is describing the bound, not the
market. This is why `dist_to_bound = min(p, 1−p)` is a mandatory fabric feature
rather than a nicety.

**Uncertainty collapses toward a known deadline.** As `T → 0` the contract
converges to 0 or 1. The terminal variance is not merely low, it is **zero by
construction**, and the path there is not smooth: resolution is often a discrete
event (a final out, a called race) that moves the price from 0.6 to 1.0 in one
print. So volatility is **non-stationary by design**, with a deterministic
component driven by `T` that has nothing to do with order flow.

**Consequence.** A GARCH-family model fitted to this series will attribute the
deadline-driven variance path to conditional heteroskedasticity and report
persistence that is really just the calendar. The structural component must be
modelled **explicitly and separately**, with a residual model fitted only to
what remains. That ordering is the design, not an optimisation.

> **The honest framing.** We are not forecasting σ better than the market. We
> are constructing a **state label** that changes what the system does. Per
> QUANT-DECISION-KERNEL-001, `g(f*) = KL(p‖q)` — beating the market's
> probability *is* the alpha, and we have never demonstrated it. This engine is
> not an alpha source and must never be reported as one.

## 2. The state vector, and what we can actually compute

`S_t = (p_t, T_t, σ_t, OFI_t, spread_t, depth_t, tradeIntensity_t, jumpState_t)`

| term | definition | source | computable today? |
|---|---|---|---|
| `p_t` | microprice (fabric #10), not mid | orderbook sid | **yes** |
| `T_t` | time to **event**, from `occurrence_datetime` | REST | **yes** — see L23 |
| `σ_t` | realised vol of 1 s microprice changes, trailing window | derived | **yes** |
| `OFI_t` | order-flow imbalance: signed depth added/removed at touch | orderbook sid | **partially — see §7** |
| `spread_t` | fabric #2 | orderbook sid | **yes** |
| `depth_t` | fabric #3/4/6/7 | orderbook sid | **yes** |
| `tradeIntensity_t` | trades per unit time, trailing | **trade sid** | **yes, with a lag** |
| `jumpState_t` | discrete-jump indicator (§6) | derived | **yes** |

**`T_t` must be measured from `occurrence_datetime`, not `close_time`**
(contract L23). `close_time` is a settlement deadline days later; using it would
put the deadline-collapse term in the wrong place entirely — a market whose
event is tonight would be modelled as having three days of remaining
uncertainty.

**`tradeIntensity_t` crosses a sid boundary.** Trades live on their own sid with
their own sequence domain (L22), so there is **no venue-guaranteed ordering**
between a trade frame and an order-book frame — only our own receive
timestamps, which carry collector latency. Every trade-derived term here
inherits the pre-declared lag from `MARKET-STATE-FABRIC-v1` §4. A regime label
that reacts to a trade *before* the book state it is labelling is look-ahead,
and it would be invisible in backtest.

## 3. The six regimes as first-class objects

A regime is a **typed label with an owner**, not a threshold buried in a
strategy. It is computed once, recorded on the tape's timeline, and consumed by
everything downstream — so two components can never disagree about what regime
it was.

| | regime | classifying observables |
|---|---|---|
| **R0** | quiet / liquid | σ below trailing median; spread at or near tick; depth at/above trailing median; low trade intensity |
| **R1** | directional flow | persistent same-sign OFI over ≥ k windows; σ elevated but depth **healthy**; spread normal |
| **R2** | information shock | σ acceleration ↑↑ **and** trade-intensity acceleration ↑↑ **and** rapid microprice repricing; depth may still be present |
| **R3** | liquidity vacuum | depth depletion at touch; spread widening; **replenishment failing** — depth not restored after consumption |
| **R4** | toxic flow | post-fill markout persistently adverse; fills concentrated on one side ahead of price moves |
| **R5** | deadline compression | `T_t` small; `∂p/∂information` large; variance path dominated by the structural term |

**R4 is not computable today and must be marked as such.** It requires a
markout model over *our own* fills, and we have never traded — there are no
fills. Encoding R4 as "not detected" would be exactly the doctrine-10 error of
turning absence into a benign state. Until a fill corpus exists, R4's state is
`NOT_COMPUTABLE:no_fill_history`, and any consumer must treat that as **unknown
toxicity, not absent toxicity** — which for a risk system means the conservative
branch, not the permissive one.

**R5 overlaps every other regime and is orthogonal.** It is emitted as a
separate axis rather than a mutually exclusive label: a market can be in R3 *and*
R5, and that combination is materially more dangerous than either alone.

## 4. The output changes the action, not the forecast

This is the whole point of the engine. Per doctrine 13, `NO_TRADE` is a
first-class action and

> Action = f(alpha, regime, liquidity, execution cost, uncertainty, portfolio state)

**Worked example A — the signal is real and the answer is still no.**

```
microstructure signal        +2.0%
regime                       R3 liquidity vacuum
depth at touch               12% of trailing median
expected execution slippage  -3.4%
uncertainty haircut          -0.3%
------------------------------------------
expected net                 -1.7%   ->  NO_TRADE
```

The signal is not disputed. The edge is real and unreachable, and a system that
must pick a side would have taken it.

**Worked example B — the same magnitude, a different regime, eligible.**

```
microstructure signal        +1.1%
regime                       R1 directional flow, depth healthy
half-spread + fees           -0.4%
uncertainty haircut          -0.2%
------------------------------------------
uncertainty-adjusted edge    +0.5%   ->  ELIGIBLE (sizing is the governor's call)
```

**The asymmetry that matters.** A signal computed from book state is
*mechanically strongest* in exactly the states where it is least executable —
thin books produce large imbalance readings and large microprice deviations.
Without a regime gate, a microstructure strategy will concentrate its trading in
R3 and R4 and be adversely selected there. That is not a hypothetical failure
mode; it is the default one.

**Eligibility is not an instruction.** This engine emits `ELIGIBLE` or
`NO_TRADE` and never a size. Sizing belongs to `RISK-GOVERNOR-001`, which holds
veto authority independently.

## 5. The uncertainty layer: forecast an interval, size against the adverse end

Per doctrine 14, the engine emits `[σ⁻, σ⁺]` and never a bare `σ̂`.

```
expected volatility   18%
90% interval          12% – 39%
```

**The risk system must reason about 39%, not 18%.** The interval is not
decoration around a point estimate; the *adverse end is the input*. A position
sized on 18% when the realised value is 39% is roughly half the size it should
have been, and the error arrives precisely during the transition that caused it.

**Why point estimates fail here specifically.** A conditional variance model is
fitted on history; regime transitions are, by construction, the moments when the
recent past stops describing the present. So the model is **most confidently
wrong exactly when the answer matters most** — its interval should widen at a
transition, and a point estimate has no way to say so.

**Calibration is the acceptance test, not fit.** The interval must be validated
on **coverage**: does the realised σ fall inside the nominal 90% band 90% of the
time, *conditional on regime*? An interval that is well-calibrated in R0 and
badly calibrated in R2 is worse than useless, because R2 is when it is consulted.
Report coverage **per regime**, never pooled — pooled coverage is dominated by
R0, which is most of the sample and none of the risk.

**Widening is a signal in itself.** A rapidly widening interval is evidence of
regime transition and should feed §6 rather than merely inflating a haircut.

## 6. EXTREME-STATE-DETECTOR-001 — a risk switch, not a strategy

**It predicts nothing about direction and must never be used to take one.**

Target:

> `P( |Δp_{t:t+h}| > q₀.₉₉  ∨  σ_{t+h} > q₀.₉₅  |  X_t )`

The disjunction is deliberate. Defining the label **jointly over future movement
and future volatility state** gives a denser, better-posed target than
predicting rare price extremes directly, where positives are so scarce that a
classifier learns the base rate and reports a plausible-looking 99% accuracy
while detecting nothing. Quantiles are computed **per market and per regime**,
not globally — a global `q₀.₉₉` is dominated by whichever markets are most
volatile and would label calm markets as permanently normal.

**The response ladder is mechanical, not discretionary.**

| P(extreme) | response |
|---|---|
| < 5% | normal operation |
| 5 – 25% | reduced size |
| 25 – 50% | maker restrictions, tighter risk, larger required edge |
| > 50% | **no new directional risk**; specialised high-volatility strategies only, if any exist |

**This has strategic value even at zero directional profit.** A system that
reduces exposure before a liquidity event survives volatility rather than
discovering it through losses. That is a real economic contribution and it does
not require predicting direction at all — a distinction most retail-grade
systems never make.

**Positive control (doctrine 7).** The detector must be run against a period
containing a **known** extreme event with the label withheld; if it does not
fire, the metric is broken. And it must be run against a synthetically calm
series where it must **not** fire. Without both arms, a detector that always
outputs 4% looks excellent on a calm sample.

## 7. Feature computability audit — what our tape can and cannot see

Doctrine 8 and doctrine 9: a feature list that quietly includes uncomputable
features is a design that fails at implementation time, so each candidate is
audited against what the archive actually contains.

| candidate feature | computable from our tape? |
|---|---|
| spread acceleration | **yes** — second difference of fabric `spread` |
| depth depletion | **yes** — `depth_*` trajectory at touch |
| OFI acceleration | **partial — see below** |
| trade intensity | **yes**, on the trade sid, subject to the cross-sid lag |
| **cancellation intensity** | **NO — not separably observable** |
| volatility acceleration | **yes** — derived |
| volatility of volatility | **yes** — derived, but noisy at 1 s sampling |
| liquidity replenishment | **yes** — depth recovery after consumption |
| microprice deviation | **yes** — fabric #11 |

**Cancellation intensity is the one that must be struck, and it matters.** The
venue sends order-book **deltas**: a net change in resting size at a price
level. A level falling from 500 to 300 is reported as −200, and that is
consistent with a cancellation of 200, a trade of 200, or a cancellation of 300
against a new order of 100. The delta stream **cannot distinguish cancellation
from execution**, and cannot see order-level identity at all.

Partial recovery is possible and its limits must be stated: the trade sid tells
us how much traded, so `depth_removed − traded ≈ cancelled` **in aggregate over
a window**. That inference is (a) only as good as the cross-sid timestamp
alignment, which L22 tells us carries no venue guarantee, and (b) aggregate
only — it yields no per-order cancellation and no queue dynamics. Any feature
built on it must be typed as **`DERIVED_LOSSY`**, and no result may be reported
as a cancellation measurement.

**This is why OFI is "partial."** Textbook OFI is defined over order additions
and cancellations at the touch. What we can compute is a **net-depth-change
imbalance**, which coincides with OFI only when cancellations are negligible —
an assumption we cannot check with this data. It is therefore named
`net_depth_flow_imbalance`, not `OFI`, everywhere in the implementation. Calling
it OFI would be a field name asserting semantics the data does not support,
which is the exact failure doctrine 8 exists to prevent.
