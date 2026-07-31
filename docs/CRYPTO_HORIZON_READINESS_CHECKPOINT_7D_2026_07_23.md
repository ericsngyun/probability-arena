# CRYPTO-HORIZON-CANDIDATE-READINESS-001 — Real Seven-Day Checkpoint (2026-07-23)

```text
SEVEN-DAY CHECKPOINT: PASS WITH OPERATIONAL FINDINGS
RECOMMENDATION: CONTINUE MEASUREMENT TO JULY 30
```

This is the **real seven-day checkpoint** for the readiness measurement activated at
cycle 3097 on 2026-07-16T19:56:26Z. It is **not** the activation sanity check — that
earlier artifact (`docs/CRYPTO_HORIZON_READINESS_ACTIVATION_SANITY_2026_07_16.md`,
~6.5 minutes / 2 cycles) was explicitly relabelled to avoid exactly this confusion.
Analysis and documentation only: no cohort was created or armed; no discovery scan,
provider call, observation, timer, daemon, unit, or write was produced by this
checkpoint. All readiness/feasibility CLIs used are read-only (`external_calls=0`,
`persisted=false`).

## Clock confirmation and exact interval

Host clocks captured independently at checkpoint start; Mac and EVO-X2 agreed to the
second (2026-07-24T02:00:18Z / 19:00:18 PDT; epoch 1784858418), past the
2026-07-23T19:56:26Z threshold.

- **Interval:** `2026-07-16T19:56:26Z → 2026-07-24T02:02:55Z` (EVO state-capture instant)
- **Elapsed:** 626,789 s = **174.11 hours = 7.2545 days**
- **Expected MarketOps cycles:** ~1,750 at the observed effective cadence
  (`OnUnitActiveSec=5min` + ~35 s cycle runtime + timer accuracy → median gap 359.9 s;
  the naive 300 s figure of ~2,089 does not reflect how oneshot timers rearm)
- **Observed enabled cycles:** **1,751** (cycles 3097–4847, contiguous)

## Baseline at checkpoint

- Mac `main` = `origin/main` = `1dfce77`; tracked-clean (known unrelated untracked
  files only). EVO-X2 pinned at `3f742c9` (intentional divergence), tracked-clean;
  Alembic `0027 (head)`; `MARKETOPS_INCLUDE_CANDIDATE_READINESS=true`.
- Timers active: marketops (5 min), meme-news (10 min), tick-aggregation (hourly),
  baseline (4 h), edge-observation (daily), retention (daily), watcher daemon.
  Backup timer **not installed** (pre-existing R7). No horizon one-shot units
  installed; no new unit, timer, or daemon appeared during the interval.
- SQLite: `journal_mode=delete`, `synchronous=FULL(2)` (unchanged).
- One unexpected **pre-existing** untracked file in the EVO repo dir (a 250-byte
  command-typo artifact dated 2026-07-15 20:57 containing stray `git log` output,
  filename beginning `ystemctl --user list-timers…`). Created before this window;
  left untouched; flagged for manual cleanup at the operator's discretion.

## Record integrity (readiness JSONL)

`~/crypto-horizon-readiness/readiness.jsonl` — 647,870 bytes, **1,751 lines**,
sha256 `ef03d749e9acbdf13d0a0b15f33f52190a876ef19b2ba08af9d06d9ccabb6545`. Validated
on a hash-verified copy, every line:

| Check | Result |
|---|---|
| valid JSON | 1,751 / 1,751 |
| schema-exact (13 canonical keys, no extras) | 1,751 / 1,751 |
| state in the 7-state enum | 1,751 / 1,751 |
| timestamps strictly increasing | yes |
| one record per enabled cycle (3097–4847 contiguous) | yes — 0 missing |
| duplicate cycle IDs / run IDs | 0 / 0 |
| `external_calls=0` | 1,751 / 1,751 |
| `candidate_readiness_error` cycles | **0** |
| malformed canonical token IDs | 0 (all candidate fields null) |
| secret / provider-payload pattern hits | 0 |
| max line size | 369 B (bound 4,096 B) |
| append-only | **proven** — sha256 of the first 740 bytes equals the activation snapshot prefix `da6f1511…`; growth 370 B/record matches cadence |

```text
enabled MarketOps cycles                 1751
actual readiness records                 1751
missing records                          0
duplicate records                        0
invalid records                          0
records with external_calls != 0         0
records with candidate_readiness_error   0
```

No mismatches exist to explain.

## MarketOps reliability and isolation

