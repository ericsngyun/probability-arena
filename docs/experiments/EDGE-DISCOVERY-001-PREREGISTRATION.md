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
