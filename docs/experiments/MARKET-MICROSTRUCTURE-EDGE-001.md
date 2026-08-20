# MARKET-MICROSTRUCTURE-EDGE-001 — preregistration

**Status: PREREGISTERED, NOT RUN. Awaiting Eric's decision before execution.**
Written 2026-08-19.

Read-only. No capital, no orders, no venue writes. This experiment measures
information, not profit, and cannot authorise a trade.

**Depends on:** `PROD-ACTIVITY-PROFILE-001` (frozen `universe.json`),
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
