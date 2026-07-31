# CRYPTO-HORIZON-CANDIDATE-READINESS-001 — Fourteen-Day Checkpoint (2026-07-30)

```text
FOURTEEN-DAY CHECKPOINT: PASS
PRIMARY 7-DAY FINDING (birth-anchor starvation): RESOLVED AND QUANTIFIED
RECOMMENDATION: PROCEED TO CANARY-004 AT AN OPERATOR-APPROVED LIVE MOMENT
```

This is the fourteen-day checkpoint for the readiness measurement activated at cycle
3097 on 2026-07-16T19:56:26Z, and it closes the observation window opened at
activation. Analysis and documentation only: **no cohort was created or armed; no
discovery scan, tape run, provider call, observation, timer, unit, daemon, or database
write was produced by this checkpoint.** Every query used was read-only (`mode=ro`
SQLite handles and append-only JSONL reads).

**The window contains four distinct measurement regimes.** As required by the Epoch-4
activation rules, they are reported **separately** and never pooled — pooling would
average a regime with zero live moments against a regime with 902 of them and
misrepresent both.

## Clock confirmation and exact interval

- **Nominal 14-day threshold:** 2026-07-30T19:56:26Z
- **Actual checkpoint instant:** 2026-07-31T20:43:20.907951Z (EVO state-capture)
- **Interval:** `2026-07-16T19:56:26Z → 2026-07-31T20:43:20.9Z` = 1,298,814.9 s
  = **360.78 hours = 15.033 days**
- **Ran ~24.8 h past the nominal threshold.** Cause: an unplanned operator-workstation
  shutdown on 2026-07-31 interrupted the checkpoint session mid-analysis. The delay is
  an artifact of the *reporting* host only — the measurement itself is host-side on
  EVO-X2, ran uninterrupted throughout, and lost no cycles (see coverage below). The
  extra ~25 h of Epoch-4 data is included and strengthens rather than confounds the
  result.
- **Observed enabled cycles:** **3,625** (cycles 3097–6721, contiguous)

## Baseline at checkpoint

- Mac `main` = `origin/main` = EVO-X2 = **`6ac5503`**; all tracked trees clean.
- Alembic **0027 (head)**. `MARKETOPS_INCLUDE_CANDIDATE_READINESS=true` and
  `MARKETOPS_INCLUDE_CRYPTO_TAPE_ANCHOR_FEED=true` on the EVO `.env` only.
- Timers: 7 probability-arena units healthy (marketops 5 min, meme-news 10 min,
  tick-aggregation hourly, baseline 4 h, edge-observation daily, retention daily,
  watcher). **No horizon one-shot units installed.** The `arena-daily` and
  `launchpadlib-cache-clean` units belong to other projects on this shared host and are
  out of scope.
- The pre-existing untracked command-typo artifact in the EVO repo dir (filename
  beginning `ystemctl --user list-timers…`, dated 2026-07-15) is **still present and
  still untouched**, as at the 7-day checkpoint.

## Record integrity (readiness JSONL) — all epochs

`~/crypto-horizon-readiness/readiness.jsonl`, 1,506,197 bytes,
sha256 `c5b442aae8d03ad7ba93a85c71ba3e68fc4f1c8ca96225028369350ca574bb06`.

| Check | Result |
| --- | --- |
| Records | 3,625 |
| Invalid JSON | 0 |
| Schema mismatch (exact key-set) | 0 |
| Secret-pattern findings | 0 |
| `external_calls != 0` | 0 |
| Error states | 0 |
| Timestamps strictly increasing | yes (0 violations) |
| Duplicate `run_id` / `marketops_cycle_id` | 0 / 0 |
| `run_id == marketops_cycle_id` always | yes |
| Max line length | 527 B |

**Cycle coverage is exact and bidirectional:** cycles 3097–6721 span 3,625 ids, all
3,625 present, **0 missing**; `marketops_runs` in that id range = 3,625;
cycles without a readiness record = **0**; readiness records without a MarketOps run =
**0**. Append-only integrity holds against every prior published prefix.

## Epoch separation

