# QDK-001 — CLOB microstructure state engine and execution model (Kalshi-facing)

STATUS: RESEARCH ONLY. No production code, no trading, no live execution, no order
placement of any kind is designed, enabled or implied by this document. Everything here
is a specification on paper plus a literature verification ledger.

SCOPE: **Kalshi CLOB only.** Solana memecoins trade on AMMs and bonding curves. They have
no book, no queue and no maker/taker distinction in the sense used here, and are covered
by a separate track. Section 8 lists exactly which constructs below do NOT transfer.

VERIFICATION CONVENTION used throughout:

- **VERIFIED** — read from the primary source named, with the numbers quoted from it.
- **INFERRED** — a defensible conclusion of ours, or read from a secondary source. Not
  authoritative.
- **UNVERIFIED** — asserted somewhere but not confirmed against wire or paper. Treated as
  an open question, never as a design input.

---

## 0. How to read this document

Read sections 9 and 7 first if you read nothing else. Section 9 is the realism pass — it
is what stops this from being a schema that cannot be populated. Section 7 is the
decision rule that the rest of the document exists to justify.

Three facts dominate everything below and are worth stating before any formula appears.

**Fact 1 — Kalshi is an extreme large-tick venue.** The contract pays $0 or $1 and the
price grid is 1¢. One tick is therefore **100 bp of notional**. Every result in the
microstructure literature that is quoted in basis points has to be re-read with that
scale in mind: Albers et al.'s −0.8 bp of maker adverse selection on Binance BTC perp is
0.008 of a Kalshi tick. Effects that are decisive in equities or crypto are rounding
error here, and effects that are rounding error there are decisive here.

