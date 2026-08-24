# MARKET-MICROSTRUCTURE-EDGE-001 — preregistration

**Status: PREREGISTERED, NOT RUN. Awaiting Eric's decision before execution.**
Written 2026-08-19.

Read-only. No capital, no orders, no venue writes. This experiment measures
information, not profit, and cannot authorise a trade.

**Depends on:** `PROD-ACTIVITY-PROFILE-001` (**COMPLETE 2026-08-22**; its
`universe.json` static-panel premise is **RETIRED by Amendment 2**),
`MARKET-STATE-FABRIC-v1` (feature spec), KALSHI-TAPE-MEASUREMENT-CONTRACT-001
§16 (measured production facts).

---

## 1. The question

Does the order book carry information about near-future price **beyond what the
current best quote already contains** — and if so, does any of it survive the
cost of acting on it?

This is deliberately narrower than "can we make money." It is the first question
whose answer we do not already know.

## 2. The prior, stated honestly before measuring

**The correct prior is that net economic edge is ≤ 0.** That is not pessimism;
it is what this repository has already measured:

* QUANT-DECISION-KERNEL-001 established `g(f*) = KL(p‖q)` — tradable growth and
  log-score advantage over the market are the *same quantity*. We have never
  measured positive forecast skill against the market.
* EDGE-DISCOVERY-001 ran four preregistered experiments and all four failed. The
  one real, replicating effect it found (E2's lead-lag) was **uneconomic at 70%
  of the cost floor**.

So the expected outcome of this experiment is "a real effect, too small to
trade." Preregistering that expectation is what stops us from discovering it and
calling it a surprise. **A null result here is a successful experiment**, and the
stopping rule in §8 is binding.

## 3. Design: M0 versus M1, with the known effect as the control

Two nested models on the fabric of §3–§4 of MARKET-STATE-FABRIC-v1:

| model | features |
|---|---|
| **M0** | the 12 state-only features |
| **M1** | M0 **plus** the flow block (Δ ∈ 1 s, 5 s, 30 s) |

**Target.** `Δmid(t, t+h) = mid(t+h) − mid(t)`, in cents, for horizons
**h ∈ {1 s, 5 s, 30 s, 300 s}**. The **primary horizon is 30 s**, fixed now; the
other three are secondary and carry the correction in §7.

**Three comparisons, in this order:**

1. **M0 vs. a mid random walk** (`Δmid = 0`). This is the **positive control**,
   not a finding. The microprice-beats-mid effect is well established in
   microstructure literature; if M0 fails to beat a random walk, the pipeline is
   broken and no other result may be read. (Doctrine 7.)
2. **M0 vs. the microprice** as a single-feature baseline. The microprice is the
   strong contemporaneous-market baseline. Beating it is the first non-trivial
   claim.
3. **M1 vs. M0** — **the actual research question.** Does order flow add
   information beyond static book state?

**Metric.** Out-of-sample R² against each baseline, plus directional accuracy
conditional on a predicted move exceeding the cost floor.

## 4. Splits, and the leakage controls

**Purged, embargoed, walk-forward by wall-clock time.** Train on window *k*,
test on window *k+1*, never the reverse. The embargo between train and test is
**≥ the maximum horizon (300 s)**, so no training row's label can overlap a test
row's features.

**Clustering.** Rows are not independent. Standard errors are **clustered by
market**, and confidence intervals come from a **block bootstrap** with block
length ≥ 300 s — the same discipline used for the event-clustered SEs in
EDGE-DISCOVERY-001.

**Effective sample size is reported, not row count.** The design yields
6 windows × 25 min × 40 markets ≈ **360,000 rows at 1 Hz**, but at a 30 s
horizon that is closer to **~12,000 quasi-independent blocks**. The writeup
reports the block count. Quoting 360,000 would be a lie of arithmetic.

**The look-ahead we are most likely to commit** is the trade-sid boundary
(MARKET-STATE-FABRIC-v1 §4). Its mitigation — a pre-declared lag on every
trade-derived feature — is fixed **before** any model is fitted, and the
experiment reports M1 results at the declared lag **and** at double it. If the
result depends on the lag, it is a timing artefact and is reported as one.

## 5. Noise floor (doctrine 4)

Two null arms, run identically to the real ones:

* **Shuffled labels.** Targets permuted within market. Any apparent R² here is
  the pipeline's own noise floor, and every reported effect is quoted **beside**
  it.
* **Shifted features.** Features from market *A* against labels from market *B*
  in the same window. This catches a subtler failure — a "signal" that is really
  a shared time-of-day or venue-wide effect rather than anything about the book.

No effect smaller than the larger of these two floors may be described as real,
regardless of its p-value.

## 6. The economic gate (doctrine 2)

Statistical significance authorises **nothing**. An effect graduates to
"economically relevant" only if the predicted move exceeds, at the row's own
state:

> **half-spread + round-trip fees**

Both are already carried in the fabric (`spread`, `mid`). The Kalshi fee formula
must be **verified against venue documentation and against realised fills before
it is used** — not assumed from memory. If the fee cannot be verified, the
economic gate is reported as **unevaluated** rather than passed.

Every effect size in the writeup is printed with the cost floor beside it. A
0.4-cent predicted move on a 2-cent spread is a measurement of the spread.

## 7. Multiple testing

Twelve primary cells (3 comparisons × 4 horizons). **Benjamini–Hochberg at
FDR 10%** across all twelve, computed once, on the pre-declared set. No cell may
be added after seeing results; a cell that looks interesting later starts a new
preregistration with a new name.

## 8. Stopping rule (binding)

* If the **positive control fails** (M0 does not beat a mid random walk at the
  primary horizon), the run is **void** — a pipeline defect, not a finding — and
  nothing else is reported.
* If **M1 does not beat M0** out-of-sample at FDR 10% on the primary horizon,
  **order flow is declared non-additive over static book state, and this lane
  stops.** No re-specification, no extra features, no "one more horizon."
* If M1 **does** beat M0 but the effect is **below the §6 cost floor**, the
  finding is recorded as **real but uneconomic** — the E2 outcome — and the lane
  stops for capital purposes while remaining open for research.
* Only an effect that beats M0, clears FDR, exceeds both §5 noise floors, and
  survives the §6 cost floor may be proposed for a prospective test. Even then it
  authorises a **preregistered forward observation**, never a trade.

## 9. What this experiment cannot conclude

* Nothing about markets outside the frozen universe, or times of day outside the
  six windows.
* Nothing about **execution** — it never models queue position, partial fills,
  latency to the venue, or adverse selection against a resting order. A
  predicted move is not a filled order.
* Nothing about **regime stability**: two weekdays is not a regime study, and any
  effect found is explicitly untested out-of-regime.
* Nothing that licenses capital. The gate to capital is a separate, prospective,
  preregistered milestone that does not yet exist.

---

## Amendment 1 — 2026-08-19, BEFORE ANY DATA WAS COLLECTED

**No change required, recorded rather than left silent.** The peak-rate
estimator was corrected on 2026-08-19 (see PROD-ACTIVITY-PROFILE-001 Amendment 1:
`peak_1s_sliding` is now the primary capacity metric, and the biased
`frames_per_second_peak_1s` field is removed).

This preregistration defines no quantity in terms of a peak or load statistic —
its thresholds are statistical (FDR 10%), temporal (300 s embargo) and economic
(half-spread plus fees). It therefore inherits the correction through the frozen
`universe.json` it consumes and needs no edit of its own.

Recorded explicitly because "we checked and nothing needed changing" and "we did
not check" are indistinguishable in a document that stays silent.

---

## Amendment 2 — 2026-08-22: the static panel is retired, before any alpha was observed

`PROD-ACTIVITY-PROFILE-001` completed on 2026-08-22 (six windows, all `VALID`,
§6 never fired, every sequenced SID contiguous). Its result — recorded at
`1f70a9e`, and produced **without reading a single microstructure alpha
quantity** — falsifies a premise this preregistration was built on.

**No alpha result exists and none was inspected.** Everything below is derived
from wire-activity and capacity evidence only. That ordering is what makes this
amendment legitimate rather than outcome-shopping.

### What the profile falsified

| this document assumed | the profile measured |
|---|---|
| a **static 40-market universe** from a frozen `universe.json` | market-level activity rank is **unstable within a day** — ρ_orderbook 0.431 / 0.004 / **−0.648** across day 1's three slot pairs. The markets busy at 14:00 ET are the *quiet* ones at 20:00 ET. |
| 40 markets are active for the whole window | **0 of 40** markets (day 1) and **5 of 40** (day 2) clear §4's activity floor in all three windows. 22 and 18 clear it in exactly one. |
| ≈360,000 rows ⇒ ≈12,000 quasi-independent 30 s blocks | rests entirely on the above; **unsupported** |
| 40 markets sit ~3.4× under the stop | realised **2.55×** vs the 6,900 envelope and **1.29×** vs the 3,500 stop; observed peak **1.33× above** the 2,040 f/s projection — and that peak is a **lower bound** (Amendment 4 of the profile) |

What *is* stable is the **series**: ρ_orderbook **0.881**, ρ_trade **0.905**
across days at series level. Activity is **event-time dependent**, not a market
property.

### §A. The unit of eligibility becomes market × event-time block

**Retired:** "the 40 markets in the frozen `universe.json`."

**Replaces it:**

```text
eligible series
      -> live markets in those series
      -> at prediction time t:
             valid current-generation book
             sufficient LAGGED order-book activity
             enough future horizon remaining
             no sequence/fault contamination
      -> eligible (market, t) block
```

**Eligibility at `t` may use only information available at or before `t`:**

> Eligibility(i,t) = f( X(i, ≤t) )

Selecting a row because the market *became* active afterwards is
future-conditioned selection and is forbidden. This is now the single most
likely way to fake a result in this experiment, and it is called out here so it
cannot be committed quietly.

### §B. The eligibility gate is lagged order-book activity

Over a frozen lookback **L = 300 s**:

> Activity(i,t) = N_orderbook(i, (t−L, t]) / L

Order book is the primary signal because it is the only channel that is both
**sequenced** and **continuously published**. Explicitly **not** used for
eligibility:

* `ticker` — §5 already bars it, and the profile confirmed why on production:
  both §7 positive controls emitted **zero ticker frames** while producing
  **279 and 73 real order-book deltas**.
* contracts/min or any traded-volume proxy — activity is measured from wire
  frames, never inferred from volume.

**Zero trades is a measurement, not ineligibility.** A market with a complete
sequenced trade stream and no trades in the lookback has `TradeFlow = 0`, which
is real information. Eligibility may not require a trade unless a hypothesis
explicitly demands one; none here does.

### §C. Event lifecycle is an explicit conditioning variable, frozen now

Every research row carries time-to-event:

> TTE(t) = t_event_resolution − t

using `occurrence_datetime` (== `expected_expiration_time`) — **not**
`close_time`, which the profile's preflight proved is a settlement deadline
days after the event.

**Bins, frozen before any M0/M1 output exists:**

| bin | TTE |
|---|---|
| `far` | > 6 h |
| `approaching` | 2 h – 6 h |
| `near_event` | 15 min – 2 h |
| `live_event` | 0 – 15 min before, through the event |
| `late_resolution` | past scheduled event time, pre-settlement |

These boundaries are fixed here so that `E[r(t+h) | OFI(t), TTE(t)]` can be
asked later without averaging structurally different market states into one
effect. **They may not be redrawn after seeing results.**

### §D. Concurrency ceiling — a capacity guard, not an alpha parameter

The 40-market premise is dead. The replacement is set from capacity evidence
alone and **selects nothing on expected return**.

The naive scaling — 2,704 × 24/40 ≈ 1,622 f/s, ~2.16× headroom — is
**optimistic**, and the profile says so directly. Traffic is heavily
concentrated, and an activity-ranked selection takes the *busiest* markets, not
a random subset. Measured on the busiest window (day 2 slot C):

| K (ranked by that window's frames) | share of window traffic | naive peak scaling | headroom vs 3,500 |
|---:|---:|---:|---:|
| 8 | 45.3% | 1,224 f/s | 2.86× |
| **12** | **59.9%** | **1,620 f/s** | **2.16×** |
| 16 | 70.9% | 1,916 f/s | 1.83× |
| 20 | 79.1% | 2,138 f/s | 1.64× |
| **24** | **86.5%** | **2,340 f/s** | **1.50×** |
| 41 | 100% | 2,704 f/s | 1.29× |

So **K = 24 buys ~1.50× headroom, not 2.16×.** The concurrency that actually
delivers the intended ~2.16× is **K = 12**.

**Frozen for the first experiment: K = 12. Absolute never-exceed ceiling: 24.**
Every figure above is a lower bound, because it scales a censored peak. None of
them is a prediction of traffic; the **3,500 f/s hard stop remains the sole
authoritative capacity control** and is unchanged.

### §E. Re-selection, not a one-time list

At each research interval, from the currently eligible set:

```text
eligible now
  -> rank by preceding-window order-book event rate  (lagged, per §B)
  -> deterministic tie-break: ticker lexicographic ascending
  -> take at most K
```

No ticker proxy, no volume proxy, no future activity, **no alpha score** may
enter this rule. The panel is expected to turn over during a session; that is
the intended behaviour, and it is what a live system would have to do anyway.

### §F. Series is a grouping variable, not the trading unit

Given ρ_series ≈ 0.88–0.90 against unstable market-level ranks, series is used
for **sampling strata, evaluation groups, model covariates, and clustering** —
never as a licence for the model to win by memorising "series X is usually
active." The target remains future price movement.

**Clustering is strengthened.** §4 already clusters standard errors by market
and block-bootstraps at ≥300 s. That survives and is **extended**: uncertainty
is clustered at the **event/market** level, and the writeup reports the
**realised** effective block count. The retired "≈12,000 blocks" figure may not
be quoted.

### §G. The six profile windows are DESIGN evidence, not confirmation data

They have now been used to design the universe rule, the concurrency ceiling,
the eligibility gate and the event-time conditioning. They are therefore
**burned for confirmation** and may not serve as the M0/M1 evaluation set.

```text
PROD-ACTIVITY-PROFILE-001  ->  research design
NEW PROSPECTIVE CAPTURE    ->  edge evaluation
```

§9's "nothing about times of day outside the six windows" is superseded: the
prospective capture defines its own coverage, declared before it starts.

### §H. Cross-SID joins are timestamp association, never causal ordering

Order book (sid 1) and trade (sid 3) are **independently sequenced**. All six
windows were fully contiguous on both, and there is still **no venue-guaranteed
common order between them**.

M1 may compute `TradeFlow(t−L, t]` from **receive timestamps**, described as
**temporally associated cross-stream flow**. It may **not** claim that a given
trade caused a given book update. Every feature window ends **at or before** the
prediction timestamp `t`; no post-`t` trade may enter a `t`-prediction. §4's
pre-declared trade lag, and the requirement to report M1 at that lag and at
double it, are unchanged.

### §I. Horizons stay exactly as frozen; the target may report itself unusable

Horizons remain **h ∈ {1 s, 5 s, 30 s, 300 s}** with **30 s primary** and the §7
Benjamini–Hochberg correction over all twelve cells. This set is **not**
re-opened — changing it now, after profile data, is precisely the move this
amendment exists to prevent.

Added: the experiment must be able to emit **`TARGET_UNINFORMATIVE`** for a
horizon whose realised mid-movement distribution is too degenerate to evaluate,
declared from the prospective data **before** any M0/M1 fit. A horizon so
labelled is reported as unevaluable, not silently dropped, and **the best-looking
horizon may never be promoted to primary after the fact.**

### §J. Unchanged

§1 the question · §2 the prior (net edge ≤ 0) · §3 M0/M1 model structure and the
12 state-only features · §5 both noise floors · §6 the economic gate and the
unverified-fee rule · §7 FDR 10% over twelve cells · §8 the binding stopping
rule · §9's execution and regime disclaimers.

**§8 remains binding in full.** If M1 does not beat M0 out-of-sample at the
primary horizon, order flow is declared non-additive and the lane stops. No
Hawkes, no transformer, no additional features. Economics stay strictly
downstream of `Loss(M1) < Loss(M0)`.

**Status after this amendment: PREREGISTERED, NOT RUN. Prospective capture not
started. M0/M1 not run.**

---

## Amendment 4 — 2026-08-23: eligibility gates on session-remaining, not TTE

The original `TTE > max_horizon + embargo` eligibility constraint incorrectly
used **event-relative time as a proxy for future-label computability**. This
made two preregistered TTE strata structurally unreachable. Future-label
feasibility depends on remaining capture/session time and is independently
enforced by the labeler. Eligibility is therefore amended to require sufficient
**session time remaining**; **TTE remains unrestricted** and retains its frozen
bin definitions.

Made before any confirmation capture and before any M0/M1 outcome existed.

### What was broken

| bin | eligible sub-range under `TTE > 600` | width |
|---|---|---:|
| `far` | > 21,600 | ∞ |
| `approaching` | 7,200–21,600 | 14,400 s |
| `near_event` | 900–7,200 | 6,300 s |
| `live_event` | 600–900 | **300 s** |
| `late_resolution` | none | **0 — UNREACHABLE** |

### The amended rule

> eligible(i,t) ⟺ Activity(i,t) ≥ 0.10 events/s
> ∧ valid current-generation book
> ∧ no sequence fault in (t−300, t]
> ∧ **session_end − t > 600 s**

`session_end` is the **scheduled** end, fixed before the socket opens — never
the observed last-frame time, because eligibility at `t` may not consult how
long the venue happened to keep publishing afterwards.

**Nothing else moves.** Activity floor, K, rotation, tie-break, warmup, the
safety stop and all five bin boundaries are untouched. The boundary stays
**strictly** greater. No market is required to remain open for another 600 s —
that would be future knowledge; if it closes naturally, `labels.py` records the
affected horizon as unavailable rather than fabricating a value. Horizon-specific
availability is preserved: a row may legitimately carry 1 s/5 s/30 s labels and
no 300 s label. **Late-resolution rows are not excluded for having worse 300 s
coverage** — doing so would reintroduce lifecycle-dependent selection.

### Reachability, proven against the production decision core

| bin | TTE | session remaining | eligible |
|---|---:|---:|---|
| `far` | 30,000 | 3,600 | ✅ |
| `approaching` | 10,000 | 3,600 | ✅ |
| `near_event` | 3,000 | 3,600 | ✅ |
| `live_event` | 300 | 3,600 | ✅ |
| `late_resolution` | −1,200 | 3,600 | ✅ |

Opposing control — the gate still bites in the other direction:

```
TTE=+7200 (near_event), 600 s session left -> INELIGIBLE (session_remaining_at_or_below_600s)
TTE=+7200,              601 s session left -> eligible
```

And TTE is not consulted at all: eligibility is identical at TTE ∈ {−100000,
−1, 0, 1, 600, 601, 100000}. A 12-mutation campaign killed all twelve;
**reverting the gate to a TTE test fails 7 tests**.

### `late_resolution` means past the nominal occurrence time, not resolved

A contract can have `TTE < 0` while still actively trading, because the event
has occurred or passed its nominal time but the contract has not settled. Once
the venue actually closes it there is simply no usable future market state, and
the labeler handles that. The distinction — *event over* ≠ *information fully
incorporated* ≠ *contract resolved* — may become scientifically interesting
later. **Nothing is changed on that basis now.**
