# CRYPTO-RECONCILER-GUARDED-TIMER-001 — Gate 3: calibrating `initial_per_token_cost_seconds`

**Status: Gate 3 CLOSED. Nothing is activated.** The constant derived here is
recorded in `.env.example` **commented out** and is set nowhere that takes
effect. Activation is Gate 6 and is a separate decision.

This document is the evidentiary record for a constant that will govern a
production writer's write-lock hold. It is written to stand on its own: the
samples, the filter they passed, the reasoning, the chosen value and every
caveat are all here, and none of it depends on the session that produced it.

The operator-facing form of the same derivation lives in
`docs/EVO_X2_RUNBOOK.md` under **"Gate 3 — the calibration session and the
chosen constant"**, immediately after the mandatory calibration filter it
depends on. The two must agree; `tests/test_calibration_gate3_001.py` fails if
they drift.

---

## 1. What Gate 3 was asked for

Recurring-timer **precondition 3** (`docs/EVO_X2_RUNBOOK.md`, "Recurring-timer
preconditions") requires *"a calibrated `initial_per_token_cost_seconds`, with
adaptive batching enabled"*. The value has **no default by design**: a fixed
token count (`CRYPTO_TAPE_RECONCILER_BATCH_SIZE`) is not a safety invariant,
because the write-lock hold it produces is `tokens_in_batch x per-token cost`
and per-token cost is host-speed dependent. Until this constant is measured on
the target host, the 2.0 s write-hold SLO is *recorded but not enforced*.

Gate 2 (`GATE2-WRITER-TELEMETRY-001`) supplied the distribution: one JSONL
record per governed pass on the SQLITE-LOCK-TELEMETRY-001A sink, carrying
`write_hold_ms_max`, `write_hold_measured`, `write_hold_slo_violations` and the
seed actually in force.

## 2. The session (Gate 3 measurements)

Eight **attended `--force`** passes on EVO-X2 at branch `CALIBRATION-GATE3-001`
(`ef92b4d`).

* `ENABLE_CRYPTO_TAPE_RECONCILER` was **off** for the whole session, so every
  pass recorded `gate_bypassed=force` and no timer existed at any point.
* Host load 0.3–0.6, with ordinary co-tenants running: the crypto watcher,
  MarketOps on its 5-minute cycle, meme-news on its 5-minute cycle, and hourly
  tick-aggregation. This is a *loaded-normal* host, not an idle one.

### 2.1 The filter, and what it removed

Every pass was required to satisfy the mandatory calibration filter:

* `writer_name == "crypto_tape"`
* `run_status` in `{ok, partial, truncated, backlog_expiring}`
* `batch_count > 0`
* `write_hold_measured == true`

**All eight passed. Zero were filtered out.** Every one of the eight also
recorded `write_hold_slo_violations = 0`.

That the filter removed nothing is worth stating explicitly rather than
omitting: it means the eight samples are the whole session, and no selection
step stands between the session and this constant.

### 2.2 The samples

Per-token cost `= write_hold_ms_max / batch_size`, `batch_size = 5` tokens.

```text
per-token ms, sorted (n = 8)
  14.8  17.8  18.0  18.8  18.8  19.6  19.8  105.4
  median      18.8 ms/token
  warm max    19.8 ms/token   (the 7-sample warm cluster)
  cold start  105.4 ms/token  (n = 1)
```

### 2.3 The raw records

| # | run_status | batches | `write_hold_ms_max` | `batch_lock_wait_ms_max` | `rows_committed` | note |
|---|-----------|---------|---------------------|--------------------------|------------------|------|
| 1 | partial | 350 | 527 | 516 | 3500 | **COLD** — first write after a fresh checkout |
| 2 | partial | 391 | 99 | 938 | 3910 | |
| 3 | partial | 394 | 94 | 514 | 3940 | |
| 4 | partial | 181 | 98 | 4038 | 1810 | `duration_ms = 41035` against a 30 s deadline |
| 5 | ok | 135 | 94 | 1015 | 1350 | |
| 6–8 | ok | — | — | — | — | identical `tokens_considered = 590`, ~11 s each |

## 3. Which denominator was used

The runbook's filter subsection requires the pairing be named, because
`write_hold_ms_max` is a **max over batches** while the two obvious
pass-level denominators are **sums over the pass**.

**The pairing used is `write_hold_ms_max / batch_size`**, i.e. the per-batch
token count — the same grain as the numerator. Verified on every pass:
`batches_committed x 5 == tokens_considered`.

**`rows_committed` was deliberately NOT used.** It is
`snapshots_created + outcomes_updated + birth_events_created`, rows summed
across three tables, and one token contributes to more than one. The table
above shows the ratio directly: 3,500 rows over 350 batches is **10 rows per
5-token batch** — about 2x the token count. Dividing by it would have
under-estimated per-token cost by roughly half and produced a first batch about
twice as large as intended.

## 4. The derivation

### 4.1 When the seed is consumed

**Once, at process start — the coldest moment of the pass.** Everything below
follows from that single fact.

`AdaptiveBatchCostEstimate` seeds its EWMA from this constant and then updates
it from every committed batch's *actual* measured wall time. So the seed
governs exactly one batch: the first one, before any real measurement exists.
That batch is issued against a cold page cache, a fresh journal and (after a
deploy) a cold process.

### 4.2 Why the single cold observation governs, and is not an outlier

A controller seeded from the warm cluster sizes its first batch as
`2.0 / 0.0198 ≈ 101` tokens. At the **observed** cold cost of 105.4 ms/token,
that batch holds the write lock for **10.6 s — 5x the 2.0 s
`RECONCILE_WRITE_TIME_SLO_SECONDS`** — before the estimator has learned
anything. Seeding from the median (18.8 ms) is worse.

So the single cold observation is not discarded. It is the **only** sample
taken under the conditions in which the seed is actually consumed, and it is a
**recurring condition, not a freak one**: every deploy and every reboot
reproduces it.

### 4.3 The chosen value

```text
samples (n=8, ms/token)  14.8  17.8  18.0  18.8  18.8  19.6  19.8  105.4
  median 18.8 | warm-cluster max 19.8 | cold-start 105.4

initial_per_token_cost_seconds = 0.15
  = worst observed (0.1054 s/token) x 1.42
first batch = 2.0 / 0.15 ≈ 13 tokens
  at cold cost 105.4 ms/token -> 1.37 s   (68% of the 2.0 s SLO)
  at warm cost  19.8 ms/token -> 0.26 s
```

The estimator is free to grow the batch from there on real measurements — this
is a seed, not a cap.

### 4.4 It loosens; it does not tighten

13 tokens is **larger** than the fixed `CRYPTO_TAPE_RECONCILER_BATCH_SIZE = 5`
that ships today. Calibration therefore **relaxes** the first batch relative to
current behaviour rather than constraining it. Stated plainly because the
opposite reading invites "recovering" throughput somewhere else.

### 4.5 What the shipped code actually does with 0.15 — two clamps

The arithmetic in 4.3 is the budget arithmetic. Two mechanisms already present
in the code make the **as-shipped** first batch smaller. Neither was changed by
this gate; both are recorded because a derivation that stops at 4.3 would
mis-describe the deployed behaviour.

1. **The seed is biased HIGH before use.** `AdaptiveBatchCostEstimate` applies
   `bias_multiplier = 1.5`, so a 0.15 seed yields a conservative estimate of
   0.225 s/token and `next_adaptive_batch_size(2.0, ...)` returns **8**, not
   13 — a hold of **0.84 s at the cold cost, 42% of the SLO**. The same bias
   applies to the warm-seed counterfactual in 4.2: 67 tokens, **7.06 s, still
   3.5x the SLO**. The conclusion is unchanged; the real margin is larger than
   4.3 suggests.
2. **The B11 sanity ceiling is `batch_size`.** Once adaptive batching is
   active, `CRYPTO_TAPE_RECONCILER_BATCH_SIZE` becomes a maximum only, and
   `min(8, 5) = 5`. **On today's constants the calibrated first batch is
   therefore 5 tokens — identical to today's fixed behaviour.** The loosening
   in 4.4 is *latent*: it is realised only if Gate 6 also raises that ceiling,
   which is a separate decision needing its own evidence and was deliberately
   not taken here.

## 5. Caveats — part of the derivation, not footnotes

* **n = 1 for the cold case.** The cold tail is **not bounded by this data**. A
  worse cold start is possible. The 1.42x margin exists for that reason and is
  not a proof of anything. A second cold observation is the cheapest available
  improvement to this constant.
* **The margin is a judgement, not a measurement.** 1.42x was chosen to put the
  cold-case hold under 70% of the SLO on the 4.3 arithmetic. No distributional
  claim is attached to it.
* **`rows_committed` is a row count summed across three tables**, not a token
  count — see §3. The denominator used here is `batch_size`, a genuine token
  count.
* **Pass 4 overshot its 30 s deadline by 37%** (`duration_ms = 41035`) on a
  4,038 ms `batch_lock_wait_ms_max`. This is the documented limit that
  `max_duration_seconds` **cannot interrupt a statement already blocked inside
  SQLite**; the deadline is evaluated only *between* batches. It is **not a
  calibration input** — the hold itself was an ordinary 98 ms, and the
  overshoot was lock wait, not write time — but it is part of the record, and
  it is the class of event precondition 1 asks to be *observed* rather than
  modelled.
* **Passes 6–8 returned `ok` on an identical `tokens_considered = 590`.** The
  historical backlog has drained to steady state, so the warm cluster is a
  steady-state measurement. A backlog surge is a different population and this
  constant was not measured against one.
* **Eight passes, one host, one session, one branch.** Not portable. If the
  host changes, re-derive — the same rule `CRYPTO_TAPE_RECONCILER_BATCH_SIZE`
  already carries.

## 6. Rejection behaviour, verified live on EVO

The constant has a validated domain on both sides. Each was exercised on EVO
during the session and each is pinned by a named test in
`tests/test_calibration_gate3_001.py` that fails if the check is removed.

| input | observed result | mechanism |
|-------|-----------------|-----------|
| `0` | `status=invalid_initial_per_token_cost_seconds` | lower bound, `run_scheduled_reconciliation` |
| `-1` | `status=invalid_initial_per_token_cost_seconds` | same lower bound |
| `abc` | pydantic `ValidationError` **before the pass runs** | `Settings` field type `float \| None` |
| `999` | `status=unsafe_host_cost` | upper bound: the conservative estimate exceeds the write-time budget, so even a 1-token batch is unsafe and the pass refuses rather than guessing |

The upper bound is not a separate constant. It falls out of
`next_adaptive_batch_size`: when `conservative_estimate_seconds >
time_budget_seconds` the function returns 0, and a batch size below 1 is a
terminal `unsafe_host_cost`, never a silent floor at 1.

## 7. What this gate did NOT change

No threshold, ladder, budget, batch size, SLO or finalize bound was altered.
Adaptive batching is not enabled anywhere that takes effect. The telemetry
sink, `_lock_tally` and migration `0029` were not touched. `.env.example`
carries the value **commented out**, which is the same state the file was in
before, with a value and a pointer added.

## 8. What Gate 6 must still decide

1. Whether to uncomment **both**
   `CRYPTO_TAPE_RECONCILER_INITIAL_PER_TOKEN_COST_SECONDS` **and**
   `CRYPTO_TAPE_RECONCILER_TIME_BUDGET_SECONDS` — adaptive batching activates
   only when both are set.
2. Whether to raise the B11 ceiling (`CRYPTO_TAPE_RECONCILER_BATCH_SIZE`), on
   its own evidence. Left at 5, the calibrated seed changes nothing (§4.5).
3. The remaining preconditions 1, 2 and 4 in the runbook, which this gate did
   not address.
