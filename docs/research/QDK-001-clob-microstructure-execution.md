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

**Verdict up front: RECOMMEND AGAINST, for this venue, for this purpose, now.** Not
because the model is wrong — it is a good model — but because at Kalshi's event rates the
fitted object would be a measurement of our own misspecification, and because the feature
basis it produces is one we can get for free without any of its failure modes. The
reasoning is below; the single decisive point is 5.4.

### 5.1 The model — VERIFIED

The marked multivariate exponential-kernel Hawkes intensity, as the mandate states it:

    λ_i(t) = μ_i + Σ_j Σ_{τ_k^j < t} α_ij · exp(−β_ij (t − τ_k^j))

is the standard un-normalised form and is correct. (A notational trap worth naming:
Bacry–Mastromatteo–Muzy write the kernel as `αβe^{−βt}`, so *their* `α` is the kernel
**integral**, not the jump size. Two papers using "α" for different quantities is a real
source of silent error.) The natural dimension set for our purposes is the one the mandate
lists — aggressive buy, aggressive sell, bid placement, ask placement, bid cancellation,
ask cancellation, large trade, liquidity removal — so `D = 8`.

Citations, audited:

| Source | Status |
|---|---|
| Bacry, Mastromatteo, Muzy, "Hawkes processes in finance", arXiv:1502.04592 | **VERIFIED** — correct ID and authors. <https://arxiv.org/abs/1502.04592> |
| Bacry & Muzy, "Hawkes model for price and trades high-frequency dynamics", arXiv:1301.1135 | **VERIFIED** — correct ID. <https://arxiv.org/abs/1301.1135> |
| Large (2007), "Measuring the resiliency of an electronic limit order book", *J. Financial Markets* 10(1):1–25 | **VERIFIED** — and it is indeed a **ten-variate** Hawkes model on the LSE book |
| Muni Toke & Pomponio, "Modelling Trades-Through in a Limit Order Book Using Hawkes Processes" (2012), HAL hal-00745554 | **VERIFIED** — but note it is only **bivariate**, and its headline finding is that **cross-excitation between bid and ask trades-through is weak** |
| Lu & Abergel, "High-dimensional Hawkes processes for limit order books" | **PAPER REAL, arXiv ID DOES NOT CHECK OUT.** *Quantitative Finance* 18(2):249–264 (2018), DOI 10.1080/14697688.2017.1403142; the preprint is on HAL, **not arXiv**. arXiv:1706.03411 is a *different* paper (Achab, Bacry, Muzy & Rambaldi on the branching-ratio matrix) published in the same QF issue at adjacent pages, which is the likely source of the confusion. **Do not cite Lu & Abergel with an arXiv number.** |

Lu & Abergel are worth quoting anyway, because the authors of the flagship
high-dimensional LOB Hawkes paper report two things that undercut the case for it: real
books exhibit **inhibition effects** (which a linear Hawkes with `α ≥ 0` structurally
cannot represent), and calibration suffers from the "**very poor convexity properties of
the MLE**".

### 5.2 Stability — VERIFIED

The process is asymptotically stationary **iff the spectral radius of the branching matrix
is strictly less than 1**, where for exponential kernels

    Γ_ij = ∫₀^∞ α_ij e^{−β_ij t} dt = α_ij / β_ij ,    condition: ρ(Γ) < 1

Note it is the **spectral radius**, not any convenient matrix norm; using `‖Γ‖_∞` as a
proxy gives a conservative and wrong constraint.

Empirically, fitted LOB models sit **against** the boundary:

| Study | Reported branching ratio |
|---|---|
| Achab, Bacry, Muzy, Rambaldi (arXiv:1706.03411), 12-dim DAX futures book | **ρ(Γ) ≈ 0.98**; exogenous fraction only "a few percent" |
| Bacry–Mastromatteo–Muzy review, calibrated financial models generally | **‖Φ‖ ≈ 0.9–0.95** |
| Hardiman, Bercot, Bouchaud (arXiv:1302.1405), E-mini S&P mid-price changes | **n ≈ 1**, stable 1998–2011, power-law kernel |
| Lallouache & Challet (arXiv:1406.3967), FX, restricted to defensible windows | **n ≈ 0.8** |

The mandate's recollection of ~0.7–0.9 is right, and the high end is the more common
finding. **This matters:** an estimator whose target sits at 0.9–1.0 is operating in the
exact region where finite-sample bias is documented to be worst, and where a misspecified
baseline is indistinguishable from true excitation (5.4).

### 5.3 Estimation cost and data requirements — VERIFIED

**Compute is not the constraint.** The exponential kernel admits Ozaki's (1979) O(1)
recursion on the excitation state,

    R(q) = e^{−β(t_q − t_{q−1})} · (1 + R(q−1)),   R(0) = 0,   λ(t_q) = μ + α·R(q)

collapsing the log-likelihood from O(N²) to **O(N)** (O(N·D²) in D dimensions), with the
compensator integral telescoping in closed form. A Hawkes MLE on a Kalshi contract runs in
milliseconds. **Anyone arguing for Hawkes on computational-elegance grounds is answering a
question we do not have.**

**Parameter count.** `D + D² + D² = D + 2D²`. At `D = 8` that is **136 parameters** — the
mandate's arithmetic is confirmed. The usual mitigations (one `β` per row → 80; one global
`β` → 73) help, but a *marked* model then adds the mark distribution on top, and the LOB
literature says the marks you need — order-size distributions — are multimodal, spiky at
round lots, and history-dependent. That is a second model, not a few extra parameters.

**Data requirements: the literature gives an upper bound on the usable window, not a
comfortable lower bound on events, and the two do not meet.**

- **Lallouache & Challet (arXiv:1406.3967)**, on FX — chosen precisely because it runs 24h
  with no session breaks — find that fits which *look* convincing largely fail statistical
  tests. What survives three simultaneous tests: **two exponentials fitted to one hour at a
  time**. "Longer periods could not be fitted within statistical satisfaction because of
  the non-stationarity of the endogenous process." On one of the highest-event-rate markets
  in existence, the defensible calibration window is **one hour**.
- **Filimonov & Sornette (arXiv:1308.6756)** recommend **10–30 minute windows**, never
  concatenated; show finite samples **systematically underestimate** `n` because pre-window
  ancestors are unobserved; show kernel misspecification alone produces **Δn ≳ 0.2**; and
  show that removing as little as **0.17%** of extreme inter-event durations moves `n̂` from
  ≈0.7 to apparent criticality.
- **Edge effects are quantitatively worse than the stationary window.** For a power-law
  kernel with ε = 0.15 they compute `T₀.₉₅ ≈ 1.1×10⁷ s` (≈489 trading days). The window
  needed to avoid edge truncation **exceeds any window over which the process is actually
  stationary.** That is an irreconcilable tension, not a tuning problem.
- A widely repeated finite-sample-bias rule of thumb — large bias for `n > 0.9`, small bias
  above roughly **200–400 events** — could **not** be traced to a primary source. Treat it
  as an order of magnitude only, and note that it is a *univariate* figure.
- Known problems, all confirmed: **non-convex likelihood** with local minima; **`β` is the
  badly identified parameter**, which is why the universal practice is to fix `β` on a grid
  and estimate only `α`; **edge effects**; and, if timestamps are binned rather than exact,
  the exact likelihood becomes **intractable** and requires particle methods.

**Nonparametric alternatives need MORE data, not less.** Bacry–Muzy Wiener–Hopf estimates
the whole kernel *shape* — strictly more to estimate than two parameters. The Achab et al.
NPHC method is the smart compromise (it estimates only the `D²` kernel *integrals* via
integrated cumulants, skipping shape entirely, and scales to D = 12–16) — but it was run on
**338 days × 300,000+ events/day**. It is a large-sample method. Nonparametric is not an
escape hatch for a sparse venue.

**The arithmetic for us.** At an optimistic 5 book updates/minute/contract, an 8-hour
session yields ~2,400 events. Split across `D = 8` types, ~300 per type against **136
parameters** — roughly 2 events per marginal parameter, before splitting into the pairwise
coincidence cells that actually identify `α_ij`. Filimonov–Sornette's recommended 10–30
minute window would contain **50–150 events in total**, below even the unverified 200–400
univariate floor. **There is no version of this arithmetic that works at D = 8.**

### 5.4 The two decisive arguments

**(1) A Hawkes intensity IS an affine function of EWMA event counts.** Define

    S_ij(t) = Σ_{τ_k^j < t} exp(−β_ij (t − τ_k^j))

`S_ij` is *exactly* an exponentially weighted count of type-`j` events with decay `β_ij`,
updatable by the same Ozaki recursion. Then

    λ_i(t) = μ_i + Σ_j α_ij · S_ij(t)

