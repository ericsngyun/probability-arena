# MARKET-MICROSTRUCTURE-ROW-BUILDER-001

**Status: QUALIFIED on the VALIDATION tape. Tranche NOT authorised. M0/M1 NOT run.**

Given the already-qualified rotating panel at tick `t`, deterministically emit
the 1 Hz M0/M1 feature rows and future-mid labels the preregistration expects.

## Structural separation, not a convention

`features.py` and `labels.py` **cannot import each other**, and a test asserts
the import graph. `rows.py` is the only place they meet, and it meets them in
two passes:

```text
pass 1   forward replay -> M0/M1 at each sample   (cannot see the future,
                                                   the replay has not reached it)
pass 2   attach labels from the completed published-mid grid
```

That ordering is the reason a future price cannot enter a feature. A metamorphic
test appends 5,000 post-`t` events and the feature block is unchanged.

## Results on the live VALIDATION tape

Session `s-20260823T231228Z-b96fc1bed4f1`, 350,391 records, `dataset_role=VALIDATION`.

| | |
|---|---|
| row opportunities | 28,800 (1,200 samples × 24 subscribed) |
| **rows emitted** | **10,800** = 12 panel × 900 post-warmup seconds |
| skips | 7,200 warmup (24 × 300) + 10,800 not-in-panel (12 × 900) |
| dispatch errors | **0** |
| clusters / panel ticks / TTE bins | 20 / 3 / `approaching`, `near_event` |

Arithmetic reconciles exactly: 10,800 + 18,000 = 28,800.

**M0 completeness: 1.0 on all thirteen columns.**

| horizon | coverage |
|---|---|
| 1 s | 0.9989 |
| 5 s | 0.9944 |
| 30 s (primary) | 0.9667 |
| 300 s | 0.6667 |

## Two defects found, both before any confirmation tape existed

### 1. The mid grid was selection-dependent — a biasing defect

300 s coverage first came back at **44.4%**, well below what session length
explains. The mid grid was populated only for markets **in the governing
panel**, so a 300 s label needed the same market to still be selected 300 s
later — and **rotation is also 300 s**. Label availability was therefore
correlated with panel persistence, which is correlated with sustained activity.
The target would have been preferentially available for markets that stayed
busy, and that bias would have been baked into all 20 confirmation sessions.

A future mid is a price the venue published. Whether we chose to emit a research
row for that market at that instant has nothing to do with whether its price
moved. The grid now covers **every subscribed market with a publishable book**;
rows stay gated on panel membership. Observability and exposure are different
quantities.

**Coverage went 44.4% → 66.7%**, which is now exactly the session-length limit
(rows in the final 300 s of a 900 s row window cannot have a 300 s label) with
no selection component. `realized_vol_5s`/`_30s` also went 0.9963 → 1.0.

### 2. `realized_vol_1s` is undefined by construction — UNRESOLVED, needs a decision

The fabric defines `realized_vol_Δ` as the **stdev of 1 s mid changes** over
window Δ. On a 1 Hz sampling grid a 1 s window contains **one** mid sample, so
there are zero differences and a stdev needs at least two. Measured
completeness is **0.0000** — the column is never computable, at any sample, in
any session.

This is a defect in the frozen spec, not in the implementation, and it is not
cosmetic: **a column that is 100% missing destroys a complete-case fit
entirely.** If the M1 fit drops rows with any missing feature, it drops *every*
row, and M1 cannot be estimated at all.

Recommended resolution — **Eric's call, because it changes M1's frozen
membership**: drop `realized_vol_1s` from the flow set, leaving
`realized_vol_5s` and `realized_vol_30s`. The justification is purely
mechanical and visible without any alpha (`stdev` of fewer than two
differences), which is why it is safe to decide now and would be
outcome-shopping later. **Not changed unilaterally.**

## Two implementation traps worth recording

**Segment file ordering.** `segment=X.r0001/events.jsonl.gz` sorts **before**
`segment=X/events.jsonl.gz`, because `.` (0x2E) < `/` (0x2F). Sorting file
paths replays a later segment first and rejects every delta for want of a base.
Ordering is now by manifest `opened_at`, with a regression test. (The profile's
`measure_tape` globs *directories* and is unaffected.)

**The dispatch adapter took `sid` from `normalized_event`.** Live tape echoes it,
so this worked — right up until a fixture did not carry the echo and every
delta was refused as belonging to "subscription None". `sid`, `seq` and the
generation now come from the archive record's own columns.

## Mutation campaign — 11 of 11 killed

| mutation | result |
|---|---|
| future order-book frame in M1 | killed |
| future trade (lag removed) | killed |
| `ticker` as an order-book source | killed |
| rows emitted before the first panel | killed |
| panel rotation ignored | killed |
| 1 Hz alignment shifted | killed |
| missing label becomes zero | killed |
| midpoint forward-filled | killed |
| `NOT_PROVIDED` collapses to `0.0` | killed **(only after the suite was hardened — see below)** |
| rows stamped `CONFIRMATION` | killed |
| mid grid re-gated on the panel | killed |

**One mutation initially survived, and that is the most useful result here.**
`NOT_PROVIDED = 0.0` passed all 31 tests, because the test asserted
`m[k] is F.NOT_PROVIDED` — comparing the absent value against *the very
constant that defines absence*. Redefining the constant kept the test green
while every unobserved field silently became a real zero. That is the
self-validation trap, and it was caught by mutation rather than by review. The
suite now asserts the literal `None`, and the mutation dies.

## Mechanical power projection — not a result about alpha

Scaling the measured validation rates to the planned 3 h sessions:

| quantity | projection | floor | margin |
|---|---:|---:|---:|
| rows / session | 126,000 | — | — |
| market-blocks / session | 420 | — | — |
| **market-blocks over 20 sessions** | **8,400** | 4,000 | **2.10×** |
| **clusters (conservative, no rotation)** | **240** | 150 | **1.60×** |
| clusters (at the validation rotation rate) | 480 | 150 | 3.20× |
| 300 s label coverage | 97.1% | — | — |
| 30 s label coverage | 99.7% | — | — |

**Both mechanical floors are met with margin.** The third floor — **≥3 sessions
contributing rows in every TTE bin** — is *not* mechanical: it depends on how
sessions are anchored. The validation session touched only **2 of 5** bins
(`approaching`, `near_event`), because it was anchored 2–3 h before tip-off.
Covering `far`, `live_event` and `late_resolution` requires deliberate
anchoring across the event lifecycle, and the tranche schedule must be built to
do that rather than assuming it falls out.

Nothing above asks whether any feature predicts anything. The question answered
is only: *did we build the intended dataset?*
