# EDGE-DISCOVERY-001 / E3 — preregistered conditional slices of ΔS

**Verdict: FAIL. No cell survives. Not one of the 35 evaluable cells is even
positive on TRAIN, so no cell reaches the confirmation stage at all.**

- Specification: `docs/experiments/EDGE-DISCOVERY-001-PREREGISTRATION.md` §4,
  with every bucket boundary fixed in §8 deviation **D-3**, committed
  (`a7b5039`) *before* the analysis script was written.
- Script: `docs/evidence/qdk-001/e3_slices.py`. Raw output:
  `docs/evidence/qdk-001/e3_slices_output.txt`. The cell table below is emitted
  by the script (`e3_cell_table.md`), not transcribed by hand.
- Interpreter: `/usr/local/bin/python3` (numpy 2.1.3). The repo venv at
  `.venv/bin/python` has neither numpy nor scipy; nothing was installed.
- **Authorizes no capital, no orders, no execution.**

---

## 1. What was measured

```
ΔS = S(q, y) − S(p, y),   S = Brier
```

ΔS > 0 means **our forecast beat the market** in that cell. ΔS < 0 means the
market beat us. The whole-sample value is **−0.01700**, reproducing the frozen
`DELTA_S_RESULT.md` figure exactly (TRAIN −0.01785, HOLDOUT −0.01556).

Data: the frozen `docs/evidence/qdk-001/edge_discovery_001_dataset.csv` — 10,285
observations, TRAIN 6,471 / HOLDOUT 3,814, 1,482 events, with no missing values
in any field this experiment uses.

## 2. Method

**Slices.** Nine of the ten preregistered slices ran. The closed list was not
extended, re-bucketed, or reordered after any result was seen. Cut points for
the continuous slices are TRAIN quantiles, computed on TRAIN only and applied
unchanged to HOLDOUT.

Slice 2 maps league from the `market_ticker` series prefix:
**`KXMLB*` → MLB**, **`KXWC*` → WC** (FIFA World Cup), **`KXMLS*` → MLS**.

**Slice 10 (resolution-clarity tier) is NOT RUN**, per the preregistration
clause permitting it only if an existing typed field supplies it. No such field
exists: `ranking.resolution_clarity_score()` is an explicit placeholder
returning the constant 0.5 for every market, and
`DomainMarketInventorySnapshot.resolution_clarity_proxy` is a
domain/series-cluster scouting aggregate, not a per-market property of these
forecasts. A proxy was **not** improvised.

**Floors.** A cell is evaluable only at **n ≥ 200 observations AND ≥ 50 events**,
required in **both** splits — a cell evaluable in only one split cannot be
discovered-then-confirmed. Cells below either floor are reported
`underpowered` and appear in the table with their counts, never as a result and
never as "trending".

**Clustering.** Every interval and p-value is an **event-level cluster
bootstrap**: 4,000 iterations per split, resampling that split's events with
replacement and recomputing each cell mean over the resampled rows. All cells
within a split share the same resample draws. p-values are two-sided, obtained
by recentring the resample distribution on the null (θ\* − θ̂) and asking how
often it reaches |θ̂|. Clustering is at **event** (1,482), never ticker (5,819).

Validation of the interval machinery: an independent analytic cluster-robust SE
of the mean gives HOLDOUT −0.01556, CI [−0.02018, −0.01094], against the
bootstrap's [−0.01973, −0.01135] — agreement to the third decimal. Re-running
the bootstrap under five different seeds moves interval endpoints by ~1e-4. No
verdict in this document sits near a decision boundary.

**Multiplicity.** **40 cells defined, 35 evaluable** — 35 is the BH denominator.
Benjamini–Hochberg FDR at **q = 0.10** is pooled across all 35 evaluable cells
in the whole family, not per slice. Intervals for a BH-selected set are
FCR-adjusted (Benjamini & Yekutieli 2005) at level 1 − R·q/m; for the 24 HOLDOUT
selections that level is 0.9314.

**Two-stage rule.** Discovery on TRAIN, confirmation on HOLDOUT. Promotion
requires TRAIN ΔS > 0 **and** HOLDOUT ΔS > 0 **and** BH selection with an
FCR-adjusted interval strictly above zero. No cell may be promoted on the data
that discovered it.