Since everyone fixes `β` on a grid anyway (5.3), the model **is linear in EWMA features**
and the `α_ij` are regression coefficients. Kirchner's INAR(p) result (arXiv:1509.02017) is
the discrete-time statement of the same fact, with consistency and asymptotic normality
proved: bin the timeline and the multivariate Hawkes becomes a **standard VAR(p) on bin
counts fit by conditional least squares**.

So the honest question is not "Hawkes features versus EWMA features" — they are the same
feature basis. It is: **given that basis, do we fit the coefficients by point-process MLE
against the arrival-rate likelihood, or discriminatively against the target we actually
care about?** For prediction, discriminative fitting on the real objective is the
textbook-dominant choice. Hawkes MLE optimises a *different* loss, constrains coefficients
to be non-negative and spectrally stable — which is a **misspecification penalty** given
that Lu & Abergel report real inhibition effects — and buys a generative simulator we do
not currently need.

The one direct empirical comparison found (arXiv:2408.03594, *Computational Economics*
67(1)) benchmarks Hawkes variants against Poisson and a plain VAR on minute counts under
Hansen's SPA test. Higher p means "not demonstrably outperformed":

| Model | SPA p-value |
|---|---|
| Sum-of-exponentials Hawkes | 0.743 |
| Conditional-law Hawkes (nonparametric) | 0.257 |
| **Plain VAR on minute counts** | **0.101** |
| **Single-exponential Hawkes** | **0.002** |
| Power-law Hawkes | 0.000 |
| Poisson | 0.000 |

The plain VAR is **not rejected at 5%**. The single-exponential Hawkes — the model anyone
would actually build first — is **decisively rejected**. The evidence base is one trading
day of NIFTY futures (315 usable minute observations), so this is not a strong result; but
it is the closest thing that exists, and it points the wrong way for Hawkes. Note also
what the winner is: *sum*-of-exponentials, i.e. **multiple timescales**, which is exactly
what a multi-halflife EWMA set gives us for free.

**(2) Kalshi's data-generating process is the exact one for which Hawkes reports a
spurious near-critical branching ratio.** Filimonov & Sornette, verbatim:

> "calibration of the Hawkes process on mixtures of pure Poisson process with changes of
> regime leads to completely spurious apparent critical values for the branching ratio
> (n ≃ 1) while the true value is actually n = 0."

A dormant Kalshi contract that wakes up on news **is** a Poisson process with a regime
change. This is not an analogy. If we fit a constant-`μ` Hawkes to our tape and obtain
`n̂ = 0.95`, we will have learned nothing about self-excitation in Kalshi order flow — and
the trap is that it will look like a strong, exciting, publishable finding.

The standard fixes all fail for us in a specific way:

- **Short windows** (10–30 min): too few events at our rates.
- **Deterministic intraday seasonality** `μ(t) = μ·s(t)`: Kalshi does have time-of-day
  structure, but the dominant non-stationarity is **event-driven and idiosyncratic per
  contract** (a goal, a data release, a resolution), which no repeatable daily profile
  touches.
- **Locally stationary Hawkes** (Roueff & von Sachs; Mammen & Müller): time-varying
  baseline *and* fertility — strictly more parameters, strictly more data.
- **Explicit burst detection** (Rambaldi, Filimonov & Lillo, arXiv:1610.05383): possible,
  and note their FX finding that **only a small fraction of detected bursts are associated
  with news arrival**.

The circularity is inescapable: separating "the baseline moved" from "the process excited
itself" requires *many* clusters, because both produce the same observable. At Kalshi rates
we will not have them. **The fix that would make the model honest is precisely the fix we
cannot afford.**

### 5.5 Two further facts worth recording

- **There is no Hawkes literature on prediction markets.** Kalshi, Polymarket, Betfair,
  sports betting exchanges — nothing found. We would be first. That is a reason for
  caution, not enthusiasm: nobody has shown this works at these rates. The most relevant
  adjacent work, a large Polymarket order-book microstructure study
  (arXiv:2604.24366; 30.3 billion events, 385,198 markets, 52 days), **does not use
  Hawkes or any self-exciting point process at all** — and reports the heterogeneity we
  should expect: random-stratum markets saw between **100 and 24,378 trades** over the
  window, with **53 of 600** sampled markets having no usable depth data.
- **Even at Euronext rates and D = 2**, Muni Toke & Pomponio found cross-excitation
  *weak*. Cross-excitation — the `D²` off-diagonals — is the entire reason to want a
  *multivariate* Hawkes. It is also the first thing to die at low event rates, because
  `α_ij` is identified only by observed j→i coincidences inside the decay window, and at a
  handful of events per minute across 8 types most `(i,j)` cells have single-digit or zero
  coincidences.

### 5.6 Recommendation

**Do not build a Hawkes model in this track.** Instead:

1. **Ship `event_rate_ewma[τ]` for τ ∈ {1s, 10s, 60s} per event type** (schema group E).
   This is the `S_ij(t)` basis, it is O(1) per event, it has no estimation step, no
   convergence condition, no branching ratio to be spuriously near-critical, and it is
   defined from the first event rather than requiring a fit.
2. **Fit coefficients discriminatively against the actual target** (next mid change,
   markout, fill), not against an arrival-rate likelihood.
3. **Keep the door open with a cheap, decisive experiment.** Once the tape exists, run:
   (i) EWMA features alone, (ii) EWMA features with Hawkes-MLE-fitted coefficients,
   (iii) a persistence baseline — compared out-of-sample on our own target. Expectation
   from 5.4: (ii) does not separate from (i). That is a day's work against the archive and
   it settles the question with our own data rather than by analogy to markets running
   10,000× our event rate. **It is a measurement, not a modelling commitment.**
4. **Revisit only if** all of: `D` drops to 2–4; we find contract families with thousands
   of events inside a defensibly stationary window; and the requirement turns out to be
   *generative* (a fill/queue simulator, or the state process for an optimal-execution
   control problem) rather than predictive. That is where the literature's demonstrated
   value actually lives — descriptive realism, resiliency measurement, causal-structure
   discovery, and execution control — and **not** out-of-sample predictive edge over
   simpler features, which none of the canonical LOB Hawkes papers claims.

## 6. Execution modelling — the core deliverable

### 6.1 The EV identity

    EV(s, venue_state) = E[terminal value]·s
                       − C_execution(s | book)
                       − E[impact](s)
                       − E[adverse selection](s)
                       − fees(s, P)
                       − latency_risk(s)

For a Kalshi YES contract the terminal value is in `{0, 1}` dollars, so
`E[terminal value] = p̂`, our probability estimate. **Everything else on that line is
subtracted from `p̂`, and on this venue the subtractions are larger than any
microstructure signal.** All quantities below are **cents per contract** unless stated.

Term by term, with what supplies each:

| Term | Source | Observable? |
|---|---|---|
| `p̂` | the forecast lane, **not** this document | — |
| `C_execution(s)` | the ladder walk against the actual book (6.2) | **yes, exactly** |
| `E[impact](s)` | `β ≈ c/D` from §3.1, applied to our own order's OFI contribution | yes, fitted |
| `E[adverse selection](s)` | conditional markout given fill (6.5) | partly (6.4) |
| `fees(s, P)` | the venue schedule, a deterministic function (6.6) | **yes, exactly** |
| `latency_risk(s)` | expected mid drift over our decision-to-arrival window (6.7) | partly — clock offset is unmeasured |

The critical structural property: **`C_execution` is a function of `s`, not a constant, and
it is convex in `s` on a discrete ladder.** That is what makes the mandate's claim true —
the same signal at different notionals can have opposite sign — and 6.2 works it out
numerically.

### 6.2 `C_execution` from the actual book, never from an assumption

`C_execution(s)` is the ladder walk and nothing else:

    remaining ← s ;  cost ← 0
    for each populated ask level (p_k, q_k) ascending from the touch:
        take ← min(remaining, q_k)
        cost ← cost + p_k · take
        remaining ← remaining − take
        if remaining == 0: return VWAP = cost / s
    return UNFILLABLE(shortfall = remaining)        # typed, NOT an extrapolated price

Three rules that must be enforced in code, not merely intended:

1. **`UNFILLABLE` is a typed outcome, never a price.** If the visible ladder cannot fill
   `s`, the answer is not "the last price plus a bit". It is a refusal carrying the
   shortfall. A model that extrapolates past the visible book is inventing liquidity, and
   on Kalshi the ladder frequently ends well before $1.00.
2. **The book used must be publishable and fresh.** `is_publishable == False` or
   `book_staleness > bound` ⇒ no quote, absence reason `BOOK_UNPUBLISHED` / `STALE`.
3. **Hidden/iceberg liquidity is assumed to be zero.** We have no evidence Kalshi has any,
   and assuming zero is the conservative direction for a cost estimate. Flagged as
   UNVERIFIED; if it exists, our costs are overstated, which is the safe error.

**Worked example — the same signal changing sign with size.** Say `p̂ = 56¢`, and the ask
ladder is **40 contracts at 52¢, then 200 at 55¢**. Fees per the Kalshi taker formula
(6.6), computed per fill price:

| Size `s` | Fill | VWAP | Gross edge/contract | Fee/contract | **Net/contract** | **Total EV** |
|---|---|---|---|---|---|---|
| 40 | 40@52 | 52.00 | 4.00¢ | 1.75¢ | **+2.25¢** | **+90¢** |
| 100 | 40@52 + 60@55 | 53.80 | 2.20¢ | 1.74¢ | **+0.46¢** | **+46¢** |
| 240 | 40@52 + 200@55 | 54.50 | 1.50¢ | 1.74¢ | **−0.24¢** | **−58¢** |

**The identical signal is worth +90¢ at 40 contracts and −58¢ at 240.** The sign flips
between them. Note also that total EV is *maximised at the top-of-book size*, not at some
larger "optimal" size — because on a 1¢ grid, stepping one level up costs a full 100 bp of
notional at once. There is no smooth impact curve here to trade off against; there is a
staircase.

**Design rule:** the sizing decision is `s* = argmax_s EV(s)` computed by walking the
actual ladder at decision time, evaluated on the tabulated `fill_cost_curve(s)` from
schema group B. It is **never** a fixed notional, never a fraction of some assumed ADV,
and never derived from a Kelly fraction applied to `p̂` alone. A Kelly-style sizing rule
that ignores `C_execution(s)` will systematically recommend sizes past the sign flip.

### 6.3 `P(fill | ·)` — the fill-probability model

This and 6.4 are **different problems** and must be separate models with separate
validation. Conflating them is the specific error section 6.5 is about.

**Features available to us at submission time** (all from schema §2.3, all archivable):
`Q_near` (size resting at our chosen price), `Q_opp`, `queue_imbalance`, `spread_ticks`,
`distance_from_touch` (0 = joining the touch, +1 = one tick behind, −1 = improving inside
the spread), `OFI(Δt)`, `signed_trade_imbalance(Δt)`, `order_arrival_intensity`,
`cancellation_intensity`, `realized_vol`, `event_rate_ewma[τ]`, `time_to_close`, and the
resting horizon `T` we are willing to commit to.

**Structure.** `P(fill within T)` is the probability that either hazard fires:

- **H1, consumption:** cumulative opposite-side taker volume at or through our price
  reaches our queue-ahead. This is the "good-looking" fill.
- **H2, run-through:** the price moves through our level. This is the adverse fill.

A hazard-rate formulation is the right shape — `P(fill) = 1 − exp(−∫₀^T h(u|x) du)` — and
it makes the horizon `T` explicit, which a static logistic does not. Albers et al.'s fitted
form is a useful prior for the level-1 case and is worth recording because of how *simple*
it turned out to be: on 232,897 live Binance orders they fit
`z = β₀ + β₁Q_near + β₂Q_opp + β₃·imb` with **β₀ = 0.5649, β₁ = 0.0159, β₂ = 0.1013,
β₃ = −0.3166, R² = 0.946** (queue sizes normalised by their 99th percentiles; `Q_opp` was
*not* significant at 5%, p = 0.065). Their conclusion, quoted: *"fill probabilities are
relatively easy to predict based on queue sizes… calling into question the need for
black-box machinery."* **Do not spend modelling effort here.** The hard part is 6.4.

**The Kalshi-specific obstruction, and it is serious.** Kalshi's WebSocket is **L2**: the
`orderbook_delta` message carries `side`, `price_dollars` and a single net `delta_fp` for
one price level. There are **no order IDs and no per-order events.** Consequences:

- **Queue-ahead at submission is observable**: it is exactly the current `Q_near`.
- **Queue-ahead thereafter is NOT observable.** When a level shrinks by `δ` through
  cancellation, we cannot tell whether the cancellations came from *ahead of* or *behind*
  our position. Our queue-ahead therefore evolves as an **interval**, not a number:
  `Q_ahead ∈ [max(0, Q_ahead − δ), Q_ahead]`.
- Albers et al. recovered exact queue position at fill by exploiting a **Binance
  idiosyncrasy** — the public trade feed publishes all maker fills for a taker order in
  execution-priority order with unique identifiers. **Kalshi's `trade` channel publishes
  aggregate prints only.** That technique does not port. This is the single largest
  data gap in this document.

**How to handle it honestly.** Model queue-ahead under a named, stated assumption *and*
report the bracket:

    A-UNIFORM (point estimate): cancellations are uniformly distributed across the
        queue, so a cancellation of δ from a level of size Q reduces our queue-ahead
        in expectation by δ · (Q_ahead / Q).
    OPTIMISTIC bound:  all cancellations came from ahead of us.
    PESSIMISTIC bound: no cancellation came from ahead of us.

**And then state the thing nobody will want to hear: A-UNIFORM cannot be validated from
observation alone.** Identifying it requires placing orders and observing our own fills,
which `OBSERVE_ONLY` forbids and which this document does not propose. It is a permanently
unfalsifiable parameter under the current capability boundary.

**Therefore: a paper maker fill can never be a single number.** Every maker fill in the
shared paper ledger must carry `(p_fill_optimistic, p_fill_point, p_fill_pessimistic)` and
every downstream paper P&L must be reported as a **bracket**. A single-point maker P&L
would be a modelled quantity presented as a measured one, which is exactly the failure mode
the repo's archive and reconciliation lanes have spent multiple milestones eliminating.
*Taker* fills do not have this problem — `C_execution` is exact from the visible ladder —
which is a real argument for making the first prospective paper P&L a **taker-only**
measurement, despite taker being the more expensive execution mode.

### 6.4 `E[return post-fill | filled]` — the markout model

A different model, a different target, a different validation set.

    markout_maker(h) = D · (M_{t_fill + h} − P_fill)
    where D = +1 if we bought YES, −1 if we sold YES

Horizons: report a **vector** of `h`, not one. Albers et al. use 1s for the markout table
and 5s for the fill-vs-return analysis; at Kalshi's event rates, clock-time horizons of
1–5s will frequently contain **zero** book events, so **event-time horizons are mandatory
alongside clock time**: markout at the next mid change, at the 5th, at the 20th, plus
clock-time 10s / 60s / 300s. A markout evaluated across an interval with no book event is
absent with reason `STALE`, never zero.

Features: everything in 6.3 *plus* everything only knowable at fill time — which side the
consuming taker was on, whether the fill was part of a sweep, the book state immediately
after. The essential asymmetry: `P(fill)` is predicted from **submission-time** state;
markout is predicted from **fill-time** state, and the two state distributions are
systematically different. That difference *is* the adverse selection.

### 6.5 The negative fill-probability/return relationship — VERIFIED, and it is stronger than a correlation

**Citation checks out.** Jakob Albers, Mihai Cucuringu, Sam Howison, Alexander Y.
Shestopaloff, "The Market Maker's Dilemma: Navigating the Fill Probability vs. Post-Fill
Returns Trade-Off", arXiv:2502.18625 (v1 2025-02-25, v2 2025-11-23).
<https://arxiv.org/abs/2502.18625> The mandate's description matches on every count.

**Design (VERIFIED).** A genuine **live** experiment on the Binance USDT-margined BTC
perpetual — not a backtest, not synthetic orders, not passive observation of others'
orders. Continuous-quoting mode, 12–19 February 2024: **232,897 minimum-size maker orders,
127,051 filled (54.6%)**, always at the touch, cancel-and-repost whenever the level ceased
to be top-of-book. A periodic-quoting robustness run (172,800 orders, August 2024) gave
"essentially identical" results. The design's purpose is to eliminate **signal bias**: an
order is cancelled *if and only if* its price level stopped being top-of-book, never for a
discretionary reason.

Their own calibration of why passive observation is misleading: prior work reports **< 2%**
fill probability for one-tick-spread balanced-queue orders inferred from historical
book data, versus **50%** in their live experiment for the same configuration — the gap
being entirely other traders' cancellation behaviour. **Any fill model we estimate purely
from observed book data will be biased low in the same way**, and we should expect that
bias to be large.

**The finding. VERIFIED, and the core of it is mechanical, not statistical.** Conditional
on the immediate post-terminal-time mid-price change, the fill probability of a
top-of-book maker order is **exactly 0 when that change is favourable and exactly 1 when it
is zero or adverse.** If the next move is against you, you fill with certainty; if it is in
your favour, your level ceased to be top-of-book and your order was cancelled unfilled.

**Does it hold? YES — and it holds by construction, for any venue, on any CLOB.** The
mechanism is the *cancel-and-repost policy*, not anything about Binance. Any policy that
requotes on a price move induces exactly this selection. So this is not an empirical
regularity that might fail to replicate on Kalshi; it is a property of the policy.

