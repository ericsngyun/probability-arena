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

### 3.1 OFI (Cont–Kukanov–Stoikov) — VERIFIED

**Citation checks out.** Rama Cont, Arseniy Kukanov, Sasha Stoikov, "The Price Impact of
Order Book Events", arXiv:1011.6402 (v3, 2011-04-13), published *Journal of Financial
Econometrics* 12(1):47–88, Winter 2014, DOI 10.1093/jjfinec/nbt003.
<https://arxiv.org/abs/1011.6402>

**Sample (VERIFIED):** 50 stocks drawn at random from the S&P 500; TAQ consolidated
quotes and trades via WRDS, aggregated to NBBO across exchanges (**not** a single
exchange); one calendar month, **April 2010**; level 1 only. Grand-mean stock: $51.75,
7.5M shares/day, 223,232 quote updates vs 4,552 trades — a **~40:1 quote-to-trade ratio**,
which is the whole reason book events carry more information than trades.

**The estimator, verbatim (VERIFIED).** For consecutive best-quote observations at times
`τ_{n−1}, τ_n`, with bid `(P^B, q^B)` and ask `(P^A, q^A)`:

    e_n = 1{P^B_n ≥ P^B_{n−1}}·q^B_n − 1{P^B_n ≤ P^B_{n−1}}·q^B_{n−1}
        − 1{P^A_n ≤ P^A_{n−1}}·q^A_n + 1{P^A_n ≥ P^A_{n−1}}·q^A_{n−1}

    OFI_k = Σ_{n : t_{k−1} < τ_n ≤ t_k}  e_n

All four inequalities are **weak** (≥, ≤, ≤, ≥), and that is deliberate, not sloppy: when
the bid price is unchanged both bid indicators fire and the term collapses to
`q^B_n − q^B_{n−1}`, the net size change. When the bid *rises*, the whole new queue counts
as `+q^B_n`; when it *falls*, the whole vanished queue counts as `−q^B_{n−1}`. Getting the
weak/strict distinction wrong silently changes the estimator at exactly the events that
matter most. The paper's own prose: "if q^B increases but P^B remains the same, we assign
e_n = q^B_n − q^B_{n−1} … If P^B increases, we let e_n = q^B_n. If P^B decreases, we let
e_n = q^B_{n−1}. The same classification is done for events on the ask side, with signs
reversed."

**The regression and the headline numbers (VERIFIED).** `ΔP_k = α + β·OFI_k + ε`, where
`ΔP` is the **mid-quote change in ticks** over a **uniform 10-second grid**, estimated per
stock per half-hour (273 subsamples per stock, ~180 observations each), White standard
errors.

| Regressor | Reported average R² |
|---|---|
| **OFI alone** | **65%** |
| Trade imbalance (TI = buy minus sell market-order volume) alone | **32%** |
| Both together | 67% |

In the joint regression the OFI t-statistic is **8.49** and the TI t-statistic is **1.16**;
OFI is significant in **95%** of subsamples and TI in only **31%**. On magnitudes rather
than signs (their Table 5): `|OFI|` explains 58% of `|ΔP|` versus 23% for traded volume,
and only `|OFI|` survives the joint regression. The paper's own conclusion, quoted:
"the dependence between the magnitude of price moves and the traded volume is mostly due
to correlation between VOL_k and |OFI_k|."

**Verdict on the mandate's claim: CONFIRMED.** Short-horizon price changes do relate more
tightly to OFI than to raw trade volume, by roughly a factor of two in R², and the trade
term is largely subsumed.

Two honesty notes. First, adding a nonlinear `OFI·|OFI|` term moves R² only 65% → 68% and
is insignificant in most subsamples — **the relationship is close to linear**, so do not
reach for a nonlinear model here without evidence. Second, the authors ran the obvious
tautology check (OFI includes price-changing events, which mechanically move the mid) by
stripping those events out; R² falls to the **35–60%** range. Still large, but the
unqualified 65% is partly mechanical and should be quoted with that caveat.

**The depth relation (VERIFIED, and it is the cleanest result in the paper).**

    β_i = c / AD_i^λ + ν_i,     AD_i = average of (q^B + q^A)/2 over the interval