## 3. Result

**Cells defined: 40. Cells evaluable: 35. Cells underpowered: 5.**

- **Evaluable cells with ΔS > 0 on TRAIN: 0 of 35.**
- **Evaluable cells with ΔS > 0 on HOLDOUT: 0 of 35.**
- **Cells promoted: 0.**

The discovery stage returns an empty set. There was nothing for HOLDOUT to
confirm or refute. The multiplicity correction, the two-stage design and the FCR
intervals were all applied as preregistered, but the result did not depend on
any of them: every evaluable cell has a negative point estimate in both splits.

Of the 35 evaluable cells, BH at q = 0.10 selects 29 on TRAIN and **24 on
HOLDOUT** — every one of them significant in the direction **ΔS < 0, the market
beating us**. The remaining 11 HOLDOUT cells are indistinguishable from zero.
None is positive.

### Full cell table

Every evaluable cell is listed, not only the interesting ones. TRAIN interval is
a plain 95% percentile interval; the HOLDOUT interval is FCR-adjusted (0.9314)
for BH-selected cells and a plain 95% interval otherwise.

| slice | cell | n_obs (tr/ho) | n_events (tr/ho) | TRAIN ΔS [95% CI] | HOLDOUT ΔS [CI] | BH adj p (ho) | verdict |
|---|---|---|---|---|---|---|---|
| 1. forecaster | baseball_evidence | 6239/3814 | 906/537 | -0.01668 [-0.02111, -0.01240] | -0.01556 [-0.01973, -0.01135] | 0.0007 | market better (BH-signif, ΔS<0) |
| 1. forecaster | soccer_evidence | 232/0 | 53/0 | — | — | — | **underpowered** |
| 2. sport / league | MLB | 6239/3814 | 906/537 | -0.01668 [-0.02111, -0.01240] | -0.01556 [-0.01973, -0.01135] | 0.0007 | market better (BH-signif, ΔS<0) |
| 2. sport / league | WC | 229/0 | 51/0 | — | — | — | **underpowered** |
| 2. sport / league | MLS | 3/0 | 2/0 | — | — | — | **underpowered** |
| 3. time-to-resolution | hours_to_close [-inf, 0.6594)h | 1294/803 | 605/345 | -0.04706 [-0.05657, -0.03754] | -0.04056 [-0.05077, -0.03031] | 0.0007 | market better (BH-signif, ΔS<0) |
| 3. time-to-resolution | hours_to_close [0.6594, 1.2698)h | 1294/833 | 593/349 | -0.01713 [-0.02694, -0.00766] | -0.01296 [-0.02138, -0.00454] | 0.0102 | market better (BH-signif, ΔS<0) |
| 3. time-to-resolution | hours_to_close [1.2698, 1.9422)h | 1293/772 | 586/367 | -0.01389 [-0.02121, -0.00688] | -0.00561 [-0.01446, +0.00391] | 0.2465 | no effect |
| 3. time-to-resolution | hours_to_close [1.9422, 2.5212)h | 1295/688 | 605/326 | -0.00960 [-0.01633, -0.00280] | -0.01210 [-0.01927, -0.00489] | 0.0051 | market better (BH-signif, ΔS<0) |
| 3. time-to-resolution | hours_to_close [2.5212, +inf)h | 1295/718 | 485/276 | -0.00156 [-0.00714, +0.00461] | -0.00464 [-0.01170, +0.00274] | 0.2280 | no effect |
| 4. market-probability decile | q [-inf, 0.065) | 604/272 | 363/173 | -0.03330 [-0.04192, -0.02564] | -0.02336 [-0.03131, -0.01522] | 0.0007 | market better (BH-signif, ΔS<0) |
| 4. market-probability decile | q [0.065, 0.16) | 677/394 | 406/230 | -0.01666 [-0.02797, -0.00577] | -0.01714 [-0.02580, -0.00757] | 0.0033 | market better (BH-signif, ΔS<0) |
| 4. market-probability decile | q [0.16, 0.24) | 639/441 | 383/262 | -0.01994 [-0.02884, -0.01115] | -0.00994 [-0.02195, +0.00249] | 0.1445 | no effect |
| 4. market-probability decile | q [0.24, 0.315) | 632/403 | 419/262 | -0.00298 [-0.01120, +0.00507] | -0.00856 [-0.01876, +0.00207] | 0.1445 | no effect |
| 4. market-probability decile | q [0.315, 0.39) | 676/399 | 453/275 | -0.00354 [-0.01110, +0.00412] | -0.00574 [-0.01460, +0.00390] | 0.2465 | no effect |
| 4. market-probability decile | q [0.39, 0.465) | 604/349 | 433/265 | -0.01084 [-0.02015, -0.00232] | -0.00561 [-0.01502, +0.00371] | 0.2465 | no effect |
| 4. market-probability decile | q [0.465, 0.555) | 686/375 | 431/242 | -0.00378 [-0.01176, +0.00408] | -0.00749 [-0.01788, +0.00275] | 0.1713 | no effect |
| 4. market-probability decile | q [0.555, 0.67) | 658/393 | 442/266 | -0.01056 [-0.02043, -0.00107] | -0.00556 [-0.01606, +0.00519] | 0.3082 | no effect |
| 4. market-probability decile | q [0.67, 0.82) | 647/381 | 409/259 | -0.02138 [-0.03439, -0.00887] | -0.01941 [-0.03272, -0.00640] | 0.0115 | market better (BH-signif, ΔS<0) |
| 4. market-probability decile | q [0.82, +inf) | 648/407 | 399/247 | -0.05732 [-0.07010, -0.04425] | -0.05350 [-0.06535, -0.04171] | 0.0007 | market better (BH-signif, ΔS<0) |
| 5. \|p-q\| disagreement | \|p-q\| [-inf, 0.0222) | 1293/712 | 561/340 | -0.00005 [-0.00063, +0.00052] | -0.00033 [-0.00120, +0.00057] | 0.4771 | no effect |
| 5. \|p-q\| disagreement | \|p-q\| [0.0222, 0.0538) | 1294/826 | 606/366 | -0.00102 [-0.00301, +0.00107] | -0.00194 [-0.00449, +0.00050] | 0.1575 | no effect |
| 5. \|p-q\| disagreement | \|p-q\| [0.0538, 0.0935) | 1295/791 | 605/357 | -0.00778 [-0.01150, -0.00394] | -0.00557 [-0.01052, -0.00047] | 0.0685 | market better (BH-signif, ΔS<0) |
| 5. \|p-q\| disagreement | \|p-q\| [0.0935, 0.16) | 1287/756 | 597/365 | -0.01156 [-0.01832, -0.00476] | -0.01136 [-0.01926, -0.00331] | 0.0171 | market better (BH-signif, ΔS<0) |
| 5. \|p-q\| disagreement | \|p-q\| [0.16, +inf) | 1302/729 | 603/345 | -0.06846 [-0.08671, -0.05008] | -0.06107 [-0.07661, -0.04538] | 0.0007 | market better (BH-signif, ΔS<0) |
| 6. spread | spread_avg [-inf, 1)c | 0/0 | 0/0 | — | — | — | **underpowered** |
| 6. spread | spread_avg [1, 1.3333)c | 2579/1473 | 832/485 | -0.02148 [-0.02793, -0.01522] | -0.01740 [-0.02347, -0.01131] | 0.0007 | market better (BH-signif, ΔS<0) |
| 6. spread | spread_avg [1.3333, 2.2)c | 1228/811 | 574/343 | -0.01534 [-0.02177, -0.00866] | -0.01448 [-0.02204, -0.00721] | 0.0017 | market better (BH-signif, ΔS<0) |
| 6. spread | spread_avg [2.2, 4.4)c | 1345/840 | 507/320 | -0.02045 [-0.02853, -0.01302] | -0.01911 [-0.02672, -0.01106] | 0.0007 | market better (BH-signif, ΔS<0) |
| 6. spread | spread_avg [4.4, +inf)c | 1319/690 | 470/273 | -0.01042 [-0.01754, -0.00369] | -0.00858 [-0.01642, -0.00055] | 0.0714 | market better (BH-signif, ΔS<0) |
| 7. depth / liquidity | liquidity_avg [-inf, 150010) | 1294/535 | 442/236 | -0.01584 [-0.02405, -0.00828] | -0.00788 [-0.01644, +0.00110] | 0.1092 | no effect |
| 7. depth / liquidity | liquidity_avg [150010, 580033) | 1294/962 | 479/297 | -0.01465 [-0.02197, -0.00769] | -0.01887 [-0.02618, -0.01178] | 0.0007 | market better (BH-signif, ΔS<0) |
| 7. depth / liquidity | liquidity_avg [580033, 1.31259e+06) | 1294/690 | 513/289 | -0.01827 [-0.02552, -0.01100] | -0.01593 [-0.02426, -0.00753] | 0.0012 | market better (BH-signif, ΔS<0) |
| 7. depth / liquidity | liquidity_avg [1.31259e+06, 3.07012e+06) | 1294/870 | 557/356 | -0.01648 [-0.02459, -0.00882] | -0.01027 [-0.01840, -0.00182] | 0.0358 | market better (BH-signif, ΔS<0) |
| 7. depth / liquidity | liquidity_avg [3.07012e+06, +inf) | 1295/757 | 477/312 | -0.02398 [-0.03393, -0.01420] | -0.02252 [-0.02974, -0.01506] | 0.0007 | market better (BH-signif, ΔS<0) |
| 8. favourite vs underdog | favourite (q > 0.5) | 2323/1385 | 789/469 | -0.02512 [-0.03259, -0.01775] | -0.02316 [-0.03064, -0.01556] | 0.0007 | market better (BH-signif, ΔS<0) |
| 8. favourite vs underdog | underdog (q <= 0.5) | 4148/2429 | 923/519 | -0.01377 [-0.01862, -0.00929] | -0.01123 [-0.01588, -0.00640] | 0.0007 | market better (BH-signif, ΔS<0) |
| 9. forecast direction | p > q (above market) | 3694/2190 | 900/511 | -0.01705 [-0.02382, -0.01083] | -0.00848 [-0.01431, -0.00217] | 0.0217 | market better (BH-signif, ΔS<0) |
| 9. forecast direction | p < q (below market) | 2711/1598 | 850/493 | -0.01936 [-0.02620, -0.01238] | -0.02552 [-0.03306, -0.01763] | 0.0007 | market better (BH-signif, ΔS<0) |
| 9. forecast direction | p == q (exact tie) | 66/26 | 27/22 | — | — | — | **underpowered** |
| 10. resolution clarity | — | — | — | — | — | — | **NOT RUN** (no typed field) |