For a policy that does **not** requote — post and leave it — the selection is weaker but
the same sign: you are filled by flow that came at you, so the fill-time state distribution
is tilted adverse. The deepest statement of it predates all of this by 29 years:
**Handa & Schwartz (1996)**, *Journal of Finance* 51(5):1835–1861, "Limit Order Trading" —
a limit order carries two risks, that adverse information triggers an *undesirable*
execution, and that favourable news means the *desirable* execution is not obtained. A
resting limit order is a free option written to the market, and it is exercised precisely
when the writer is wrong. See also **Glosten & Milgrom (1985)**, *JFE* 14(1):71–100, for
why the spread exists at all under information asymmetry.

**Magnitudes (VERIFIED).** Average 1-second markout in basis points, excluding the maker
rebate, by queue configuration and queue position at fill:

| Queue config | QP 0–0.1 | 0.1–0.4 | 0.4–0.75 | 0.75–1 |
|---|---|---|---|---|
| Large near, small opposite (favourable imbalance) | **−0.058** | −0.586 | −0.743 | −0.775 |
| Large near, large opposite | −0.296 | −0.882 | −0.967 | **−1.157** |
| Small near, small opposite | −0.562 | −0.711 | −0.622 | −0.677 |
| Small near, large opposite (adverse imbalance) | −0.539 | −0.645 | −0.686 | −0.763 |

**Every cell is negative.** Unconditionally: **−0.8 bp, or −0.3 bp net of rebate.** The
best cell in the entire table — front of a large near-side queue with a small opposite
queue — is −0.058 bp, essentially breakeven *before* the rebate. And note the interaction:
when the near-side queue is **small**, queue position barely matters at all (−0.539 to
−0.763 across all four bins), because there is little liquidity behind you either way.
Queue position only buys protection inside a large near-side queue — and you cannot get
front-of-queue in a large queue by joining one, only by having joined it when it was small.

**What their strategies did (VERIFIED, and worth quoting for calibration):** naive
market making — post both sides at the touch, repost on price move — lost **~60% of
capital in ~3 days**, annualised **Sharpe −109.0**, −0.4307 bp per trade net. Imbalance-
following maker: −0.4705 bp. Imbalance *taker*: −1.9621 bp net despite roughly +1 bp per
round trip **before** fees — killed outright by the 1.5 bp taker fee. Their words for the
naive strategy: *"a recipe for poverty."*

**Their proposed rescue, and its honest limits.** Post **against** the prevailing book
imbalance, but only when a model predicts the imbalance is about to *reverse* — that way
you get the high fill probability of an adverse-imbalance order together with favourable
drift. Fitting a logistic on 173 submission-time features over 182,381 high-fill-probability
orders, 5-second returns cross into positive territory at a reversal threshold of ≈0.36 —
**at 69 orders per day**, down from 21,689. At the thresholds where the effect is
statistically comfortable (0.24–0.30) returns are still **negative** (−0.62, −0.32 bp).
The authors explicitly present this "not as a definitive solution, but to illustrate the
mechanics," under what they call the **Unprofitability Principle**: ease of prediction ×
ease of exploitation < c.

**Caveats they state (VERIFIED):** minimum order size throughout, with results expected to
**deteriorate** with size, since the taker's market impact is the maker's adverse
selection; best-tier fees assumed (taker 1.5 bp, maker −0.5 bp rebate) and "any strategy
results would only deteriorate with less favourable fees"; latency treated as negligible,
with a proper treatment stated to "reinforce our conclusions"; **L2 data only**, which is
why they could not do rigorous intermediate-time fill-probability updating — the same
limitation we have. Independent corroboration in a completely different asset class:
**DeLise (2024)** reports the same negative maker drift in **US Treasuries**.

**Porting the magnitudes to Kalshi — do this carefully, because the naive scaling is
wrong.** Their tick is ~0.02–0.03 bp; ours is 100 bp. Rescaling −0.8 bp by tick size would
give an absurd answer. **Adverse selection scales with volatility over the fill horizon,
not with tick size.** Their −0.8 bp at a 1-second horizon is a modest fraction of BTC's
1-second realised volatility. The correct port is therefore: *estimate `E[adverse
selection]` on Kalshi as a fitted fraction of that contract's own realised volatility over
the empirical time-to-fill*, using `normalized_vol` from schema group E. Order of magnitude
expectation on an active Kalshi contract, where mid moves in 1¢ steps: **~0.5–1 tick, i.e.
0.5–1¢ per side.** This is INFERRED and is a measurement task, not a result.

### 6.6 Fees — the term that dominates everything else

**Kalshi taker fee: `ceil_to_cent(0.07 · C · P · (1−P))` dollars, peaking at 1.75¢ per
contract at P = 0.50. Maker fee (where charged): `ceil_to_cent(0.0175 · C · P · (1−P))`,
peaking at ~0.44¢.** Status: **INFERRED from secondary sources** (section 10); the maker
fee in particular is reported as applying to *some* markets only. **This must be read from
Kalshi's own published fee schedule before any use, and re-read on a cadence — a fee
schedule change silently invalidates every threshold in this document.**

Three consequences, in order of importance.

**(a) The fee is larger than the tick, and larger than the documented signal.** At
P = 0.50 the taker fee is **1.75 ticks**. Now take the single strongest short-horizon
result in the literature — Gould & Bonart's large-tick regime, `P(up) ≈ 0.85` at extreme
queue imbalance — and give it the most generous possible reading: a perfect one-tick-ahead
prediction, traded costlessly at the mid. Its expected value is
`0.85·(+1¢) + 0.15·(−1¢) = +0.70¢ per contract.`

That is **less than the round-trip maker fee (0.875¢) and one fifth of the round-trip
taker fee (3.50¢)** at mid-range prices. **The best-case documented microstructure edge
does not cover Kalshi's round-trip cost.** This is the most important number in this
document.

**The conclusion that follows is a reframing of the whole track: on Kalshi, microstructure
is not a standalone alpha source. Its job is to make `C_execution` smaller and to avoid
adverse fills on a position we were going to take anyway for a probability-forecast
reason.** Any proposal to trade a pure microstructure signal on Kalshi should be measured
against the 0.70¢-versus-0.875¢ comparison above and, absent a specific reason it does not
apply, declined.

**(b) The ceiling rounding creates a minimum economic order size.** The fee is rounded up
to the next whole cent **on the order**, not per contract, so the overhead is
`≤ 1/C` cents per contract. Per-contract taker cost at P = 0.50: **2.00¢ at C = 1**,
1.80¢ at C = 10, 1.75¢ at C = 100. At P = 0.03 it is worse in relative terms: the marginal
rate is 0.21¢ but a 1-contract order still pays the 1¢ minimum — **~5× the marginal rate.**
Rule: **require C ≥ 10, prefer C ≥ 20**, and make rounding overhead an explicit line in the
EV computation rather than an approximation.

**(c) The fee curve pushes toward the tails, and the tails are where the estimators are
worst.** `P(1−P)` falls by a factor of ~8 between P = 0.50 and P = 0.03, so a tail contract
is dramatically cheaper to trade. But tail contracts have the thinnest books, the widest
spreads, the fewest events, and the least reliable micro-price and imbalance estimates
(§3.4, §4.2). **The venue's cost structure and its information structure point in opposite
directions.** This tension is real, has no clean resolution, and should be stated in any
strategy proposal rather than resolved by picking whichever side favours the proposal.

**(d) Holding to settlement avoids the exit fee entirely.** A directional
probability-forecast position held to resolution pays fees **once**. A microstructure
round trip pays **twice**. That asymmetry is another argument for microstructure as an
execution-quality tool rather than a signal.

### 6.7 Latency risk

Under `OBSERVE_ONLY` there is no order path, so "latency risk" here means exactly one
thing: **the risk that a decision is made against a book that has already moved.**

    L = data_age_us  +  decision_compute_time  +  (unmeasured) submit-to-venue time
    latency_risk ≈ E[ |ΔM| over L ]  ≈  normalized_vol · sqrt(L)

Two honest disclosures:

1. **`data_age_us` is a biased estimate of true age because our host-vs-venue clock offset
   is NOT MEASURED** (`app/cli.py:717-722` records this explicitly for the replay lane).
   Until it is measured, `latency_risk` carries an unquantified bias of unknown sign.
2. **Observation gaps are not latency, they are blindness.** `subscription_generation`
   changes and the `[disconnected_at, reconnected_at]` intervals from the collector's own
   measurement stream mark periods where we had no book at all. A decision timestamped
   inside a gap is not a high-latency decision; it is an invalid one, and must be typed
   absent.

Encoding: a hard gate, not a penalty term. **Refuse to quote when `book_staleness > bound`
or `is_publishable == False` or the timestamp falls inside an observation gap.** A refusal
is a valid, recordable decision outcome; a decision on a stale book is not.

---

## 7. The maker/taker decision rule