Estimated by log-log regression `log β̂ = α − λ·log AD + ε` with Newey–West errors. Grand
mean across 50 stocks: **λ̂ = 0.98**, t = 29.53, mean 5% CI **[0.88, 1.08]**; **ĉ = 0.45**,
t = 20.74, CI [0.38, 0.52]; R² = 74%. The hypothesis λ = 1 **cannot be rejected for 35 of
the 50 stocks**. The stylised model they derive predicts exactly λ = 1 and c = 1/2, so
ĉ = 0.45 is close to but distinguishable from theory.

**Price impact is inversely proportional to depth: CONFIRMED, with an estimated exponent
of 0.98.** The practical form is `ΔP ≈ OFI/(2·D)`. Note which three stocks fit worst — APOL,
AZO, CME — all **wide-spread, low-depth** names. That is the Kalshi-relevant warning: the
depth relation is at its least reliable in exactly the regime a dormant event contract
lives in.

### 3.2 MLOFI — what deeper levels add — VERIFIED

**Citation checks out.** Ke Xu, Martin D. Gould, Sam D. Howison, "Multi-Level Order-Flow
Imbalance in a Limit Order Book", arXiv:1907.06230 (v2, 2019-10-26), published *Market
Microstructure and Liquidity* 4(3&4), 2020. <https://arxiv.org/abs/1907.06230>
Companion: Rama Cont, Mihai Cucuringu, Chao Zhang, "Cross-impact of order flow imbalance
in equity markets", arXiv:2112.13213, *Quantitative Finance* 23(10):1373–1393 (2023).
<https://arxiv.org/abs/2112.13213>

**Definition (VERIFIED).** With level-`m` bid price/size `(b^m, r^m)` and ask price/size
`(a^m, q^m)` measured immediately after each order arrival or cancellation:

    ΔW^m(τ_n) =  r^m(τ_n)                 if b^m(τ_n) > b^m(τ_{n−1})
                 r^m(τ_n) − r^m(τ_{n−1})  if b^m(τ_n) = b^m(τ_{n−1})
                 −r^m(τ_{n−1})            if b^m(τ_n) < b^m(τ_{n−1})

    ΔV^m(τ_n) =  −q^m(τ_{n−1})            if a^m(τ_n) > a^m(τ_{n−1})
                  q^m(τ_n) − q^m(τ_{n−1}) if a^m(τ_n) = a^m(τ_{n−1})
                  q^m(τ_n)                if a^m(τ_n) < a^m(τ_{n−1})

    e^m(τ_n) = ΔW^m(τ_n) − ΔV^m(τ_n)
    MLOFI^m(t_{k−1}, t_k) = Σ_{n : t_{k−1} < τ_n ≤ t_k}  e^m(τ_n)

`M = 1` recovers CKS's scalar OFI exactly. **Two implementation traps the paper is
explicit about, and both bite on Kalshi:**

1. **Only populated levels count.** Level `m+1` is the next price at which anything
   rests, not the next tick. On Kalshi's sparse 1¢ grid, "level 3" may be 8¢ away from the
   touch.
2. **A change at level 1 cascades.** If `b¹` changes, then `b², b³, …` all change, so a
   single event writes into many MLOFI components. Their worked example: a 7-lot buy limit
   arriving inside the spread yields `MLOFI = (7, 10, 10)`, **not** `(7, 0, 0)`. An
   implementation that treats the levels as independent is wrong.

**What deeper levels add (VERIFIED).** LOBSTER Nasdaq, full-year 2016, six stocks,
`M = 10`, out-of-sample RMSE in ticks against the `M = 1` baseline:

| | AMZN | TSLA | NFLX | ORCL | CSCO | MU |
|---|---|---|---|---|---|---|
| OFI (M=1) | 9.72 | 5.35 | 2.03 | 0.25 | 0.19 | 0.22 |
| MLOFI Ridge (M=10) | 8.05 | 4.53 | 1.41 | 0.08 | 0.05 | 0.08 |
| **Improvement** | 17% | 15% | 31% | **68%** | **74%** | **64%** |

The authors' summary: "about **65–75% for large-tick stocks** and about **15–30% for
small-tick stocks**." In-sample adjusted R² at M=10 reaches ~1.0 for the large-tick names.
Cont–Cucuringu–Zhang, on 100 S&P 500 names over 2017–2019, report the same direction with
their PC1-integrated construction: **out-of-sample R² 64.64% → 83.83%**, a **+19.2 pp**
gain, with PC1 explaining 89.06% of the multi-level OFI variance. They also find that once
integrated OFI is in the model, contemporaneous **cross-asset** impact becomes largely
redundant — worth knowing before anyone proposes a cross-contract feature.

