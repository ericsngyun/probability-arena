# OUTCOME-SYNC-COVERAGE-ACTIVATION-001 — activating the need-based selector

**Status:** activated on EVO-X2 under conservative drain limits; window results in §6–§9.

[`OUTCOME_SYNC_COVERAGE_001`](OUTCOME_SYNC_COVERAGE_001.md) built and dark-deployed
the repair. This activates it and measures whether the two frozen prefixes
actually break, on natural cycles only.

---

## 1. Root cause, reconfirmed on production immediately before activation

Both defects were still visibly frozen right up to the moment the flag flipped.
The last pre-activation cycle (**7729**, 01:01:03–01:01:47Z) recorded:

```json
"score_counts": {"scored": 0, "pending_outcome": 0, "unscorable": 0, "skipped": 1000}
```

One thousand forecasts loaded and one thousand discarded as already-current, for
the *n*-th consecutive cycle — the id-ordered prefix, doing no work at full cost.
Outcome sync told the same story: `outcomes_synced=100` on every cycle while
`market_outcomes` did not grow at all, because all 100 reachable markets already
held terminal outcomes.

The coverage report's own selection audit, run against production pre-activation:

> `legacy_alphabetical_prefix` — 4,902 forecasted tickers unreachable on EVERY
> cycle; of the 100 reachable, **100 already hold a TERMINAL outcome**.

## 2. Immutable pre-activation baseline (Gate 2)

Captured read-only at 2026-08-05T00:50Z, before any host change. Artifacts held
outside the repo; the numbers that matter are recorded here.

### Coverage

| | |
|---|---:|
| all forecasts | 12,759 |
| matured eligible | 11,617 |
| usable outcomes | 1,684 |
| **matured coverage** | **14.5%** |
| scored_current | 903 |
| missing outcome | 9,933 |
| `sync_never_attempted` | 9,847 |
| `local_outcome_stale` | 86 |
| conflicts | 0 |

### Scorability

`forecasts=12,759  matured_eligible=11,923  scored_current=903
legitimately_pending=10,801  scorable_backlog=1,055  stale=0  inconsistent=0`

Verdict: **`OUTCOME_SYNC_COVERAGE_IS_THE_BLOCKER`**

### Reliability (n = 903)

| | |
|---|---:|
| mean Brier | 0.180043 |
| neutral baseline | 0.25 |
| base-rate baseline | 0.234201 |
| skill vs base rate | 0.2312 |
| skill vs neutral | 0.2798 |
| ECE / MCE | 0.0464 / 0.165 |
| Murphy reliability | 0.003585 |
| Murphy resolution | 0.058839 |
| Murphy uncertainty | 0.234201 |
| prevalence | 0.3743 |

Verdict: **`DOMAIN_HETEROGENEITY_DOMINATES`**

### Calibration

`total=1000 resolved=903 pending=97 unscorable=0`, overall Brier 0.180043.

Per domain: soccer 0.0033 (n=34) · general 0.1652 (n=116) · baseball 0.1815
(n=686) · tennis 0.2805 (n=67).

### Representation skew (the reason composition matters)

| Domain | share of all | share of scored | Δ |
|---|---:|---:|---:|
| sports_baseball | 85.11% | 75.97% | −9.14 pp |
| sports_soccer | 11.30% | 3.77% | −7.54 pp |

> **Changes after activation may reflect population expansion and composition
> shift. They must not be interpreted as forecast-model improvement without
> controlled evidence.**
>
> This is not boilerplate here. Domain Brier spans 0.003 to 0.281 in the
> baseline, and the two most under-represented domains are the two the repair
> reaches first. The aggregate will move on mix alone.

## 3. Selection-repair audit (Gate 3)

69 milestone tests green; full repository suite 2,536 passed (one known
load-sensitive perf flake, clean in isolation). The twelve guarantees are each
pinned by a test — need-based selection, terminal exclusion, deterministic
rotation, rotation independent of prunable `marketops_runs` **count**
(`MAX(id)`, not `COUNT(*)`), progress under partial and total provider failure,
no permanent starvation, need-based scoring past the id prefix, flip
recomputation, canceled/void/unknown/conflicting left unscored, idempotence, and
no forecast mutation.

