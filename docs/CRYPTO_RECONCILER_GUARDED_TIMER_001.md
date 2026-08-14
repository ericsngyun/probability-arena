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

> **HARD PRECONDITION for any future re-calibration: no batch may be partial.**
> The identity `batches_committed x batch_size == tokens_considered` **must be
> checked, and must hold on every pass in the sample.** It is not a note about
> this session; it is the difference between a valid and an invalid derivation.
>
> The numerator is a **max over batches**; the denominator is a **fixed** token
> count. If any batch was short, the worst batch's true per-token cost is
> `write_hold_ms_max / (tokens in THAT batch)` — *larger* than the arithmetic
> reports. **The failure direction is an UNDER-estimate of per-token cost**,
> which inverts into a seed that is too small, a first batch that is too large,
> and a write-lock hold longer than the SLO was ever checked against. That is
> the unsafe direction, and nothing else in the record flags it: every other
> field on such a pass still looks healthy.
>
> A short batch is ordinary, not exotic. Any pass whose `tokens_considered` is
> not an exact multiple of `batch_size` has one, and a **deadline-stopped**
> session is the standard way to get there. Gate 3's eight passes all satisfied
> the identity (§2.3), which is why the arithmetic above is valid. A session
> that cannot assert the same has not derived a constant.

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

### 4.3 The chosen value, and the first batch the code actually issues

`2.0 / 0.15 ≈ 13` is the **budget** arithmetic, and it is not a batch size the
shipped code ever issues. Two mechanisms already in the code sit between the
seed and the first batch, and both are applied before a single token is
written (§4.5 names them). The derivation is therefore stated end-to-end here,
so the first number a reader meets is the number that governs.

```text
samples (n=8, ms/token)  14.8  17.8  18.0  18.8  18.8  19.6  19.8  105.4
  median 18.8 | warm-cluster max 19.8 | cold-start 105.4

initial_per_token_cost_seconds = 0.15
  = worst observed (0.1054 s/token) x 1.423

AS SHIPPED, end to end  (each line evaluated against the shipped code)
  bias_multiplier 1.5                  -> conservative estimate 0.225 s/token
  next_adaptive_batch_size(2.0, 0.15)  -> 8 tokens    (NOT 2.0/0.15 = 13)
      at cold 105.4 ms/token -> 0.843 s   (42.2% of the 2.0 s SLO)
  B11 ceiling CRYPTO_TAPE_RECONCILER_BATCH_SIZE = 5
  min(8, 5)                            -> 5 tokens    <- THE FIRST BATCH
      at cold 105.4 ms/token -> 0.527 s   (26.3% of the 2.0 s SLO)
      at warm  19.8 ms/token -> 0.099 s
```

The estimator is free to grow the batch from there on real measurements — this
is a seed, not a cap.

### 4.4 On today's constants the effect is NO CHANGE; the loosening is latent

**The as-shipped first batch is 5 tokens — exactly what ships today.** Setting
this constant, against today's other constants, changes the reconciler's
behaviour not at all. That is the conclusion of §4.3, produced by the two
clamps §4.5 describes; §4.5 is the mechanism behind this paragraph, not a
correction of it.

Two readings are wrong, in opposite directions, and both are worth naming:

* **It is not a tightening.** Neither intermediate figure is below current
  behaviour: the budget arithmetic gives 13 tokens and the biased sizer gives
  8, both **larger** than the fixed `CRYPTO_TAPE_RECONCILER_BATCH_SIZE = 5`.
  Nothing here constrains the first batch, so there is no throughput anywhere
  that needs "recovering".
* **It is not a loosening either — not yet.** The loosening is **latent**. It
  is realised only if the B11 ceiling is raised above 5, at which point the
  first batch becomes 8 tokens (0.843 s, 42.2% of the SLO at the cold cost).
  Raising that ceiling is a separate Gate 6 decision and it carries a hard
  precondition — §4.6.

### 4.5 The two clamps, in the order the code applies them

Neither clamp was changed by this gate. They are recorded because §4.3's
end-to-end figure is only readable if both are named.

1. **The seed is biased HIGH before use.** `AdaptiveBatchCostEstimate` applies
   `bias_multiplier = 1.5`, so a 0.15 seed yields a conservative estimate of
   0.225 s/token and `next_adaptive_batch_size(2.0, ...)` returns **8** — a
   hold of **0.843 s at the cold cost, 42.2% of the SLO**. The same bias
   applies to the warm-seed counterfactual in 4.2: a conservative 0.0297
   s/token, **67 tokens, 7.06 s, still 3.5x the SLO**. The bias does not
   rescue a warm seed; §4.2's conclusion holds after the clamp as well as
   before it.
2. **The B11 sanity ceiling is `batch_size`.** Once adaptive batching is
   active, `CRYPTO_TAPE_RECONCILER_BATCH_SIZE` becomes a maximum only, and
   `min(8, 5) = 5` — the shipped first batch, and the reason §4.4's effect is
   no change at all.