From `marketops_runs` 3097–4847 (run-scoped summaries, not global counters):

- Completed **1,751**, successful **1,751**, failed **0**; `stage_errors` empty in
  every cycle; systemd `Failed with result` count for marketops: **0**.
- Cycle duration: median **35.3 s**, max **93.5 s**. Cadence gaps (JSONL): min 280.2 s,
  median 359.9 s, mean 358.1 s, max 408.5 s; no gap > 900 s.
- `crypto_scan` stage `ok` in all 1,751 cycles; readiness evaluator failures **0**.
- **Second scans: 0.** Exactly one crypto watcher run per cycle. Two audit artifacts
  explained precisely: (a) `crypto_watcher_runs` rows for the first 42 in-window
  cycles (3097–3138, Jul-16 19:56 → Jul-17 00:05) were **retention-pruned** — the
  earliest surviving row sits exactly at the 7-day retention boundary of the Jul-24
  00:00 retention run, while all 42 cycle summaries record `crypto_scan: ok`; (b) an
  apparent second scan on cycle 4847 is cycle 4848's scan (started 02:08:04, after
  the query cutoff) misattributed by interval bucketing.
- Run-scoped readiness attribution, every cycle: `candidate_readiness.external_calls=0`.

```text
readiness provider calls                 0
second scans                             0
readiness-attributable cohort writes     0
readiness-attributable observation writes 0
readiness-attributable units installed   0
MarketOps failures caused by readiness   0
```

Cohorts remain 1–6 (members 10/3/2/1/2/1); observations by cohort 15/6/4/–/6/4;
**0 cohorts and 0 observations created during the interval**; Alembic `0027`.

## State distribution (canonical history report, 1,751 evaluations)

| State | Count | % |
|---|---|---|
| `expired` | 1,751 | 100% |
| `no_complete_candidates` | 0 | 0% |
| `no_overlapping_pair` | 0 | 0% |
| `pair_detected_not_due` | 0 | 0% |
| `pair_ready_for_manual_preparation` | 0 | 0% |
| `shared_due_now_ready` | 0 | 0% |
| `insufficient_arm_slack` | 0 | 0% |

Total evaluations 1,751; distinct candidate pairs **0**; distinct ready moments **0**;
ready moments by UTC date, Los Angeles date, and hour: **none**; consecutive same-pair
moment durations: n/a; min/median/max safe arm slack: n/a; next-cycle persistence of
ready pairs: n/a. `rejection_reason="expired"` and `overlapping_pairs=197` (the
pre-activation historical pairs), `usable_pairs=0` on **every** record. No ready
moment ⇒ no operator-review dry-run command was ever printed for a live pair and
**none was executed**; no CANARY-004 request arises from this window. No cohort was
or may be created from an expired pair.

## Catch-rate analysis (primary checkpoint finding)

Per the feasibility model (anchors = `crypto_token_birth_events`):

```text
historically usable moments (in-interval)  0
live-recorded usable moments               0
missed usable moments                      0
catch rate                                 N/A — zero-denominator
```

**Why the denominator is zero — the load-bearing finding.** The birth-anchor
population did not grow at all during the interval: still **508 anchors**, latest
`created_at` **2026-07-16T04:45:57Z** (before activation); the feasibility funnel's
24 h and 174 h ranges have **denominator 0**. `CryptoTokenBirthEvent` rows are
produced **only** by the manual, human-approved crypto-tape lane
(`crypto_tape.py:292`), and **no tape session ran during the window**. The readiness
evaluator therefore re-evaluated a frozen 508-anchor population whose 197
overlapping pairs had all closed before activation — `expired` × 1,751 is the
**correct** classification of its input, every time.

Classification of the four distinguished cases:

1. **Discovery never surfaced the pair in time — NO at the raw layer.** Discovery
   persisted **2,079 new solana tokens** during the interval (`crypto_tokens`).
   A read-only estimate applying the deployed completeness/window/margin rules to
   raw discovery rows (anchor = earliest pair creation; initial state = first tick):
   ~**802** complete-state tokens, **295** persisted while 15m-feasible, **203** with
   safe arm slack (45 s grace + 180 s margin), and ~**15 would-have-been usable
   overlapping pairs** (~2.1/day — consistent with the feasibility report's ~1.4/day).
   Median persist lag for complete tokens was ~22 min at the raw layer — materially
   fresher than the ~85 min tape-anchor lag measured on 07-16.
2. **Persisted in time but MarketOps cadence missed it — NO.** Cadence was healthy
   (median 359.9 s, max gap 408.5 s); nothing entered the anchor table to miss.
3. **Recorded but insufficient arm slack — NO.** No in-window pair was ever recorded.
4. **Recorded as operationally ready — NO.**

Missed-moment classification: all ~15 estimated usable pairs fall under
**`other_evidence_backed_reason` — birth-anchor production starvation**: the anchor
lane (manual tape sessions) did not run, so the readiness evaluator's input feed was
static. This is **not** a readiness-hook failure, not a cadence failure, and not —
on this week's raw-layer evidence — primarily a discovery-latency failure. (The
raw-layer numbers are an estimate labelled as such; the deployed tape completeness
rule includes fields this approximation cannot check.)