## 4. Drain-limit policy (Gate 4)

`MARKETOPS_SCORE_LIMIT=100` for the first window.

The backlog is ~10,000 matured forecasts and `score_forecast` commits **once per
forecast**. At the previous limit of 1000 that is 1,000 separate commits per
cycle on `journal_mode=delete`, against a database with **4 recorded
`database_locked` events in its entire history** and a live watcher writing
ticks. 100 bounds commits and cycle duration while still letting the repaired
selector advance every cycle.

### Stop thresholds, fixed before activation

Any MarketOps cycle not `ok`; `stage_errors` containing outcome sync or scoring;
lock events above 6 (baseline 4); cycle duration above ~120 s (baseline 40–64 s);
provider failures above 10% of calls; max scored id failing to pass 1000 within
three cycles; any duplicate current score; any silently-scored conflict; coverage
flat for three consecutive cycles despite eligible backlog; provider calls again
concentrating on terminal-current markets.

## 5. Activation (Gate 5)

```env
ENABLE_OUTCOME_SYNC_COVERAGE_REPAIR=true
MARKETOPS_SCORE_LIMIT=100          # was 1000
```

`.env` SHA-256 `041394bba799f0de…` → `0ae18cb9d9e5d900…`, applied 01:01:56Z.
Semantic key diff proved **exactly two keys changed**, 88 → 89 keys total. No
other value read or exposed. `.env` is not committed. Rollback copy at
`/tmp/.env.osca001.bak`; previous value `MARKETOPS_SCORE_LIMIT=1000` recorded.

**No restart was performed, and none was required.** `probability-arena-marketops.service`
is `Type=oneshot` with `EnvironmentFile=.env`, so each cycle is a fresh process
that re-reads both. The only long-running process is
`probability-arena-watcher.service`, which was verified not to call scoring or
outcome selection — the `@lru_cache`d-settings trap that applies to
`RAW_PAYLOAD_CAPTURE_MODE` does not apply to these keys.

**A timing note worth recording, because it briefly looked like a failure.**
Cycles 7728 (00:55) and 7729 (01:01:03–01:01:47) both ran with `scored=0` *after*
activation was requested but *before* `.env` was written at 01:01:56 — 7729
finished nine seconds early. They are pre-activation cycles. The first genuinely
active cycle is **7730**.

## 6. First natural active cycle — 7730 (Gate 6)

`ok`, `stage_errors={}`, 44,194 ms, Alembic `0027`, not manually triggered.

```json
"score_counts": {"scored": 89, "pending_outcome": 11, "unscorable": 0, "skipped": 0}
```

**`skipped: 0`** is the whole result. The previous cycle skipped 1,000 of 1,000;
this one skipped none, because the selector now returns only forecasts that need
work. `89 + 11 = 100` — the drain limit, exactly enforced.

`market_outcomes` grew **2,026 → 2,126 (+100)** on the same 100 provider calls
that had been producing zero new rows. Distinct scored forecasts **1,000 →
1,072**, max scored id **1,000 → 1,072**: the id prefix broke on the first
active cycle.

## 7. Bounded drain window (Gate 7)

Five natural cycles, 01:07–01:31Z. No cycle was triggered manually and no
monitor, timer or daemon was created on the host.

| cycle | scored | pending | unscorable | skipped | synced | duration | stage_errors |
|---|---:|---:|---:|---:|---:|---:|---|
| 7730 | 89 | 11 | 0 | **0** | 100 | 44,194 ms | `{}` |
| 7731 | 92 | 8 | 0 | **0** | 100 | 42,807 ms | `{}` |
| 7732 | 99 | 1 | 0 | **0** | 100 | 42,173 ms | `{}` |
| 7733 | 59 | 41 | 0 | **0** | 100 | 44,569 ms | `{}` |
| 7734 | 66 | 34 | 0 | **0** | 100 | 41,484 ms | `{}` |

