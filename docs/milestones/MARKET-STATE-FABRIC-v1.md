# MARKET-STATE-FABRIC-v1

**Status: DESIGN, NOT IMPLEMENTED.** Written 2026-08-19. The feature
representation consumed by `MARKET-MICROSTRUCTURE-EDGE-001`.

Derived **exclusively** from reconstructed order-book state. No provider calls,
no model output, no forecaster input, no capital.

---

## 1. What the fabric is, and the one rule that shapes it

The fabric is the state of one market at one instant, as a fixed-width typed
record, computed only from evidence classified `RAW_REPLAYABLE` or
`DERIVABLE_FROM_RAW`. If a quantity cannot be recomputed by replaying the tape,
it is not in the fabric.

**The rule that shapes every field (doctrine 10): epistemic absence is never
encoded as a numerical market state.** This is not a style preference — it is
the defect that has bitten this repository most often. A market with an empty
ask ladder does not have a "wide spread"; its spread is **undefined**, and a
model fed `spread = 1.0` there learns a fact about our encoder rather than about
the venue.

Every field is therefore a tagged value:

| tag | meaning |
|---|---|
| `PRESENT` | the venue told us, and the value is real |
| `EMPTY` | the venue told us, and the answer is genuinely "nothing there" |
| `NOT_PROVIDED` | we do not know — not yet based, awaiting snapshot, halted |

A row containing any `NOT_PROVIDED` in a feature the model consumes is
**dropped**, not imputed. Drop rate is a reported metric, not a hidden step.

## 2. Sampling

Fabric rows are emitted on a **fixed wall-clock grid** (default 1 s), not per
event. Event-triggered sampling makes the sample density itself a function of
activity, which correlates with volatility, which is the thing being predicted —
a subtle look-ahead that would flatter every result.

A row is emitted only when the book is `publishable` for that market at that
instant, using the typed `PublicationState`. "Awaiting snapshot for generation"
produces **no row**, rather than a stale one.

## 3. M0 — state-only features (12)

Computed from the book at time *t*, in probability units (cents/100). Nothing
below reads any event before *t* except through the current ladder.

| # | field | definition |
|---|---|---|
| 1 | `mid` | (best_bid + best_ask) / 2 |
| 2 | `spread` | best_ask − best_bid |
| 3 | `depth_bid_l1` | contract units resting at best bid |
| 4 | `depth_ask_l1` | contract units resting at best ask |
| 5 | `imbalance_l1` | (bid_l1 − ask_l1) / (bid_l1 + ask_l1) |
| 6 | `depth_bid_5c` | cumulative bid units within 5c of mid |
| 7 | `depth_ask_5c` | cumulative ask units within 5c of mid |
| 8 | `imbalance_5c` | (bid_5c − ask_5c) / (bid_5c + ask_5c) |
| 9 | `levels_bid` / `levels_ask` | distinct occupied price levels per side |
| 10 | `microprice` | (bid_px·ask_sz + ask_px·bid_sz) / (bid_sz + ask_sz) |
| 11 | `micro_minus_mid` | `microprice` − `mid` — the imbalance-implied drift |
| 12 | `dist_to_bound` | min(mid, 1 − mid) — these are **probabilities**, and behaviour near 0 and 1 is structurally different |

Two structural covariates travel with every row and are **not** predictive
features: `seconds_to_close` and `book_age_ms` (time since last modification).
They are controls, used for stratification.

**Why `dist_to_bound` is not optional.** A Kalshi contract is bounded in [0,1].
A 1-cent move at mid 0.50 and a 1-cent move at mid 0.02 are not the same event —
the second is a 50% relative move and cannot continue far in one direction. Any
model that ignores the bound will discover this and call it alpha.

## 4. M1 — flow features (added to M0, not replacing it)

Computed over a trailing window Δ ∈ {1 s, 5 s, 30 s}, from the event sequence
strictly before *t*.

| field | definition | source sid |
|---|---|---|
| `delta_count_Δ` | order-book deltas in the window | orderbook sid |
| `signed_depth_flow_Δ` | Σ (units added at bid − units added at ask) | orderbook sid |
| `quote_reversal_Δ` | count of best-price direction changes | orderbook sid |
| `realized_vol_Δ` | stdev of 1 s mid changes | derived |
| `trade_count_Δ` | trades printed in the window | **trade sid** |
| `signed_trade_flow_Δ` | Σ signed traded units (buyer- vs seller-initiated) | **trade sid** |

**The trade features cross a sid boundary and that is a real hazard.** §16.4 of
the measurement contract established that trades live on their own sid with their
own sequence domain. There is therefore **no venue-guaranteed ordering between a
trade frame and an order-book frame** — only our own receive timestamps relate
them, and those carry collector-side latency. Every trade-derived feature must be
lagged by a **pre-declared safety margin**, to be fixed by measurement before
first use; the measured max interarrival on the P4 tape was 580 ms, which is the
scale the margin has to respect. A trade that is actually simultaneous with, or
after, the book state being labelled is look-ahead, and it is the most likely way
this experiment produces a fake result.

`ticker` is not a source for any fabric field. It is unsequenced
(`seq = null` on all 2,395 production frames) and cannot be ordered against
anything.

## 5. What the fabric deliberately excludes

* **Any forecaster output.** The point is to test whether microstructure carries
  information; mixing in the model we already know tracks the market (R² 0.661 on
  logit) would confound the question.
* **Any cross-market feature.** Correlated-market structure is real and is
  deferred, because it multiplies the selection surface.
* **Any outcome or settlement data.** Labels come from future *prices*, not from
  event resolution.
* **Any field the venue did not send.** No inferred liquidity, no synthetic depth.

## 6. Cost floor travels with the fabric

Per doctrine 2, no effect size from this fabric may ever be reported without the
cost floor beside it. The floor is the **half-spread plus fees** at the row's own
state — which the fabric already carries (`spread`, `mid`), so there is no excuse
for reporting a naked effect size. A 0.4-cent predicted move on a 2-cent spread
is not an edge; it is a measurement of the spread.
