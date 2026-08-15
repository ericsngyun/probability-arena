# EDGE-DISCOVERY-001 — E1 result

**Question.** Conditional on everything already in the market price `q`, does
our forecaster's disagreement `p − q` say anything additional about `Y`?

**Verdict: FAIL.** `β_d = +0.132`, event-clustered 95% CI **[−0.037, +0.316]**,
which includes zero. The fitted two-term model does **not** beat the raw market
on HOLDOUT on either metric — it is very slightly worse on both. Combined, these
are the strong form of the negative result the preregistration anticipated:
**the forecaster is informationally REDUNDANT given the market, not merely
inferior to it.**

Protocol: `docs/experiments/EDGE-DISCOVERY-001-PREREGISTRATION.md` §2, run as
written. Additions logged pre-report in §8 D-3.
**Authorizes no capital, no orders, no execution.**

---

## 1. Method

Fitted on **TRAIN ONLY** (`created_at < 2026-08-02`):

```
logit P(Y=1) = α + β_q·z_q + β_d·d        z_q = logit(q)   d = logit(p) − logit(q)
```

`q` and `p` clipped to `[0.01, 0.99]` before any logit. HOLDOUT
(`created_at >= 2026-08-02`) was scored **exactly once**, at the end. Nothing
was tuned on it, and no variant was hunted after seeing the result — variant
search is E3's preregistered job, under FDR control.

**Inference is clustered at the EVENT level** (`event`, 1,482 events), never at
ticker (5,819), which the preregistration forbids as anticonservative by ~2×.

**Primary machinery: nonparametric cluster bootstrap** — resample events with
replacement, 2,000 draws for the train coefficients (model refitted from
scratch on every draw; 0/2,000 failed to converge), 10,000 draws for the paired
holdout differences (model fixed, so only holdout sampling variability enters).
Percentile CIs. A **CR1 cluster-robust sandwich** is reported beside the train
coefficients as an independent cross-check; the two agree closely.

### Counts, exactly as used

| step | n |
|---|---|
| rows in frozen dataset | 10,285 |
| TRAIN rows fitted | **6,471** (959 event clusters) |
| HOLDOUT rows scored | **3,814** (537 event clusters) |
| rows in neither split | 0 |
| **rows dropped for any reason** | **0** |
| `p` clipped | **0** |
| `q` clipped | **287** (all at the high end, `q > 0.99`; 0 at the low end) |
| rows with either clipped | 287 (2.79%) |

Base rate `Y=1`: TRAIN 0.40257, HOLDOUT 0.44468.

---

## 2. TRAIN-fitted coefficients

**Two-term model** (n = 6,471, 959 event clusters):

| term | estimate | bootstrap 95% CI | CR1 SE | CR1 95% CI |
|---|---|---|---|---|
| α | −0.08559 | [−0.21860, +0.04432] | 0.06725 | [−0.21740, +0.04622] |
| β_q | +1.06015 | [+0.95310, +1.17927] | 0.05642 | [+0.94956, +1.17073] |
| **β_d** | **+0.13220** | **[−0.03711, +0.31600]** | 0.08992 | [−0.04403, +0.30843] |

**Market-only model** (the decomposition control):

| term | estimate | bootstrap 95% CI | CR1 SE | CR1 95% CI |
|---|---|---|---|---|
| α | −0.09011 | [−0.22042, +0.04066] | 0.06621 | [−0.21988, +0.03966] |
| β_q | +1.01267 | [+0.93292, +1.09997] | 0.04089 | [+0.93252, +1.09282] |

Two readings matter:

- **β_d is not distinguishable from zero.** Even in-sample, on the data it was
  fitted to, the agent residual buys almost nothing: the nested LR statistic is
  χ²(1) = 5.99, and that is an iid-naive number which event clustering deflates
  — the clustered CI on β_d is the honest inference, and it straddles zero.
- **β_q is not distinguishable from 1** (market-only: +1.013, CI
  [+0.933, +1.100]; β_q − 1 CI [−0.067, +0.100], includes zero). **The market
  price needs no recalibration.** It is already, to the resolution this sample
  supports, a well-calibrated probability on the logit scale.

---

## 3. HOLDOUT evaluation — touched once

n = 3,814, 537 event clusters. Lower is better on both metrics.

| model | Brier | log loss |
|---|---|---|
| **(a) market alone, `q`** | **0.179173** | **0.528599** |
| **(b) two-term, `α + β_q·z_q + β_d·d`** | **0.180145** | **0.531419** |
| **(c) our forecast alone, `p`** | **0.194733** | **0.570061** |
| (x) market recalibrated, `α + β_q·z_q` | 0.180197 | 0.531284 |

Paired differences, event-clustered bootstrap (10,000 resamples of 537 events).
Negative favours the first model.

