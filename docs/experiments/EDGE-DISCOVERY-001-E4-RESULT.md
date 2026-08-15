# EDGE-DISCOVERY-001 · E4 — proper-betting decomposition at executable prices

**Verdict: FAIL.** No preregistered strategy shows positive net expectancy after costs on
HOLDOUT. Every one of them is negative, and they are negative for the same reason.

**The mathematical claim under test is nonetheless CONFIRMED.** A negative score gap does
*not* imply that every betting transform loses: the Bregman divergence term more than
offsets our ΔS, and the frictionless profit of proper betting on this data is **positive**
on both splits and under both rules. It is simply **an order of magnitude too small to pay
the spread and the taker fee**. The divergence rent is real, measurable, and worth
+0.17¢ per contract on HOLDOUT against a **2.16¢** cost wedge.

- Script: `docs/evidence/qdk-001/e4_proper_betting.py` · results:
  `docs/evidence/qdk-001/e4_results.json` · independent cross-check:
  `docs/evidence/qdk-001/e4_crosscheck_numpy.py`
- Preregistration: `docs/experiments/EDGE-DISCOVERY-001-PREREGISTRATION.md` §5;
  deviations logged there as **D-3 … D-6 before these numbers were reported**.
- Theory: arXiv 2607.06166, Theorem 8 / Lemma 9 / Corollary 19, as verified verbatim in
  `docs/research/QDK-001-prediction-market-math.md` §1.1–1.2, §3.4, §7.4.
- **Authorizes no capital, no orders, no execution.**

---

## 1. Method

### 1.1 The decomposition, implemented rather than cited

Theorem 8, Equation (1): `π = [S(p;p*) − S(q;p*)] + D_G(q,p) − L_ρ(s;q)`. The unobservable
`p*` is avoided by using **Lemma 9**, which is *pointwise in the realized outcome* `y`:

```
s_G(p,q) · (1_y − q)  =  [ S(p,y) − S(q,y) ]  +  D_G(q,p)
```

Everything on both sides is observable. The script builds `G`, `∇G`, the Savage
representation `S(v,y) = G(v) + ∇G(v)·(1_y − v)`, and `D_G(a,b) = G(a) − G(b) − ∇G(b)·(a−b)`
explicitly as 2-vectors, and **asserts Lemma 9 to 1e-9 on every single traded row**. The
assertion passed for all 8,824 trades. This is the sense in which the decomposition is
implemented and not asserted.

In a binary market `1_y − q = (y−q, −(y−q))`, so only the scalar
`n = s_yes − s_no` survives:

| rule | potential `G` | reduced exposure `n` |
|---|---|---|
| Brier | `Σ v_k²` | `4(p − q)` |
| log | `Σ v_k log v_k` | `logit(p) − logit(q)` |

**Consequence, and it matters for reading every table below:** the *sign* of `n` is
`sign(p − q)` under both rules. The proper Brier transform, the proper log transform and
fractional Kelly on `p` therefore **produce the identical trade set and the identical
direction on every row**. They differ *only* in relative weight. The closed strategy list
collapses to one trade set × four weightings, and the equal-weight column is by
construction the same number for all four.

### 1.2 Scale — handled explicitly (D-6)

`s_G` is defined only up to a positive scalar (Theorem 13); the research doc's §3.4 point is
that proper betting is an **allocator**, not a sizing rule, and composes with an exogenous
scale. So the headline is stated in a scale-free unit:

- **Headline: net cents *per contract*, 1 contract per trade.** Any positive rescaling λ
  multiplies every trade identically and **cannot change the sign** of this statistic.
- **Allocator-weighted**: weights `|n_i|` (Brier/log) or `f_i` (Kelly), normalised to mean 1
  contract per trade — a ratio estimator, recomputed inside each bootstrap resample. λ
  cancels here too.
- **Absolute sizing** ($1,000 per trade, non-compounding) is reported separately in §5,
  which is the only place fractional-Kelly λ bites at all.

No arbitrary scale drives the P&L sign anywhere in this document.

### 1.3 Executable prices (mandatory — this is the point)

Midpoint is **invalid** for profitability and is used here only to isolate the spread term.

```
BUY YES  pays      yes_ask_c
BUY NO   pays      100 − yes_bid_c
```

