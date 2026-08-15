# EDGE-DISCOVERY-001 — VERDICT

**All four preregistered experiments FAIL. The §6 stopping rule triggers.**

Protocol: `EDGE-DISCOVERY-001-PREREGISTRATION.md`, committed at `16abe02`
**before** any experiment ran, against a frozen dataset (`b8f4e93`, 10,285
observations / 1,482 events). Ten deviations logged, every one **before** the
affected number was reported.

---

## 1. The four verdicts

| | question | verdict | the number |
|---|---|---|---|
| **E1** | does `p` add information given `q`? | **FAIL** | holdout Brier: market `0.179173` vs two-term `0.180145` (worse). `β_d = +0.132`, CI **[−0.037, +0.316]** — includes zero |
| **E2** | does `p` lead the market? | **FAIL on cost** | lead **+0.0236** at 1h vs cost floor **0.0336** — 70% of what it needs. Positive at **all six** horizons |
| **E3** | any conditional regime? | **FAIL** | **0 of 35** evaluable cells positive on TRAIN. Discovery returned empty |
| **E4** | does any proper transform profit? | **FAIL** | net **−1.99¢/contract**; no-trade dominates on both splits |

No experiment passed. Per §6 the sports forecasting lane earns no further
investment.

---

## 2. The lane did NOT fail for the reason we assumed

Three findings complicate the simple reading, and all three point the same way.

**E2 found a real, replicating lead — not a fade.** `E[sign(d)·Δq]` is positive
at all six horizons, holdout CIs exclude zero independently at 5m/15m/30m/1h,
and it survives residualising `Δq` on `q` at p ≤ 0.0008. The market moves
*toward* our disagreement. The preregistered fade hypothesis is affirmatively
not what the data show. **It failed on economics at ~70% of the cost floor, not
on absence of signal.**

**E4 confirmed the theory it was sent to test.** Score gap −0.0345 plus Bregman
divergence +0.0396 sums to **+0.0051 — positive**. Negative ΔS genuinely does
not imply every betting transform loses. The divergence rent is simply worth
**+0.17¢/contract against a 2.16¢ executable wedge** — about **12.5× too
small**. The mechanism is real and economically irrelevant here.

**E1's "redundancy" reading is weaker than it appears.** `β_d = +0.132` with CI
**[−0.037, +0.316]** is *not significantly distinguishable from zero* — which is
not the same as *zero*. E1 was underpowered against settlement noise. E2 detects
the related effect against `Δq`, a far lower-variance target. The honest joint
statement is:

> **`d` contains real information — detectable against price, too small to
> detect against settlement, and insufficient to pay costs either way.**

---

## 3. One mechanism explains all four results

E1's exploratory regression is the key: `logit(p) = −0.094 + 0.568·logit(q)`,
**R² = 0.661**. Our forecast is a **shrunk copy of the market** — two-thirds of
its variance is the price it is meant to beat, same side of 0.5 on 87.6% of
rows. A slope of 0.568 pulls `p` toward 0.5, so `d` almost always points at the
mid.

- **E2**: `Δq` also drifts toward the mid, so `d` "predicts" price. Genuine, but
  30–36% of it was that shrinkage meeting mean reversion.
- **E3**: at settlement, shrinking away from a calibrated price is pure loss —
  hence loss rising **monotonically** with `|p−q|` (top quintile −0.068).
- **E1**: `β_q = 1.013`, CI [0.933, 1.100] — the market needed no recalibration,
  so there was nothing for the shrinkage to correct. Refitting the market made
  holdout Brier *significantly worse*.

**The model is the market, blurred.** Blurring a calibrated price loses at
resolution while still tracking its short-run drift.

---

## 4. Why this is not a noisy null

E3 was designed expecting regression to the mean, the failure that destroyed the
EDGE-SELECTION lane. **It did not occur.** TRAIN/HOLDOUT correlation of cell ΔS
across 35 cells is **r = +0.957**. What replicates out of sample is the ordering
of *how badly the model loses*. A noisy null would show near-zero correlation
with a few cells drifting positive by chance.

The best cell in the entire family is `|p−q|` **bottom quintile** (TRAIN
−0.00005, HOLDOUT −0.00033, spanning zero): **the only place the model stops
losing is where it agrees with the market to within 2.2pp — where it says
nothing different.**

Spread and liquidity slices are flat, so there is **no illiquid or wide-spread
corner** where the forecasts are merely neutral rather than harmful.

---

## 5. The cost wedge, measured

- Mean realised taker fee **1.75–1.84¢** *exceeds* mean half-spread **1.52¢** —
  the binding constraint is the `ceil_to_cent` **fee minimum**, which does not
  improve with better execution.
- A counterfactual **all-maker** fill (fee 1.235¢ → 0.309¢) still leaves net
  ≈ **−1.06¢**. Fees alone are not binding; **fees plus spread** are.
- E4's primary specification was the **most generous** available: both
  sensitivities (no-abstain, adverse `C=1` fee block) are *significantly*
  negative.
- Price impact is carried throughout as an **unmeasured adverse term, never
  zero**. `L_ρ ≥ 0` is one-way, so measuring it cannot rescue the verdict.

---

## 6. Actions taken (per §6)

1. **STOP developing the sports forecasting models.** No further effort on
   making them more accurate. Current forecasts retain **zero authorization path
   to capital**; nothing here creates one.
2. **DO NOT delete them.** They continue at low frequency as **scientific
   controls and regression benchmarks**, so any future technique can be scored
   instantly against `market` / `old forecaster` / `new forecaster`.
3. **The ΔS instrument is the durable asset.** `docs/evidence/qdk-001/` holds a
   90-second, read-only, market-relative falsification harness. It is the thing
   this milestone should be remembered for.
4. Redirect to market-structure alpha families: microstructure, structural
   probability inconsistencies, calibration residuals, information-arrival.

---

## 7. Scope of the claim — what was NOT shown

- **Not shown: LLMs cannot have prediction-market edge.** This is **sports** —
  baseball and soccer — the domain the calibration literature reports markets are
  *already* best calibrated in, and E1 measured exactly that (`β_q ≈ 1`). It is
  the hardest case, deliberately.
- **Not shown: no edge exists at other horizons or venues.** E2's 3h and 6h
  horizons are underpowered by construction (6.2% / 2.8% coverage, D-2).
- **Soccer is untested out of sample** — all 232 rows fall in TRAIN.
- Metrics are Brier and log loss; the Kelly identity `g(f*) = KL(p‖q)` is stated
  in log score. The **sign** carries; the identity is not literally reproduced.
- 14/1,482 events (0.9%) span both splits, biasing toward TRAIN/HOLDOUT
  agreement — the direction that would have *helped* a false positive survive.

---

## 8. Doctrine confirmed by this milestone

> **No signal graduates because it looks predictive.** Every signal must defeat
> the strongest available contemporaneous market baseline before any execution
> engineering or capital allocation begins.

> **Before declaring a required dataset unavailable, exhaustively inspect raw,
> derived, aggregate, archival and observability stores.** `MarketPriceTickBucket`
> was present the whole time and turned a question thought to need years of
> collection into a query.

Adds a third, earned here:

> **A signal can be real, replicating, and still uneconomic.** Report the cost
> floor next to the effect size, always. E2's lead is genuine and reproduces out
> of sample; it is 70% of what it needs, and 70% is a loss.
