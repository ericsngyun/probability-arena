# EDGE-DISCOVERY-001 — preregistration

**Status: PREREGISTERED. Written and committed BEFORE any of the four
experiments below was executed.** Nothing in this document was chosen after
seeing a result. The only prior data inspection was counts and dates, used for
power planning, deliberately reading no outcome column (`shape.py`, reproduced
in `docs/evidence/qdk-001/`).

**Authorizes no capital, no orders, no execution.** Every capability in
`docs/SAFETY_BOUNDARIES.md` remains as governed there.

---

## 0. What has already been established, and what has NOT

**Established** (`docs/evidence/qdk-001/DELTA_S_RESULT.md`): our source-backed
forecasts are *worse than the contemporaneous market as standalone probability
estimates*. Strict no-lookahead ΔS = −0.01700, and the sign is not in doubt.

**NOT established, and the reason this milestone exists:** that `p − q` carries
no information *conditional on* `q`. A forecaster can be worse than the market
standalone and still hold incremental or conditionally exploitable information.
The prior session's phrasing "there is no edge to transform" **overreached** and
is retracted; it tested `p` against `q`, never `p − q` given `q`.

The `ΔS` result is nonetheless strong enough that **current forecasts have zero
authorization path to capital**, and this milestone does not create one.

### The canonical evidence hierarchy (adopted)

```
beats base rate  <  beats naïve model  <  beats market price
                 <  survives executable price  <  survives fees/slippage
                 <  prospective positive expectancy
```

Only the last two levels bear on capital. The repo's existing
`brier_skill_vs_base_rate` figures (baseball +0.2286, soccer +0.2434) sit at
level 1 and must never be read as market-relative skill.

---

## 1. Dataset, frozen

- **Population:** `market_forecasts` with `evidence_depth='source_backed'`, joined
  to a resolved `market_outcomes` (`winning_side in ('yes','no')`), matched to a
  `market_price_tick_buckets` row with a two-sided quote.
- **`q` (market probability):** `(close_bid + close_ask) / 200`.
- **No-lookahead rule:** the matched bucket must **END strictly before** the
  forecast's `created_at`, within **900 s**. This is the primary specification.
- **Matched n = 10,285** observations, **5,819 market tickers**, **1,482 events**.
- **Date range:** 2026-07-06 → 2026-08-15.

### Clustering — binding

All standard errors and confidence intervals are **cluster-robust at the EVENT
level**, where event = `market_ticker` minus its final `-<segment>`. There are
**1,482 events** against 5,819 tickers, so ticker-level clustering is
anticonservative by roughly 2× and is **not permitted** as a headline.

### Chronological split — frozen now

- **TRAIN:** `created_at < 2026-08-02` — n = 6,685
- **HOLDOUT:** `created_at >= 2026-08-02` — n = 3,600

Every fitted parameter, threshold, and subgroup choice is derived on TRAIN only.
HOLDOUT is touched exactly once per experiment, at evaluation.

### Numerical guards

`q` and `p` clipped to `[0.01, 0.99]` before any logit. Clipping counts reported.

---

## 2. E1 — Does the model add information beyond the market?

**The most important test.** Fit on TRAIN only:

```
logit P(Y=1) = α + β_q · z_q + β_d · d
z_q = logit(q)        d = logit(p) − logit(q)
```

Evaluate on HOLDOUT: Brier and log loss for (a) market alone `q`, (b) the fitted
two-term model, (c) our `p` alone.

**PASS (E1)** iff the fitted model beats **market alone** on HOLDOUT on **both**
Brier and log loss, with event-clustered 95% CIs on the paired differences
excluding zero.

Also reported regardless: `β_d` with event-clustered CI. `β_d ≈ 0` plus no
holdout improvement is the strong death certificate — informational redundancy,
not merely inferiority.

**Pre-committed interpretation if E1 passes:** the correct architecture was never
"replace `q` with `p`". It is `p_final = F(q, agent residual)` — market as prior,
agent permitted to move it only where historically justified.

---

## 3. E2 — Does the forecast LEAD the market?

At forecast time `t`, `d_t = p_t − q_t`. For horizons
**h ∈ {5m, 15m, 30m, 1h, 3h, 6h}** compute `Δq_{t,h} = q_{t+h} − q_t` from
`MarketPriceTickBucket` (nearest bucket ending at or after `t+h`, tolerance
±½h, else typed-absent).

Primary statistic per horizon: `E[sign(d_t) · Δq_{t,h}]`, event-clustered.
Secondary: magnitude-weighted `E[d_t · Δq_{t,h}] / E[|d_t|]`.

