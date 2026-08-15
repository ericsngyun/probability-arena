# EDGE-DISCOVERY-001 — E2 result: does the forecast LEAD the market?

**Verdict: FAIL(E2)** against the preregistered criterion.

**But the underlying effect is real, positive, and large relative to everything
else this milestone has measured.** The forecast *does* lead the market, at four
of six horizons, with Holm-corrected event-clustered CIs comfortably excluding
zero. It fails only because the lead — about **+2.2 probability points** — is
roughly **two-thirds of the executable cost floor** of about **3.3 points**. The
signal is real and is not big enough to pay for itself.

**There is NO fade signal.** The direction is **positive at every one of the six
horizons**. Disagreement predicts the market moving **toward** the model, not
against it. The negative net numbers below are *costs*, not direction.

- Preregistration: `docs/experiments/EDGE-DISCOVERY-001-PREREGISTRATION.md` §3,
  with deviations §8 D-1, D-2, D-3, D-4 (D-3/D-4 were written and committed
  **before** any statistic here was computed, commit `448c6d0`).
- Script: `docs/evidence/qdk-001/e2_leadlag.py` — raw output
  `docs/evidence/qdk-001/e2_leadlag_results.json`.
- Authorises no capital, no orders, no execution. §6's stopping rule is
  unaffected by E2 alone.

---

## 1. Method

At forecast time `t`, `d_t = p_t − q_t` in probability space (no logit, so §1's
clipping guard does not bind; clipping count is zero by construction). For each
horizon `h ∈ {5m, 15m, 30m, 1h, 3h, 6h}`, `Δq_{t,h} = q_{t+h} − q_t`, where
`q_{t+h}` is the mid of the nearest `MarketPriceTickBucket` ending at or after
`t+h` within ±½h, else typed-absent.

- **Primary statistic:** `A_h = E[sign(d)·Δq_h]`.
- **Secondary:** magnitude-weighted `E[d·Δq_h] / E[|d|]`.
- **Inference:** **cluster bootstrap resampling the 1,482 EVENTS with
  replacement, B = 10,000 replicates**, percentile intervals, two-sided
  bootstrap p-values. Never ticker-clustered. Every statistic is a ratio of
  cluster-additive sums, so each replicate is exact from resampled per-event
  sums.
- **Multiple testing:** Holm–Bonferroni, **family = all six horizons**, α = 0.05.
  Per D-2 the family is deliberately **not** narrowed to the well-covered
  horizons; 3h and 6h are reported as **underpowered**, never as nulls.

### Cost floor — per observation, never a global constant

`floor_i = half_spread_i + taker_fee_i`, computed at each observation's own
price and spread:

- `half_spread_i = (yes_ask_c − yes_bid_c) / 200` in probability units.
- `taker_fee_i = ceil_to_cent(M × 0.07 × C × P_i × (1−P_i))`, M = 1, C = 1,
  `P` in dollars (Kalshi primary schedule, effective 2026-07-07). A $1-notional
  contract makes dollars and probability units the same, so no conversion.

`ceil_to_cent` is what makes this floor bite: it forces **at least $0.01** of fee
on every contract regardless of price, and the mean realised fee is **1.75–1.84
cents** — larger than the mean half-spread of 1.52 cents.

Two fee prices are reported (D-3). *Mid* prices the fee at `q` (the half-spread
term already carries the mid→executable crossing cost). *Adverse* prices it at
the price actually paid — YES at the ask, NO at `(100−bid)/100` — and **governs
the verdict**. They differ by less than 0.01 cents here, so nothing turns on the
choice.

The **PASS statistic** is the net excess `N_h = E[s·sign(d)·Δq_h − floor]`, the
per-observation comparison of move against floor. `s = +1` at every horizon
(D-4: direction is data-chosen because only *existence* was preregistered; the
raw statistic is tested two-sided, and the HOLDOUT column fixes `s` from TRAIN).

---