| comparison | metric | difference | 95% CI | zero? |
|---|---|---|---|---|
| **(b) − (a)** — *the preregistered test* | Brier | **+0.000972** | [−0.000132, +0.002079] | **includes 0** |
| **(b) − (a)** | log loss | **+0.002819** | [−0.000181, +0.005816] | **includes 0** |
| (b) − (c) | Brier | −0.014587 | [−0.019075, −0.010119] | excludes 0 |
| (b) − (c) | log loss | −0.038642 | [−0.050046, −0.027138] | excludes 0 |
| (c) − (a) | Brier | +0.015560 | [+0.010981, +0.020147] | excludes 0 |
| (c) − (a) | log loss | +0.041462 | [+0.030064, +0.052923] | excludes 0 |

The (c) − (a) row independently reproduces the already-established result that
our forecasts are worse than the contemporaneous market standalone, on
genuinely held-out data and with correct clustering.

---

## 4. Verdict against the preregistered criterion

> **PASS (E1)** iff the fitted model beats **market alone** on HOLDOUT on
> **both** Brier and log loss, with event-clustered 95% CIs on the paired
> differences excluding zero.

| requirement | result |
|---|---|
| beats market alone on Brier, CI excludes 0 | **NO** (worse by +0.00097; CI includes 0) |
| beats market alone on log loss, CI excludes 0 | **NO** (worse by +0.00282; CI includes 0) |

# E1 : FAIL

The two-term model does not beat the market. It is *point-estimate worse* on
both metrics, though not significantly so — the honest statement is **"no
detectable difference, with the point estimate on the wrong side"**, not "the
model is significantly worse than the market".

Paired with `β_d`'s CI straddling zero, this is precisely the configuration the
preregistration named the **death certificate**: not "our forecaster is worse
than the market" (which was already known) but **"our forecaster contributes
nothing the market does not already contain."** Inferiority could in principle
be repaired by recalibration. Redundancy cannot.

---

## 5. Market recalibration vs agent information — the decomposition

This is the part that would have been misread without the market-only control.
The two-term model differs from raw `q` in **two** ways at once: it re-estimates
`α` and `β_q`, *and* it adds `β_d·d`. Splitting them:

| effect | comparison | Brier | 95% CI | log loss | 95% CI |
|---|---|---|---|---|---|
| **MARKET recalibration** | (x) − (a) | **+0.001024** | [+0.000028, +0.002004] **excludes 0** | **+0.002685** | [+0.000131, +0.005202] **excludes 0** |
| **AGENT information** | (b) − (x) | **−0.000051** | [−0.000536, +0.000431] includes 0 | **+0.000135** | [−0.001157, +0.001427] includes 0 |

Read this carefully, because it inverts the naive story:

1. **Essentially the entire holdout degradation of the two-term model is the
   recalibration term, and it is a finding about the MARKET, not about us.**
   Fitting `α, β_q` on TRAIN and applying them to HOLDOUT makes things
   *significantly worse* than just using the raw price. The market did not need
   recalibrating (§2: β_q ≈ 1), so the fitted adjustment captured TRAIN noise
   and a TRAIN-specific base rate (0.403) that did not carry to HOLDOUT
   (0.445). **The correct estimator of the market is the market.**

2. **The agent's incremental contribution is ≈ 0 to three decimal places** —
   −0.00005 Brier and +0.00014 log loss, both CIs comfortably spanning zero,
   and the two metrics disagree in sign. This is the cleanest available
   statement of the result: with the market's own recalibration held fixed,
   adding the agent residual moves the holdout score by nothing.

So E1 fails, but *not* because the agent term actively hurt. It fails because
the agent term did nothing, and the only component that did anything was a
recalibration of the market that the market did not need.

---

## 6. Why `d` is redundant — mechanism (exploratory, §8 D-3)

Regressing our forecast on the price it is meant to beat, on TRAIN:

```
logit(p)  =  −0.094  +  0.568 · logit(q)        R² = 0.661
corr(z_q, d) = −0.728        p on the same side of 0.5 as q: 87.6%
mean |logit q| = 1.339        mean |logit p| = 0.930
```

**Two thirds of the variance of our forecast is explained by the market price
itself**, with a slope of 0.568 — `p` is largely a *shrunk copy* of `q`. Our
forecasts are systematically less extreme than the market (mean |logit| 0.93 vs
1.34) and agree with its direction 88% of the time.

That is why `d` carries no information: it is not an independent view that
happens to be wrong, it is **mostly mechanical attenuation toward 0.5**, which
is a deterministic function of `q` and therefore by construction adds nothing
conditional on `q`. The disagreement is not small — |p − q| exceeds 5 points on
62% of rows and 10 points on 37% — but its structure is shrinkage, not signal.