**Do the deeper coefficients decay? For large-tick names, NO — they slightly increase.**
Ridge estimates for ORCL run β¹…β¹⁰ = 0.05, 0.06, 0.05, 0.05, 0.07, 0.09, 0.09, 0.08, 0.07,
0.09. For AMZN, β¹⁰ is still ~47% of β¹. The common prior that MLOFI is a fast-decaying
correction to level 1 is **refuted by this paper**, and refuted most strongly in the
large-tick regime that Kalshi resembles.

**A methodological warning that must not be skipped: do not fit MLOFI with OLS.** MLOFI
components are correlated > 0.5 even between levels 1 and 10 for small-tick names and
**> 0.7 for all pairs** in large-tick names; eigenvalue ratios `λ_i/λ_1 ≈ 0` for all
`i ≥ 2`. Under OLS only 11–21% of coefficients are significant and out-of-sample RMSE
*increases* past M ≈ 5. Cont et al. (2014) concluded deeper levels do not matter; Xu et al.
concluded they do; **the difference is OLS + in-sample R² versus Ridge + out-of-sample
RMSE.** Use Ridge with cross-validated `λ`, or the PC1 integration. This is the single
most load-bearing implementation note in section 3.

### 3.3 Queue imbalance as a one-tick-ahead predictor — VERIFIED

**Citation checks out, and the authors are not who the arXiv ID is often assumed to be.**
Martin D. Gould, Julius Bonart, "Queue Imbalance as a One-Tick-Ahead Price Predictor in a
Limit Order Book", arXiv:1512.03492 (2015-12-11), published *Market Microstructure and
Liquidity* 2(2):1650006 (2016), DOI 10.1142/S2382626616500064.
<https://arxiv.org/abs/1512.03492>

**Correction to a common misattribution:** the sample is **Nasdaq (LOBSTER), 10 US stocks,
all of 2014**, not LSE. Anyone carrying the LSE recollection should drop it.

**Normalisation (VERIFIED).** The paper uses the **signed** form

    I(t) = (n^b(b_t,t) − n^a(a_t,t)) / (n^b(b_t,t) + n^a(a_t,t))  ∈ [−1, 1]

and explicitly contrasts it in a footnote with the `Q_bid/(Q_bid+Q_ask) ∈ [0,1]` form used
by Yang & Zhu, noting the two are a linear rescaling of each other. **Both forms are in
the literature and they are not interchangeable in a fitted coefficient.** The mandate
asked about the `[0,1]` form; our schema (2.3) uses the `[−1,1]` form because it is what
Gould–Bonart's published coefficients are on. Stoikov's micro-price (section 4) uses the
`[0,1]` form. Mixing them silently is a real and easy bug — the schema names which is
which.

**Method (VERIFIED).** Target `y_i = 1` if the mid rises at the next **mid-price change
event** (not the next clock tick). The predictor is sampled at a time drawn **uniformly at
random from the interval between mid changes**, not immediately before the move — a
deliberately conservative design. Fit is logistic, `ŷ(I) = 1/(1+e^{−(x₀+I·x₁)})`, plus a
semi-parametric local logistic. Null model is `ŷ = 1/2`, whose mean squared residual is
exactly 0.25. 25,200 points per stock, 80/20 train/test.

**EFFECT SIZES, which is what was asked for (VERIFIED). No pseudo-R² is reported anywhere
in the paper** — the metrics are AUC and mean squared residual.

| | slope x₁ | out-of-sample AUC | out-of-sample MSR (null = 0.25) |
|---|---|---|---|
| CSCO (large tick) | 2.73 | 0.805 | 0.180 |
| INTC | 2.56 | 0.798 | 0.183 |
| MSFT | 2.49 | 0.762 | 0.198 |
| ORCL | 2.25 | 0.770 | 0.195 |
| MU | 2.03 | 0.752 | 0.202 |
| AMZN (small tick) | 0.85 | 0.642 | 0.235 |
| NFLX | 0.65 | 0.627 | 0.239 |
| TSLA | 0.60 | 0.602 | 0.243 |
| GOOG | 0.54 | 0.581 | 0.246 |
| PCLN | 0.50 | 0.583 | 0.245 |