All in cents per contract, YES side, buying. `p̂` = probability estimate ×100; `b`, `a` =
best bid/ask; `s = a − b`; `f_T`, `f_M` = taker and maker fee per contract at the relevant
fill price; `A = E[adverse selection | filled] ≥ 0`; `P_f = P(fill within T)`; `κ` = the
opportunity cost of not getting the position at all.

    EV_taker        = p̂ − VWAP_ask(size) − f_T
    EV_maker|fill   = p̂ − b − f_M − A
    EV_maker        = P_f · (p̂ − b − f_M − A) − (1 − P_f) · κ

**Per filled contract, maker beats taker by**

    EV_maker|fill − EV_taker = s + (f_T − f_M) − A

At P = 0.50 with a 1¢ spread this is `1 + 1.31 − A = 2.31 − A` cents. **Maker wins unless
adverse selection exceeds ~2.3 ticks**, which is well above our order-of-magnitude estimate
of 0.5–1 tick (6.5). Note how different this is from Albers et al.'s venue, where the
maker's advantage is a 2 bp fee differential against 0.8 bp of adverse selection — a close
call. **On Kalshi the maker/taker gap is dominated by the fee differential (1.31¢) and the
spread (1¢), not by adverse selection.** Maker is the correct default by a wide margin.

**But per *filled* contract is the wrong comparison**, because the taker fills with
certainty and the maker does not. With `κ = 0` (we are indifferent to missing a trade):

    TAKE  if   EV_taker  >  [ P_f / (1 − P_f) ] · ( s + f_T − f_M − A )

At `P_f = 0.5`, `s = 1`, `A = 1`: take only if `EV_taker > 1.31¢`, i.e. only if
`p̂ − a > 3.06¢`. **The taker threshold at mid-range prices is roughly a 3-tick edge.**
That is a high bar, and it is the correct one.

### The decision rule

    0. GATES (any failure ⇒ NO ACTION, typed reason; these are not penalties)
       is_publishable ∧ book_staleness ≤ bound ∧ not in observation gap
       ∧ lifecycle_state == open
       ∧ both sides non-empty
       ∧ size ≥ C_min (fee-rounding floor, 6.6b)
       ∧ spread_regime ∈ {TOUCHING, NARROW}      # §3.4: no prediction in WIDE books

    1. SIZE.  s* = argmax_s EV_taker(s) over the tabulated fill_cost_curve.
              If max EV_taker(s) ≤ 0 for all s, no taker action is available at any size.

    2. MAKER FIRST.  Compute EV_maker at the best posting price. Post if
              EV_maker > 0  ∧  EV_maker ≥ EV_taker.

    3. TAKE ONLY IF the threshold above is cleared AND the signal's decay horizon is
              short relative to the modelled time-to-fill. Paying 1.75¢ to convert a
              probabilistic fill into a certain one is only rational when the edge would
              be gone before the maker order fills.

    4. OTHERWISE NO ACTION.  This is the expected outcome most of the time and must be
              a first-class, recorded decision — not an absence of one.

### Encoding the "never optimise maker execution for fill rate alone" rule

This is the design constraint the mandate asks for, and it needs to be structural rather
than advisory, because fill rate is the metric everyone reaches for by default: it is easy
to compute, it is always available, and it goes up when you make your policy worse.

1. **Fill rate is a diagnostic, never an objective.** The maker objective is
   `Σ_placements [ P_f(x) · (E[markout | fill, x] − f_M) ] − (1−P_f(x))·κ`. A policy that
   raises `P_f` while lowering that sum is worse. Encode by *type*: the optimiser accepts
   only an objective function returning expected **value**, and `P_f` alone is not of that
   type.
2. **A strictly positive conditional-markout precondition.** Reject any maker placement
   whose modelled `E[markout | fill] − f_M` is not strictly positive, **regardless of
   `P_f`**. A high-fill placement with negative conditional markout is not a "good fill
   with bad luck"; it is the adversely selected fill the whole of 6.5 is about. This
   precondition is what makes "never optimise for fill rate" enforceable rather than
   aspirational.
3. **A mandatory monotonicity guard in evaluation.** Regress realised markout on predicted
   `P_f` across placements. Under the Albers/Handa–Schwartz mechanism, the *unconditional*
   slope should be **negative** — that is the expected, healthy signature. A policy is
   fill-chasing when it has selected placements that sit at the wrong end of that curve.
   Report the slope and the mean conditional markout by `P_f` decile on **every**
   evaluation, and fail the evaluation if the highest-`P_f` decile is being preferentially
   selected without a reversal signal justifying it.
4. **Ban naive market making explicitly.** Two-sided quoting at the touch with
   requote-on-move is the exact strategy Albers et al. measured at Sharpe −109. It should
   be named as a prohibited baseline in the paper ledger so that nobody rediscovers it.
5. **Report maker P&L as a bracket, always** (6.3). A single-point maker P&L conceals an
   unfalsifiable queue-position assumption.
6. **Prefer taker for the first prospective measurement.** Its cost is exact from the
   visible ladder and carries no unidentifiable parameter. It is more expensive and more
   honest, and for a first trustworthy paper P&L, honest beats cheap.

## 8. Does NOT transfer to AMMs

Solana memecoins trade on constant-product AMMs and bonding curves. There is no book, no
queue, no resting order, and no maker/taker distinction in the sense used above. Anyone
porting this schema sideways should read this list first.

**Structurally absent — these constructs have no referent on an AMM:**

| Construct | Why it does not exist |
|---|---|
| Queue position, queue-ahead, FIFO priority | There is no queue. A swap executes atomically against the curve at the moment of inclusion. |
| Queue imbalance `I` | There is no two-sided book. The nearest analogue, the reserve ratio, **is the price itself**, so it is not a predictor of the price — it is the thing being predicted. |
| OFI, MLOFI, integrated OFI | These are built from **placements and cancellations**. An AMM emits neither. |
| Cancellation intensity | Nothing to cancel. |
| Order-arrival intensity | No orders arrive. LP add/remove events exist but are a different, far slower process. |
| Liquidity replenishment / resilience | The curve does not deplete and refill; it reprices continuously. A pool's depth changes only when an LP acts. |
| Microprice | Requires a bid, an ask and a spread to correct for. There is no bid-ask bounce to de-noise; the AMM spot price is the marginal price, already unique. |
| Effective spread, realised spread | There is no quoted spread to measure against. |
| Bid/ask, spread, spread regime, depth slope at the touch | No touch. |
| Sweep detection | A "sweep" is multi-level book consumption. One swap consumes one continuous curve. |
| `P(fill \| queue position)` | An LP is not "filled". Any trade crossing the LP's range trades against it, unconditionally. Fill probability is not a decision variable. |
| Maker/taker fee differential | LPs earn a pool fee; swappers pay it. There is no rebate/fee choice at order time. |
| Stale-book state, book publishability, sequence-gap unpublishing | Chain state is canonical and totally ordered by slot. Different failure model entirely. |

**The information asymmetry, stated once more because it is the important one:** the
strongest verified result in this document — that book events explain short-horizon price
moves roughly twice as well as trades (§3.1, R² 65% vs 32%) — **is unavailable on an AMM
by construction.** An AMM emits only swap flow, which is the weaker variable. There is no
clever reconstruction that recovers the stronger one, because the information is never
created. A CLOB track and an AMM track are therefore not two implementations of one
design; they have different achievable ceilings.

**Transfers, but with different mathematics:**

| Construct | How it changes |
|---|---|
| `C_execution(s)` | Becomes **exact and closed-form** from pool reserves and the curve — *easier* than on a CLOB, not harder. But add priority fees and the possibility of a failed/reverted transaction, which have no CLOB analogue. |
| Price impact | Deterministic given reserves, not a fitted `β`. The CKS `β ≈ c/D` relation is replaced by the curve's own algebra. |
| Adverse selection | Transfers in spirit, as **loss-versus-rebalancing (LVR)** for LPs. Entirely different derivation; do not reuse any number from §6.5. |
| Signed trade imbalance, trade count, volume acceleration | Transfer directly, and are among the few features that do. |
| Realised volatility, vol-of-vol, normalised volatility | Transfer, though the `p(1−p)` normalisation is Kalshi-specific and must be dropped. |
| Trade toxicity / VPIN | Transfers; taker direction is inferable from the swap direction. |
| Regime labels, time-of-day, event-time | Transfer as concepts, with different cut points. |
| Latency risk | Transfers as a *concept* and nothing more: the CLOB version is stale-book risk; the AMM version is slot inclusion, MEV sandwiching, and priority-fee competition. |

**One direction that transfers and is worth carrying over:** the discipline of §6.2 — that
execution cost comes from the actual state of the venue at decision time, and that
"cannot fill" is a typed outcome rather than an extrapolated price — is venue-independent
and should be the shared contract between the two tracks.

---

## 9. Realism pass — what our collector can actually feed

**A feature schema that cannot be populated is worse than no schema**, because it makes a
plan look complete when it is not. This section is the check.

### 9.1 What Kalshi exposes, and what we have evidence for