## 2. Per-horizon table — POOLED (the preregistered criterion set)

`n = 10,285` rows, `1,482` events. 92 rows have `d = 0` exactly and contribute
zero to the primary statistic.

| h | n | events | coverage | `E[sign(d)·Δq]` | Holm-level CI | p | cost floor | net of floor | Holm CI on net | verdict |
|---|---:|---:|---:|---:|---|---:|---:|---:|---|---|
| **5m** | 3,934 | 1,218 | 38.2% | **+0.02186** | [+0.01504, +0.02913] | 0.0002 | 0.03342 | **−0.01156** | [−0.01844, −0.00420] | **FAIL — below floor** |
| **15m** | 9,083 | 1,454 | 88.3% | **+0.01876** | [+0.01271, +0.02475] | 0.0002 | 0.03279 | **−0.01403** | [−0.02077, −0.00763] | **FAIL — below floor** |
| **30m** | 8,183 | 1,440 | 79.6% | **+0.02354** | [+0.01545, +0.03136] | 0.0002 | 0.03309 | **−0.00954** | [−0.01822, −0.00117] | **FAIL — below floor** |
| **1h** | 6,285 | 1,379 | 61.1% | **+0.02362** | [+0.01278, +0.03478] | 0.0002 | 0.03360 | **−0.00998** | [−0.02142, +0.00166] | **FAIL — below floor** |
| 3h | 641 | 255 | 6.2% | +0.01179 | [−0.02279, +0.05003] | 0.5029 | 0.04777 | −0.03598 | [−0.07807, +0.01691] | **UNDERPOWERED** (D-2) |
| 6h | 292 | 84 | 2.8% | +0.02827 | [−0.00514, +0.08719] | 0.0794 | 0.06274 | −0.03447 | [−0.08146, +0.03748] | **UNDERPOWERED** (D-2) |

Holm on the **primary** statistic rejects at **5m, 15m, 30m and 1h** (all
p = 0.0002, the bootstrap floor at B = 10,000) and does not reject at 3h or 6h.
Holm on the **net** statistic rejects at 5m, 15m and 30m — **in the negative
direction**, i.e. the shortfall against cost is itself statistically established,
not merely unproven.

**Secondary, magnitude-weighted `E[d·Δq_h]/E[|d|]`** (pooled), same sign, larger:
5m +0.03956 [+0.03009, +0.04962]; 15m +0.03145 [+0.02389, +0.03895];
30m +0.03865 [+0.02855, +0.04880]; 1h +0.03927 [+0.02553, +0.05332];
3h +0.01830 [−0.04407, +0.09005]; 6h +0.05083 [−0.00832, +0.18061].
That the magnitude-weighted figure exceeds the sign-only figure means **larger
disagreements lead the market by proportionally more** — the signal scales with
`|d|` rather than being driven by a few noisy small-`d` rows.

### Cost floor decomposition (pooled, per observation, mean)

| h | mean half-spread | mean taker fee | floor (mid) | floor (adverse) | round-trip net |
|---|---:|---:|---:|---:|---:|
| 5m | 0.01594 | 0.01752 | 0.03346 | 0.03342 | −0.04497 |
| 15m | 0.01519 | 0.01761 | 0.03280 | 0.03279 | −0.04681 |
| 30m | 0.01522 | 0.01789 | 0.03310 | 0.03309 | −0.04263 |
| 1h | 0.01525 | 0.01837 | 0.03362 | 0.03360 | −0.04358 |
| 3h | 0.02892 | 0.01891 | 0.04783 | 0.04777 | −0.08374 |
| 6h | 0.04418 | 0.01856 | 0.06274 | 0.06274 | −0.09721 |

The preregistered floor is **one-way**. A mid-to-mid move must in reality be both
entered and exited, so the round-trip column is the economically honest number
and it is roughly **−4.3 to −4.7 points** at the tradable horizons. The verdict
uses the one-way floor as preregistered; the round-trip figure only widens the
gap.