The NO side is derived from the YES book — **NO ask = 100 − YES bid, NO bid = 100 − YES
ask** — because the frozen extract carries only the YES book. Entry condition is
Corollary 19's bid-ask form of proper betting (D-4): buy YES iff `p > ask`, buy NO iff
`p < bid`, abstain in between. The naive `sign(p − q)` variant that crosses the spread
unconditionally is reported beside it everywhere; it is uniformly worse.

Positions are entered at the quote in force at forecast time and **held to settlement** —
there is no exit trade, so no second spread and no second fee.

### 1.4 Fees — Kalshi schedule effective 2026-07-07, taker only

```
taker: fee_dollars = round_up_to_cent(M × 0.07   × C × P × (1−P))     M = 1
maker: fee_dollars = round_up_to_cent(M × 0.0175 × C × P × (1−P))     not used
```

- **Taker only.** Queue position is unobtainable on Kalshi L2, so maker fills are
  unfalsifiable under observe-only. The 4× cheaper maker path is not claimable here.
- `P` is the dollar price of the contract **actually bought** — YES ask, or NO ask —
  so the fee is computed **per trade at its own price**, never as a flat rate. Mean entry
  price on HOLDOUT is 34.3¢, which is why the realised mean fee (1.235¢) sits below the
  1.75¢ at-the-money maximum.
- `C = 100` contracts per order for the primary (D-5). The round-up is applied to the whole
  order, so `C` is not cosmetic: at `C = 1` every trade pays ≥1¢ whatever the price. **The
  adverse `C = 1` variant is reported in full** and is 0.48¢/contract worse.
- `M` is assumed 1 (default taker) because the frozen extract carries no per-market fee
  multiplier. Declared as an assumption, not read from market state.
- **Settlement:** the schedule as quoted in the research doc (§7.4) states there is **no
  settlement fee**, and the preregistered formula block contains only taker and maker legs.
  Settlement cost is therefore modelled as zero. **That source is flagged
  `[VERIFIED — secondary sources; confirm against the primary PDF before use]`.** The
  primary PDF was not retrieved in this session. Any settlement charge that does exist is a
  strictly positive cost, so this assumption can only be making the reported net **too
  good**, never too bad. It does not threaten a FAIL verdict; it would have threatened a
  PASS.

### 1.5 Statistics

Cluster bootstrap at the **EVENT** level (never ticker), 5,000 iterations, resampling
events with replacement and recomputing the statistic (a ratio estimator where weighted),
percentile 95% CI, seed 20260815. TRAIN and HOLDOUT are computed and reported separately;
**HOLDOUT is the criterion.**

### 1.6 Universe

10,285 rows / 1,482 events. `q` clipped to [0.01, 0.99] on **287** rows, `p` on **0**
(preregistered guard). 111 crossed quotes excluded (D-3). Tradable universe after the
abstain band: **5,529 trades / 945 events (TRAIN)** and **3,295 trades / 534 events
(HOLDOUT)**.

### 1.7 Interpreters, and an independent cross-check

`e4_proper_betting.py` is **pure stdlib** and was run on the repo venv,
`/Users/ericyun/code-stuff/probability-arena/.venv/bin/python` (3.12.3), which has neither
numpy nor scipy installed; no installs were performed. This matches `delta_s_strict.py` in
the same directory, which is also pure stdlib.

Because numpy 2.1.3 *is* available at `/usr/local/bin/python3`, the headline was then
recomputed there by a **deliberately separate code path**
(`e4_crosscheck_numpy.py`): vectorised rather than row-by-row, a different bootstrap RNG,
and the Brier terms written in closed form (`S(v,y) = 1 − 2(v−y)²`, `D(q,p) = 2(q−p)²`)
instead of via the generic Savage/Bregman machinery. Agreement is therefore evidence rather
than a shared bug.

| quantity (HOLDOUT) | stdlib | numpy cross-check |
|---|---:|---:|
| trades / events | 3,295 / 534 | 3,295 / 534 |
| gross @ mid | +0.1724 | +0.1724 |
| spread | −0.9244 | −0.9244 |
| fee | −1.2347 | −1.2347 |
| **net** | **−1.9868** | **−1.9868** |
| score gap / divergence / sum | −0.03452 / +0.03964 / +0.00511 | −0.03452 / +0.03964 / +0.00511 |
| 95% CI | [−3.9797, +0.1359] | [−3.9733, +0.1073] |

