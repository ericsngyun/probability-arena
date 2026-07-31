# CRYPTO-HORIZON-CANDIDATE-READINESS-001 — Fourteen-Day Checkpoint (captured 2026-07-31)

```text
FOURTEEN-DAY CHECKPOINT: PASS
CANDIDATE-READINESS VERDICT: PASS
ANCHOR-FEED VERDICT:        PASS
STRUCTURAL CONCLUSION:      ANCHOR FEED SOLVED THE LIVE DENOMINATOR
ANCHOR-FEED OPERATING:      KEEP ANCHOR FEED ENABLED
CANARY-004 STRATEGY:        PRE-AUTHORIZE ONE CANARY-004 ON NEXT QUALIFYING LIVE PAIR
FORECAST PR INTEGRATION:    DEFER (PR#2 fails on a latent absolute-date time-bomb)
NEXT OPERATIONAL MILESTONE: SQLITE-STORAGE-GROWTH-001
```

**Filename vs threshold.** The checkpoint *threshold* was `2026-07-30T19:56:26Z`; the
checkpoint was *captured* on 2026-07-31, so the filename and title carry the actual
capture date. The ~25 h delay is a reporting-host artifact (an unplanned operator
workstation shutdown interrupted the session mid-analysis); the host-side measurement
on EVO-X2 ran uninterrupted and lost no cycles.

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

- **At analysis start:** Mac `main` = `origin/main` = EVO-X2 = **`6ac5503`**; all tracked
  trees clean. (The repo advanced during the checkpoint as its own artifacts were
  committed and EVO was resynced; at the Gate-1 capture instant all three were
  **`8d9731d`**. See the gate-structured section below for the capture-time baseline.)
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

---

# Checkpoint decisions (gate-structured)

Capture instants: Mac `2026-07-31T21:11:18Z`, EVO-X2 `2026-07-31T21:11:23Z` (hosts agree
to within ssh round-trip; both far past the `2026-07-30T19:56:26Z` threshold). Full
interval `2026-07-16T19:56:26Z → 2026-07-31T21:11:23Z` = **1,300,497 s = 361.25 h =
15.052 d**. Epoch 4 `2026-07-24T07:46:20Z → capture` = **653,103 s = 181.42 h = 7.559 d**.
At capture the readiness JSONL held **3,629 records** (cycles 3097–6725). The tabulated
analysis above is snapshotted at cycle 6721 (3,625 records); the live-pair and episode
sections below extend through cycle 6725. Baseline at capture: Mac = origin = EVO-X2 =
`8d9731d`, Alembic `0027 (head)`, both `MARKETOPS_INCLUDE_*` flags `true`, tracked trees
clean, zero horizon one-shot units (no `c<N>-j<M>` unit files, none loaded, no manifest
dir), cohorts/members/observations 6 / 19 / 35.

## Live readiness moments — persistence thresholds (497 distinct pairs, 904 live evaluations)

| Threshold | Episodes | Share |
| --- | --- | --- |
| ≥1 cycle | 497 | 100.0% |
| ≥2 cycles (still live at next cycle) | 309 | **62.2%** |
| ≥5 min (300 s) | 308 | 62.0% |
| ≥10 min (600 s) | 98 | 19.7% |

Arm slack across live evaluations: min 2 s, median **378 s**, p75 389.6 s, p90 746.8 s,
max 809 s. **Proposed manual dry-run commands executed automatically: 0.** The readiness
hook prints a proposal only; it has never created, armed, or observed anything.

Representative live pairs (bounded sample; full counts above) — every one reached
`shared_due_now_ready` and then **expired naturally, unarmed**:

| Pair | Symbols | Shared 15m window | Cycles | Max slack |
| --- | --- | --- | --- | --- |
| `9jzqkPV6…` + `FQnwa9JJ…` | Smudge / Shiloh | 08:04:34→08:19:34Z | 3 | 746.5 s |
| `42dkTUTB…` + `BseYv6zj…` | RACCMOJI / BIO | 09:52:34→10:07:34Z | 3 | 718.7 s |
| `6tbUPtKa…` + `9CoWKD5K…` | FRODO / Freud | 14:07:34→14:22:34Z | 3 | 749.5 s |
| `CzY4w9GC…` + `G6ugqRML…` | Rupert / URC | 15:13:34→15:28:34Z | 3 | 750.4 s |
| `9XuWt4W2…` + `BQgedgDG…` | Zion / COBY | 15:43:34→15:58:34Z | 3 | 746.7 s |

Both members of every sampled pair carry complete anchors (non-null initial price **and**
liquidity) and identical anchor-persist timestamps — the exact-cycle feed materialized
both members in the same bounded transaction, as designed.

## Gate 8 — Currently live pair at report time

**A qualifying pair was live at capture**, and its decay is the single most decision-relevant
observation in this checkpoint:

```text
cycle              6725   (2026-07-31T21:07:24.605120Z)
state              shared_due_now_ready
A  LilJesus        5MP9ZTzay6Ly89zEyncvS3QmS3a4hf2XaLCmFCjYgGJy  (raydium,  price 2.12e-05, liq 5695.40)
B  pibble          Eppcp4FhG6wmaRno3omWWvKsZHbzucVLR316SdXopump  (pumpswap, price 7.076e-05, liq 34602.52)
source discovery   crypto run 1484; both anchors persisted 2026-07-31 20:55:22.614281
shared 15m window  2026-07-31T21:02:34.259173Z → 2026-07-31T21:17:34.259173Z
slack at eval      384.7 s
slack at 21:12:17Z  92.1 s   (window close − 45 s grace − 180 s operator prep)
```

The pair progressed `pair_detected_not_due` (6723, slack 1106.2 s) →
`pair_ready_for_manual_preparation` (6724, 747.4 s) → `shared_due_now_ready` (6725,
384.7 s), i.e. **~11 minutes from first detection to under two minutes of safe slack.**

It was **not** created, armed, or observed. It expired naturally unarmed.

**This is the empirical proof that per-moment human approval cannot work.** Any
operator round-trip — read the request, verify the evidence, decide, reply — exceeds the
remaining slack. Requesting approval for *this* pair would have been useless: the window
closed before a human could answer. That is precisely why Gate 16 below selects
pre-authorization rather than `REQUEST CANARY-004 APPROVAL FOR CURRENTLY LIVE PAIR`.

## Gate 12 — Storage thresholds and projections

Configured gates (`app/config.py`): `db_growth_warning_mb = 1536.0`,
`db_growth_critical_mb = 3072.0`. Database **4,251,414,528 B = 4,054 MiB**.

| Threshold | Status | Days to breach |
| --- | --- | --- |
| App warning 1,536 MiB | **breached** (264% of gate) | 0 — already past |
| App critical 3,072 MiB | **breached** (132% of gate) | 0 — already past |
| Host 80% used | not breached (62%) | **~723 d** at 75.9 MB/day |
| Host 90% used | not breached | **~1,056 d** |

Host volume 252,996,411,392 B total / 94,063,386,624 B free. JSONL growth is negligible:
readiness ~100 KB/day (1,508,125 B over 15.05 d), telemetry ~358 KB/day (2,687,731 B
over 7.56 d). Lock events by writer: `tick_aggregation` 3, all `retried_success`; every
other writer 0. Hard-failed scheduled runs: **0**. Recovered retries: **3**.

Classification, deliberately split:

- **Application-level DB-size gate: CRITICAL** (breached 26 days, 295 open critical alerts)
- **Actual host disk pressure: ACCEPTABLE** (~2 years of headroom)
- **Lock-contention risk: ACCEPTABLE** (3 events, 0 hard failures, none since 07-28)
- **Data-retention risk: WARNING** (raw tick retention is the growth driver, not crypto)