The authors' own framing: binary classification improves **50–60% for large-tick and
10–30% for small-tick** stocks (as AUC−0.5 over 0.5); probabilistic prediction improves
**20–30% for large-tick and 2–6% for small-tick** (as MSR reduction against 0.25). From
the local logistic fits: `P(up)` is **0.8–0.9 at I ≈ 1 for large-tick** stocks and only
**~0.6 at I ≈ 1 for small-tick** stocks. At `I = 0`, `P(up) ≈ 0.50–0.515`.

**Verdict: CONFIRMED, but the effect size is entirely a function of tick regime.** A
2-to-1 odds shift at extreme imbalance in a large-tick book is a genuinely large effect. A
0.6 probability in a small-tick book is close to unusable after costs.

**Caveats the authors state, all of which apply to Kalshi (VERIFIED):**

1. **The mechanism is the spread being pinned at one tick.** When `s(t) = π`, no order can
   arrive *inside* the spread, which "eliminates one of the two possible reasons for
   changes in the mid price". When `s(t) > π`, traders buy priority one tick inside, queues
   stay short, and `I` loses its power. This is the entire large/small-tick split.
2. **`I` is less informative when both queues are small**, because a single market order
   can move the mid regardless of the ratio.
3. **The headline numbers are unconditional averages** that include the many near-balanced
   observations. The authors note performance "would improve considerably" if restricted to
   `|I| ≈ 1`, and that a practitioner "may therefore simply abstain from trading when
   I ≈ 0". That is a design instruction, not a footnote.
4. **The small-tick local-logistic fits are non-monotonic** for 4 of 5 small-tick names,
   and the authors call this "rather puzzling" and leave it unexplained. Do not build on
   the small-tick curve shape.
5. Single-venue data (Nasdaq only) and a single year in which all the significantly
   positive intercepts belong to stocks that rose a lot — a possible drift artifact.

### 3.4 What this means for Kalshi specifically

Kalshi has **one tick size (1¢) and a wildly varying spread**, so a single contract moves
between the two regimes over its own life. Combining 3.1–3.3:

- **When `spread_regime = TOUCHING`** (spread = 1 tick): this is the large-tick regime.
  Queue imbalance should be the strongest single feature; expect the Gould–Bonart shape,
  and MLOFI's largest gains (their 65–75%) should be available. Fit and score here.
- **When `spread_regime ∈ {WIDE, VERY_WIDE}`**: this is worse than the small-tick equity
  regime, because the spread is not merely several ticks — it is several *percent of
  terminal value*. Both Gould–Bonart's mechanism-2 caveat (both queues small) and Xu et
  al.'s ambiguity argument (the same book state maps to very different next-mid outcomes)
  apply simultaneously. **Recommendation: do not attempt short-horizon prediction in this
  regime at all.** The spread alone exceeds the edge.
- **The depth relation `β ≈ c/D` is a sizing tool, not just a descriptive fact.** It is
  the reason "the same signal at different notionals can have opposite sign" (section 6):
  the impact of your own order scales as `1/D`, and Kalshi's `D` at the touch is routinely
  two orders of magnitude smaller than a liquid equity's.
- **Fit per contract family, not per contract, and not pooled globally.** Contract-level
  samples will be far too small; global pooling mixes regimes that the literature says
  differ by a factor of four in effect size.

---

## 4. Microprice

### 4.1 The construction — VERIFIED (with a sourcing caveat)

**Citation.** Sasha Stoikov, "The micro-price: a high-frequency estimator of future
prices", *Quantitative Finance* 18(12):1959–1966 (2018), DOI 10.1080/14697688.2018.1489139.
Also SSRN 2970694. **It is not on arXiv.** Author's reference code and sample data:
<https://github.com/sstoikov/microprice>

*Sourcing caveat, stated plainly:* the journal text and the SSRN copy are both paywalled
and were not read. The construction below is VERIFIED against **Stoikov's own conference
presentation of the same work** (theorems, algorithm and the BAC/CVX March-2011 dataset
all match): <https://www.ma.imperial.ac.uk/~ajacquie/Gatheral60/Slides/Gatheral60%20-%20Stoikov.pdf>
Treat it as primary-adjacent. Anything below marked with † should be confirmed against the
journal text before implementation.

