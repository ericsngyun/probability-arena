# FORECAST-RELIABILITY-DECOMP-001 — read-only calibration decomposition

Reliability report over **current, valid scored forecasts only** (the
`scored_current` population from FORECAST-SCORABILITY-AUDIT-001). Answers whether
forecast probabilities are empirically calibrated, whether they beat simple non-model
Brier baselines, how calibration error decomposes, whether over/underconfidence
dominates, whether reliability differs by cohort, and whether it is improving / stable
/ deteriorating over time — with composition-shift and thin-sample guards.

**Forecast-measurement infrastructure only.** It reuses the deployed scoring primitives
(`calibration.brier_score`, `log_loss`) and the scorability population logic
(`forecast_scorability.classify_forecast` / `SCORED_CURRENT` / `_load_rows` /
`_representation`) — it does not reinvent current-score semantics, alter any
forecast/outcome/score, change a calibration gate, touch MarketOps, write anything, or
call a provider. No EV, side, size, order, recommendation, wallet, or trading output
exists. **"Skill" is the standard Brier skill score**, never financial edge / profit /
return / actionability.

## Scored-current population

Only forecasts classified `scored_current` (settled binary yes/no outcome + a current
score whose Brier matches the outcome) enter the analysis. Every report discloses the
full split: `all_forecasts`, `scored_current`, `excluded_pending`,
`excluded_unscorable`, `excluded_backlog`, `excluded_stale`, `excluded_inconsistent`.
`y = 1.0` for a `yes` settlement, `0.0` for `no`; `p` is the forecast's estimated
probability.

## Reliability bins

Default 10 equal-width bins `[0.0,0.1) … [0.9,1.0]` (`--bins N`, N≥2). Inclusion is
left-closed/right-open except the last bin, which is right-closed (includes `p=1.0`).
Per bin: bounds + inclusion, count, share, mean forecast probability, observed positive
rate, signed and absolute calibration gap (`mean_p − observed_rate`), mean Brier, mean
log loss, yes/no counts, a deterministic Wilson 95% interval around the observed rate,
directional label, and a sample label (`too_thin` < 3, `descriptive_only` < 10,
`measured` ≥ 10).

## Calibration error

- `ECE = Σ (n_bin / N) · |mean_p_bin − observed_rate_bin|` over populated bins.
- `MCE = max |gap|` over measured bins (falls back to populated if none measured).
Reported with populated / measured / too-thin bin counts and the minimum-bin threshold.
Descriptive diagnostics only — never a promotion gate.

## Baseline comparison

- model mean Brier;
- **neutral baseline** `p=0.50` (Brier = 0.25);
- **base-rate baseline** `p = observed prevalence` (Brier = `prev·(1−prev)`);
- absolute difference; **Brier skill score** `1 − model_brier / baseline_brier` vs each.
Zero-variance base rate (all one outcome) is flagged and skill is `None` (no division).

## Brier (Murphy) decomposition

Binned `Brier ≈ reliability − resolution + uncertainty`:
`reliability = Σ (n_k/N)(p̄_k − o_k)²`, `resolution = Σ (n_k/N)(o_k − ō)²`,
`uncertainty = ō(1−ō)`. Reports each component, the reconstructed Brier, the actual
Brier, and the **discretization residual** `actual − reconstructed` (equal-width binning
introduces a residual; exact equality is not claimed). Components are non-negative
within numeric tolerance; perfect, constant-base-rate, and uninformative-0.5 populations
reconcile to residual 0.

## Cohort reliability

Segmented by domain, forecaster+version, evidence depth, forecast risk,
research-completeness bucket, research risk, resolution risk, tradeability — each with
scored count, prevalence, mean Brier, neutral/base-rate Brier, Brier skill vs base rate,
ECE/MCE, populated-bin count, representation share (reconciled with the scorability
audit via the same `_load_rows`/`_representation`), and a `measured`/`too_thin` label.
Thin cohorts are not interpreted and cohorts are never ranked as actionable selections.
Per-cohort rows are truncated to `--top`, so displayed per-cohort `scored_count` values
sum to at most (not exactly) the total scored sample when a dimension has more than
`--top` distinct cohorts.

## Temporal reliability

Deterministic UTC-day and UTC-week cohorts by forecast creation time; per period: count,
prevalence, mean Brier, base-rate Brier, Brier skill, ECE, top-domain/forecaster
concentration, sample label. A trend is computed only from ≥ 4 weekly periods each
meeting the sample floor: early vs late mean ECE → `reliability_improving` /
`reliability_stable` / `reliability_deteriorating`, `too_thin_for_trend` otherwise.

## Composition controls

If domain mix (top-domain share) or outcome prevalence shifts materially between the
early and late halves, the trend is labeled `composition_shift_dominates` instead of
attributing the change to the forecasting system. No causal claim is made.

## Primary verdict

One of: `INSUFFICIENT_RELIABILITY_DATA`, `RELIABILITY_SAMPLE_NOT_REPRESENTATIVE`,
`BASE_RATE_BASELINE_NOT_BEATEN`, `RELIABILITY_ERROR_DOMINATES`, `RESOLUTION_IS_WEAK`,
`OVERCONFIDENCE_DOMINATES`, `UNDERCONFIDENCE_DOMINATES`,
`DOMAIN_HETEROGENEITY_DOMINATES`, `COMPOSITION_SHIFT_DOMINATES`, `RELIABILITY_STABLE`,
`RELIABILITY_IMPROVING`, `RELIABILITY_DETERIORATING`, `MULTIPLE_RELIABILITY_FINDINGS`.
Deterministic, threshold-driven precedence: below the sample floor →
`INSUFFICIENT_RELIABILITY_DATA`; a strongly-skewed scored sample (per the scorability
representation) gates any healthy/beaten call → `RELIABILITY_SAMPLE_NOT_REPRESENTATIVE`;
otherwise each independent finding (base-rate not beaten, reliability-error, weak
resolution, over/underconfidence, domain heterogeneity, composition shift) is tested at
its documented threshold — ≥2 ⇒ `MULTIPLE_RELIABILITY_FINDINGS`, exactly one ⇒ that
finding, else the temporal trend label. The verdict is a research conclusion only and
changes no system behavior.

## CLI

```bash
forecast-reliability-decomposition-report \
  [--hours N | --since <ISO-UTC>] [--until <ISO-UTC>] [--domain D] [--forecaster F] \
  [--bins N] [--minimum-bin-count N] [--minimum-cohort-count N] [--top N] \
  [--format text|json]
```

`--since` overrides `--hours`; `--until` defaults to now; UTC timestamps; invalid bins
(`<2`) or window (`since>until`) fail clearly (exit 2); text and JSON derive from one
result object; `external_calls=0`, `persisted=false`; zero writes, no outcome sync, no
scoring, no persistence, no unit, no MarketOps call.

## Deployment note

The CLI wiring touches the frozen `app/cli.py`, so this is **branch-only**, **stacked on
PR #1** (base `worktree/forecast-scorability-audit`). It **must not merge to `main` or
deploy to EVO-X2 before the 2026-07-23 candidate-readiness checkpoint**; EVO-X2 stays
pinned at `3f742c9`.