### 4.6 HARD PRECONDITION for Gate 6 — raising the B11 ceiling re-opens this derivation

**The calibration risk did not resolve here; it transferred.** `0.15` is inert
today only because `min(8, 5) = 5`. Every margin in this document is a
statement about a **5-token** batch and about nothing else.

> **Precondition (Gate 6).** Raising `CRYPTO_TAPE_RECONCILER_BATCH_SIZE` above
> **5** requires `initial_per_token_cost_seconds` to be **re-derived** first.
> The ceiling is not an independent throughput knob.

It is a precondition and not a note because the cold tail is bounded by
**n = 1** (§5): the only thing between an unmeasured worse cold start and an
SLO breach is the margin, and the margin is sufficient only at 5 tokens. At a
ceiling of 8 the cold-cost hold is already 42.2% of the SLO on the single cold
observation that exists — a cold start twice as slow as the one measured puts
8 tokens at **84%** and 13 tokens **past** the SLO. Whichever milestone raises
the ceiling inherits the derivation, not just the ceiling.

## 5. Caveats — part of the derivation, not footnotes

* **n = 1 for the cold case.** The cold tail is **not bounded by this data**. A
  worse cold start is possible. The 1.42x margin exists for that reason and is
  not a proof of anything. A second cold observation is the cheapest available
  improvement to this constant.
* **The margin is a judgement, not a measurement.** It is exactly
  `0.15 / 0.1054 = 1.423x` over the single worst observation, and no
  distributional claim is attached to it. **What it buys is stated against the
  as-shipped batch, not against the budget arithmetic:** at the shipped 5-token
  first batch the cold-case hold is **26.3% of the SLO**, and at an 8-token
  batch (the B11 ceiling raised — §4.6) **42.2%**. An earlier form of this
  caveat anchored on "under 70% of the SLO" from the uncorrected `2.0/0.15 ≈
  13`-token figure; that batch size is never issued and the anchor is
  superseded by §4.3.
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
sink, `_lock_tally`'s behaviour (its scoping, its predicate and
`LOCK_EVENT_WRITERS`) and migration `0029` were not changed — the only later
edit to `_lock_tally` is to its docstring, correcting a misattributed heap
measurement (§9 and the runbook's growth section). `.env.example`
carries the value **commented out**, which is the same state the file was in
before, with a value and a pointer added.

## 8. What Gate 6 must still decide

1. Whether to uncomment **both**
   `CRYPTO_TAPE_RECONCILER_INITIAL_PER_TOKEN_COST_SECONDS` **and**
   `CRYPTO_TAPE_RECONCILER_TIME_BUDGET_SECONDS` — adaptive batching activates
   only when both are set.
2. Whether to raise the B11 ceiling (`CRYPTO_TAPE_RECONCILER_BATCH_SIZE`), on
   its own evidence. Left at 5, the calibrated seed changes nothing (§4.4).
   **This one is not free-standing: raising it above 5 requires re-deriving
   `initial_per_token_cost_seconds` first — the hard precondition in §4.6.**
   Any such re-derivation is itself subject to the no-partial-batch
   precondition in §3.
3. The remaining preconditions 1, 2 and 4 in the runbook, which this gate did
   not address.

## 9. Named follow-ups — recorded here, NOT built by this gate

1. **`_lock_tally` has no time window, and the `> 6` stop condition is
   therefore unbounded in time.** The Gate 4 scoping fixed *which* population
   is counted (`LOCK_EVENT_WRITERS`), not *over what interval*. `lock_events`
   still counts every matching line in a file that nothing rotates before
   `001E`, so it is **cumulative and monotonic for the in-scope writers too**:
   once it crosses, it stays crossed for the life of the file.

   Measured: **90 days of hourly `tick_aggregation` at a benign 0.5% genuine
   contention rate produces 11 in-scope events — past the `> 6` stop
   condition** with nothing wrong. The fix is a `--since` argument, or a
   `lock_events_last_24h` figure reported beside the cumulative count. This is
   load-bearing rather than cosmetic: `> 6` governs exactly the attended-session
   stop conditions the calibration work in this document depends on. Owned by
   `001E` alongside rotation; deliberately not built here.

2. **`run_source` forgery is inert only because nothing reads it back.** The
   field is derived from systemd's `INVOCATION_ID`, but a caller may pass it
   straight to `emit_writer_pass` and `export INVOCATION_ID=anything` satisfies
   the derivation. It is harmless today for one reason only — **grep-verified
   across `app/` and `scripts/`: no consumer reads it back from the sink.** The
   only reads are on the emit path itself (the derivation in
   `app/telemetry/writer_pass.py` and the enum check in
   `app/telemetry/schema.py`); the one hit in `app/services/crypto_tape.py` is
   a comment. Whichever
   milestone adds the first reader inherits the enforcement question and must
   answer it before branching on the field. A note for `001E`.
