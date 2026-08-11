# CRYPTO-RECONCILIATION-CAPACITY-001 — where the per-token reconciliation cost goes

Status: **MEASUREMENT AND CLASSIFICATION ONLY.** Nothing was merged, pushed,
deployed, migrated or activated. No production behaviour, systemd unit, `.env`
or live database was modified. `enable_crypto_tape_reconciler` is still default
OFF and `worktree/crypto-coverage-repair` is still not on `main`.
Everything below is evidence for a later design pass to consume; the
"Recommendations" section is a recommendation, not an implemented change.

Branch: `worktree/crypto-coverage-repair` (measured at `2775f60`).
Measured: 2026-08-11, on EVO-X2, against a **throwaway online copy** of the
production database (`sqlite3.Connection.backup()` — the same API
`app/services/backup.py` uses; 4.55 GB copied in **2.40 s** with the live
watcher running), at `/mnt/data/crypto-recon-capacity-bench/`, **deleted after
use**. Harness: `scripts/crypto_reconcile_cost_profile.py` (committed with this
document so the numbers are re-derivable).

Prior context: CRYPTO-COVERAGE-REPAIR-001's fifth round established that safety
is solved and capacity is the blocker — write-time SLO 2.0 s (evidence-backed),
adaptive time-budget batching works, ~130 tokens/pass, `INSUFFICIENT_
RECONCILIATION_CAPACITY`. It also concluded that "the lever that actually
changes capacity is `max_duration_seconds` or cadence, not batch sizing."
**That conclusion is correct about batch sizing and wrong about the lever.**
The lever is neither: it is a missing SQLite query plan.

---

## 0. Headline

| | |
|---|---|
| What limits **tokens per transaction** (the 2.0 s write-lock budget) | **source-row load — 87.6 % of the write-lock hold** |
| What limits **tokens per day** (total pass wall time) | **source-row load — 82.8 % of pass wall** |
| Why source-row load is expensive | the production DB has **no `sqlite_stat1`**, so SQLite picks `ix_*_chain` (one distinct value: `'solana'`) over the selective `token_address` index and adds a temp-B-tree sort — every `_load_sources` call walks 394 219 + 309 201 + 137 842 index entries |
| Everything the pass **persists**, combined | **0.21 % of pass wall, 0.28 % of the write-lock hold** |
| Measured headroom from one `ANALYZE` (1.0 s) | **130 → 1 285 tokens/pass (9.9×)**, and write-lock hold p95 **0.702 s → 0.031 s** |

The write set is not the cost. The read plan is.

---

## 1. Arrival rate, backlog, and backlog age (re-measured, not inherited)

Measured on the copy at 2026-08-11 01:13 UTC. Primary signal is
`crypto_tokens.first_seen_at` (chain=`solana`) — the actual arrival. The prior
doc's metric (`crypto_token_birth_events.observed_at`) agrees to within one
token in every window ≥ 24 h, so both are reported and neither is disputed.

| window | tokens (`first_seen_at`) | per day | births (`observed_at`) |
|---|---|---|---|
| 24 h | 538 | **538.0** | 538 |
| 3 d | 1 345 | 448.3 | 1 345 |
| 7 d | 2 930 | **418.6** | 2 930 |
| 14 d | 5 517 | 394.1 | 5 517 |
| 30 d | 10 164 | 338.8 | 7 291 (feed gap 07-17…07-23) |

Per-UTC-calendar-day, 29 complete days (partial first/last days dropped):

| min | p50 | **p95** | max | mean |
|---|---|---|---|---|
| 231 | 352 | **425** | 529 (2026-08-10) | 343.3 |

**The task's figures are confirmed** (536 vs 538 at 24 h; 419 vs 418.6 at 7 d).
The rate is rising monotonically as the window shortens — 394 → 419 → 448 →
538 — so the stationary p95 of 425/day **understates** the current regime; the
most recent complete day is 529. For planning, use **~530/day, still rising**,
not 425.

### Backlog

Recorder predicate (`unreconciled_backlog` / `backlog_size`): outside the 48 h
window, outcome row missing **or** `final = 0`.