## 4. Regression to the mean — the guard that mattered, and what it found

The prior EDGE-SELECTION failure was cohort rankings that inverted out of
sample, with a "negative control" that was merely the in-sample worst cohort
regressing upward. This experiment was built expecting that pattern. **It did
not occur, and the reason is worth stating precisely.**

Across the 35 evaluable cells, the correlation between TRAIN ΔS and HOLDOUT ΔS
is **r = +0.957**. The cell structure is highly stable out of sample. That is
*not* a signal — every cell is negative in both splits, so what replicates is
the ordering of *how badly we lose*, not any edge. A stable ranking of losses is
still a ranking of losses.

This makes the finding more robust than a noisy null would have been. Had the
cells been pure noise we would expect near-zero TRAIN/HOLDOUT correlation and a
few cells drifting positive by chance. Instead the negative ΔS reproduces cell
by cell, in both splits, across nine independent partitions of the same data.

The best TRAIN cell in the entire family is `|p−q|` bottom quintile at
**−0.00005**, which confirms to **−0.00033** on HOLDOUT. Its interval spans zero
in both splits. This is not a marginal edge; it is the mechanical statement that
when our forecast agrees with the market to within 2.2 percentage points, our
Brier score *is* the market's Brier score. **The only cell where we stop losing
is the one where we say nothing different.**