Read from `docs/milestones/KALSHI-LIVE-TAPE-COLLECTOR-001.md` (design-only; **no
production collector code exists yet** — `app/realtime/kalshi.py:366` declares a
`Transport` interface whose only implementation is `FixtureTransport`), plus
`app/realtime/{kalshi,book,fixedpoint,canonical}.py`, plus
`docs/KALSHI_DEMO_READONLY_VALIDATION_2026_08.md`.

**Channel allowlist, closed in code** (`app/realtime/kalshi.py:99-107`):
`orderbook_delta`, `ticker`, `trade`, `market_lifecycle_v2`. Everything else — including
every private channel and every channel a future venue release adds — is refused at frame
construction. All four are entitled to an exactly-`["read"]` key: **VERIFIED on the demo
wire** (each received a `subscribed` ack on its own sid).

| Channel | Payload | Evidence status |
|---|---|---|
| `orderbook_delta` (snapshot) | `yes_dollars_fp[]`, `no_dollars_fp[]` — **full ladders**, `[price, count]` pairs, both already YES-scaled under `use_yes_price=true` | **VERIFIED on our wire.** Empty book omits both keys |
| `orderbook_delta` (delta) | `side` ∈ {yes,no}, `price_dollars`, **`delta_fp` — one signed net change at one price level** | **VERIFIED on our wire** |
| `ticker` | last price, `yes_bid`/`yes_ask` with sizes, volume, open interest, last trade size | Bid/ask + sizes **VERIFIED on our wire**; the fuller field list is INFERRED from venue docs |
| `trade` | `trade_id`, `market_ticker`, `yes_price`, `no_price`, `count`, **`taker_side`**, `ts` | **Entitlement VERIFIED** (sid 3 acked). **Payload shape UNVERIFIED on our wire** — INFERRED from a third-party mirror of the venue docs. The demo capture was 4 records and contained no trade print |
| `market_lifecycle_v2` | open/close/settle transitions | Entitlement VERIFIED; payload UNVERIFIED on our wire |

**Structural facts that shape everything in section 9.2:**

1. **The book is FULL DEPTH.** Both ladders arrive whole and are maintained as
   `dict[price_units, contract_units]` (`app/realtime/book.py:212-213`). On a 1¢ grid there
   are at most 99 levels. **Multi-level features are not depth-limited by the feed** — a
   genuine advantage over the L2-top-N feeds most of the literature uses.
2. **It is L2, not L3.** No order IDs, no per-order events, one net `delta_fp` per level.
3. **`seq` is subscription-global, not per-market** (VERIFIED on the wire), and
   **non-orderbook frames consume a sequence number** — an `error` frame took seq 4 between
   deltas at seq 3 and seq 5. Any feature pipeline that re-derives ordering must use
   `SubscriptionState`, not its own counter.
4. **`use_yes_price=true` means the NO ladder is ALREADY YES-scaled — no complement is
   applied.** This was the most serious defect found in demo validation: the earlier code
   complemented it, producing an ask that was uncrossed, plausible, and **two cents wrong on
   every quote**. Any independent feature implementation must not re-introduce it.
5. **Venue timestamps are not uniform:** `ts` is an ISO string on `orderbook_delta` and
   epoch **seconds** on `ticker`; `ts_ms` is unambiguous on both and is read first.
6. **Only measured rate on record: 4 records over ~2 minutes (DEMO).** Everything about
   sample sizes in this document is conditioned on a production rate we have never measured.

### 9.2 Feature-by-feature mapping

Legend: **YES** = computable from an archived tape with the default subscription;
**YES+trade** = requires `trade` in the subscription (which is **not** the planned default —
the milestone's default is `orderbook_delta` + `ticker`, and `trade` requires an explicit
`--channels`); **DERIVED-APPROX** = computable but only under a stated assumption;
**NO** = not obtainable from Kalshi at any subscription.

| Feature | Feasible? | Notes |
|---|---|---|
| `best_bid/ask`, sizes, `spread`, `mid`, `queue_imbalance`, `weighted_mid` | **YES** | Already implemented (`book.py:464-501`) |
| `depth[m]`, `cum_depth_within(k)`, `depth_slope`, `depth_convexity`, `book_pressure_k` | **YES** | Full ladder available; ≤ 99 levels |
| `fill_cost_curve(s)` / `C_execution` | **YES, exactly** | The strongest feature in the schema. The visible ladder is the whole ladder |
| `microprice` | **YES**, with a fit | Needs a pooled `g(I,S,price_regime)` table; absent in `WIDE` books by design (§4.3) |
| `OFI`, `MLOFI[m]`, `integrated_OFI` | **YES** | Our deltas are close to the CKS/XGH primitive. **But `e_n` must be computed on the reconstructed best-quote sequence, not by summing `delta_fp`** — a delta at a non-best level that *becomes* best changes the estimator (§3.2 cascade rule) |
| `signed_trade_imbalance` | **YES+trade** | **Better than the literature's version: `taker_side` is given, so no Lee–Ready classification error.** Exact, not estimated |
| `trade_count`, `trade_volume` | **YES+trade** | |
| `order_arrival_intensity` | **DERIVED-APPROX** | We observe *positive net level changes*, not order arrivals. If the venue coalesces two arrivals into one delta, we undercount. **UNVERIFIED whether Kalshi coalesces — this is a measurement task for the first live session** |
| `cancellation_intensity` | **DERIVED-APPROX, and needs `trade`** | A negative delta is a cancellation *or* a trade consumption. Separating them requires joining book deltas to trade prints by `(price, size, timestamp)` — approximate at ms resolution, and ambiguous when several events collide. Without `trade` subscribed, absence reason `REQUIRES_JOIN_UNAVAILABLE` |
| `sweep_flag`, `sweep_depth_ticks` | **YES+trade** | On a 1¢ grid a sweep spans few levels; the size test (print > pre-trade touch size) is the more useful one |
| `replenishment_time`, `resilience_ratio` | **YES** | Book-only; no trade join needed |
| `effective_spread`, `realized_spread(h)`, `price_impact(h)`, `markout(h)` | **YES+trade** | All need the trade print's price and direction |
| `beta_impact` (`ΔP = β·OFI`, `β ≈ c/D`) | **YES**, with a fit | Sample size is the constraint, not data availability |
| `toxicity_vpin` | **YES+trade**, but expect `WINDOW_UNDERFILLED` | Volume buckets fill glacially on a dormant contract. Likely unpopulated for most of the universe |
| `realized_vol`, `normalized_vol` | **YES** | Report event-time and clock-time separately |
| `vol_of_vol` | **YES in principle, expect absent in practice** | Needs ≥ 10 sub-windows each meeting their own floor. On most contracts this will never populate |
| `volume_acceleration` | **YES** (book events) / **YES+trade** (traded volume) | |
| `event_rate_ewma[τ]` | **YES** | O(1), defined from the first event, no fit. The Hawkes substitute (§5.6) |
| `spread_regime`, `depth_regime`, `activity_regime`, `price_regime` | **YES** | The conditioning variables the whole document depends on |
| `lifecycle_state` | **YES**, needs `market_lifecycle_v2` subscribed | Not in the planned default subscription |
| **`time_to_close`** | **NO from WebSocket** | Scheduled close time is REST market metadata. **The collector milestone explicitly has no REST reconciliation loop** (`reconcile_with_rest` stays a replay-time function). This is a real gap — see 9.4 |
| `time_of_day`, `day_of_week` | **YES** | From the envelope's receive clock |
| `event_clock_phase` | **NO** for most contract families | Game clock / release schedule is external. `VENUE_DOES_NOT_EXPOSE` |
| `data_age_us`, `book_staleness`, `is_publishable`, `subscription_generation` | **YES** | All already on the envelope / router (`book.py:76-80`, `archive.py:633`) |
| `observation_gap` | **YES**, from collector metrics | The collector milestone records `disconnected_at`/`reconnected_at` per reconnect precisely to close the replay lane's acknowledged blind spot |
| `clock_offset_bound` | **NO today** | Explicitly NOT MEASURED (`app/cli.py:717-722`). Biases `data_age_us` by an unknown sign |
| **Queue position at fill** | **NO** | No order IDs; the `trade` channel publishes aggregate prints, not per-maker fills in priority order. Albers et al.'s Binance recovery technique does not port. See §6.3 |
| **Own-order state** | **NO, and correctly so** | `fill`, `user_orders`, `market_positions` are in `FORBIDDEN_CHANNELS` and are refused at frame construction. Not a gap — a boundary |
| **Order-level cancel/replace attribution** | **NO** | L2 only |
| **Hidden / iceberg liquidity** | **UNVERIFIED whether any exists** | Assumed zero, which is the conservative direction (§6.2) |

### 9.3 Features that must NOT go in the schema

A feature whose only possible answer is `VENUE_DOES_NOT_EXPOSE` should not be a column.
Excluded on that ground:

- **Queue position at fill** and anything derived from it (queue-position deciles,
  front-of-queue indicators, priority-adjusted fill models). Present instead as the
  **bracket** of §6.3, which is a different and honest object.