| Epoch | Regime | Cycles | n | States observed |
| --- | --- | --- | --- | --- |
| **E1** | Anchor feed inactive (tape never ran) | 3097–4881 | 1,785 | `expired` 1,785 (100%) |
| **E2** | One governed `crypto-tape-run-once` (ANCHOR-FEED-CANARY-001, tape run 60) | 4882–4885 | 4 | `pair_ready` 1, `shared_due_now_ready` 2, `expired` 1 |
| **E3** | Post-canary, feed not yet activated | 4886–4904 | 19 | `expired` 19 (100%) |
| **E4** | Exact-cycle anchor feed active | 4905–6721 | 1,817 | see below |

E1 interval 7.399 d; E4 interval 7.540 d — the two principal regimes are almost
exactly matched in duration, which makes the comparison below a clean A/B rather than
an artifact of unequal exposure.

**E2 evidence preserved** (the first live pair ever observed): King
`2nxtQZ…pump` + Octen `7z4cgs…pump`, `pair_ready_for_manual_preparation` at
05:36:29Z (slack 739.6 s) → `shared_due_now_ready` at 05:42:31Z (378.1 s) and
05:48:21Z (27.9 s) → `expired` 05:54:32Z. Never armed.

## Epoch 4 — anchor-feed measurement results

### Reliability and isolation (the hook's safety contract)

| Invariant | Observed |
| --- | --- |
| MarketOps cycles | 1,817, **100% `ok`**, zero stage errors |
| Cycles with an `anchor_feed` result | 1,817 (0 missing) |
| Anchor-feed status | `ok` 1,422 / `no_new_tokens` 395 / **errors 0** |
| `external_calls` violations | **0** |
| `skipped_cap` (cap 40) | **0** |
| `crypto_watcher_runs` | 1,817 for 1,817 cycles = **exactly 1 scan/cycle, no second scans** |
| Distinct `source_crypto_run_id` | 1,817 of 1,817 — **zero run reuse** |
| Tape lifecycle runs | 1,422, **all `mode=exact_cycle`, zero manual runs** |
| Cohorts / members / observations created | **0 / 0 / 0** |
| Readiness summaries present | 1,817 (0 missing, 0 errors) |

All-time inventory is **unchanged since CANARY-003**: cohorts 6, members 19,
observations 35. Nothing was armed, and cohort 4 remains permanently unarmed.

Token pass-through is exact and lossless: `tokens_received` = `tokens_validated` =
`anchors_attempted` = `anchors_created` = **2,773**, `anchors_existing` = 0.

**Cost:** hook duration median **171 ms**, p75 297 ms, p95 528 ms, max 6.2 s, against a
cycle median of 36.8 s. E4 cycle duration median 36,849 ms vs E1 35,268 ms (**+1.6 s,
+4.5%**); p95 56.0 s vs 57.0 s (unchanged). The hook is operationally free.

### The starvation fix, quantified

This is the headline result. The 7-day checkpoint's primary finding was that
`CryptoTokenBirthEvent` anchors were produced *only* by the manual tape lane, which
never ran — so the catch rate was vacuous on a zero denominator.

| Metric | Pre-feed baseline | Epoch 4 |
| --- | --- | --- |
| `first_evidence_at` → anchor persist, median | ~85 min | **17.2 s** |
| Same, p95 / max | — | 35.9 s / 180.6 s |
| Anchors persisted while the 15 m window is still feasible | 44/198 = **8.7%** | **2,773/2,773 = 100.0%** |
| Raw tokens converted to anchors | (none in window) | 2,773/2,773 = **100.0%** |

Anchor lag is no longer a limiting factor by roughly three orders of magnitude, and
**every single anchor** in Epoch 4 landed inside its own 15-minute feasibility window.

### Anchor completeness

1,153 complete (**41.6%**) / 1,620 incomplete (58.4%). The sole failure mode is
`missing_initial_liquidity` (1,619; one record missing both price and liquidity) —
matching the canonical `_completeness_reason` classification exactly.

Segmenting by venue shows this is **not** a pipeline defect but a property of one
launch venue:

| `first_dex_id` | n | complete | rate |
| --- | --- | --- | --- |
| pumpfun | 2,353 | 752 | **32.0%** |
| pumpswap | 326 | 325 | **99.7%** |
| raydium | 37 | 37 | 100% |
| meteora | 20 | 20 | 100% |
| meteoradbc | 16 | 12 | 75.0% |
| launchlab | 17 | 5 | 29.4% |

