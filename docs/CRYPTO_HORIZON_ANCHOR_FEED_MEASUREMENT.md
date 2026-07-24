# CRYPTO-HORIZON-ANCHOR-FEED-MEASUREMENT-001 — exact-cycle anchor feed (2026-07-24)

Measurement-only. An explicitly gated, isolated MarketOps hook materializes
canonical `CryptoTokenBirthEvent` anchors from **exactly** the raw tokens
newly persisted by the same natural MarketOps crypto discovery cycle — making
the candidate-readiness measurement operational without a second scan, any
provider call, a timer, a daemon, cohort creation, arming, or observation.
It reuses the existing provider-free lifecycle-tape logic (no second
lifecycle-anchor implementation) and is never a trading surface of any kind
(`docs/SAFETY_BOUNDARIES.md`).

## Why

The seven-day readiness checkpoint proved the evaluator flawless but starved:
anchors were produced only by manual tape sessions, so 1,751 cycles evaluated
a frozen population (catch rate vacuous). ANCHOR-FEED-CANARY-001 proved one
manual exact-cycle tape pass produces live pairs within minutes; the two
CANARY-004 sourcing attempts showed compliant cycles arrive too rarely
(~8%/cycle) for manual one-shot sourcing. This milestone closes the loop:
every natural cycle feeds its own tokens to the anchor lane, continuously and
provider-free.

## Flow

```text
natural MarketOps crypto scan (unchanged, still exactly one per cycle)
→ crypto-stage persistence commits (unchanged)
→ 4a. anchor-feed hook (isolated session, exact cycle, provider-free)
→ 4b. candidate-readiness evaluation (unchanged; sees the new anchors
      in the same cycle — fresh query after the hook's commit)
→ append-only readiness record (unchanged, one per cycle)
```

## Exact-cycle contract (`CryptoLifecycleTapeRecorder.record_discovery_run`)

`record_discovery_run(session, crypto_run_id, token_ids, *, dry_run)`:

1. Exact canonical token ids only — no symbol/partial matching, no
   freshest-first fallback, no substitution (`--hours` selection is not used).
2. Exact originating discovery run: every id must have been FIRST persisted
   inside that run's own start/finish window (`membership_mismatch` otherwise).
3. Input (persistence) order preserved.
4. Validation is fail-closed and runs BEFORE any write: `unknown_run`,
   `invalid_token`, `membership_mismatch`, `skipped_cap`, `no_new_tokens` all
   persist nothing — no partial membership ever.
5. Existing anchors deduplicate idempotently (replay creates zero duplicates).
6. One bounded transaction (single terminal commit; same error-row semantics
   as the manual tape).
7. Zero provider access — structurally (see below).
8. Existing lifecycle-anchor semantics reused unchanged: the same
   `_assemble_pass` the manual `run_once` now delegates to (`_universe`
   selection stays exclusive to the manual path).
9. Hard cap `MAX_ANCHOR_FEED_TOKENS_PER_CYCLE = 40` — an internal safety
   constant, not an environment knob. An over-cap cycle fails closed
   (`skipped_cap`, zero anchors, loud, never truncated) and never fails
   MarketOps.

Token-level provenance stays in the lifecycle tables (`run_id` → tape run →
`config.source_crypto_run_id`); the MarketOps summary carries counts only.

## MarketOps hook (`_materialize_cycle_anchors`)

Gated by **`MARKETOPS_INCLUDE_CRYPTO_TAPE_ANCHOR_FEED`** (default `false`;
off = complete no-op — dark deployment changes nothing). When on:

- runs at most once per cycle, only after the crypto stage persisted and
  committed, and before the readiness evaluation (4a in the stage order);