Every cycle processed exactly 100 candidates and skipped none. Durations
41.5–44.6 s sit inside the pre-activation band of 40–64 s.

`scored` varies (59–99) because the selector now reaches forecasts whose markets
have not settled yet; those become `pending_outcome`, which is the correct
outcome and not a failure.

## 8. Progression (Gates 8–9)

| | before | after | Δ |
|---|---:|---:|---:|
| matured eligible | 11,617 | 11,638 | +21 |
| outcome rows present | 1,770 | 2,916 | +1,146 |
| usable outcomes | 1,684 | 2,830 | **+1,146** |
| **matured coverage** | **14.5%** | **24.32%** | **+9.82 pp** |
| scored_current | 903 | 1,308 | +405 |
| distinct scored forecasts | 1,000 | 1,472 | +472 |
| max scored forecast id | 1,000 | **1,472** | **prefix broken** |
| ids beyond 1000 | 0 | **472** | — |
| missing outcome | 9,933 | 8,808 | −1,125 |
| `market_outcomes` rows | 2,026 | 2,526 | +500 (= 100/cycle × 5) |
| duplicate current scores | 0 | **0** | — |
| silently-scored conflicts | 0 | **0** | — |
| stale / inconsistent | 0 / 0 | 0 / 0 | — |
| **terminal rows re-fetched** | **100 of 100** | **0** | — |
| unreachable tickers | 4,902 | **0** | — |
| lock events | 4 | **4** | 0 |
| database bytes | 4,550,623,232 | 4,550,623,232 | 0 |
| backup freshness | healthy | healthy | — |

Coverage verdict moved `OUTCOME_SYNC_SELECTION_IS_THE_BLOCKER` →
`COVERAGE_RECOVERS_WITH_CURRENT_PROVIDERS`; selection moved
`legacy_alphabetical_prefix` → `need_based`.

**All thirteen Gate 8 success criteria pass.**

### Two defects the activation found in the report itself

Neither affects the repair. Both made the instrument misdescribe it, which is
the same failure class this milestone series has already corrected twice, so
they are fixed here rather than noted.

1. **`candidate_pool` measured the wrong set.** It probed
   `select_sync_candidates` with `limit=10**9`; that selector fills any shortfall
   from recently-seen *non-forecasted* markets, so an enormous limit made the
   fallback engulf the whole `markets` table. Production reported a pool of
   **101,166** against 5,019 forecasted tickers and a fictitious **101.2-hour**
   sweep with verdict `SELECTION_SWEEP_PERIOD_TOO_LONG`. The real production
   path (`limit=100`) never reaches the fallback. The pool is now counted
   directly as non-terminal forecasted tickers.
2. **The id-contiguity finding inverted its own meaning.** "every forecast with
   id ≤ N has a score row and none above it does — the scoring selection is an
   id-ordered prefix, not a backlog" was written to detect the frozen prefix.
   With the repair on, the selector walks forecasts in id order taking those that
   need work, so a contiguous scored prefix is the *expected* shape of a draining
   queue. It is now suppressed when the repair is enabled and replaced by a
   drain-progress line.

## 9. Statistical interpretation

| metric | before | after |
|---|---:|---:|
| sample size | 903 | 1,308 |
| prevalence | 0.3743 | 0.3960 |
| mean Brier | 0.180043 | 0.179702 |
| base-rate baseline Brier | 0.234201 | 0.239189 |
| skill vs base rate | 0.2312 | 0.2487 |
| skill vs neutral | 0.2798 | 0.2812 |
| ECE / MCE | 0.0464 / 0.165 | 0.0362 / 0.152 |
| Murphy reliability | 0.003585 | 0.002403 |
| Murphy resolution | 0.058839 | 0.061572 |
| Murphy uncertainty | 0.234201 | 0.239189 |
| verdict | `DOMAIN_HETEROGENEITY_DOMINATES` | `DOMAIN_HETEROGENEITY_DOMINATES` |