**Multiple testing:** 6 horizons → **Holm–Bonferroni** at family α = 0.05.

**PASS (E2)** iff at least one horizon shows `|E[sign(d)·Δq]|` **exceeding the
executable cost floor** (half-spread + taker fee at that price, §5) with a
Holm-corrected CI excluding zero.

**A NEGATIVE value is a finding, not a failure.** If disagreement predicts the
market moving *against* the model, that is a fade signal and must be reported as
prominently as a positive one. Direction is not preregistered; existence is.

---

## 4. E3 — Preregistered conditional slices

**Closed list. No slice may be added after any result is seen.**

1. forecaster
2. sport / league
3. time-to-resolution (bucketed)
4. market-probability decile
5. `|p − q|` disagreement magnitude (quintile)
6. spread (quintile)
7. depth / liquidity (quintile)
8. favourite vs underdog (`q` ≷ 0.5)
9. forecast direction relative to market (`sign(p − q)`)
10. resolution-clarity tier, **if and only if** an existing typed field supplies
    it; otherwise recorded as not-run rather than improvised.

Per cell: `ΔS = S(q,y) − S(p,y)` with event-clustered CI.

**Floors:** a cell is evaluable only at **n ≥ 200 observations AND ≥ 50 events**.
Below either floor the cell is reported `underpowered`, never as a result.

**Correction:** Benjamini–Hochberg FDR at **q = 0.10** across all evaluable cells,
pooled across the whole family — not per slice.

**Two-stage rule:** a cell is *discovered* on TRAIN and *confirmed* on HOLDOUT.
**No cell may be promoted on the data that discovered it.**

**PASS (E3)** iff at least one cell shows ΔS > 0 on TRAIN and confirms ΔS > 0 on
HOLDOUT with an FDR-corrected event-clustered CI excluding zero.

---

## 5. E4 — Proper-betting decomposition, at executable prices

Implemented observationally, not cited. Strategies, closed list:

- proper **Brier** transform
- proper **log** transform
- **fractional Kelly** at λ ∈ {0.25, 0.50} on `p`
- **no-trade** baseline

**Executable prices, not midpoint.** Buying YES pays `close_ask`; selling YES
receives `close_bid`; the NO side likewise. Midpoint remains valid for judging
the market *as a forecaster* (E1–E3) and is invalid for profitability (E4).

**Fees — verified from Kalshi's primary schedule, effective 2026-07-07:**

```
taker: round up(M × 0.07   × C × P × (1−P))     M default 1
maker: round up(M × 0.0175 × C × P × (1−P))     M default 0
```

Taker only (queue position is unobtainable on Kalshi L2, so maker fills are
unfalsifiable under observe-only). Per-market fee variation must be read from
market state where available rather than assumed universal; any assumption is
declared as an **adverse bound**, never a zero.

**Reported decomposition:** Score Gap · Divergence · Spread · Fees · Price Impact
· **Net P&L** (the only headline).

**PASS (E4)** iff any preregistered strategy shows positive **net** expectancy
after costs on HOLDOUT with an event-clustered CI excluding zero.

---

## 6. THE STOPPING RULE — binding

The sports forecasting lane earns further investment **only if at least one of
E1, E2, E3, E4 passes on genuinely held-out data**, by the criteria above.

**If all four fail:**

- **STOP developing the sports forecasting models.** Zero further effort on
  making them "smarter".
- **Do NOT delete them.** Run at low frequency as scientific controls and
  regression benchmarks, so any future technique can be compared instantly
  against `market` / `old forecaster` / `new forecaster` using the ΔS
  instrument.
- Redirect to market-structure alpha families: microstructure, structural
  probability inconsistencies, calibration residuals, information-arrival.

**No result from this milestone authorizes capital under any outcome.**

---

## 7. Adopted doctrine

> **No signal graduates because it looks predictive.** Every signal must defeat
> the strongest available contemporaneous market baseline before any execution
> engineering or capital allocation begins.

> **Before declaring a required dataset unavailable, exhaustively inspect raw,
> derived, aggregate, archival and observability stores.** `MarketPriceTickBucket`
> was present the whole time, survived the OPS-014 pruning, and converted a
> question thought to need years of new collection into a query.

---

## 8. Deviations

Any departure from this document must be recorded here, with reason and
timestamp, **before** the affected result is reported. An unlogged deviation
invalidates the affected experiment — the EDGE-SELECTION retirement was itself
an unlogged deviation and that is not repeated here.