- receives the exact crypto run and derives the exact newly persisted token
  ids (`new_token_ids_for_run` — first_seen within the run's own window);
- opens a short-lived **isolated** session (never the shared cycle session;
  shared-session transaction boundaries untouched), closes it
  deterministically;
- performs no provider or filesystem I/O while holding uncommitted writes;
- reuses the canonical tape lock convention exactly
  (`DB_LOCKED_MAX_ATTEMPTS=3`, `DB_LOCKED_RETRY_SECONDS=3.0`,
  `_is_db_locked`) — bounded, idempotent on retry, no new ladder;
- cannot fail the cycle (swallow-all, even under `fail_fast`); failures are
  recorded as `summary["anchor_feed"].status="error"`;
- creates no cohort, no observation, installs no unit, starts no scan, and
  does not alter the readiness verdict logic in any way.

Summary shape (bounded, counts only): `status, source_crypto_run_id,
tokens_received, tokens_validated, anchors_attempted, anchors_created,
anchors_existing, complete_anchors, incomplete_anchors, skipped_cap,
external_calls (always 0), duration_ms, error`.

## Provider-free proof

Structural: `crypto_tape.py` imports only stdlib/SQLAlchemy/`app.config`/
`app.models` (AST-asserted in tests — no `httpx`, adapters, provider
registry/policy, or provider module reachable); the hook body imports only
`app.db` + `crypto_tape`. The lazy `_completeness_reason` import pulls
`crypto_horizon`, which is itself persisted-row-only. Runtime: a test blocks
every socket primitive during a full hook cycle — the hook succeeds with
`external_calls=0`. Any provider attempt is a hard test failure and an
activation blocker.

## CLI exact-run mode (validation aid)

`crypto-tape-run-once --source-crypto-run-id N [--dry-run] [--confirm]`:
exact run membership only (rejects `--limit`/`--hours`); previews by default
and persists **only** with `--confirm`; explicit rejections for unknown runs,
no-new-token runs, and over-cap runs. The classic `--hours/--limit` behavior
is unchanged.

## Measurement epochs

```text
Epoch 1  2026-07-16T19:56:26Z → ANCHOR-FEED-CANARY-001: feed inactive
         (readiness records expired-only; catch rate vacuous).
Epoch 2  ANCHOR-FEED-CANARY-001 (2026-07-24 ~05:32Z): one manual
         provider-free tape pass; King+Octen live pair; expired unarmed.
Epoch 3  CANARY-004 sourcing attempts 2+3: no qualifying cycle; zero
         mutations; passive measurement uncontaminated.
Epoch 4  THIS MILESTONE, from activation (timestamp recorded in
         DEPLOYMENT_REPORT_EVO_X2.md): exact-cycle anchor feed active on
         every natural cycle.
```

The July-30 report must analyze each epoch separately; Epoch 1's
expired-only records must never be combined with Epoch 4 into one
undifferentiated catch rate. During Epoch 4: no extra scans, no manual tape
runs, no cohort creation, no arming, no observation triggering, no cadence
change, no completeness/anchor change, and readiness is never interpreted as
market opportunity. If a live pair appears, preserve the evidence and
request separate CANARY-004 approval — nothing is created or armed
automatically.

## Storage and lock posture

No new table, no migration (Alembic stays `0027`). Anchor rows are the same
lifecycle rows the manual tape already writes (~a few rows per new-token
cycle; most cycles add zero). The DB's pre-existing size-gate exceedance and
the R1–R5 contention profile are unchanged by design: short isolated
transaction, bounded token cap, canonical bounded lock retry, no WAL/pragma/
schedule/transaction-boundary change. Database bytes, free space, and lock
events are captured at activation and tracked through July 30.

## Validation

Independent reviews surfaced two confirmed HIGH findings, both fixed with
regression tests before merge: (1) the completeness classifier's lazy import
transitively loaded the DexScreener adapter + `httpx` into the anchor-feed
path — `_completeness_reason`'s canonical home moved into the provider-free
tape module (re-exported by `crypto_horizon` unchanged) and a clean-subprocess
test now proves `record_discovery_run` loads no `app.adapters.*`/`httpx`/
`crypto_horizon` module; (2) a crypto spike alert flushed-but-uncommitted on
the shared cycle session would have held the SQLite write lock against the
hook's isolated session in the same coroutine (self-contention, ~96 s stall,
guaranteed hook failure on the busiest cycles) — the hook branch now
checkpoint-commits the shared session first (flag-gated, so the disabled path
is byte-identical; consistent with the documented "checkpoint-committed, not
atomic" cycle contract), proven by a real-file-DB spike-cycle regression.
Documented review notes: membership is a run-window proxy (MarketOps runs are
serialized by the active-run guard, so attribution is unambiguous in
production); the CLI exact-run mode intentionally exits nonzero for a
no-new-token cycle (Phase-6 explicit rejection); replay idempotency applies
to anchors (snapshots/outcomes append per pass, identical to the manual
tape's semantics).

`tests/test_crypto_anchor_feed_measurement_001.py` — 27 tests covering the
40-item milestone matrix (flag-off no-op, defaults, ordering, once-per-cycle,
one-scan, exact membership/ids/order, no-fallback, unknown/empty/over-cap
fail-closed, dedup/idempotent replay, mixed existing, complete + honest
incomplete anchors, structural + runtime provider impossibility, no
cohort/observation/unit, one bounded transaction, failure isolation under
both fail_fast modes, no migration, disposable-DB lock contention, CLI
exact-run preview/confirm/rejections, classic CLI parity, safety-grep pins,
bounded-summary shape, overhead benchmark). Full suite 1,907 passed; safety
grep + AST audits clean; `git diff --check` clean. Three independent reviews
(correctness/duplication, security/provider-impossibility, transaction
safety/contract) recorded in the commit history.