| | |
|---|---|
| backlog size | **11 830 tokens** |
| oldest unreconciled `first_seen_at` | 2026-07-04 01:36 UTC |
| frontier age | **911.6 h = 38.0 days** |
| in-window eligible (48 h, ≥ 22.5 min old, not final) | 927 |
| `work_available` | **12 757** |
| survival outcome rows | 7 341 (`final=1`: **2**; `survived_24h` non-null: **0**) |
| tokens with no birth event at all | 12 768 − 7 341 = **5 427** |

Age distribution (days since `first_seen_at`), cumulative:

| age | count | cum | age | count | cum |
|---|---|---|---|---|---|
| 2 d | 409 | 409 | 20 d | 290 | 6 953 |
| 3 d | 394 | 803 | 22 d | 233 | 7 487 |
| 5 d | 388 | 1 573 | 25 d | 256 | 8 227 |
| **7 d** | 425 | **2 419** | 30 d | 215 | 9 441 |
| 10 d | 381 | 3 427 | 34 d | 404 | 10 706 |
| 14 d | 405 | 4 984 | 37 d | 403 | **11 830** |

It is close to flat at 187–425/day across the whole 2–37-day span — i.e. the
backlog is not a one-off spike, it is *every* token that has ever arrived and
never been reconciled.

**Live ticks span 2026-08-04 → 2026-08-11 only** (`crypto_retention_days = 7`).
So **9 411 of the 11 830 backlog tokens (79.6 %) are older than their own
evidence** and can now only ever finalize as B5's `retention_lost` — real
capacity spent writing off, not measuring. That is a designed behaviour, not a
defect, but it means the first ~2 days of any fixed reconciler drain
write-offs.

---

## 2. B1 — cost decomposition

Method: the real `CryptoLifecycleTapeRecorder.run_once` with **exactly** the
kwargs `run_scheduled_reconciliation` passes (`limit=2000, hours=48,
oldest_first=True, include_backlog=True, exclude_final=True,
min_age_minutes=22.5, skip_redundant_when_final=True, batch_size=5,
max_duration_seconds=20.0`). Instrumentation is monkeypatched in the harness
process only: SQLAlchemy `before/after_cursor_execute` hooks for per-statement
wall/CPU/rowcount and **whether a write transaction was already open**, plus
`perf_counter` wrappers on the recorder's own methods and on `Session.commit`
to close each write-transaction window. Three repetitions, each from a fresh
restore of the pristine copy: **385 tokens, 77 batches, 83 write transactions,
61.33 s total.**

### Write-lock hold (the 2.0 s SLO budget)

| n | p50 | p95 | max | sum | p95 as % of SLO |
|---|---|---|---|---|---|
| 83 | 0.5657 s | 0.7024 s | 0.7595 s | 45.77 s | **35.1 %** |

(The fifth round's "batch-5 uses 41–44 %" measured the whole batch's wall time;
this measures the actual lock hold — first write statement to commit return.
Both are consistent; this is the tighter number.)

### Per-component decomposition