**Exploratory alternative, clearly labelled and graduating nothing.** The
preregistered model can be penalised by base-rate drift through its free `α`
and `β_q`. The most favourable honest test removes that confound entirely by
pinning the market at its raw price:

```
logit P(Y=1) = logit(q) + β_d·d        (one free parameter)
β_d = +0.06856,  event-clustered 95% CI [−0.06298, +0.19035]  — includes 0
```

| model | Brier | log loss |
|---|---|---|
| (a) market alone `q` | 0.179173 | 0.528599 |
| (o) offset `logit(q) + β_d·d` | 0.179033 | 0.528355 |

Paired: Brier −0.000140, CI [−0.000404, +0.000127]; log loss −0.000244, CI
[−0.000974, +0.000492]. **Both include zero.** Given every advantage the data
permits — no recalibration penalty, no base-rate exposure, one parameter — the
agent residual still cannot be shown to improve on the raw price. This
strengthens the null rather than rescuing anything from it.

---

## 7. Limits

- **One market family, one venue, ~6 weeks** (2026-07-06 → 2026-08-15). The
  result is about these forecasters on these Kalshi sports markets in this
  window. It does not generalise to other market families by itself.
- **Power.** With 537 holdout event clusters, the paired-difference CIs are
  roughly ±0.001 Brier wide. An edge smaller than ~0.001 Brier would not be
  detected — but an edge that small is far below the executable cost floor
  (half-spread plus taker fee, §5 of the preregistration) and so is
  economically irrelevant even if real. The null is decision-relevant at the
  resolution that matters.
- **This is a null, not proof of absence.** E1 rules out a *linear* logit-scale
  contribution of `d` pooled across all rows. It does not rule out a
  conditional edge confined to a subgroup — that is exactly what E3 tests, on a
  closed preregistered slice list under BH-FDR at q = 0.10, with two-stage
  discover-on-TRAIN / confirm-on-HOLDOUT promotion. **E1 failing is not
  authority to go slice-hunting outside that protocol.**
- **Event overlap across the split is small but nonzero.** The chronological
  rule leaves **14 events with rows on both sides**, covering **65 holdout rows
  (1.70%)**. This is a property of the preregistered split rule, not a
  departure from it, and it is far too small to move any number here.
- **`q` is midpoint**, valid for judging the market as a forecaster (E1–E3) and
  invalid for profitability. Nothing here speaks to executable P&L; that is E4.
- **287 rows (2.79%) had `q` clipped at 0.99.** No `p` was clipped. Clipping can
  only compress extreme-confidence market quotes toward the interior, which if
  anything flatters the agent; it does not manufacture the null.

---

## 8. What this means for the stopping rule

E1 is one of four preregistered gates (§6). **It fails.** The sports
forecasting lane survives only if E2, E3, or E4 passes on genuinely held-out
data. E1's specific contribution to that decision is to close the most
favourable remaining interpretation of the ΔS result — that the forecaster
might be *conditionally* informative even while standalone-inferior. It is not.

The preregistration's pre-committed architecture on an E1 pass —
`p_final = F(q, agent residual)`, market as prior with the agent permitted to
move it where historically justified — **is not authorized**, because the
holdout says there is no region where moving it is justified: `β_d` cannot be
distinguished from zero and the incremental holdout effect is −0.00005 Brier.

The evidence hierarchy in §0 places this at the "beats market price" level, and
the answer there is **no**. E2 (does disagreement *lead* the market?) and E4
(executable net expectancy) ask different questions and remain open; E3 remains
the only sanctioned route to a conditional claim.

**No result here authorizes capital.**

---

## 9. Reproduction

```
PYTHONPATH=docs/evidence/qdk-001 python3 docs/evidence/qdk-001/e1_conditional_information.py
PYTHONPATH=docs/evidence/qdk-001 python3 docs/evidence/qdk-001/e1_exploratory_addendum.py
```

- `docs/evidence/qdk-001/e1_conditional_information.py` — preregistered E1.
  Frozen output: `e1_output.txt`.
- `docs/evidence/qdk-001/e1_exploratory_addendum.py` — exploratory only.
  Output: `e1_exploratory_output.txt`.
- Input: `docs/evidence/qdk-001/edge_discovery_001_dataset.csv` (frozen).
- Deterministic: seed 20260815, numpy only, no statistical library. The repo
  `.venv` lacks numpy; run under `/usr/local/bin/python3` (numpy 2.1.3). No
  package was installed (§8 D-3.4).
- `E1_SMOKE=1` re-runs every code path with TRAIN standing in for HOLDOUT. It
  exists so the machinery could be debugged **without** looking at the held-out
  split, which was scored exactly once.