## 5. Structure in the losses (descriptive, not a signal)

Reported because it is informative about *why* the forecasts lose, not because
any of it is exploitable. Every figure below is ΔS < 0, and every one replicates
across splits:

- **Disagreement magnitude is the strongest gradient.** ΔS falls monotonically
  from −0.00005 (agreement ≤ 2.2pp) to **−0.06846 / −0.06107** in the top
  `|p−q|` quintile. The more the model departs from the market, the worse it
  does — the departures are noise, not information, and their cost scales with
  their size.
- **Confident markets are where the model does most damage.** The extreme `q`
  deciles are the worst cells: q ≥ 0.82 gives −0.05732 / −0.05350, q < 0.065
  gives −0.03330 / −0.02336. The middle deciles (0.24–0.67) are closest to zero.
  The model is miscalibrated at the tails, exactly where the market is sharpest.
- **Time-to-resolution runs the same way**: the nearest-to-close quintile
  (< 0.66h) is −0.04706 / −0.04056, decaying to −0.00156 / −0.00464 in the
  farthest quintile. Close to resolution the market incorporates information the
  model does not have.
- **Direction is symmetric.** Forecasting above the market (−0.01705 / −0.00848)
  and below it (−0.01936 / −0.02552) both lose. The ΔS instrument shows no fade
  signal: neither side of the disagreement is the profitable one.