Graduated/AMM pools report initial liquidity essentially always; pumpfun bonding-curve
tokens frequently do not at birth. Completeness is stable day over day (38.4%–44.6%
across all 8 days) and mildly diurnal (30.8% at 12Z vs 56.5% at 02Z).
`dexscreener:boost` sourcing outperforms `dexscreener:profile` (70.3% vs 40.6%).

### Live readiness moments — the decision-relevant result

| State | n | share |
| --- | --- | --- |
| `shared_due_now_ready` | 652 | 35.88% |
| `pair_ready_for_manual_preparation` | 250 | 13.76% |
| `pair_detected_not_due` | 199 | 10.95% |
| `insufficient_arm_slack` | 105 | 5.78% |
| `expired` | 611 | 33.63% |
| `no_complete_candidates` / `no_overlapping_pair` | 0 / 0 | 0% |

- **Live evaluations (`pair_ready` + `shared_due_now_ready`): 902 = 49.64% of cycles.**
- **Distinct candidate pairs: 496** over 7.54 d = **~66 live moments/day**.
- **Epoch 1 comparison: ZERO live moments across 1,785 cycles.**
- Arrival is stable, not bursty: 76 / 100 / 97 / 141 / 130 / 131 / 105 / 122 per UTC day
  (Jul 24→31), with a mild diurnal peak at 14–20Z.
- Safe arm slack: median **377 s**, p75 390 s, p90 747 s, max 809 s, min 2 s.

**Episode persistence — this is what makes CANARY-004 operationally feasible:**

| Episode metric | Value |
| --- | --- |
| Episodes (distinct pairs) | 496 |
| Median cycles per episode | 2 (max 3) |
| Episodes spanning ≥2 cycles | 308 = **62.1%** |
| Median episode duration | **358 s** |
| Episodes ≥300 s / ≥600 s | 307 (61.9%) / 98 (19.8%) |

A live pair is not a single-instant coin flip. Roughly **62% of live moments persist
across two or more consecutive MarketOps cycles (~6–12 minutes)**, which is a window a
human operator can realistically be notified within and act inside.

At the checkpoint instant this was directly observable: cycles 6717→6718 carried one
live pair through `pair_ready` → `shared_due_now_ready`, cycles 6719→6720 carried a
second, and 6721 expired.

### Operational funnel (Epoch 4, by cycle)

| Stage | cycles | share |
| --- | --- | --- |
| Natural MarketOps cycles | 1,817 | 100% |
| …with new raw tokens | 1,422 | 78.3% |
| …with ≥2 new raw tokens | 808 | 44.5% |
| …with ≥1 complete anchor | 844 | 46.5% |
| …with ≥2 complete anchors | 243 | 13.4% |
| …with a detected pair (due or not-due) | 1,101 | 60.6% |
| …recorded as a LIVE moment | 902 | 49.6% |

Miss classification for non-live cycles: no two new tokens 422 (227 + 195 with no new
tokens at all); fell between cycles / not due yet 199; two new tokens but not two
*complete* ones 189; insufficient operational margin 105.

**Raw-layer counterfactual:** 10,594 raw co-15m token pairs vs 1,933 complete-anchor
co-15m pairs → **18.2% structural retention**. The gap is entirely the pumpfun
liquidity-completeness effect above, not lag and not scan coverage. Relaxing
completeness is *not* recommended without a separate decision — initial liquidity is
load-bearing for the horizon math.

## SQLite operational context

Telemetry `~/probability-arena-telemetry/sqlite-writes.jsonl` (2,687,731 B,
sha256 `22d6b7d81f3bb51b6c38b998eba6fa3589abd6707e408f42d7b8304eecf301fe`), 2,877
events, 0 malformed, 0 secret hits.

- All events `writer_name=tick_aggregation` (2,697 `commit_unit` + 180 `aggregate`);
  001A instruments only that writer, as designed.
- Outcomes: 2,874 `success` + **3 `retried_success`**; **0 retry exhaustions, 0
  hard-failed runs**.
- **3 `database_locked` events total** (2026-07-26 08:02, 07-27 15:50, 07-28 05:10),
  all `commit_unit`, all recovered on attempt 2, lock wait ~32 s each. **None since
  2026-07-28** — 3+ days clean. Compare **51 lock events** in the 7-day checkpoint
  window: a large improvement, driven by the OPS-014 retention reduction rather than by
  anything in this milestone.