---

## 3. TRAIN and HOLDOUT (reported for stability, not for selection)

The preregistered criterion is on the full matched set. The split is reported
because §1 requires it. Direction `s` for the HOLDOUT net figure is fixed from
TRAIN (D-4), so the holdout column carries no direction selection.

| h | TRAIN `E[sign(d)·Δq]` (95% CI) | n | HOLDOUT `E[sign(d)·Δq]` (95% CI) | n |
|---|---|---:|---|---:|
| 5m | +0.01966 [+0.01277, +0.02634] | 2,500 | +0.02569 [+0.01717, +0.03408] | 1,434 |
| 15m | +0.02189 [+0.01584, +0.02791] | 5,734 | +0.01340 [+0.00571, +0.02079] | 3,349 |
| 30m | +0.02584 [+0.01772, +0.03371] | 5,178 | +0.01959 [+0.00933, +0.03003] | 3,005 |
| 1h | +0.01978 [+0.00834, +0.03147] | 4,002 | +0.03035 [+0.01533, +0.04559] | 2,283 |
| 3h | +0.02044 [−0.01621, +0.06410] | 436 | −0.00659 [−0.08174, +0.06856] | 205 |
| 6h | +0.01040 [−0.00069, +0.03409] | 214 | +0.07731 [−0.04067, +0.17833] | 78 |

**The effect replicates out of sample at all four well-covered horizons**, with
holdout CIs excluding zero at 5m, 15m, 30m and 1h. This is not a train-period
artefact. It still never clears the floor: the best holdout net figure is 1h at
**−0.00220**, and the single positive net cell (6h, +0.04481) rests on 78
observations across 32 events and is not interpretable.

---

## 4. Mean-reversion confound check (exploratory — clearly labelled)

**The confound is real and substantial, but it does not explain the effect away.**

`Δq` *is* predictable from `q` alone. `E[Δq · sign(0.5 − q)]` is positive and
significant at every well-covered horizon — the market mid drifts back toward
0.5 — and it grows with horizon: 5m +0.00563 [+0.00072, +0.01074] p = 0.0216;
15m +0.01082 [+0.00627, +0.01552] p = 0.0002; 30m +0.01409 [+0.00816, +0.02004]
p = 0.0002; 1h +0.02170 [+0.01360, +0.02995] p = 0.0002.

And the model is heavily exposed to it: **`sign(d)` points toward the mid in
69.8–72.8% of observations** at the tradable horizons. The model
systematically disagrees with the market in the direction of 0.5, so a naive
reading of `E[sign(d)·Δq]` would partly be measuring mean reversion in `q`
wearing the model's clothes.

To separate them, `Δq_h` was residualised on `q` alone — `Ê[Δq_h | q]` as a
20-bin equal-count binned mean **fit on TRAIN only** — and the primary statistic
recomputed on the residual:

| h | raw | residualised on `q` | 95% CI | p | attenuation |
|---|---:|---:|---|---:|---:|
| 5m | +0.02186 | **+0.01934** | [+0.01416, +0.02466] | 0.0002 | 11.5% |
| 15m | +0.01876 | **+0.01314** | [+0.00844, +0.01776] | 0.0002 | 30.0% |
| 30m | +0.02354 | **+0.01503** | [+0.00869, +0.02116] | 0.0002 | 36.2% |
| 1h | +0.02362 | **+0.01592** | [+0.00698, +0.02492] | 0.0008 | 32.6% |
| 3h | +0.01179 | +0.00973 | [−0.01928, +0.04195] | 0.5163 | underpowered |
| 6h | +0.02827 | +0.02690 | [−0.00381, +0.07652] | 0.1036 | underpowered |

**`d`'s predictive power does not vanish once you condition on `q`.** It
attenuates by roughly **a third** at 15m–1h and survives with p ≤ 0.0008. So
mean reversion accounts for a meaningful minority of the raw statistic, and `d`
carries genuine incremental lead information beyond the market price alone.