Every point estimate reproduces exactly; the CIs differ in the third decimal only, which is
bootstrap RNG noise and does not move any conclusion. The closed-form agreement also
independently confirms the Savage-representation algebra used in §2.

---

## 2. The theory terms — the divergence DOES offset the negative score gap

Per trade, in score units (Savage-representation Brier / log; `ScoreGap + Divergence` is
exactly `s_G·(1_y − q)`, Lemma 9, asserted row by row):

| split | rule | Score Gap | Divergence `D_G(q,p)` | Sum = frictionless profit |
|---|---|---:|---:|---:|
| TRAIN | Brier | **−0.03868** | **+0.04478** | **+0.00609** |
| TRAIN | log | −0.05424 | +0.06035 | +0.00611 |
| HOLDOUT | Brier | **−0.03452** | **+0.03964** | **+0.00511** |
| HOLDOUT | log | −0.04546 | +0.05213 | +0.00666 |

**Read this carefully, because it is the finding.** The score gap is negative on both
splits and under both rules — our forecasts are worse than the market, exactly as
`DELTA_S_RESULT.md` established. But `D_G(q,p) ≥ 0` always, and here it is *larger in
magnitude* than the score gap. **The sum is positive.** The preregistered claim —
*negative ΔS does not mathematically imply that every betting transform has negative
profit* — is **confirmed observationally on our own data**, not merely cited.

Consistency check on the instrument: the Savage Brier score gap is exactly `2 × ΔS`, so
HOLDOUT's −0.03452 implies **ΔS = −0.01726** on the traded subset, against the frozen
headline **ΔS = −0.01700** on the full frame. The decomposition reproduces the existing
result to the third decimal, which is the evidence that this machinery is measuring what it
claims to measure.

What the positive sum is *not*: it is the **zero-price-impact, zero-spread, zero-fee**
number — precisely the `L_ρ = 0` idealisation the research doc flags as "the single largest
backtest-to-live gap in the design" (§2.2). §3 prices it.

---

## 3. HEADLINE — the full decomposition at executable prices

Mean **cents per contract**, one contract per trade, HOLDOUT, Corollary 19 abstain band,
`C = 100`. Costs are shown as negative so the column sums.

| strategy | n trades | events | Score Gap | Divergence | **Gross @ mid** | Spread | Fees | Price impact | **NET** | 95% CI (event-clustered) |
|---|---:|---:|---:|---:|---:|---:|---:|:--:|---:|:--:|
| proper **Brier** | 3,295 | 534 | −0.0345 | +0.0396 | **+0.1724** | −0.9244 | −1.2347 | unmeasured, ≤ 0 | **−1.9868** | [−3.9797, +0.1359] |
| proper **log** | 3,295 | 534 | −0.0455 | +0.0521 | **+0.1724** | −0.9244 | −1.2347 | unmeasured, ≤ 0 | **−1.9868** | [−3.9797, +0.1359] |
| **Kelly λ=0.25** | 3,295 | 534 | −0.0345 | +0.0396 | **+0.1724** | −0.9244 | −1.2347 | unmeasured, ≤ 0 | **−1.9868** | [−3.9797, +0.1359] |
| **Kelly λ=0.50** | 3,295 | 534 | −0.0345 | +0.0396 | **+0.1724** | −0.9244 | −1.2347 | unmeasured, ≤ 0 | **−1.9868** | [−3.9797, +0.1359] |
| **no-trade** | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | **0** | [0, 0] |

Score Gap and Divergence are in score units (§2); the P&L columns are in cents per
contract. The identical P&L across the four strategies is not a bug — §1.1: they share the
trade set and direction, and equal weighting removes the only thing that differs.

**Allocator-weighted** (weights normalised to mean 1 contract; this is where the four
strategies actually separate), HOLDOUT:

| strategy | net ¢/contract, weighted | 95% CI |
|---|---:|:--|
| proper Brier | −0.9922 | [−3.2440, +1.4296] |
| proper log | −0.8419 | [−2.5773, +0.9719] |
| Kelly λ=0.25 | −0.9116 | [−3.4522, +1.7357] |
| Kelly λ=0.50 | −0.9116 | [−3.4522, +1.7357] |
| no-trade | 0 | [0, 0] |