### D-1 — split counts differ from the planning figures (2026-08-15, pre-results)

§1 states TRAIN n = 6,685 / HOLDOUT n = 3,600. The frozen extract gives
**TRAIN 6,471 / HOLDOUT 3,814**. Total (10,285), event count (1,482) and the
split *rule* (`created_at < 2026-08-02`) are unchanged.

Cause: the planning script (`shape.py`) derived the date from an index
percentile over a float array; the extractor applies the literal date rule to
tuple-keyed buckets, which matches marginally differently at bucket boundaries.
**The preregistered object is the RULE, not the descriptive counts** — the rule
was applied exactly as written and was not re-chosen after seeing anything. No
outcome column was read at either step. Logged rather than silently corrected.

### D-2 — E2 horizon coverage is uneven, and two horizons are near-unusable

Measured on the frozen extract, before any E2 statistic was computed:

| horizon | rows with `q_{t+h}` | coverage |
|---|---|---|
| 5m  | 3,934 | 38.2% |
| 15m | 9,083 | **88.3%** |
| 30m | 8,183 | **79.6%** |
| 1h  | 6,285 | 61.1% |
| 3h  |   641 | 6.2% |
| 6h  |   292 | 2.8% |

Consequence: the 3h and 6h horizons are **underpowered by construction** and
will be reported as such rather than as null results. The Holm family remains
all six horizons as preregistered — the correction is **not** narrowed to the
well-covered horizons, since narrowing it after seeing coverage would inflate
the family-wise error rate. Coverage is a property of the tick-bucket record,
not of the forecasts, so it is non-differential with respect to `d`.

> **Numbering note.** E1–E4 ran in parallel against this frozen document, so
> each assigned its own deviation numbers independently and they collide. The
> labels below are scoped by experiment — `D-3 (E1)`, `D-3 (E2)` — so that every
> cross-reference in the individual result documents still resolves. No
> deviation text was altered in resolving the collision.

### D-3 (E1) — E1 additions, logged pre-report (2026-08-15)

The E1 test in §2 was run **exactly as written** — same model, same TRAIN-only
fit, same single HOLDOUT evaluation, same PASS criterion, event-clustered CIs.
Nothing below altered it. Logged anyway, before the numbers were reported.

1. **Market-only model added as a decomposition control.** §2 specifies only
   the two-term model and the three scored predictors. A third TRAIN fit,
   `α + β_q·z_q`, is reported alongside them so that "two-term beats raw `q`"
   can be split into a **market-recalibration** effect (`α + β_q·z_q` vs raw
   `q`) and an **agent-information** effect (two-term vs `α + β_q·z_q`).
   Without it, a win driven purely by `β_q ≠ 1` would be misread as a finding
   about the forecaster when it is a finding about the market. This is an
   addition to the reported quantities, not a change to the PASS criterion.

2. **CI machinery.** §1 permits either machinery; the **cluster bootstrap**
   (2,000 event resamples for coefficients with refit, 10,000 for the paired
   holdout differences) is the headline, with a **CR1 cluster-robust sandwich**
   reported beside the train coefficients as an independent cross-check. The
   two agree closely.

3. **One exploratory alternative, clearly quarantined.** An offset model
   `logit(q) + β_d·d` (market pinned at its raw price, one free parameter) is
   reported in `docs/evidence/qdk-001/e1_exploratory_addendum.py` and labelled
   **exploratory, not preregistered**. It exists because it is the most
   favourable honest test of the agent — it cannot be penalised by base-rate
   drift or by a failed recalibration of `q`. It graduates nothing and did not
   inform the verdict. It is also null.

4. **Interpreter.** The repo `.venv` has no `numpy`; the analysis was run on
   `/usr/local/bin/python3` (numpy 2.1.3). No package was installed. Scripts
   are numpy-only and stdlib-CSV based; no statistical library was relied on.

### D-3 (E2) — E2 cost-floor aggregation and Holm CI construction, specified (2026-08-15, pre-results)

§3 requires `|E[sign(d)·Δq]|` to exceed "the executable cost floor (half-spread
+ taker fee at that price, §5)" with "a Holm-corrected CI excluding zero". It
does not say how a **per-observation** floor aggregates into that comparison,
nor how a Holm-corrected *interval* is constructed from a step-down procedure.
Both are specified here, written and committed **before any E2 statistic was
computed** (script `docs/evidence/qdk-001/e2_leadlag.py`, committed in the same
commit as this entry):