This makes the economic verdict **worse, not better**: the confound-free lead is
+0.013 to +0.019, against a floor of 0.033. Removing the confound moves the
signal further below the cost floor, not closer to it.

---

## 5. Verdict against the preregistered criterion

> **PASS (E2)** iff at least one horizon shows `|E[sign(d)·Δq]|` **exceeding the
> executable cost floor** with a Holm-corrected CI excluding zero.

Both clauses must hold at the same horizon. The second clause is satisfied at
four horizons. **The first is satisfied at none** — the largest point estimate
(+0.02362 at 1h) is 70% of that horizon's floor (0.03360), and the best net
excess (−0.00954 at 30m) has a Holm-corrected CI entirely below zero.

### **FAIL(E2).**

**What is nevertheless established, and should not be rounded down to "no
effect":**

1. **The forecast leads the market.** `E[sign(d)·Δq]` is positive and
   Holm-significant at 5m, 15m, 30m and 1h, replicates on HOLDOUT, and survives
   conditioning on `q` at about two-thirds strength. The market moves **toward**
   the model's disagreement over the following hour.
2. **This coexists with the model losing on settlement.** The established ΔS
   = −0.01700 says `p` is a worse *settlement* forecast than `q`. E2 says `d`
   nonetheless predicts *short-horizon price*. Those are not contradictory, and
   the combination is the most informative thing this milestone has produced:
   the model contains information the market has not yet impounded, and also
   contains enough error that it ends up worse at resolution.
3. **The gap is a cost gap, not a signal gap.** A ~2.2-point lead against a
   ~3.3-point one-way floor (~4.4 round-trip). The binding constraint is the
   `ceil_to_cent` fee minimum plus a 1.5-cent half-spread, not the absence of
   predictive content.

### Explicitly: no fade signal

Direction is **positive at all six horizons**, pooled, TRAIN, and HOLDOUT (the
one exception, 3h holdout at −0.00659, has a CI spanning ±0.08 on 205 rows).
Nothing here supports fading the model's disagreement. The preregistered
possibility that disagreement predicts adverse movement is **not** what the data
show.

---

## 6. Limits

- **Coverage is uneven and two horizons are near-unusable** (D-2). 3h at 6.2%
  and 6h at 2.8% are underpowered by construction and are reported as such, not
  as nulls. They remain in the Holm family as preregistered. 5m coverage is only
  38.2%; coverage is a property of the tick-bucket record rather than of `d`, so
  it is non-differential, but 5m is the weakest of the four "good" horizons on
  that ground.
- **`Δq` is a mid-to-mid move.** It is the right object for "does the forecast
  lead the market" and the wrong object for profitability, which is exactly why
  the cost floor is applied. E2 does not establish that the lead is capturable at
  *any* size; it establishes the opposite at the observed one.
- **No price impact, no queue, no size.** The floor is half-spread + taker fee
  only. Real execution adds impact and partial fills, all adverse. Per §5 maker
  fills are excluded because queue position is unobtainable under observe-only.
- **The one-way floor is generous.** Round-trip is the honest cost and it roughly
  doubles the shortfall.
- **The residualisation is a binned mean of `q`, not a full conditional model.**
  It rules out the specific and most likely confound named in the protocol —
  distance from 0.5 — and does not rule out every function of `q` or other
  microstructure covariates. It is labelled exploratory.
- **`s` is data-chosen for the net statistic** (D-4). The raw statistic is
  two-sided and preregistered; the pooled net figure carries mild in-sample
  direction selection, which the TRAIN→HOLDOUT column removes.
- **E2 alone does not resolve §6.** The stopping rule turns on E1–E4 jointly.
  This result does, however, bear directly on E1: a `d` that predicts price
  movement conditional on `q` is precisely the kind of thing that could give
  `β_d ≠ 0`, and E1 should be read with that in mind.