Weighting halves the loss — the allocators do put more size on the trades that do less
badly — but not one of them reaches zero, and every CI spans it.

### Gross vs net, side by side — the cost wedge

HOLDOUT, cents per contract:

| | mid-price (frictionless) | executable (spread paid) | executable + fees = **NET** |
|---|---:|---:|---:|
| Brier / log / Kelly | **+0.1724** | −0.7520 | **−1.9868** |

The wedge is **2.1591¢ per contract** (0.9244 spread + 1.2347 fee). The divergence rent
delivers 0.1724¢. **Gross would have to be 12.5× larger to break even before price impact.**

### TRAIN (reported, not the criterion)

| | n | events | Gross @ mid | Spread | Fees | NET | 95% CI |
|---|---:|---:|---:|---:|---:|---:|:--:|
| all four strategies (equal weight) | 5,529 | 945 | +0.6753 | −1.0006 | −1.2191 | **−1.5445** | [−3.0919, +0.0057] |

Weighted TRAIN nets: Brier −0.8476 [−2.8278, +1.1057], log −0.9897 [−2.5444, +0.5482],
Kelly −0.7740 [−3.1083, +1.5505]. TRAIN's gross-at-mid is ~4× HOLDOUT's (+0.675 vs +0.172)
and still does not cover the wedge. The direction of that drift — in-sample better than
out-of-sample — is the ordinary one.

### Totals

| split | trades | contracts (C=100) | total net |
|---|---:|---:|---:|
| TRAIN | 5,529 | 552,900 | **−$8,539.37** |
| HOLDOUT | 3,295 | 329,500 | **−$6,546.40** |

---

## 4. Sensitivities — every one of them is worse

HOLDOUT, equal weight, cents per contract:

| variant | n | NET | 95% CI |
|---|---:|---:|:--|
| **primary** (abstain band, C=100) | 3,295 | −1.9868 | [−3.9797, **+0.1359**] |
| naive `sign(p−q)`, crosses the spread (no abstain band) | 3,748 | −2.2571 | [−4.1580, **−0.3477**] |
| adverse fee block `C = 1` | 3,295 | −2.4689 | [−4.4572, **−0.4411**] |

Both alternatives are not merely negative but **significantly** negative — their CIs exclude
zero on the wrong side. The primary specification is the most generous one available under
the preregistration, and it is the only one whose CI even touches zero.

The abstain band's contribution is instructive: it removes 453 HOLDOUT trades (12%) and
improves net by 0.27¢/contract. Corollary 19's filter works — it is just far too small a
correction.

---

## 5. Fractional Kelly at absolute scale — where λ actually bites

λ cancels in both normalised statistics (§1.2), so the closed list's two Kelly variants are
indistinguishable there. At an absolute scale — λ·f of a fixed $1,000 per trade,
non-compounding, contracts floored — they separate in dollars but not in rate:

| split | λ | contracts | notional | net P&L | ROI |
|---|---:|---:|---:|---:|---:|
| HOLDOUT | 0.25 | 1,220,331 | $124,767.72 | **−$13,574.68** | **−10.88%** |
| HOLDOUT | 0.50 | 2,442,276 | $250,080.80 | **−$27,186.50** | **−10.87%** |
| TRAIN | 0.25 | 2,257,835 | $220,165.34 | −$19,347.46 | −8.79% |
| TRAIN | 0.50 | 4,518,367 | $441,258.79 | −$38,692.83 | −8.77% |

Doubling λ doubles the loss and leaves ROI unchanged to two decimals. This is Theorem 13's
scale-freedom showing up as an empirical fact: **λ is a volume knob on a negative-expectancy
book.** It is exactly the regime the research doc's Table 4 "Regime B" describes — proper
betting faithfully converting a non-edge into a loss.

---

## 6. Verdict against the preregistered criterion

> **PASS (E4)** iff any preregistered strategy shows positive **net** expectancy after costs
> on HOLDOUT with an event-clustered CI excluding zero.