- **Order-level cancellation attribution** (cancel-ahead vs cancel-behind), and any
  "cancellation aggressiveness by trader type" feature.
- **Any own-order, own-position, or own-fill feature.** Structurally forbidden by the
  capability boundary, not merely unavailable.
- **Cross-venue features** (Polymarket price, sportsbook line). Not forbidden, but out of
  scope for this track and not producible by this collector — putting them in the schema
  now would make it look populated when it is not.
- **`event_clock_phase`** as a general column. Admit it only per contract family, once a
  family-specific source exists.

### 9.4 The three gaps that need a decision, in priority order

1. **`trade` is not in the planned default subscription — and a large fraction of this
   schema depends on it.** Without `trade`: no signed trade imbalance, no effective or
   realised spread, no price impact, no markout, no toxicity, no sweep detection, and
   `cancellation_intensity` degrades to `REQUIRES_JOIN_UNAVAILABLE`. That is most of
   schema groups C and D. **Recommendation: `trade` should be in the default subscription
   for any session intended to feed this schema.** It is already on the allowlist and
   already entitled, so this is a configuration decision, not a boundary change. The cost
   is additional archive volume against unmeasured rotation constants — which the
   collector's own measurement plan is designed to settle. Flag it as a decision the
   collector milestone should take explicitly rather than inherit from a default.