The app gate is a *real alarm about an unmanaged retention policy*, not an imminent
outage. Its practical harm today is alert-channel poisoning: 295 open criticals mask
genuine signals.

## Gate 13 — Structural conclusion

```text
ANCHOR FEED SOLVED THE LIVE DENOMINATOR
```

Evidence: live moments went from **0 across 1,785 Epoch-1 cycles** to **497 distinct
pairs across 1,821 Epoch-4 cycles**; anchor lag fell ~85 min → median 17.2 s; and
**100% (2,773/2,773)** of anchors persisted while their 15m window was still feasible,
versus 8.7% pre-feed. No residual limit dominates the funnel the way starvation did.

Direct answers:

- **Is pair scarcity still a problem?** No. ~66 qualifying pairs/day, arriving steadily
  every day of Epoch 4 (76/100/97/141/130/131/105/122).
- **Is human approval timing still the practical CANARY-004 constraint?** **Yes, and it
  is now the *only* one.** Gate 8 above demonstrates it concretely: median slack is
  378 s and the observed pair fell to 92 s during the capture itself.
- **Did anchor feed materially increase MarketOps failures or lock contention?** No.
  Zero hook-caused failures across 1,817 cycles; cycle duration +1.6 s (+4.5%) median
  with p95 unchanged; only 3 lock events, all on `tick_aggregation` (an unrelated
  writer), all recovered, none since 2026-07-28.
- **Is the current anchor feed worth keeping enabled?** Yes — it is the sole reason a
  live denominator exists, at a cost of 171 ms/cycle and 1,576→2,773 tiny rows.

## Gates 14–16 — Verdicts and decisions

**Candidate-readiness verdict: PASS.** Zero provider calls, zero second scans, exact
one-record-per-cycle coverage with zero gaps or duplicates across 3,625 records, zero
error states, zero secret findings, no automatic cohort/arming behavior, correct state
classification (0 `no_complete_candidates` and 0 `no_overlapping_pair` in Epoch 4 is
correct, not suspicious — the feed guarantees complete candidates exist most cycles).

**Anchor-feed verdict: PASS.** `external_calls=0`, `skipped_cap=0`, membership
mismatches 0, second scans 0, MarketOps failures caused by hook 0, 1,817/1,817 distinct
`source_crypto_run_id` (no reuse), 1,422 tape runs all `mode=exact_cycle` with zero
manual runs, same-cycle readiness visibility proven, bounded overhead (median 171 ms),
no secret leakage, and full idempotency (`anchors_existing=0` with
`tokens_received = validated = attempted = created = 2,773`).

**Anchor-feed operating decision: `KEEP ANCHOR FEED ENABLED`.** All KEEP criteria met.
No `.env` change is made by this checkpoint.

**CANARY-004 strategy: `PRE-AUTHORIZE ONE CANARY-004 ON NEXT QUALIFYING LIVE PAIR`.**
Every precondition holds: Epoch 4 produced 497 recurring live moments; median safe slack
378 s with 62.2% of episodes persisting ≥2 cycles; exact-cycle membership is reliable
(0 mismatches); the cohort selector (COHORT-SELECT-001/002) and orchestrator
(DUE-NOW-001) were validated by CANARY-003's full-lifecycle pass; and one bounded canary
can be condition-triggered without implying any recurring cohort creation.
`REQUEST CANARY-004 APPROVAL FOR CURRENTLY LIVE PAIR` was considered and **rejected on
evidence** — the Gate 8 pair proves the approval round-trip outlives the slack.

Proposed pre-authorization scope, to be granted or refused by the operator as a whole:

```text
exactly one cohort
exactly two complete members
first qualifying shared_due_now_ready pair
minimum canonical slack
one confirmed arming
natural 15m execution
future horizons host-owned
then stop
```

The dry-run form (zero-call, persists nothing) that a canary would preview with:

```bash
python -m app.cli crypto-horizon-cohort-create \
  --token <CANONICAL_ID_A> --token <CANONICAL_ID_B> \
  --require-complete --require-shared-horizon-windows --dry-run
```

Persisting additionally requires `--confirm`; arming is a separate explicit step. **No
such command was executed in this checkpoint.**

## Gate 17 — Operational milestone ranking

Scored on urgency, evidence strength, operational risk, effort, independent
deployability, interaction with live measurement, and expected measurable benefit.

| # | Milestone | Urgency | Evidence | Risk | Effort | Indep. | Benefit |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **1** | **SQLITE-STORAGE-GROWTH-001** | High | Very strong (132% of gate, 295 criticals, `market_price_ticks` 2.1 GB) | Low (measure/plan first) | Low–Med | Yes | Clears the only breached gate; unpoisons alerts |
| 2 | RETENTION-COVERAGE-001 | High | Strong (raw ticks unpruned; OPS-014 precedent) | Medium (deletes data) | Medium | Yes | Attacks the growth driver directly |
| 3 | SQLITE-LOCK-TELEMETRY-001B | Medium | Strong (001A proven; MarketOps still uninstrumented) | Low (emit-only) | Medium | Yes | Ends inference about uninstrumented holds |
| 4 | SQLITE-BACKUP-COORDINATION-001 | Medium | Strong (**backup.timer declared in canon but never installed**) | Low | Low | Yes | Removes a real unbacked-up-4 GB-DB exposure |
| 5 | RUNTIME-UTIL-001 | Low | Moderate (`_now()` ×35, 3 retry ladders) | Low | Low | Yes | Removes duplication ahead of decomposition |

Deliberately below the line: **SQLITE-WAL-HEALTH-001** and
**SQLITE-TRANSACTION-OWNERSHIP-001** (Tier-3 runtime changes; 3 lock events in 7.5 days
does not justify touching journal mode or transaction boundaries — the topology report's
own sequencing puts telemetry first), and **CLI-DECOMP-REGISTRY-001** (blocked on the
forecast stack landing first, per the decomposition design).

**Selected next operational milestone: `SQLITE-STORAGE-GROWTH-001`** (measurement +
plan; no retention execution without separate approval).

**Must storage work precede other tracks?** Decided explicitly:

- **Before forecast PR integration — NO.** The PRs add read-only report code and touch
  no SQLite runtime behavior. Host pressure is ~2 years away. (They are nonetheless
  deferred for an unrelated reason — see Gate 18.)
- **Before CLI decomposition — NO,** but decomposition is blocked on the forecast stack
  regardless.
- **Before telemetry expansion (001B) — YES.** Broadening instrumentation while the DB
  is 132% over gate adds writes and noise to an already-breached system.
- **Before WAL work — YES, decisively.** WAL changes on-disk behavior and growth
  characteristics; doing that on top of an unmanaged retention policy would confound
  both. Fix retention first, then reassess whether WAL is needed at all.

## Gate 18 — Forecast PR integration assessment

Both branches were merge-tested against current `main` (`8d9731d`) in throwaway
worktrees; `main` was never touched and no branch was modified or force-pushed.

**PR #1 — FORECAST-SCORABILITY-AUDIT-001**

- Head `ed5805e`; base `main`; draft; **merges cleanly** (`app/cli.py` auto-merged; 0
  conflicts) despite `main` having advanced — `cli.py` changed once since the PR base
  (`4383172`, anchor-feed) without overlap.
- Changed paths vs main: 4 files, **+1,383, purely additive** — `app/cli.py` (+96),
  `app/services/forecast_scorability.py`, `docs/FORECAST_SCORABILITY_AUDIT.md`,
  `tests/test_forecast_scorability_audit_001.py`.
- Full suite on the merged result: **1,951 passed, 2 skipped, 1 failed** — the single
  failure was `test_sqlite_lock_telemetry_001a.py::test_emit_overhead_within_budget`, a
  **load-sensitive perf assertion belonging to `main`, not to PR #1**; it passes 3/3 in
  isolation. **No rebase needed; earlier test claims remain valid.**