### Domain composition — where the movement actually comes from

| domain | n before → after | Brier before → after | share before → after |
|---|---|---|---|
| sports_soccer | 34 → 112 | **0.0033 → 0.1162** | 3.77% → 8.56% |
| sports_baseball | 686 → 1,013 | 0.1815 → 0.1817 | 75.97% → 77.45% |
| general | 116 → 116 | 0.1652 → 0.1652 | 12.85% → 8.87% |
| sports_tennis | 67 → 67 | 0.2805 → 0.2805 | 7.42% → 5.12% |

### Classification of every movement

| movement | class |
|---|---|
| coverage 14.5% → 24.32%; scored_current 903 → 1,308; sample 903 → 1,308 | `population_expansion` |
| prevalence 0.3743 → 0.3960; base-rate Brier 0.234 → 0.239; all four domain shares moved | `composition_shift` |
| skill vs base rate 0.2312 → 0.2487 | `composition_shift` — the baseline it is measured against moved with it |
| ECE 0.0464 → 0.0362, MCE 0.165 → 0.152, Murphy reliability 0.0036 → 0.0024 | `composition_shift` — on a 45% larger, differently-mixed sample |
| soccer Brier 0.0033 → 0.1162 | `insufficient_evidence` in the baseline: n=34 was never a measurement |
| mean Brier 0.180043 → 0.179702 | `insufficient_evidence` — a −0.0003 move |
| — | `possible_forecasting_signal`: **none** |
| — | `score_currency_repair`: **none observed** — stale scores were 0 before and after, and no yes/no flip occurred in the window |

**Nothing here is evidence that forecasting improved.** The aggregate Brier is
flat to four decimal places while soccer's Brier rose 35×, baseball's share of
the scored sample rose, and general's and tennis's fell. The apparent
calibration improvement is a mix effect on a larger sample. The one thing the
window *does* establish is that the previous soccer number was an artifact of a
34-row sample, which is a data-quality finding, not a skill finding.

## 10. Backlog-drain projection (Gate 10)

Measured throughput: **100 forecasts processed per cycle** (89–99 scored, the
remainder correctly pending), 10 cycles/hour, and **100 markets synced per
cycle** now all productive.

| | |
|---|---:|
| scoring backlog (`scorable_backlog`) | 2,004 |
| cycles to drain scorable backlog | ~21 (~2.1 h) |
| matured forecasts still missing an outcome | 8,808 |
| markets behind them | ~3,400 |
| cycles to sync those markets | ~34 (~3.4 h) |
| forecasts to score once synced | ~10,800 |
| cycles to score them | ~108 (~10.8 h) |
| **total to drain at limit 100** | **~11 h** |
| measured database growth over the window | **0 bytes** (freelist absorbing; 312,994 free pages) |
| measured lock events over the window | **0 new** (still 4 lifetime) |
| provider calls | 100/cycle — unchanged, but productive instead of wasted |

**Keep the limit at 100.** It drains the whole backlog inside a day at zero
measured lock cost and zero measured file growth, and the milestone forbids
raising it. There is no evidence a higher limit is needed and the only argument
for it — speed — is worth little against a database with four lifetime lock
events. Revisit only if a later window shows the drain stalling.

## 11. Operating decision (Gate 11)

**KEEP COVERAGE REPAIR ENABLED AT SCORE LIMIT 100.**

All thirteen activation-window criteria pass, no stop threshold was approached,
and the repaired normal MarketOps selection owns the gradual drainage. No
separate automatic historical backfill loop was added or is wanted.

### Rollback, if a later window degrades

```env
ENABLE_OUTCOME_SYNC_COVERAGE_REPAIR=false
MARKETOPS_SCORE_LIMIT=1000
```

Copy at `/tmp/.env.osca001.bak`. No migration, no schema change, no data
transformation — the repair changes only which rows are selected, so rolling
back is inert. Rows already written are legitimate source-backed outcomes and
scores and are kept; per §9 they permanently change the calibration denominator,
which is why the pre-activation baseline in §2 is recorded in full.