2. **`time_to_close` has no source in this design.** For a contract that expires, time to
   expiry is arguably the *most* important conditioning variable there is, and it is not on
   the WebSocket. Options: subscribe `market_lifecycle_v2` and derive what we can; or
   accept a one-shot REST metadata fetch at session start (a read-only GET, inside the
   existing boundary, but explicitly outside the collector milestone's stated non-goals).
   **Do not silently add a REST loop** — this needs a decision, not an implementation.
3. **The production event rate is unknown.** Every sample-size claim in this document is
   conditioned on it. The only measurement on record is 4 records in ~2 minutes on DEMO.
   Until the collector's measurement lane runs, treat every "expect this to be absent"
   judgement here as a hypothesis.

### 9.5 What is genuinely strong about our position

Worth stating, because the section above is mostly caveats:

- **Full-depth ladders.** Most published microstructure work uses top-N L2 feeds and treats
  depth beyond level 10 as unobservable. We get all ≤ 99 levels. `C_execution` is therefore
  **exact**, not modelled — which is precisely what the mandate demands of it, and which
  most practitioners cannot achieve.
- **Exact taker direction.** `taker_side` removes Lee–Ready classification error from every
  trade-sign feature. A meaningful chunk of the empirical microstructure literature spends
  its error budget on that classifier.
- **A digest-chained, replay-deterministic archive already exists.** The feature pipeline
  can be validated by replaying evidence rather than by re-collecting it — proven by the
  demo replay producing identical digests, checksums and book state with
  `external_calls=0`.
- **Sequence integrity is settled at the subscription level and already fails closed.** The
  `BOOK_UNPUBLISHED` absence mode of §2.2 is not something we have to build; it falls out
  of `SubscriptionRouter` (`book.py:733-795`).

## 10. Citation ledger

### 10.1 Checked and correct

| # | Citation | URL | Status |
|---|---|---|---|
| C1 | Cont, Kukanov, Stoikov, "The Price Impact of Order Book Events", arXiv:1011.6402; *J. Financial Econometrics* 12(1):47–88 (2014), DOI 10.1093/jjfinec/nbt003 | <https://arxiv.org/abs/1011.6402> | **VERIFIED** — ID, authors, venue, formula, all numbers |
| C2 | Gould, Bonart, "Queue Imbalance as a One-Tick-Ahead Price Predictor in a Limit Order Book", arXiv:1512.03492; *Market Microstructure and Liquidity* 2(2):1650006 (2016) | <https://arxiv.org/abs/1512.03492> | **VERIFIED** — the mandate's arXiv ID is correct |
| C3 | Xu, Gould, Howison, "Multi-Level Order-Flow Imbalance in a Limit Order Book", arXiv:1907.06230; *MML* 4(3&4) (2020) | <https://arxiv.org/abs/1907.06230> | **VERIFIED** |
| C4 | Cont, Cucuringu, Zhang, "Cross-impact of order flow imbalance in equity markets", arXiv:2112.13213; *Quantitative Finance* 23(10):1373–1393 (2023) | <https://arxiv.org/abs/2112.13213> | **VERIFIED** |
| C5 | Albers, Cucuringu, Howison, Shestopaloff, "The Market Maker's Dilemma: Navigating the Fill Probability vs. Post-Fill Returns Trade-Off", arXiv:2502.18625 | <https://arxiv.org/abs/2502.18625> | **VERIFIED** — matches the mandate's description on every count |
| C6 | Bacry, Mastromatteo, Muzy, "Hawkes processes in finance", arXiv:1502.04592 | <https://arxiv.org/abs/1502.04592> | **VERIFIED** |
| C7 | Bacry, Muzy, "Hawkes model for price and trades high-frequency dynamics", arXiv:1301.1135 | <https://arxiv.org/abs/1301.1135> | **VERIFIED** |
| C8 | Large, "Measuring the resiliency of an electronic limit order book", *J. Financial Markets* 10(1):1–25 (2007) | DOI 10.1016/j.finmar.2006.09.001 | **VERIFIED** — and it is a ten-variate Hawkes model, as recalled |
| C9 | Filimonov, Sornette, "Apparent criticality and calibration issues in the Hawkes self-excited point process model", arXiv:1308.6756; *Quant. Finance* 15(8) | <https://arxiv.org/abs/1308.6756> | **VERIFIED** — source of the spurious-criticality result |
| C10 | Lallouache, Challet, "The limits of statistical significance of Hawkes processes fitted to financial data", arXiv:1406.3967 | <https://arxiv.org/abs/1406.3967> | **VERIFIED** |
| C11 | Achab, Bacry, Muzy, Rambaldi, "Analysis of order book flows using a nonparametric estimation of the branching ratio matrix", arXiv:1706.03411; *QF* 18(2) | <https://arxiv.org/abs/1706.03411> | **VERIFIED** — source of ρ(Γ) ≈ 0.98 |
| C12 | Hardiman, Bercot, Bouchaud, "Critical reflexivity in financial markets", arXiv:1302.1405; *EPJ B* 86:442 | <https://arxiv.org/abs/1302.1405> | **VERIFIED** |
| C13 | Kirchner, "An estimation procedure for the Hawkes process" (INAR(p) ≈ VAR(p)), arXiv:1509.02017 | <https://arxiv.org/abs/1509.02017> | **VERIFIED** |
| C14 | Ozaki, "Maximum likelihood estimation of Hawkes' self-exciting point processes", *AISM* 31:145–155 (1979) | DOI 10.1007/BF02480272 | **VERIFIED** — the O(N) recursion |
| C15 | Muni Toke, Pomponio, "Modelling Trades-Through in a Limit Order Book Using Hawkes Processes" (2012), HAL hal-00745554 | <https://hal.science/hal-00745554v1> | **VERIFIED** — but bivariate only, and finds cross-excitation *weak* |
| C16 | "Forecasting High Frequency Order Flow Imbalance using Hawkes Processes", arXiv:2408.03594; *Comp. Economics* 67(1) | <https://arxiv.org/abs/2408.03594> | **VERIFIED** — the SPA-test table; note the thin evidence base (one trading day) |
| C17 | Handa, Schwartz, "Limit Order Trading", *J. Finance* 51(5):1835–1861 (1996) | DOI 10.1111/j.1540-6261.1996.tb05228.x | **VERIFIED** |
| C18 | Glosten, Milgrom, "Bid, ask and transaction prices in a specialist market with heterogeneously informed traders", *JFE* 14(1):71–100 (1985) | DOI 10.1016/0304-405X(85)90044-3 | **VERIFIED** |
| C19 | Rambaldi, Filimonov, Lillo, "Detection of intensity bursts using Hawkes processes", arXiv:1610.05383 | <https://arxiv.org/abs/1610.05383> | **VERIFIED** |
| C20 | Blakely, "High resolution microprice estimates … hyperdimensional vector Tsetlin Machines", arXiv:2411.13594 (2024) | <https://arxiv.org/abs/2411.13594> | **VERIFIED as existing.** Treat the result as a lead: 6 days, 2 tickers, single author |

### 10.2 Corrections — citations or common beliefs that do NOT check out

| # | Claim | Correction |
|---|---|---|
| X1 | arXiv:1512.03492 is Lipton/Pesavento/Sotiropoulos, or Cartea/Donnelly/Jaimungal | **Wrong.** It is **Gould & Bonart**. (The mandate's own attribution was correct; this corrects a widespread misattribution.) |
| X2 | Gould & Bonart used **LSE** stocks | **Wrong.** **Nasdaq**, via LOBSTER, 10 US stocks, all of 2014 |
| X3 | Gould & Bonart use `I = Q_bid/(Q_bid+Q_ask) ∈ [0,1]` | **They use the signed `(Q_b − Q_a)/(Q_b + Q_a) ∈ [−1,1]` form** and explicitly contrast it with the `[0,1]` form in a footnote. Stoikov's micro-price uses the `[0,1]` form. **These are different variables and mixing them silently is a real bug.** The schema names which is which |
| X4 | Gould & Bonart report a pseudo-R² | **No pseudo-R² appears anywhere in the paper.** The metrics are AUC and mean squared residual |
| X5 | Stoikov's micro-price is on arXiv | **It is not.** *Quantitative Finance* 18(12):1959–1966 (2018), DOI 10.1080/14697688.2018.1489139, and SSRN 2970694. Code: <https://github.com/sstoikov/microprice> |
| X6 | The micro-price is `lim_{k→∞} E[M_{t+k}]` in clock time | **Wrong, and the error matters.** The limit is over successive **mid-price-change events** `τ_i`. That is why it is horizon-independent |
| X7 | Lu & Abergel, "High-dimensional Hawkes processes for limit order books", arXiv:1706.xxxxx | **The paper is real; the arXiv ID is not.** *Quantitative Finance* 18(2):249–264 (2018), DOI 10.1080/14697688.2017.1403142; preprint on HAL, **not arXiv**. arXiv:1706.03411 is a *different* paper (C11) in the same issue at adjacent pages. **Do not cite Lu & Abergel with an arXiv number** |
| X8 | Cont et al. (2014) showed deeper book levels do not matter | **Superseded and explained.** Xu et al. show the opposite; the difference is OLS + in-sample R² versus Ridge + out-of-sample RMSE under severe multicollinearity (§3.2) |
| X9 | Deeper MLOFI coefficients decay quickly | **Refuted for large-tick names, where they slightly *increase* with level** (§3.2) |

### 10.3 Sourcing caveats — read from a secondary or primary-adjacent source

| # | Item | Caveat |
|---|---|---|
| S1 | The Stoikov micro-price construction (§4.1) | Journal text and SSRN are **paywalled and were not read.** Verified against Stoikov's own conference deck (same theorems, same BAC/CVX March-2011 data): <https://www.ma.imperial.ac.uk/~ajacquie/Gatheral60/Slides/Gatheral60%20-%20Stoikov.pdf>. Items marked † in §4.1 should be confirmed against the journal text |
| S2 | Cont et al. (C1) and Gould–Bonart (C2) numeric tables | Read from the **arXiv preprints**; the published JFE (2014) and MML (2016) versions are paywalled. CKS's arXiv v3 predates the journal version by ~3 years, so table values could in principle have been revised in review |
| S3 | **Kalshi fee formulas** — taker `ceil(0.07·C·P·(1−P))`, maker `ceil(0.0175·C·P·(1−P))` | **INFERRED from secondary sources only** (<https://pm.wiki/learn/kalshi-fees-explained>, <https://marketmath.io/platforms/kalshi>, <https://whirligigbear.substack.com/p/makertaker-math-on-kalshi>). Sources agree on the taker coefficient and the 1.75¢ peak; they disagree on whether maker fees apply universally or only to some markets. **§6.6 and §7 both depend on these numbers. Read the venue's own schedule before relying on any threshold in this document, and re-read on a cadence — a schedule change silently invalidates them** |
| S4 | Kalshi `trade` and `ticker` channel payload field lists (§9.1) | **INFERRED from a third-party mirror of the venue docs.** Field naming is consistent with the `*_fp` / `*_dollars` conventions our own code already handles, which is corroborating but not proof. **UNVERIFIED on our own wire** — the demo capture was 4 records and contained no trade print. Verify in the first live session |
| S5 | Hawkes finite-sample bias floor of "200–400 events" | **Primary source not opened.** Order of magnitude only, and it is a *univariate* figure |
| S6 | Lu & Abergel's "very poor convexity properties of the MLE" | Quoted from indexed abstract text, not from the publisher's page |

### 10.4 Stated non-findings

Recorded because an absence of evidence is itself information:

- **No paper was found benchmarking Hawkes-derived order-flow features against EWMA /
  rolling-count intensity features for short-horizon price prediction, out of sample.** After
  15+ years of LOB Hawkes work, that gap is notable. It is not a negative result and is not
  presented as one.
- **No Hawkes literature exists on any prediction market or betting exchange** — Kalshi,
  Polymarket, Betfair, sports exchanges. Nothing found.
- **No published fill-probability model was found for a prediction-market CLOB.**

---

## 11. Open questions and next steps

### 11.1 Decisions needed before any implementation

| # | Question | Why it blocks | Owner |
|---|---|---|---|
| Q1 | **Should `trade` be in the default collector subscription?** | Most of schema groups C and D are unpopulated without it (§9.4.1). It is already allowlisted and already entitled, so this is configuration, not a boundary change — but it changes archive volume against unmeasured rotation constants | KALSHI-LIVE-TAPE-COLLECTOR-001 |
| Q2 | **Where does `time_to_close` come from?** | Arguably the most important conditioning variable for an expiring contract, and it is not on the WebSocket. A one-shot read-only REST metadata fetch is inside the capability boundary but outside the collector milestone's stated non-goals. **Do not add a REST loop silently** | Needs an explicit decision |
| Q3 | **Verify the Kalshi fee schedule from the venue's own publication.** | §6.6 and §7 are quantitatively built on it, and it is currently INFERRED (S3) | Before any EV threshold is used |
| Q4 | **Confirm the `trade` and `market_lifecycle_v2` payload shapes on our own wire.** | S4. Cheap — one live session with `--channels` set | First live session |
| Q5 | **Does Kalshi coalesce multiple order events into one `delta_fp`?** | Determines whether `order_arrival_intensity` is a count or an undercount (§9.2) | First live session, measurable |
| Q6 | **Does hidden/iceberg liquidity exist on Kalshi?** | If it does, `C_execution` is overstated. Assumed zero, the conservative direction | Measurable by comparing trade prints against pre-trade visible depth |

### 11.2 Measurements this document is waiting on

1. **The production event rate**, per contract and across the universe. Every sample-size
   claim here is conditioned on a number we have never measured (only 4 records over ~2
   minutes on DEMO). Until then, treat every "expect this to be absent" judgement as a
   hypothesis.
2. **The empirical distribution of `spread_regime`** across the Kalshi universe. §3.4 and
   §4.3 restrict most modelling to `TOUCHING`/`NARROW`. If those regimes are rare, the
   addressable universe is much smaller than the contract count suggests, and that should
   be known before anything is built.
3. **`E[adverse selection]` as a fraction of `normalized_vol`** (§6.5). The port of the
   Albers magnitudes to Kalshi is INFERRED and needs its own estimate.
4. **The Hawkes-vs-EWMA horse race** (§5.6.3) — a day's work against the archive once a
   tape exists, and it settles the question with our own data. **It is a measurement, not
   a modelling commitment.**
5. **The clock-offset bound** (§6.7). Currently NOT MEASURED, biasing `data_age_us` by an
   unknown sign.

### 11.3 What this document deliberately does not do

- It designs no production code, proposes no order placement, and enables nothing. The
  capability boundary (`OBSERVE_ONLY`, read-only GETs, the closed channel allowlist) is
  unchanged and untouched by anything here.
- It does not produce `p̂`. The forecast lane owns `E[terminal value]`; this document owns
  everything subtracted from it.
- It does not propose a strategy. §6.6(a) is the reason: the best-case documented
  microstructure edge does not cover the venue's round-trip fee, so the honest output of
  this track is an **execution-quality layer** for positions taken on forecast grounds —
  not a signal.

### 11.4 The three findings to carry forward

1. **The fee is the dominant term, and it is larger than the documented signal.** At
   P = 0.50, round-trip maker fees (0.875¢) exceed the 0.70¢ expected value of a *perfect*
   one-tick-ahead prediction at Gould & Bonart's strongest measured effect size; round-trip
   taker fees (3.50¢) are five times it. Microstructure on Kalshi is an execution-cost
   tool, not an alpha source. Any proposal that says otherwise should be measured against
   this comparison.
2. **Maker execution must never be optimised for fill rate, and the reason is mechanical
   rather than statistical.** Under a requote-on-move policy, `P(fill | favourable next
   move) = 0` and `P(fill | adverse next move) = 1` **by construction**, on any CLOB. §7
   gives six structural encodings, of which the load-bearing one is the strictly-positive
   conditional-markout precondition applied *regardless* of `P_f`.
3. **Queue position at fill is unobtainable on Kalshi**, and the assumption needed to
   model it cannot be validated under `OBSERVE_ONLY`. Therefore **maker paper P&L must be
   reported as a bracket**, and the first prospective paper P&L should be **taker-only** —
   more expensive, exactly computable, and free of unidentifiable parameters.