**PR #2 — FORECAST-RELIABILITY-DECOMP-001**

- Head `b0ab073`; base `worktree/forecast-scorability-audit` (correctly stacked);
  draft; **merges cleanly** onto the PR#1 result (0 conflicts).
- Incremental content is **only** reliability-decomposition work:
  `app/services/forecast_reliability.py`, `docs/FORECAST_RELIABILITY_DECOMP.md`,
  `tests/test_forecast_reliability_decomp_001.py`, plus additive `cli.py` lines. Full
  stack vs main = 7 files, +2,704, purely additive.
- Full suite on the merged stack: **1,993 passed, 2 skipped, 1 failed** —
  `test_forecast_reliability_decomp_001.py::test_json_text_parity_and_cli`, and this one
  is **real, deterministic, and reproducible**.

**Root cause of the PR#2 failure — a third instance of the same defect class.** The test
pins `NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)` and seeds 30 scored rows
around it. Its `_report()` helper passes `now=NOW` (so the object under test sees the
data), but the CLI path it compares against takes no `now` and reads the real clock
against a `hours=240` lookback. The seeded rows aged out of that window on
**~2026-07-26T12:00Z**, so the CLI now reports `INSUFFICIENT_RELIABILITY_DATA` /
`"only 0 scored_current (<30)"` while the direct call still reports
`MULTIPLE_RELIABILITY_FINDINGS`. The branch's original "1936 passed" claim was true when
written on 2026-07-16; the branch has since rotted on the calendar.

This is the same failure mode as the `test_crypto_horizon_cohort_select_001.py` time-bomb
fixed in `ed0df90`. **Three occurrences now** (cohort-select, reliability-decomp, and the
`now=`-injecting files that merely avoid it by luck of construction) make this a
systemic pattern worth its own small hygiene milestone rather than another point fix.

**Decision: `DEFER FORECAST PR INTEGRATION`.**

Gate 19 conditions 5 and 10 both require a green full suite; condition 10 fails
deterministically, and Gate 19 states plainly that if any condition fails, **neither** PR
merges. PR #1 is genuinely ready and would merge cleanly today, but merging it alone
would strand PR #2's base mid-sequence for a defect that takes minutes to fix. The
correct next step is a small refresh of PR #2's test to anchor its seeds relative to real
now (mirroring `ed0df90`), re-run both suites, obtain the required independent reviews,
and then run the Gate 19 sequence intact. **No merge, rebase, retarget, force-push, or
branch mutation was performed.**

## Gate 20 — CLI decomposition decision

```text
DEFER CLI DECOMPOSITION
```

CLI-DECOMP-REGISTRY-001 may begin only once the forecast commands are part of the
canonical CLI manifest, and the forecast stack is deferred (Gate 18). Decomposing first
would force the golden CLI-manifest compatibility test — the design's own safety net —
to be rewritten mid-flight when the forecast subcommands land.

## Gate 9 answer — has the system earned a shift toward research value?

**Partially, and the honest answer is "not yet, by one milestone."** The measurement
infrastructure has demonstrated real maturity: 3,625 consecutive cycles with perfect
integrity, zero provider leakage, zero unintended mutations, and a hook that costs
171 ms and provably cannot fail its host cycle. That is a system worth trusting.

But two loose infrastructure threads still gate a clean pivot: an application storage
gate breached for 26 days with no retention policy behind it, and a **4 GB production
database whose canon-declared backup timer has never been installed**. Neither is an
emergency; both are exactly the sort of thing that becomes one while attention is
elsewhere. Clear `SQLITE-STORAGE-GROWTH-001` and the backup exposure, land the forecast
stack, and the centre of gravity can then move to research value — where the forecast
scorability/reliability work and CANARY-004's actual horizon evidence are the payload.