1. **The floor is computed per observation at its own price and spread**, never
   as a global constant. `floor_i = half_spread_i + taker_fee_i`, with
   `half_spread_i = (yes_ask_c − yes_bid_c) / 200` in probability units and
   `taker_fee_i = ceil_to_cent(1 × 0.07 × 1 × P_i × (1−P_i))` in dollars, which
   equals probability units for a $1-notional contract.
2. **Two fee prices are reported.** *Primary* prices the fee at the mid `q_i`
   (internally consistent: the half-spread term already carries the
   mid→executable crossing cost). *Adverse* prices it at the executable price
   actually paid for the implied direction — buy YES at the ask, buy NO at
   `(100 − bid)/100` — per §5's instruction that any assumption be declared as
   an adverse bound, never a zero. **The adverse floor governs the verdict.**
3. **The PASS statistic is the net excess** `N_h = E[s·sign(d)·Δq_h − floor]`,
   which is the per-observation comparison of move against floor and is
   algebraically `|E[sign(d)·Δq_h]| − E[floor]` at the point estimate. PASS
   requires the Holm-corrected event-clustered CI on `N_h` to exclude zero from
   above. The CI on the raw `E[sign(d)·Δq_h]` is reported alongside it.
4. **A round-trip floor** `2 × floor_i` is reported as a stricter sensitivity,
   since a mid-to-mid move must be both entered and exited. It is *not* the
   preregistered criterion; §3 specifies a one-way floor and that is what the
   verdict uses.
5. **Holm CIs.** Holm's rejection decision is taken from the six bootstrap
   p-values. The accompanying interval for each hypothesis is reported at its
   Holm level `1 − α/(m−k+1)` at rank `k`, the standard step-down reading; the
   most stringent hypothesis is therefore judged at the Bonferroni level.
6. **Inference method, stated as required:** cluster bootstrap resampling the
   1,482 **events** with replacement, **B = 10,000** replicates (≥2,000
   required), percentile intervals, two-sided bootstrap p-values. Every
   statistic is a ratio of cluster-additive sums, so replicates are exact from
   multinomial event counts.
7. No logit is taken in E2 (`d = p − q` in probability space, per §3), so the
   §1 clipping guard does not bind; clipping count is therefore zero by
   construction and reported as not-applicable.

### D-4 (E2) — E2 direction is data-chosen, because only existence was preregistered (2026-08-15, pre-results)

§3 preregisters **existence, not direction**, and states that a negative value
is a finding. The raw statistic is therefore tested **two-sided**, which is
fully preregistered and needs no adjustment.

The net-excess statistic `N_h`, however, needs a sign `s` to subtract a cost
floor from — you cannot pay a cost floor without choosing a side. `s` is taken
from the point estimate on the set being analysed, which is a mild in-sample
selection on the pooled figure. Mitigation, fixed now: `s` is determined on
**TRAIN only** and applied unchanged to HOLDOUT, so the holdout net figure
carries no direction selection. This is disclosed rather than hidden, and the
two-sided raw-statistic CI is reported for every horizon regardless of `s`.

### D-3 (E3) — E3 bucket definitions, resolved before any ΔS was computed (2026-08-15)

§4 names ten slices but does not define the bucket boundaries for the
continuous ones. The boundaries below were fixed and committed **before the E3
script was written or run**, and no outcome column (`y`) was read at any point
in choosing them. Only covariate marginals were inspected.

1. **forecaster** — as recorded (`baseball_evidence`, `soccer_evidence`).
2. **sport / league** — from the `market_ticker` series prefix:
   `KXMLB*` → **MLB**, `KXWC*` → **WC** (FIFA World Cup), `KXMLS*` → **MLS**.
   Note this slice is near-collinear with (1) by construction.
3. **time-to-resolution** — **TRAIN quintiles of `hours_to_close`**. Fixed
   wall-clock buckets (<1h/1–3h/3–6h/6–24h/24h+) were considered and rejected
   *before* running: 92% of rows sit under 3h, so fixed buckets would place
   three of five cells under the n≥200 floor and forfeit the slice. Quintiles
   are also what slices 5–7 use, so the family is internally consistent.
4. **market-probability decile** — TRAIN deciles of `q`.
5. **`|p − q|` magnitude** — TRAIN quintiles.
6. **spread** — TRAIN quintiles of `spread_avg`.
7. **depth / liquidity** — TRAIN quintiles of `liquidity_avg`.
8. **favourite vs underdog** — `q > 0.5` vs `q <= 0.5`.
9. **forecast direction** — three cells, `p > q`, `p < q`, and `p == q`
   (92 exact ties exist; they are given their own cell rather than being
   silently folded into a side).