- **Spread and liquidity are flat.** All four populated spread cells and all
  five liquidity cells sit in a narrow −0.009 to −0.024 band with no monotone
  pattern. The loss is not a microstructure artifact of thin or wide markets.

The last two points bear on the §6 stopping rule: there is no corner of this
data — not illiquid markets, not wide spreads, not one direction of disagreement
— where the forecasts are merely neutral rather than actively harmful.

## 6. Limits, stated plainly

1. **Slice 10 never ran.** Resolution clarity is untested. If clarity is the
   moderator that matters, this experiment could not have found it.
2. **Slices 1 and 2 are nearly the same slice.** `soccer_evidence` maps almost
   exactly onto the WC/MLS tickers, so both collapse to a single evaluable cell
   (MLB, 6,239/3,814) carrying identical numbers. The family therefore contains
   one redundant evaluable cell, which makes BH marginally conservative.
3. **Soccer is untested out of sample.** All 232 soccer observations fall in
   TRAIN; HOLDOUT contains zero. The sport is underpowered by construction here,
   not found wanting.
4. **Time-to-resolution spans hours, not days.** 92% of rows sit under 3h to
   close, so the five quintiles cover roughly 0–0.66h through 2.5h+. This slice
   tests intraday timing only; genuinely long-dated forecasts are not in the
   sample. Fixed wall-clock buckets were considered and rejected in D-3 before
   running, because they would have put three of five cells under the floor.
   The bucketing is a real weakness of the slice, noted rather than re-cut.
5. **14 of 1,482 events (0.9%) have rows in both splits**, since the split is by
   forecast timestamp and one event can be forecast on both sides of 2026-08-02.
   The two stages are therefore not perfectly independent. At 0.9% this cannot
   manufacture the observed result, and it biases toward TRAIN/HOLDOUT
   agreement — the direction that would have helped a false positive survive,
   not one that would suppress a real edge.
6. **The spread slice yields 4 populated cells, not 5.** `spread_avg` has heavy
   ties at 1.0, so the bottom TRAIN quintile edge collapses and the nominal
   first bin is empty. Reported as a 0-observation underpowered cell.
7. **ΔS is a scoring-rule measurement at midpoint, not a P&L.** It answers
   whether `p` is a better probability than `q`. Executable prices, spread and
   fees belong to E4. Nothing here speaks to tradability in either direction.
8. **This is one instrument on one sample** — 1,482 events over roughly six
   weeks, dominated by MLB.

## 7. Verdict against the preregistered criterion

> **PASS (E3)** iff at least one cell shows ΔS > 0 on TRAIN and confirms ΔS > 0
> on HOLDOUT with an FDR-corrected event-clustered CI excluding zero.

Zero cells show ΔS > 0 on TRAIN. Zero cells show ΔS > 0 on HOLDOUT.

# **E3: FAIL — no cell survives.**

The negative ΔS result does not slice. It is not concentrated in a forecaster, a
league, a horizon, a probability region, a disagreement band, a spread regime, a
liquidity regime, a side of the book, or a direction of disagreement. It is
uniform, it replicates out of sample at r = +0.957, and it is worst exactly
where the model is most assertive.

E3 contributes no pass to the §6 stopping rule.