**Fact 2 — the fee is larger than the tick.** Kalshi's taker fee is
`ceil_to_cent(0.07 · C · P · (1−P))`, which peaks at **1.75¢ per contract at P = 0.50** —
that is 1.75 ticks, or 175 bp. No microstructure signal in this document produces an edge
of that size. The fee, not the spread and not adverse selection, is the first-order term
in the EV identity. (Fee coefficients: INFERRED from secondary sources, section 10 —
**must be re-read from Kalshi's own published schedule before any use.**)

**Fact 3 — the venue is two regimes wearing one name.** A liquid Kalshi contract with a
1¢ spread and hundreds of contracts resting at the touch is the *large-tick* regime where
queue imbalance is at its strongest (Gould & Bonart: out-of-sample AUC 0.76–0.81). A
dormant contract with a 7¢ spread and 3 contracts a side is the regime where every
estimator here degrades, where Stoikov's micro-price is data-starved, and where the
spread alone exceeds any plausible edge. **The same feature schema must never be scored
the same way in both.** Every model in this document is specified as conditional on a
regime label, and the regime label is itself a first-class feature.

---

## 1. Scope boundary — what a CLOB gives you that an AMM does not

This track is Kalshi-facing. The reason the boundary matters is not tidiness; it is that
the single most valuable result in this literature **does not exist on an AMM at all**.

Cont, Kukanov and Stoikov's finding (section 3.1, VERIFIED) is that short-horizon price
changes are explained far better by **order-book events** — placements and cancellations
at the quotes, most of which never trade — than by **trades**. Their reported figures are
R² = 65% for order-flow imbalance versus R² = 32% for trade imbalance, and when both are
in the regression the trade term loses significance in 69% of subsamples.

An AMM has no order-book events. There are no resting orders, so there are no placements
and no cancellations; there is only the swap flow. **An AMM therefore gives you only the
variable that Cont et al. showed is the weaker one**, and there is no construction that
recovers the stronger one, because the information simply is not emitted. This is a
structural asymmetry between the two tracks, not a data-collection gap, and it is worth
being explicit about before anyone tries to port this schema sideways. Section 8 lists
the full non-transfer set.

The converse is also true and should temper any enthusiasm: on an AMM the execution cost
function is a **closed-form, exactly known** function of pool reserves and trade size,
whereas on a CLOB it must be reconstructed from a book that may be stale, may be
unpublishable, and may not contain enough depth to fill at all. AMMs are harder to
predict and easier to price; CLOBs are easier to predict and harder to price.

---

## 2. The CLOB state vector — typed feature schema

### 2.1 Design rules

1. **Every feature is a function of archived evidence and a clock, and nothing else.** No
   feature may read a live socket, a REST endpoint, or a database at evaluation time. The
   schema must be computable by replaying the archive. This follows the repo's existing
   replay-determinism contract (`app/realtime/archive.py`, `kalshi-realtime-replay`).
2. **Fixed-point throughout, never float.** Prices are integer price units
   (`app/realtime/fixedpoint.py`: 1 dollar = 10,000 units), sizes are integer contract
   units (1 contract = 100 units). Any feature whose natural definition is a ratio is
   stored as a rational or as a scaled integer with its scale named. Reason: the archive
   is digest-chained and canonical, and a float feature cannot be a fixpoint.
3. **Cents, not basis points, and never log returns.** A binary contract's price is a
   probability. `log(0.03/0.02)` is a real number and a meaningless one. Price changes are
   expressed in integer price units; where a variance-normalised quantity is wanted, use
   `Δp / sqrt(p(1−p))`, because `p(1−p)` is the per-contract variance of the terminal
   payoff, not `Δp / p`.
4. **No feature is allowed to have a silent default.** A feature that cannot be computed
   is absent, with a typed reason (2.2). Zero is a value; it is not "unknown".
5. **Every feature carries its own window and its own minimum sample count.** A feature
   computed from fewer observations than its floor is absent, not noisy. This mirrors the
   existing `MIN_SAMPLES_FOR = {"p50": 3, "p95": 20, "p99": 100}` gate in
   `app/realtime/archive.py:947`, which is the right instinct and should be generalised.
6. **The book's own publishability is upstream of everything.** `SubscriptionRouter`
   unpublishes every book on a subscription when a sequence fault occurs
   (`app/realtime/book.py:770-772`). When a book is unpublishable, **every** book-derived
   feature is absent with reason `BOOK_UNPUBLISHED`. There is no partial credit and no
   "last known good" fallback: a stale book presented as current is the exact failure the
   archive milestone was built to prevent.

### 2.2 Typed absence

    class Absence(Enum):
        NOT_SUBSCRIBED        # the channel this feature needs was not in the subscription
        VENUE_DOES_NOT_EXPOSE # structurally unobtainable from Kalshi at any subscription
        BOOK_UNPUBLISHED      # sequence fault / pre-snapshot / integrity halt
        EMPTY_SIDE            # the ladder side needed has no levels
        WINDOW_UNDERFILLED    # fewer observations in the window than the feature's floor
        INSUFFICIENT_DEPTH    # ladder does not reach the level the feature requires
        STALE                 # newest evidence older than the feature's staleness bound
        MARKET_NOT_OPEN       # lifecycle state is not `open`
        REQUIRES_JOIN_UNAVAILABLE  # needs a second channel/source not present in this run

Every feature is typed `Result[T, Absence]`, never `Optional[T]`, and never `T` with a
sentinel. A consumer that wants a number must handle the absence branch. `VENUE_DOES_NOT_EXPOSE`
is a compile-time-ish absence: it is known before any data arrives, and a feature that can
only ever return it should not be in the schema (section 9.3).

### 2.3 The schema

Columns: **Feature** · **Definition** · **Inputs** (channels) · **Update** · **Cost** ·
**Absence modes**. Cost is per update, on the assumption of an incrementally maintained
book: O(1) means constant work per book event, O(L) means one pass over the ladder
(L ≤ 99 on Kalshi's 1¢ grid, so O(L) is cheap in absolute terms), O(W) means one pass
over a rolling window buffer.

#### A. Level-1 book state

| Feature | Definition | Inputs | Update | Cost | Absence |
|---|---|---|---|---|---|
| `best_bid`, `best_ask` | `max(yes ladder)`, `min(no ladder)` — both already YES-scaled under `use_yes_price=true` (`book.py:464-480`) | `orderbook_delta` | per book event | O(1) w/ heap, O(L) naive | `EMPTY_SIDE`, `BOOK_UNPUBLISHED` |
| `bid_size`, `ask_size` | contract units resting at those two prices | `orderbook_delta` | per book event | O(1) | as above |
| `spread` | `best_ask − best_bid`, in price units | derived | per book event | O(1) | `EMPTY_SIDE` |
| `spread_ticks` | `spread / 100` price units (1¢ tick) | derived | per book event | O(1) | `EMPTY_SIDE` |
| `mid` | `(best_bid + best_ask)/2` | derived | per book event | O(1) | `EMPTY_SIDE` |
| `queue_imbalance` | `I = (Q_bid − Q_ask)/(Q_bid + Q_ask) ∈ [−1,1]` (Gould–Bonart normalisation, 3.3) | derived | per book event | O(1) | `EMPTY_SIDE` |
| `weighted_mid` | `(P_bid·Q_ask + P_ask·Q_bid)/(Q_bid+Q_ask)` | derived | per book event | O(1) | `EMPTY_SIDE` |
| `microprice` | `M + g(I, S)`, section 4 | derived + fitted table | per book event | O(1) lookup | `EMPTY_SIDE`, `WINDOW_UNDERFILLED` (unfitted `(I,S)` cell) |

#### B. Multi-level depth and its shape

| Feature | Definition | Inputs | Update | Cost | Absence |
|---|---|---|---|---|---|
| `depth[m]` for m=1..M | contract units at the m-th populated level from the touch, each side | `orderbook_delta` | per book event | O(1) incremental | `INSUFFICIENT_DEPTH` |
| `cum_depth_within(k)` | contracts available within `k` ticks of the touch, each side | derived | per book event | O(k) | `INSUFFICIENT_DEPTH` |
| `depth_slope` | OLS slope of cumulative size on distance-from-touch, per side, over the populated levels within `k` ticks | derived | per book event or throttled | O(k) | `INSUFFICIENT_DEPTH` (< 3 levels) |
| `depth_convexity` | quadratic term of the same fit; positive = liquidity concentrated away from the touch | derived | throttled | O(k) | `INSUFFICIENT_DEPTH` (< 4 levels) |
| `fill_cost_curve(s)` | the ladder walk: `VWAP` to fill size `s`, tabulated at a fixed size grid. **This is `C_execution`, section 6.2** | derived | per book event | O(k) | `INSUFFICIENT_DEPTH` → typed *unfillable*, never an extrapolated price |
| `book_pressure_k` | `(cum_bid_within(k) − cum_ask_within(k))/(sum)` — imbalance beyond the touch | derived | per book event | O(k) | `INSUFFICIENT_DEPTH` |

#### C. Order flow

| Feature | Definition | Inputs | Update | Cost | Absence |
|---|---|---|---|---|---|
| `OFI(Δt)` | Cont–Kukanov–Stoikov, section 3.1, summed over the interval | `orderbook_delta` | per interval | O(1)/event | `BOOK_UNPUBLISHED`, `WINDOW_UNDERFILLED` |
| `MLOFI[m](Δt)`, m=1..M | Xu–Gould–Howison, section 3.2 | `orderbook_delta` | per interval | O(M)/event | `INSUFFICIENT_DEPTH` per level |
| `integrated_OFI(Δt)` | `w₁ᵀ·MLOFI / ‖w₁‖₁`, PC1 of the MLOFI vector (Cont–Cucuringu–Zhang) | derived + fitted `w₁` | per interval | O(M) | `WINDOW_UNDERFILLED` (unfitted `w₁`) |
| `signed_trade_imbalance(Δt)` | `Σ count·(+1 if taker_side="yes" else −1)`. **Exact, not classified** — see 9.2 | `trade` | per trade | O(1) | `NOT_SUBSCRIBED` |
| `trade_count(Δt)`, `trade_volume(Δt)` | counts and contract units | `trade` | per trade | O(1) | `NOT_SUBSCRIBED` |
| `order_arrival_intensity(Δt)` | count of positive-delta book events per unit time, by side and by distance-from-touch bucket | `orderbook_delta` | per book event | O(1) | `BOOK_UNPUBLISHED` |
| `cancellation_intensity(Δt)` | count/volume of negative-delta book events **that could not be matched to a trade print** — a JOIN, and an approximate one (9.2) | `orderbook_delta` + `trade` | per book event | O(1) amortised w/ a small time-window match buffer | `REQUIRES_JOIN_UNAVAILABLE` when `trade` is not subscribed |
| `sweep_flag`, `sweep_depth_ticks` | a burst of same-`taker_side` prints within `τ_sweep` spanning ≥ 2 price levels, or a single print with `count` > pre-trade touch size | `trade` (+ book for the size test) | per trade | O(1) | `NOT_SUBSCRIBED` |
| `replenishment_time` | time from a touch-consuming event until `bid_size`/`ask_size` at the touch recovers to a fraction `ρ` of its pre-event value | derived | on event | O(1) | `WINDOW_UNDERFILLED` |
| `resilience_ratio` | fraction of touch liquidity restored within a fixed horizon `H` after a consuming event | derived | on event | O(1) | `WINDOW_UNDERFILLED` |

#### D. Impact, cost and toxicity

| Feature | Definition | Inputs | Update | Cost | Absence |
|---|---|---|---|---|---|
| `effective_spread` | `2·D·(P_trade − M_before)` in price units, `D = +1` for a YES-taker print, `−1` for a NO-taker print. **Absolute, not relative** | `trade` + book | per trade | O(1) | `BOOK_UNPUBLISHED` at the print |
| `realized_spread(h)` | `2·D·(P_trade − M_{t+h})` | `trade` + book | at `t+h` | O(1) | `STALE` if no book event by `t+h` |
| `price_impact(h)` | `effective_spread − realized_spread(h) = 2·D·(M_{t+h} − M_before)` | derived | at `t+h` | O(1) | as above |
| `markout(h)` | `D·(M_{t+h} − P_trade)`, the signed post-trade drift of the *taker*; the maker's markout is its negation | derived | at `t+h` | O(1) | as above |
| `beta_impact` | fitted `ΔP = β·OFI`; and the depth relation `β ≈ c/AD^λ` with `λ ≈ 1` (3.1). Store `β` and `AD` separately | fitted | per estimation window | O(W) | `WINDOW_UNDERFILLED` |
| `toxicity_vpin` | VPIN over volume buckets, using **exact** `taker_side` rather than a Lee–Ready classifier | `trade` | per completed bucket | O(1) | `WINDOW_UNDERFILLED` — expect this to be the common case (9.2) |
| `adverse_selection_proxy` | rolling mean of `markout(h)` conditional on taker direction — the empirical cost a resting order pays | derived | per window | O(W) | `WINDOW_UNDERFILLED` |

#### E. Volatility and activity dynamics

| Feature | Definition | Inputs | Update | Cost | Absence |
|---|---|---|---|---|---|
| `realized_vol(Δt)` | `sqrt(Σ (ΔM)²)` over the window, in price units. Compute in **event time** as well as clock time; report both | derived | per interval | O(W) | `WINDOW_UNDERFILLED` (floor: ≥ 20 mid changes) |
| `normalized_vol` | `realized_vol / sqrt(p(1−p))` — removes the mechanical variance collapse near 0 and 1 | derived | per interval | O(1) | as above |
| `vol_of_vol` | dispersion of `realized_vol` across sub-windows | derived | per super-interval | O(W) | `WINDOW_UNDERFILLED` (floor: ≥ 10 sub-windows each meeting their own floor) |
| `volume_acceleration` | `(V(Δt) − V(prev Δt))/Δt`, and the same for book-event counts | derived | per interval | O(1) | `WINDOW_UNDERFILLED` |
| `event_rate_ewma[τ]` for τ ∈ {1s, 10s, 60s} | exponentially weighted book-event and trade counts at three timescales. **This is the recommended substitute for a Hawkes intensity, section 5** | derived | per event | O(1) | none — defined from the first event, but carries its own `n_events_seen` |

#### F. Regime, clocks and state-of-the-world

| Feature | Definition | Inputs | Update | Cost | Absence |
|---|---|---|---|---|---|
| `spread_regime` | ordinal: `TOUCHING` (spread = 1 tick) / `NARROW` (2–3) / `WIDE` (4–9) / `VERY_WIDE` (≥ 10) / `ONE_SIDED` / `NO_BOOK`. **The primary conditioning variable in this document** | derived | per book event | O(1) | never absent — `NO_BOOK` is a value |
| `depth_regime` | ordinal on touch size: `THIN` / `NORMAL` / `DEEP`, cut at contract-specific quantiles | derived | per book event | O(1) | `WINDOW_UNDERFILLED` until quantiles are estimated |
| `activity_regime` | ordinal on `event_rate_ewma[60s]` relative to that contract's own history: `DORMANT` / `NORMAL` / `BURST` | derived | per event | O(1) | `WINDOW_UNDERFILLED` |
| `price_regime` | ordinal on mid: `TAIL_LOW` (< 10¢) / `MID` / `TAIL_HIGH` (> 90¢). Drives both the fee curve and the variance | derived | per book event | O(1) | `EMPTY_SIDE` |
| `time_to_close` | seconds until the contract's scheduled close | **REST market metadata**, not WS | on load | O(1) | `REQUIRES_JOIN_UNAVAILABLE` — see 9.2, this is a real gap |
| `lifecycle_state` | open / closed / settled / determined | `market_lifecycle_v2` | on event | O(1) | `NOT_SUBSCRIBED` |
| `time_of_day`, `day_of_week` | receive-clock features | envelope | per event | O(1) | never absent |
| `event_clock_phase` | position within the *underlying* event (game clock, release schedule). Contract-family specific | external | — | — | `VENUE_DOES_NOT_EXPOSE` for most families |

#### G. Latency and observation quality

| Feature | Definition | Inputs | Update | Cost | Absence |
|---|---|---|---|---|---|
| `data_age_us` | `receive_time − venue ts`, integer microseconds, already on the envelope (`book.py:76-80`) | envelope | per event | O(1) | never absent |
| `book_staleness` | wall time since the last accepted book event on this market | derived | on read | O(1) | never absent |
| `is_publishable` | the `SubscriptionRouter` verdict for this book | `book.py` | per event | O(1) | never absent |
| `subscription_generation` | reconnect generation carried on the record (`archive.py:633`) | envelope | per event | O(1) | never absent |
| `observation_gap` | `[disconnected_at, reconnected_at]` intervals — the periods the tape is blind. **A feature evaluated inside a gap is absent, not interpolated** | collector metrics | per reconnect | O(1) | never absent |
| `clock_offset_bound` | our host-vs-venue clock offset, explicitly **NOT MEASURED** today (`cli.py:717-722`) | — | — | — | `VENUE_DOES_NOT_EXPOSE` until measured |

A closing note on cost. The whole schema above is O(1)–O(99) per book event on an
incrementally maintained ladder. At the only measured Kalshi rate on record — 4 records in
~2 minutes on DEMO (`segment.py:200-212`) — compute cost is not a design constraint by
several orders of magnitude. **Sample size is the binding constraint, not CPU.** Any
design tradeoff that spends samples to save compute is backwards here.

## 3. Order-flow imbalance

### 3.1 OFI (Cont–Kukanov–Stoikov) — verification and estimator
### 3.2 MLOFI — what deeper levels add
### 3.3 Queue imbalance as a one-tick-ahead predictor
### 3.4 What this means for Kalshi specifically

## 4. Microprice

### 4.1 The construction
### 4.2 Failure modes in thin and wide books
### 4.3 Recommendation for Kalshi

## 5. Hawkes processes for order flow — assessment

### 5.1 The model
### 5.2 Stability and estimation cost
### 5.3 Verdict

## 6. Execution modelling

### 6.1 The EV identity
### 6.2 C_execution from the actual book
### 6.3 P(fill | ...) — the fill-probability model
### 6.4 E[return post-fill | filled] — the markout model
### 6.5 The negative fill-probability/return relationship

## 7. The maker/taker decision rule

## 8. Does NOT transfer to AMMs

## 9. Realism pass — what our collector can actually feed

### 9.1 What Kalshi exposes
### 9.2 Feature-by-feature mapping
### 9.3 Features we must NOT put in the schema

## 10. Citation ledger

## 11. Open questions and next steps
