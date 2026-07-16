# CRYPTO-HORIZON-CANDIDATE-READINESS-001 — Activation Sanity Check (2026-07-16)

```text
ACTIVATION SANITY CHECK: PASS
SEVEN-DAY CHECKPOINT: NOT YET DUE
RECOMMENDATION: CONTINUE MEASUREMENT UNCHANGED
```

Analysis and documentation only. No cohort was created or armed; no scan, provider
call, observation, timer, or daemon was produced.

## What this is (and is not)

The readiness hook was activated at cycle **3097 on 2026-07-16T19:56Z**. This artifact
was recorded at **2026-07-16T20:02Z** — an **activation sanity check** confirming the
hook behaves correctly immediately after activation. It is **not** a seven-day
checkpoint, seven-day sample, or seven-day measurement, and nothing here is presented
as one. The effective observation window is **~6.5 minutes / 2 MarketOps cycles**. The
substantive multi-day catch-rate analysis is deferred to the actual checkpoints below.

Standing checkpoint dates (unchanged):
- **Seven-day checkpoint:** 2026-07-23 after 19:56 UTC / 12:56 PM America/Los_Angeles.
- **Fourteen-day checkpoint:** 2026-07-30 after 19:56 UTC / 12:56 PM America/Los_Angeles.

## Baseline

- Commit `ddf9c40` at activation (Mac = origin = EVO-X2); Alembic `0027`; tracked-clean
  apart from the known untracked files.
- Flag `MARKETOPS_INCLUDE_CANDIDATE_READINESS=true` (active). The only cadence is the
  existing MarketOps oneshot timer (~364 s median gap). **No auxiliary readiness timer,
  unit, daemon, or poller exists** (verified via `systemctl --user` and the unit
  directory; the only long-running host `sleep` processes are unrelated system daemons
  — CUPS `cups-browsed` and a containerized `backup.sh`).
- Readiness JSONL `~/crypto-horizon-readiness/readiness.jsonl`: 740 bytes, 2 lines,
  sha `da6f1511…`.

## Exact effective observation window

- UTC: 2026-07-16T19:56:26Z → 2026-07-16T20:02:30Z.
- America/Los_Angeles: 2026-07-16T12:56:26 → 2026-07-16T13:02:30 (−07:00).
- MarketOps cycles with the flag on: **2** (3097, 3098). Expected readiness records:
  2. Actual: **2**. Missing: 0. Duplicate: 0. Invalid: 0.

## Record integrity

Every line valid JSON and schema-valid; cycle IDs present (`3097`, `3098`); one record
per cycle; timestamps strictly ordered; `external_calls=0` on every record; secret-free
(no api_key/secret/private/authorization/password/raw_payload); no malformed canonical
token ID persisted (both records `candidate_pair=null`); append-only (2 lines, growth
consistent with cadence); no prior line modified.

## State distribution (2 cycles)

| State | Count | % |
|---|---|---|
| `expired` | 2 | 100% |
| all other states | 0 | 0% |

Distinct candidate pairs: 0. Distinct ready moments: 0. Ready moments by day (UTC or
LA): none. Max/median/min safe arm slack: n/a (no ready moment). Median MarketOps
cadence gap: **364 s** (~6.07 min). Both `expired` results are correct: 508 candidates
/ 198 complete / **197 overlapping pairs all closed** (pre-activation births), 0
feasible-15m, 0 usable — historical overlap only, never labelled ready.

## Ready moments

**None.** No `pair_ready_for_manual_preparation` or `shared_due_now_ready` record
exists in the window, so no CANARY-004 authorization is requested.

## Usable-moment presence during the observed interval

**Zero usable historical moments existed to catch** in the ~6.5-minute window: 0 births
were first-persisted during the window, and 0 complete tokens had an open 15m window at
either evaluation instant. So recorded usable = 0, missed = 0. This is the expected
consequence of the upstream discovery lag (SHARED-CANDIDATE-FEASIBILITY-001: ~85 min
median persistence lag) over so short an interval — *the source surfaced no fresh
complete token in time* — **not** a readiness-hook failure. A real catch-rate estimate
requires the actual observation period; none is asserted here.

## MarketOps isolation

Across both activated cycles: completed 2, successful 2, failed 0; crypto-stage
failures 0; readiness evaluator failures 0; cycles with `candidate_readiness_error` 0;
no evaluator error changed a cycle result; second scans **0** (exactly 1 crypto watcher
run per cycle); provider behavior unchanged; cohorts/observations created by readiness
**0** (cohorts still 1–6: 4:1/0, 5:2/6, 6:1/4); horizon units installed **0**; new
recurring timer/daemon **0**; Alembic `0027`.

Readiness-attributable: provider calls **0**, second scans **0**, cohort writes **0**,
observation writes **0**, units **0**, MarketOps failures **0**. Global SolanaTracker
counters moved +15/cycle (today/month; rolling_24h unchanged) — the normal crypto
stage's `PER_RUN_LOOKUP_LIMIT=15` via `ENABLE_CRYPTO_RISK_ENGINE`, confirmed by the
run-scoped `candidate_readiness.external_calls=0`, not the hook.

## JSONL growth estimate

~370 bytes/record; at the observed ~364 s cadence ≈ 237 cycles/day ≈ **~86 KB/day**.
Projected: **~1.17 MB at 14 days**, **~2.51 MB at 30 days**. Bounded, append-only,
negligible.

## Verdict

**ACTIVATION SANITY CHECK: PASS.** Expected record cadence (1/cycle); zero duplicate
evaluations; zero readiness-attributable provider calls; zero second scans; zero
cohorts/observations/units; no MarketOps failure caused by readiness; no secret
leakage; bounded JSONL growth; valid history aggregation; accurate ready-state
classification. The hook operates safely and reliably immediately after activation.

**SEVEN-DAY CHECKPOINT: NOT YET DUE** (real date 2026-07-23).

**RECOMMENDATION: CONTINUE MEASUREMENT UNCHANGED.** Keep the hook enabled on the
existing MarketOps cadence. No engineering or operational action is authorized before
the actual seven-day checkpoint unless the hook causes an operational failure. Rollback
(only if a safety failure requires it) = set `MARKETOPS_INCLUDE_CANDIDATE_READINESS=false`
in EVO-X2 `.env` (no-op next cycle; no code change).