**State.** The order book is assumed Markov in `(M_t, I_t, S_t)` where `M` is the mid,
`I = Q^b/(Q^b + Q^a)` is the **[0,1]** top-of-book imbalance, and `S` is the spread in
ticks. († The slide writes `S = ½(P^a − P^b)`, a *half*-spread, while the discrete model
indexes `S` as an integer tick count `1 ≤ i_S ≤ m`. This looks like a slide typo; the
operative object in the released code is spread in ticks.)

**Definition.** Let `τ₁, τ₂, …` be the random times at which **the mid price changes**.

    P^micro_t = lim_{i→∞} P^i_t,    where  P^i_t = E[ M_{τ_i} | F_t ]

This is the point most restatements get wrong, so it is worth isolating: **the limit is
over successive mid-price-change events, not over a clock horizon `k → ∞`.** That is
precisely why the estimator is *horizon independent* and why it needs less data than
averaging mid changes over a fixed clock horizon.

**Theorem (the recursion).**

    P^i_t = M_t + Σ_{k=1..i} g^k(I_t, S_t)
    g^1(I,S)   = E[ M_{τ₁} − M_t | I_t=I, S_t=S ]
    g^{i+1}(I,S) = E[ g^i(I_{τ₁}, S_{τ₁}) | I_t=I, S_t=S ]

so `P^micro = M + g(I,S)` with `g = Σ_{k≥1} g^k` — the `g` in the mandate's
`P_micro = M + g(I,S)`. **The mandate's form is correct.**

**The algorithm is a Markov chain, not a regression.** Discretise `I ∈ {1..n}`,
`S ∈ {1..m}`, state `X = (I,S)` with `nm` values, and mid changes `k` with `0 < |k| ≤ 2m`.
Build an absorbing chain:

    Q_ij  = P( ΔM = 0  ∧  X_{t+1} = j | X_t = i )      transient, nm × nm
    R¹_ik = P( ΔM = k                 | X_t = i )      absorbing, nm × 4m
    R²_ik = P( ΔM ≠ 0  ∧  I_{t+1} = k | I_t = i )

    g¹ = (1 − Q)⁻¹ R¹ k̲ ,     k̲ = [−2m … −1, 1 … 2m]ᵀ
    g^{i+1} = B g^i ,           B = (1 − Q)⁻¹ R²

with the closed form `P^micro = M + Σ_{i≥2} exp(λ_i) B_i g¹` over the spectral
decomposition of `B` (the Perron term `i = 1` is excluded).