**Consequence:** unless a tape/anchor-producing session is separately authorized to
run during the remaining window, the 2026-07-30 checkpoint's catch rate will also be
vacuous (zero-denominator). That authorization decision is the operator's; nothing
in this checkpoint changes any lane.

## SQLite operational context (no SQLite change made)

- **`database is locked` events in-window: 51** — watcher **38** (recovers next
  iteration; 3 `error` rows in 9,870 watcher runs), meme-news **9** journal mentions
  including **3 hard-failed scheduled invocations** (Jul-16 20:50, Jul-16 22:49,
  Jul-23 03:08 — the known R3 `meme_scout_runs` INSERT loss; 959 runs succeeded),
  baseline **4** (0 failed invocations), marketops **0**, retention **0**.
- Tick-aggregation: 170 `ok` runs, 0 failures; **2 lock incidents self-recovered**
  via its bounded retry ladder (Jul-21 retry 1/3; Jul-23 retries 2/3).
- MarketOps cycles affected by locks: **0**. Hard-failed scheduled runs: **3**
  (all meme-news). Successfully recovered retries: **2** (tick-aggregation).
- **Database size: 3,681,239,040 B (~3.68 GB)** vs 2,884,030,464 B on 2026-07-17 —
  **+797 MB (~115 MB/day)**, now **above the documented absolute `db_growth_critical_mb`
  gate (3,072 MB)**. Filesystem: 75,679,907,840 B free (69% used, ~71 GiB) vs ~74 GiB
  on 07-17. No free-space alerting exists (known gap R5).
- Exact transaction-hold durations are **not claimed** — hold-time telemetry is not
  yet installed (SQLITE-LOCK-TELEMETRY-001A is the designed follow-up).

None of the lock activity is readiness-attributable; the readiness path writes no DB
row and its JSONL append is non-DB and swallow-all.

## Verdict

**PASS WITH OPERATIONAL FINDINGS.** All PASS criteria hold: record integrity valid;
exactly one record per enabled cycle with zero unexplained gaps; zero
readiness-attributable provider calls, second scans, cohort/observation/timer/
daemon/unit creation; zero MarketOps failures caused by readiness; no secret
leakage; bounded append-only JSONL growth (370 B/record, ~89 KB/day); correct
readiness classification throughout.

Operational findings (none caused by the readiness hook):

1. **Birth-anchor starvation** made the catch-rate vacuous; ~15 raw-layer usable
   pairs existed that no anchor ever represented (primary finding, above).
2. **DB passed its absolute critical size gate** (3.68 GB > 3.07 GB) at ~115 MB/day
   growth; no filesystem free-space alerting exists. Strengthens the case for the
   already-designed storage/lock telemetry (R5).
3. Pre-existing background lock contention continues (51 events; R1–R3 profile
   unchanged); 3 meme-news scheduled runs were lost to locks.
4. Pre-existing stray untracked typo-artifact file in the EVO repo dir (2026-07-15).

**RECOMMENDATION: CONTINUE MEASUREMENT TO JULY 30.** No live pair is ready at report
time, so no CANARY-004 approval is requested (retrospective arming is never
requested). The freshness question is materially reframed by this week's evidence —
raw discovery persistence is fresher than the tape-anchor lag suggested — but a
seven-day, zero-denominator window does not justify launching
CRYPTO-DISCOVERY-FRESHNESS-001 early; that call belongs to the 14-day review with
the anchor-starvation finding on the table. No July-30 conclusion is drawn from
seven-day data.

## EVO-X2 resynchronization (post-checkpoint)

The controlled fast-forward of EVO-X2 to current `main` proceeds only on this
verdict, and is recorded — with its pending-diff audit and post-sync natural-cycle
observation — in `DEPLOYMENT_REPORT_EVO_X2.md`, per repository convention.