| strategy | HOLDOUT net expectancy | CI excludes zero? | PASS? |
|---|---:|:--:|:--:|
| proper Brier | −1.9868 ¢/contract | no (spans 0) | **FAIL** |
| proper log | −1.9868 ¢/contract | no (spans 0) | **FAIL** |
| fractional Kelly λ=0.25 | −1.9868 ¢/contract | no (spans 0) | **FAIL** |
| fractional Kelly λ=0.50 | −1.9868 ¢/contract | no (spans 0) | **FAIL** |
| no-trade | 0 | — | n/a (baseline) |

**E4 FAILS.** Not one strategy has a positive point estimate, so the CI condition is never
reached. **The no-trade baseline dominates every active strategy on both splits.**

The honest characterisation of the strength of this result: on the primary specification the
losses are *point-estimate* losses whose CIs still span zero — this is a clear failure to
demonstrate profitability, and it is not, on the primary spec alone, a demonstration that
the strategies are significantly unprofitable. Under the two sensitivities (§4) and under
absolute Kelly sizing (§5) they *are* significantly negative. Add the unmeasured price
impact of §7 and every interval moves further left. Nothing here is close to the PASS line
from the correct side.

---

## 7. Limits, and the honest ones first

**Price impact is unmeasured and is not zero.** Our own market impact is unobservable in
this dataset: the frozen extract has a top-of-book quote and no depth ladder, and we have no
record of our own (nonexistent) orders. It is therefore reported as an **unmeasured adverse
term**, never as zero. `L_ρ ≥ 0` by construction — walking a book can only raise the entry
price. **The verdict's direction is one-way: any true value of `L_ρ` makes every net number
in this document worse, and none of them better.** A FAIL cannot be rescued by measuring it.
This matters more than usual here, because the research doc's §2.2 warning is exactly our
situation: the entire frictionless profit is divergence rent, and Corollary 15 says
divergence and `L_ρ` cancel exactly in the idealised AMM case. Our +0.17¢/contract of rent
is the first quantity impact would eat.

**Other limits:**

1. **Composition.** 10,053 of 10,285 rows are `baseball_evidence`; 232 are
   `soccer_evidence`. This is a baseball result with a soccer garnish, not a sports-wide one.
2. **The four strategies are not four experiments.** They share a trade set and differ only
   in weighting (§1.1). E4 has substantially less strategy diversity than the closed list
   makes it look like it has, and no rule choice can fix a gross that is 12.5× short.
3. **Settlement cost** is modelled as zero from a secondary-sourced schedule (§1.4);
   confirming the primary PDF can only move net down.
4. **Maker execution is not claimable.** The 4× cheaper maker leg would cut the fee from
   1.235¢ to ~0.309¢ — which still leaves net ≈ −1.06¢/contract against the same spread, so
   *even a counterfactual all-maker fill does not reach break-even here*. That is worth
   knowing: the fee is not the binding constraint on its own; fee **plus** spread is.
5. **Hold-to-settlement** is assumed. No exit-timing strategy was tested, and none is in the
   closed list.
6. **Crossed quotes excluded** (D-3) — 1.08% of rows, an aggregation artifact that would
   have manufactured negative spread cost.
7. **No strategy outside the closed list was tried**, in either specification or weighting,
   and no post-hoc subgroup was searched for a winner.

---

## 8. What this result is worth

It is a clean, informative negative, and it separates two things that are routinely
conflated:

1. **The theory is right and it is now verified on our data.** Lemma 9 holds row by row; the
   Bregman divergence is positive and *does* outweigh a negative score gap. Anyone claiming
   "ΔS < 0, therefore no transform can profit" is wrong, and we can now show they are wrong
   with our own numbers rather than by citation.
2. **And it does not matter.** The rescue is worth +0.17¢ per contract against a 2.16¢
   executable cost wedge, before an unmeasured adverse impact term. Convexity rent at our
   level of disagreement with the market is roughly an order of magnitude below the cost of
   trading.

For E4's contribution to the §6 stopping rule: **E4 does not pass.** The general lesson for
the market-structure lane the stopping rule redirects to is that any future candidate must
clear a ~2.2¢/contract executable wedge at these price levels — the bar the fee table in the
research doc's §7.4 predicted, now measured on our own tape rather than assumed.

**No result in this document authorizes capital under any outcome.**