**Convergence requires `B g¹ = 0`, and that is enforced by an explicit symmetrisation
step, not by luck.** The estimation recipe symmetrises `g¹` so that
`g¹(i_I, i_S) = −g¹(n − i_I, i_S)` and symmetrises `B` correspondingly. This step is
**load-bearing** — it is what guarantees the limit exists. An implementation that skips it
because it "looks cosmetic" produces a divergent or arbitrary answer. Discretisation in the
released notebook: 10 imbalance buckets, 2 spread buckets (INFERRED, from a third-party
walkthrough of the author's notebook, not from the paper).

**What it is for.** Short-horizon fair value: an estimate of where the mid will settle,
purged of bid-ask bounce, that is a martingale by construction. It is **not** a forecast of
terminal value and must never be substituted for one in the EV identity of section 6.

**Why not just use the weighted mid.** Stoikov attacks `M^w = I·P^a + (1−I)·P^b` (which is
algebraically identical to `(P^b·Q^a + P^a·Q^b)/(Q^b+Q^a)`) on three grounds: **"Not a
martingale. Noisy. Counter-intuitive examples."** His counter-example is the decisive one
and it transfers directly to Kalshi: with a bid of 9 at 32.17, an ask of 1 at 32.18 and 27
resting at 32.19, `M^w = 32.179`. Now **cancel the single ask** — supply is removed, so
fair value should rise. Instead `I` falls to 9/36 and `M^w` drops to 32.175. His punchline:
*"The 'fair' price just moved down after an ask order canceled?"* The weighted mid is not
merely a worse predictor; it is **structurally capable of moving in the economically wrong
direction** whenever depth is asymmetric across levels. On a thin Kalshi book with a
1-contract top level, that configuration is not exotic — it is Tuesday.

**Deeper-book extension.** One direct follow-up exists: Christian D. Blakely,
"High resolution microprice estimates from limit orderbook data using hyperdimensional
vector Tsetlin Machines", arXiv:2411.13594 (2024). <https://arxiv.org/abs/2411.13594> It
does **not** modify `P^micro = M + g(I,S)`; it adds a learned tick-level error correction
in `{−2,…,+2}` from volume features beyond the touch, reporting ~10–20% `L₂` error
reduction on TSLA over 6 days of Databento L3 data. The tell that matters for us: the
author notes the adjustment "tends to perform the same as the microprice when spreads are
more tight", and its results on the small-cap, wider-spread name (TEM) are **more
variable** — i.e. **the deeper-book correction helps least exactly where the book is thin
and wide**, which is the opposite of what we would want on Kalshi. Six days, two tickers,
one author: treat as a lead, not as an established result.

### 4.2 Failure modes in thin and wide books

This is the part of the micro-price literature that is thinnest, so the honest answer is
partly structural inference rather than a quoted result.

**What Stoikov shows empirically (VERIFIED).** He tests exactly the large-tick /
small-tick question, with BAC ("a typical large tick stock", spread mass essentially a
spike at 1 tick) and CVX ("a typical small tick stock", spread spread over ~1–4 ticks),
March 2011. Out of sample, **BAC tracks tightly; CVX is visibly noisier**, with realised
curves whipping around the fitted `g`. For BAC only spreads of 1 and 2 ticks exist and
the two `g` curves differ in shape, the 1-tick curve being notably non-monotone near the
extremes. His conclusions are hedged — the micro-price "**seems to** live between the bid
and the ask" is an empirical observation, not a proved property — and his "future work"
slide lists connections to volatility, volume and **tick size** as unresolved. That is a
direct admission that tick-size dependence was not settled.

**The structural failure mode (INFERRED, but mechanical).** The state space is `n·m` cells
and `R¹` has `4m` absorbing columns. As the maximum spread `m` grows, the chain grows
quadratically-ish while the data per `(I,S)` cell shrinks. **The estimator is data-starved
exactly where spreads are wide.** On Kalshi, where a dormant contract can show a 15¢
spread, `m = 15` gives 10 × 15 = 150 states and 60 absorbing columns to estimate from a
contract that may emit a few hundred book events in its entire life. This is not a tuning
problem; there is no amount of care that fits it.

**A second, independent statement of the same problem (VERIFIED, from Xu–Gould–Howison
§6.4).** When the spread exceeds one tick, a limit order arriving *one* tick inside and
one arriving *many* ticks inside produce **identical top-of-book state vectors but very
different mid-price outcomes**. The same input maps to different outputs. This
mechanically caps the predictive power of *any* `(I, S)`-only estimator in a wide-spread
regime, micro-price included — it is a property of the state space, not of the fitting
method.

**A third failure mode specific to binary contracts (ours, INFERRED).** `g(I,S)` is a
price adjustment in ticks, but a Kalshi tick is 1% of terminal value, and the payoff
variance `p(1−p)` collapses toward the boundaries. A micro-price adjustment of half a tick
is worth the same in cents at `p = 0.50` and `p = 0.03`, but at `p = 0.03` it is a 17%
relative revision of the probability and is bounded below by zero. **The `(I,S)` state must
be extended with a price bucket** — `g(I, S, price_regime)` — or the estimator will be
systematically wrong in the tails. This is a departure from the published construction and
is flagged as such.

### 4.3 Recommendation for Kalshi

1. **Fit `g(I, S, price_regime)` only on `spread_regime = TOUCHING` and `NARROW` data.**
   In `WIDE`/`VERY_WIDE`, return the micro-price as absent with reason
   `WINDOW_UNDERFILLED` rather than extrapolating `g`. An absent fair value is a usable
   input to a decision rule; a fabricated one is not.
2. **Never use the weighted mid as a cheap substitute.** Stoikov's cancellation
   counter-example is a routine configuration on a thin binary book, and a fair-value
   estimator that moves the wrong way on a cancellation will systematically mis-sign the
   maker/taker decision in section 7.
3. **Pool the fit across contracts within a family**, keyed on `(I, S, price_regime)`, and
   require a minimum cell count before a cell is usable. Per-contract fits will not have
   the data.
4. **Keep `I` in the `[0,1]` convention for the micro-price and `[−1,1]` for the
   Gould–Bonart features, and name both explicitly in the schema.** They are different
   variables that look identical in code.

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