- 0 provider I/O and 0 filesystem I/O inside a transaction; 0 external-call violations.
- Transaction hold median 313 ms / p95 368 ms / max 625 ms. `commit_ms` median 0 /
  p95 17 ms / max 17.2 s (a single Jul-30 outlier).

### Storage

- DB **4,251,414,528 B (4,054 MiB / 4.25 GB)**; freelist 14,966 pages.
- Epoch-4 growth: **+570 MB over 7.51 d = 75.9 MB/day** — consistent with the Jul-28
  mid-window reading and **down from ~115 MB/day** at the 7-day checkpoint. The anchor
  feed contributed 2,773 small rows and did **not** accelerate growth.
- Host: 236 G volume, 88 G available, **62% used** → host-level risk remains
  **acceptable** (~1,000+ days of headroom at the current rate).
- **App-level 3072 MiB gate is exceeded at 132%**, `db_growth_warning` critical open.
  This is **pre-existing** (first fired 2026-07-05, three weeks before Epoch 4) and
  **not attributable to this milestone**. Largest consumers: `market_price_ticks`
  2,078 MB, `market_snapshots` 502 MB, `crypto_token_discovery_events` 279 MB,
  `market_price_tick_buckets` 234 MB.
- Overall storage status: **WARNING (pre-existing, not hook-attributable, not host-critical)**.

## Verdict

**FOURTEEN-DAY CHECKPOINT: PASS.** The measurement ran its full window with perfect
record integrity, perfect cycle coverage, zero errors, zero external calls, zero
unintended mutations, and a negligible performance cost. Every safety invariant
declared at activation held for all 3,625 cycles.

**The primary 7-day finding is resolved.** Birth-anchor starvation — the reason the
7-day catch rate was vacuous — was correctly diagnosed and correctly fixed by the
exact-cycle anchor feed. Anchor lag fell from ~85 min to 17 s; 15m-feasibility at
persist went from 8.7% to 100%; live moments went from **0 in 1,785 cycles** to
**496 distinct pairs in 1,817 cycles**.

**The binding constraint has moved from pair scarcity to human approval timing.** That
is a materially different problem, and the episode-persistence data says it is a
tractable one: ~66 live moments/day, 62% of them lasting ≥2 cycles, median 358 s of
safe slack.

### Recommendation (decision required — nothing has been acted on)

Of the three options set at activation, the recommendation is **Option 1: proceed to
CANARY-004 at an operator-approved live moment.**

- *Option 3 (CRYPTO-DISCOVERY-FRESHNESS-001)* is now **unnecessary for its stated
  purpose**. It was scoped to fix anchor freshness; freshness is fixed. It should be
  closed or re-scoped rather than built.
- *Option 2 (continue measurement)* has low marginal value. The Epoch-4 distribution
  was stationary across all 8 days; another week would refine third-digit estimates and
  add ~530 MB to a database already past its app-level gate.
- *Option 1* is the only option that converts the measurement into the evidence it was
  built to produce. CANARY-004 attempts 2 and 3 both failed for a reason that no longer
  applies (they used single-shot or 45-minute bounded windows against a ~8%
  compliant-cycle rate; the compliant rate is now ~50% of cycles).

**CANARY-004 arming remains NOT authorized by this document.** It requires explicit
human approval given *at* a live moment, never retroactively. What this checkpoint
changes is only the expected wait: an approval granted at an arbitrary instant should
now find a qualifying pair within roughly 20–30 minutes.

### Separately flagged, not part of this milestone

1. **Storage gate (recommend acting on this next).** The DB is at 132% of its
   app-level gate on pre-existing growth dominated by `market_price_ticks`. This wants
   its own OPS milestone — a retention/aggregation decision, not a horizon decision.
2. **Post-window merges are now unblocked.** The freeze that deferred
   SQLITE-LOCK-TELEMETRY-001B, the CLI decomposition, and forecast PRs #1/#2 was tied
   to this window closing. They are eligible for scheduling; none were touched here.
3. **Test time-bomb (fixed alongside this checkpoint).**
   `tests/test_crypto_horizon_cohort_select_001.py` pinned an absolute
   `NOW = 2026-07-15T12:00Z` against a `hours=240` lookback, so 4 tests began failing on
   2026-07-25 for calendar reasons alone. Product code is unaffected.