10. **resolution-clarity tier** — **NOT RUN.** No typed field supplies it.
    `ranking.resolution_clarity_score()` is an explicit placeholder returning
    the constant 0.5 for every market, and
    `DomainMarketInventorySnapshot.resolution_clarity_proxy` is a
    domain/series-cluster aggregate for scouting, not a per-market property of
    these forecasts. Per §4 this is recorded as not-run rather than improvised.

All cut points are computed on **TRAIN only** and applied unchanged to HOLDOUT.
Where a quantile edge repeats (e.g. `spread_avg` has heavy ties at 1.0),
duplicate edges are collapsed, so a slice may yield fewer than its nominal
number of cells; actual boundaries and counts are reported.

Two further points of procedure, also fixed in advance:

- **Evaluability** requires the n≥200 **and** ≥50-event floors to be met in
  **both** TRAIN and HOLDOUT. A cell evaluable in only one split cannot be
  discovered-then-confirmed, so it is `underpowered`, not a result.
- **BH family** = every evaluable cell, pooled across all slices, as §4 says.
  BH at q=0.10 is applied to the HOLDOUT p-values for the confirmation stage
  and, separately, to the TRAIN p-values for the discovery stage. Interval
  reporting for any BH-selected set uses FCR-adjusted intervals at level
  1 − R·q/m (Benjamini & Yekutieli 2005), which is what "FDR-corrected CI" is
  taken to mean.

### D-3 (E4) — E4 excludes crossed quotes (`close_ask < close_bid`) (2026-08-15, pre-results)

§5 fixes the executable prices but does not say what to do when the recorded quote is
**crossed**. 111 of 10,285 rows (1.08%) have `yes_ask_c < yes_bid_c` — 72 TRAIN, 39
HOLDOUT, median cross depth 6¢, max 84¢. These are bucket-aggregation artifacts
(`close_bid` and `close_ask` need not come from the same tick), not real books.

They are **excluded from the tradable universe** because including them would hand the
strategy a *negative* spread cost — an entry price below the midpoint — i.e. manufactured
edge from a recording artifact. Exclusion is the adverse choice for the strategy and
therefore the safe one. Locked books (`ask == bid`, 114 rows) are retained: zero spread
cost is a legitimate quote, not an artifact.

### D-4 (E4) — E4 adopts Corollary 19's abstain band as the primary trade filter (2026-08-15, pre-results)

§5 names the strategies and the executable prices but not the entry condition. E4 uses the
bid-ask form of proper betting given by **Corollary 19** of arXiv 2607.06166 (quoted in
`docs/research/QDK-001-prediction-market-math.md` §1.2): buy YES iff `p > ask`, buy NO iff
`p < bid`, otherwise abstain.

This is *favourable* to the strategies (it suppresses trades that are negative-edge under
`p` by construction), so the naive alternative — trade on `sign(p − q)` and cross the
spread — is reported alongside it in every table rather than being dropped. The naive
variant is uniformly worse, so the choice does not manufacture the verdict.

### D-5 (E4) — E4 declares the fee block size `C = 100` contracts (2026-08-15, pre-results)

The preregistered fee formula `round_up(M × 0.07 × C × P × (1−P))` leaves `C` free, and the
round-up is applied to the **whole order**, so `C` materially changes the per-contract fee:
at `C = 1` every trade pays a minimum of 1¢ regardless of price. E4's primary is
`C = 100` (a ~$34 average ticket, the realistic small-order case); the adverse `C = 1`
variant is reported in full beside it and is worse by ~0.48¢/contract. `M = 1` (taker,
default) universally — per-market fee multipliers are not present in the frozen extract, so
the default is used and declared as an assumption, not read from market state.

### D-6 (E4) — E4 scale convention (2026-08-15, pre-results, specification not departure)

`s_G` is defined only up to a positive scalar (Theorem 13), so §5's strategy list does not
by itself determine a P&L magnitude. E4 fixes the scale as **1 contract per trade** for the
headline (equal weight) and additionally reports an allocator-weighted figure with weights
normalised to mean 1 contract. No positive rescaling λ can change the sign of either
statistic, which is stated in the result document so that no arbitrary scale drives the
verdict. Fractional-Kelly λ therefore cancels in both normalised statistics; a separate
absolute-sizing table ($1,000 per trade, non-compounding) is reported where λ does bite.