`p50/p95/max` are **per token** for token-scoped components and **per batch**
for `commit` / `inter_batch_yield`. `%hold` = share of the 45.77 s of
write-lock hold. Read/write seconds are cursor-execute wall time; CPU is
`process_time` (user+system, so SQLite's own scanning counts).

| component | stmts | CPU s | DB read s | DB write s | rows written | IN-txn s | OUT-txn s | % pass | **% hold** | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **source-row load** | 2 310 | 49.927 | 50.759 | 0.000 | 0 | **40.088** | 10.671 | **82.8 %** | **87.6 %** | 128.29 | 147.34 | 403.77 |
| inter-batch yield | 0 | 0.000 | 0.000 | 0.000 | 0 | 0.000 | 3.854 | 6.3 % | 0.0 % | 50.05 | 50.07 | 50.08 |
| commit | 0 | 0.474 | 0.000 | 0.000 | 0 | 3.273 | 0.007 | 5.3 % | 7.2 % | 17.94 | 89.26 | 529.04 |
| selection | 15 | 0.100 | 0.105 | 0.000 | 0 | 0.000 | 0.105 | 0.2 % | 0.0 % | 30.93 | 43.62 | 43.62 |
| diff creation (birth+snapshot+actor build) | 0 | 0.085 | 0.000 | 0.000 | 0 | 0.076 | 0.008 | 0.1 % | 0.2 % | 0.19 | 0.36 | 1.97 |
| birth write | 385 | 0.027 | 0.000 | 0.062 | 385 | 0.062 | 0.000 | 0.1 % | 0.1 % | 0.06 | 0.14 | 9.15 |
| snapshot write | 385 | 0.012 | 0.000 | 0.036 | 385 | 0.036 | 0.000 | 0.1 % | 0.1 % | 0.03 | 0.05 | 8.14 |
| survival computation | 0 | 0.032 | 0.000 | 0.000 | 0 | 0.032 | 0.000 | 0.1 % | 0.1 % | 0.06 | 0.15 | 1.91 |
| actor write | 385 | 0.010 | 0.000 | 0.018 | 385 | 0.018 | 0.000 | 0.0 % | 0.0 % | 0.02 | 0.04 | 8.30 |
| outcome write (incl. its read half) | 773 | 0.014 | 0.004 | 0.011 | 385 | 0.014 | 0.001 | 0.0 % | 0.0 % | 0.03 | 0.06 | 1.29 |
| transaction overhead (run row + prepass birth read) | 9 | 0.004 | 0.005 | 0.001 | 6 | 0.001 | 0.005 | 0.0 % | 0.0 % | 0.12 | 2.78 | 2.78 |
| **accounted** | | | | | | **43.601** | **14.651** | **95.0 %** | | | | |
| unaccounted (Python/ORM glue) | | | | | | | 3.075 | 5.0 % | | | | |

Statement-level breakdown of the source-row load (per-statement, rep 1):

| statement | n | wall s | % of all SQL | in-txn s | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|---|
| SELECT `crypto_token_discovery_events` | 130 | 9.199 | 53.7 % | 7.196 | **68.70** | 75.88 | 238.57 |
| SELECT `crypto_token_risk_assessments` | 130 | 5.859 | 34.2 % | 4.588 | **43.71** | 49.41 | 138.51 |
| SELECT `crypto_price_ticks` | 130 | 1.777 | 10.4 % | 1.398 | **13.38** | 15.03 | 26.32 |
| SELECT `meme_attention_snapshots` | 130 | 0.139 | 0.8 % | 0.138 | 0.01 | 8.67 | 40.94 |
| SELECT `meme_catalyst_events` | 130 | 0.068 | 0.4 % | 0.051 | 0.01 | 8.08 | 8.73 |
| SELECT `crypto_pairs` | 130 | 0.006 | 0.0 % | 0.004 | 0.04 | 0.13 | 0.18 |
| all 5 INSERT/UPDATE statement kinds | 777 | **0.045** | **0.3 %** | 0.045 | — | — | — |

Source rows actually loaded per token: p50 **69**, p95 263, max 2 008
(mean 68.4 discovery events, 38.1 risk assessments, 3.1 pairs, 2.0 ticks).
Loading ~69 rows costs 128 ms at p50 — 1.9 ms **per row**.

### (a) What limits TOKENS PER TRANSACTION

**Source-row load, 87.6 % of the 45.77 s write-lock hold.** Runner-up:
`commit` at 7.2 %. Everything the pass persists — birth + snapshot + actor +
outcome + run row — is **0.28 % of the hold combined**.

Mechanism, and it is a structural one worth naming: pysqlite defers `BEGIN`
until the first write statement. Within a batch of 5, **token 1's** outcome
`SELECT` triggers autoflush of its already-staged snapshot/actor `INSERT`s,
which takes the `RESERVED` lock — and then **tokens 2–5 run their entire
`_load_sources` under the held lock**. The measurement confirms the arithmetic
exactly: 40.088 / 50.759 = **79.0 %** of all source-load time is inside the
lock, against a predicted `(batch_size − 1) / batch_size = 80 %`.

### (b) What limits TOKENS PER DAY

**Source-row load again, 82.8 % of pass wall.** Runner-up: `inter_batch_yield`
6.3 % (26 batches × 0.05 s), then `commit` 5.3 %, `selection` 0.2 %.

The two limiters have the *same* answer today — but for two different reasons,
and they diverge the moment the read cost is fixed (§4).

Throughput is linear in the wall budget, as the fifth round found: a 60 s
deadline gave **380 tokens** in 60.43 s (6.29 tok/s) against **130** in 20.18 s
(6.44 tok/s). It also pushed max hold from 0.760 s to **1.018 s** (51 % of the
SLO) — lengthening the deadline buys throughput at a real, if still-safe, cost
in worst-case hold.

---

## 3. Root cause of the source-row load: no query-plan statistics

`EXPLAIN QUERY PLAN` for the three expensive `_load_sources` queries, run
**read-only against the live production database**, not just the copy:

```
SEARCH crypto_token_discovery_events USING INDEX ix_crypto_token_discovery_events_chain (chain=?)
USE TEMP B-TREE FOR ORDER BY
```

`SELECT count(*) FROM sqlite_master WHERE name='sqlite_stat1'` returns **0** on
the live database. **`ANALYZE` has never been run.** Without statistics SQLite
cannot know that `chain` has exactly one distinct value (`'solana'`) while
`token_address` has 12 768, so it picks the `chain` index and walks the whole
table, then sorts the result in a temp B-tree. Same plan on
`crypto_token_risk_assessments`, `crypto_price_ticks` and `crypto_pairs`. The
two `meme_*` tables have no `chain` predicate in their queries, hit their
selective index, and cost 0.01 ms — which is what all six should cost.

The indexes that *would* serve these queries already exist
(`ix_crypto_token_discovery_events_token_address` etc.). Nothing is missing
from the schema. Only the statistics are missing.

**Page-cache confound explicitly ruled out.** `ANALYZE` also warms the page
cache, so a control run was done: fresh copy, full read of all 867 863 rows of
all four tables (0.92 s — the entire dataset is resident; EVO has 92 GB RAM and
29 GB page cache), **no `ANALYZE`**, then the same pass. Result: **130
tokens/pass, source load 17.44 s, hold p50 0.561 s** — identical to baseline.
The 9.9× is entirely the query plan.

---

## 4. Measured headroom (on the copy; NOT deployed)

Same pass shape, same 20 s deadline, same `batch_size=5`, fresh restore each
time:

| variant | tokens/pass | pass wall | hold p50 | hold p95 | hold max | source load |
|---|---|---|---|---|---|---|
| baseline (rep 1/2/3) | 130 / 125 / 130 | 20.18 / 20.60 / 20.54 s | 0.566 s | 0.702 s | 0.760 s | 17.5–17.8 s |
| warm-cache control, no `ANALYZE` | 130 | 20.32 s | 0.561 s | 0.598 s | — | 17.44 s |
| **+ `ANALYZE`** (1.0 s to build) | **1 285 / 1 280** | 20.06 / 20.09 s | **0.0216 s** | **0.0306 s** | 0.0942 s | 3.15 s |
| + composite indexes (1.2 s to build) | **1 300** | 20.04 s | 0.021 s | 0.030 s | 0.110 s | 3.12 s |
| baseline @ 60 s deadline | 380 | 60.43 s | 0.569 s | 0.708 s | 1.018 s | 52.2 s |

`ANALYZE` and purpose-built composite indexes on `(chain, token_address, …)`
are **statistically indistinguishable** (1 285 vs 1 300). Both raise throughput
**9.9–10.0×** *and simultaneously reduce* the write-lock hold p95 from 35.1 %
of the SLO to **1.5 %**.

### Where the limiters move after the read fix (2 reps, 2 565 tokens, 40.15 s)

| component | % pass wall | % hold |
|---|---|---|
| **inter-batch yield** | **64.0 %** | 0.0 % |
| Python/ORM glue (unaccounted) | 20.1 % | — |
| **commit** | 11.4 % | **37.2 %** |
| diff creation | 1.2 % | 3.5 % |
| source-row load | 1.3 % | 3.1 % |
| survival computation | 0.9 % | 2.5 % |
| all four persisted writes | 0.85 % | 2.8 % |

The two limiters now **diverge**, which is why the task asked for them
separately:

* **tokens per transaction** → `commit` (37.2 % of hold) — but the hold is
  p50 0.022 s, **1.1 % of the 2.0 s SLO**. It is no longer a binding constraint
  at all.
* **tokens per day** → `RECONCILE_POST_BATCH_YIELD_SECONDS` — 257 batches ×
  0.05 s = 12.85 s of a 20 s pass.

### Capacity arithmetic

| | tokens/pass | ×4 passes/day | vs 538/day arrivals | backlog drain |
|---|---|---|---|---|
| today | 130 | 520 | **deficit** | never |
| with the read fix, nothing else changed | 1 285 | **5 140** | **9.6× margin** | 11 830 ÷ 4 602/day ≈ **2.6 days** |

This is achieved **without touching the 2.0 s SLO, the cadence,
`RECONCILE_BATCH_SIZE`, `RECONCILE_MAX_DURATION_SECONDS`, or the write set** —
all of which stay exactly as shipped.

---

## 5. B2 — minimal mature-outcome write set

Per token the pass persists at most four rows (plus two run-row writes per
pass): birth event (only when absent), lifecycle snapshot (appended every
pass), actor observation (appended every pass), survival outcome (upsert,
skipped when already `final`).

### Measured write cost (baseline, 3 reps / 385 tokens)

| write | statements | DB write s | % pass wall | % write-lock hold | avg row bytes |
|---|---|---|---|---|---|
| birth event | 385 | 0.062 | 0.10 % | 0.14 % | 1 309.3 |
| lifecycle snapshot | 385 | 0.036 | 0.06 % | 0.08 % | 407.2 |
| actor observation | 385 | 0.018 | 0.03 % | 0.04 % | 355.4 |
| survival outcome | 385 (+388 reads) | 0.011 | 0.02 % | 0.03 % | 320.5 |
| run row | 2 | 0.001 | 0.00 % | 0.00 % | — |
| **all writes combined** | | **0.127** | **0.21 %** | **0.28 %** | |

Post-read-fix (2 reps / 2 565 tokens) the same four writes are **0.85 % of pass
wall and 2.8 % of the hold**. At no point are they a capacity lever.

### Measured duplicate / unchanged writes

Two consecutive in-window passes over the *same* 400-token head, on an
`ANALYZE`d copy:

| | before | after pass A | after pass B |
|---|---|---|---|
| `crypto_token_lifecycle_snapshots` | 8 395 | 8 795 | 9 195 |
| `crypto_token_actor_observations` | 8 395 | 8 795 | 9 195 |
| `crypto_token_birth_events` | 7 341 | 7 341 | 7 341 |
| `crypto_token_survival_outcomes` | 7 341 | 7 341 | 7 341 |
| tokens with > 1 snapshot | 165 | 565 | 569 |

Pass B rewrote 400 outcome rows. Of those:

| | count | share |
|---|---|---|
| identical labels **and** `final` | 398 | **99.5 %** |
| identical labels, `final` **and** `details` | 396 | **99.0 %** |
| genuinely changed | 2 | 0.5 % |

So on a repeat visit, **99 % of outcome rewrites store byte-identical values**,
differing only in `last_run_id` and `computed_at`. Snapshots and actors append
unconditionally: neither table has any unique constraint
(`app/models.py:1391-1393` is a **non-unique** index; the actor table has no
`__table_args__` at all — confirmed in `alembic/versions/0026_crypto_lifecycle_tape.py:156-189`),
so a duplicate write cannot fail, it silently grows.

### Classification, with every consumer and what breaks

| write | class | consumers (production only) | what breaks / skews if dropped | measured saving |
|---|---|---|---|---|
| **birth event** (new) | **REQUIRED_CANONICAL_STATE** | `_assemble_pass:2120`; `build_tape_report:2716`; `crypto_coverage.token_coverages:177`; `crypto_retrospect.rows:218` (the `tape_backed` stratification); `crypto_horizon.create_cohort:522`, `_create_cohort_explicit:645`, `:1257`; `crypto_horizon_feasibility._load_anchors:118`; `crypto_horizon_readiness:186` | everything — outcome rows are keyed by `birth_event_id` | n/a |
| ↳ `birth.raw_payload` column | **REDUNDANT** | **none** — `raw_payload_policy.py:122-165` and `raw_payload_reclamation.py:126` both state it has no reader | nothing | 1 309 B avg row, largest payload in the write set; column-level, not row-level |
| **survival outcome** (new / changed) | **REQUIRED_CANONICAL_STATE** | `_universe:674`, `unreconciled_backlog:712`, `backlog_size:737`, `oldest_unreconciled_first_seen_at:772`, `universe_size:814`, `_assemble_pass:2133`, `_process_batch:1897`, `build_tape_report:2727`, `summarize_tape_session:2964`, `crypto_coverage:198` | the reconciler's own selection predicate | n/a |
| ↳ **unchanged** outcome rewrite (99.0 %) | **REDUNDANT** | `computed_at` → `build_tape_report:2729` window filter; `last_run_id` → **`summarize_tape_session:2966`** (manual-session summary: `outcomes_tracked`, `outcomes_final`, `horizon_maturity`, `provider_gap_true`) | making the rewrite conditional would drop unchanged rows out of `build_tape_report`'s `outcomes_computed`/`outcomes_final`/`survival_labels` window **and** out of `summarize_tape_session`'s per-run set. A reporting-semantics change that must be stated, not a silent one | **0.02 % of pass wall / 0.03 % of hold** |
| **lifecycle snapshot** — first per token | **REQUIRED_AUDIT** | `build_tape_report:2719-2744`; `crypto_coverage.token_coverages:205-216` | see below | — |
| **lifecycle snapshot** — every repeat | **DERIVABLE** (pure function of `TokenSources` + birth) **but the row population *is* the reported population** | same two | `build_tape_report`: `risk_level_mix`, `provider_coverage_mix`, `missing_data_mix` are per-snapshot-row histograms (`:2732-2744`, emitted `:2821-2829`) — this is the already-measured **77 % → 53 % high** bias. `crypto_coverage`: `run_appearances:243`, `last_observed_at:244`, `revisited_after_due:295` → `_classify:325/327` picks `CAUSE_JOIN_FAILED` vs `CAUSE_NOT_REVISITED` → `bottleneck_verdict` shares `:551-571`, `rates_vs_due["revisited_after_due"]:373`, `selection_analysis.appearances_min/max/mean:398,419-424`. Dropping repeats collapses `revisited_after_due` toward 0 and `appearances_*` toward 1 — i.e. it makes the coverage instrument report that the reconciler never revisits tokens, which is the exact metric this lane uses to diagnose itself | **0.06 % of pass wall / 0.08 % of hold**; 407 B/token |
| **actor observation** — first per token | **REQUIRED_AUDIT** | `build_tape_report:2723-2726` **only** | `actor_observations_recorded:2812`, `actor_pattern_examples:2758-2772` (top-N by `holder_distribution.top10_holder_pct`), CLI `app/cli.py:3185-3193` | — |
| **actor observation** — every repeat | **REDUNDANT** | same single reader | nothing measurable. The same fields are also on the snapshot row (`models.py:1357-1361`) and are recomputable on demand from `crypto_token_risk_assessments` via `merged_assessment_flags` (`crypto_tape.py:577`) — which is how `crypto_retrospect.py:243` obtains them without reading either table | **0.03 % of pass wall / 0.04 % of hold**; 355 B/token |
| **run row** (INSERT + UPDATE) | **REQUIRED_AUDIT** | `build_tape_report:2709` (the `run_id` join key for snapshots+actors), `summarize_tape_session:2959`, `run_scheduled_reconciliation:3380` (relabels `truncated`) | the snapshot/actor joins lose their key | 0.00 % |

**LEGACY_SIDE_EFFECT: none of the five row types qualifies.** The legacy
inheritance the task suspected is real, but it lives in the *repeat* snapshot /
actor appends and the unchanged outcome rewrite — not in a distinct write.
Note also that `skip_redundant_when_final=True` (which the scheduled path does
set) is **structurally inert**: it only fires for already-`final` tokens, and
`exclude_final=True` has already removed every one of them from selection.
CRYPTO-COVERAGE-REPAIR-001 documents this at its own line 531; this measurement
confirms it (`snapshots_skipped_redundant = 0` in every rep).

### Write-volume saving if repeat snapshot + actor rows were dropped

| throughput | bytes/pass | per day (×4) | per year |
|---|---|---|---|
| today (130 tokens/pass) | 96.8 KiB | 0.38 MiB | **0.13 GiB** |
| post-read-fix (1 285 tokens/pass) | 956.9 KiB | 3.74 MiB | **1.33 GiB** |

At today's throughput this is noise. **At post-fix throughput it is 1.33 GiB/yr
on a database already past its 3 072 MB alert gate** — which is a real argument,
but a *storage* argument belonging to the SQLITE-STORAGE-GROWTH lane, not a
capacity argument. It requires an explicit reporting-semantics decision
(§5 table), never a silent drop.

---

## 6. Recommendations for a later design pass

Ranked by measured capacity gain per unit of risk. **None of this is
implemented.**

1. **Give SQLite the plan it needs for `_load_sources`.** 9.9–10.0× tokens per
   pass, *and* it reduces write-lock hold p95 from 0.702 s to 0.031 s. Converts
   `INSUFFICIENT_RECONCILIATION_CAPACITY` (520/day vs 538/day) into ~5 140/day
   with a 9.6× margin and a 2.6-day backlog drain, **without touching the SLO,
   the cadence, `RECONCILE_BATCH_SIZE`, `RECONCILE_MAX_DURATION_SECONDS`, or
   the write set.** Two indistinguishable options:
   * **`ANALYZE`** — 1.0 s on the 4.55 GB copy, writes `sqlite_stat1`.
     Cheapest, but it is a *global* planner change: every other query in the
     application gets re-planned, which needs its own regression check, and
     statistics go stale as tables grow, so it needs a maintenance story
     (`PRAGMA optimize` on a schedule, coordinated with the backup timer).
   * **Composite indexes** on `(chain, token_address, …)` for
     `crypto_token_discovery_events`, `crypto_token_risk_assessments`,
     `crypto_price_ticks`, `crypto_pairs` — 1.2 s to build, deterministic,
     scoped to exactly these four queries. Costs index storage and a per-INSERT
     penalty on the four hottest *write* tables (scout/watcher path), which
     must be measured before adoption. Requires a migration.
2. **Move `_load_sources` outside the write transaction** (load a whole batch's
   sources before the batch's first write). **Derived, not measured:** 79.0 %
   of source-load time is currently inside the lock; removing it would cut the
   hold sum from 45.77 s to 5.68 s (≈ 0.069 s per transaction). Largely
   *redundant* if (1) lands — post-fix, in-lock source loads are 3.1 % of a hold
   that is itself 1.1 % of the SLO. Keep as defence in depth, not as the lever.
3. **Revisit `RECONCILE_POST_BATCH_YIELD_SECONDS` only after (1).** It is 6.3 %
   of the pass today but becomes the **#1 daily limiter at 64 %** once the reads
   are fixed. It is a deliberate writer-fairness mechanism (NEW-H1) and
   changing it re-opens a closed safety argument; the same effect is available
   by raising `batch_size` (fewer batches → less total yield), which is
   HIGH-2-pinned. Either needs its own competing-writer measurement.
4. **Do not pursue the write set as a capacity lever.** Every persisted write
   combined is 0.21 % of pass wall / 0.28 % of the write-lock hold today
   (0.85 % / 2.8 % post-fix). Dropping repeat snapshot + actor rows buys ≤ 0.1 %
   throughput and costs `build_tape_report` its `risk_level_mix` and
   `crypto_coverage` its `revisited_after_due` / `appearances_*` metrics.

---

## 7. Defects found (reported, not fixed — per this task's scope)

* **D1 — no `sqlite_stat1` on the production database.** The capacity blocker
  itself. Verified read-only against the live file, not only the copy.
* **D2 — `crypto_token_birth_events.raw_payload` has no reader** yet averages
  1 309 B across 7 341 rows — the largest payload in the reconciler's write set.
  `raw_payload_policy.py:122-165` already documents the absence of a reader.
* **D3 — `crypto_token_lifecycle_snapshots` / `_actor_observations` /
  `_lifecycle_runs` have no unique constraint, are never pruned by
  `retention.py`, and are absent from `retention_coverage.py`'s
  `PRESERVATION_FLOORS` and `DEPENDENCIES`** (`:87-113`) — so they render as
  undocumented, unowned, never-pruned growth. At post-fix throughput that is
  1.33 GiB/yr for the two append-only tables alone.
* **D4 — 99.0 % of repeat outcome rewrites are byte-identical** except
  `last_run_id` / `computed_at`, both of which *do* have readers
  (`summarize_tape_session:2966`, `build_tape_report:2729`) — so this cannot be
  optimised away without an explicit reporting decision.
* **D5 — 79.6 % of the current backlog (9 411 of 11 830) is older than the
  7-day tick retention**, so those tokens can only ever finalize as
  `retention_lost`. Designed behaviour (B5), but it means a fixed reconciler
  spends its first ~2 days writing off rather than measuring, and no amount of
  capacity recovers those labels.

## 8. What could not be measured, and why

* **Competing-writer WAIT after the read fix.** `scripts/crypto_reconcile_lock_bench.py`
  was not re-run post-`ANALYZE`. The hold collapse strongly implies improvement,
  but this milestone's own NEW-H2 lesson is that hold and wait are *different
  metrics* and do not move proportionally. **Required before any activation
  decision.**
* **Effect of `ANALYZE` / new indexes on the rest of the application's query
  plans.** A global planner change was measured only for the reconciler.
* **Per-INSERT cost of composite indexes on the scout/watcher hot path.**
* **Cold-disk behaviour.** EVO has 92 GB RAM and 29 GB page cache; the 4.55 GB
  database is effectively resident. Every figure here is a warm-cache figure.
* **`run_scheduled_reconciliation` end-to-end.** The harness calls `run_once`
  with exactly the kwargs that function passes, so the gate check and the
  `_reconciliation_should_abort` MarketOps query are excluded. That is one
  indexed SELECT per pass.
* **Cross-repetition variance of the `ANALYZE`/index variants** is 2 reps and
  1 rep respectively (baseline is 3 + a control). The effect size (9.9×) is far
  outside the observed 125–130 baseline spread, but the post-fix percentiles are
  thinner evidence than the baseline ones.

## 9. Safety and provenance

* Copy made with `sqlite3.Connection.backup()` against a **read-only** source
  connection; 2.40 s; live watcher unaffected; `/mnt/data` had 709 GB free.
* All measurement ran against `/mnt/data/crypto-recon-capacity-bench/bench.db`,
  restored from a pristine copy before every repetition. The harness refuses any
  path containing `projects/probability-arena/data`.
* **EVO's live database, systemd units and `.env` were never modified.** EVO
  remains on `main` at `2c8f75b`; this branch was never installed there — the
  branch source was extracted to the scratch directory via `git archive` and
  run with the existing venv interpreter.
* The 8.6 GB scratch directory was deleted; `/mnt/data` is back to 38 G used.
* No secrets were read or printed.
* Zero external calls: the reconciliation pass is provider-free by construction
  and `external_calls=0` in every run.
* Suite: **3 196 passed / 3 skipped**, both with and without the two files this
  document adds. One earlier run *of the same tree* (under concurrent load;
  476 s vs 300–324 s) produced five failures that did not reproduce in either
  subsequent clean run — `test_crypto_horizon_obs_001.py::TestCohort::{test_window_filters_on_first_evidence_not_observed_at,
  test_hours_1_never_returns_token_older_than_60_minutes,
  test_timezone_naive_first_evidence_handled}`,
  `test_edge_precheck.py::TestApi::test_run_list_report_roundtrip`,
  `test_ops009.py::TestRunSummaryStats::test_summary_includes_promotion_metrics`.
  Recorded as a **suspected load/clock-sensitivity flake**, unrelated to this
  work (both added files are inert — one markdown doc, one script imported by
  nothing) but worth a dedicated look.
