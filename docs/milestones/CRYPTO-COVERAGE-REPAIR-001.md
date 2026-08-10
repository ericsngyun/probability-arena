# CRYPTO-COVERAGE-REPAIR-001 — survival-horizon coverage repair

Status: **Stage 1 implemented on this branch. NOT MERGED, NOT DEPLOYED anywhere (dark or otherwise) — `worktree/crypto-coverage-repair` has not landed on `main` and nothing is installed on EVO-X2. The write-LOCK-HOLD defect that previously blocked activation is fixed and measured (below); a THIRD review (NEW-H2) found the write-lock-hold reduction does NOT proportionally reduce a competing writer's worst-case WAIT, which instead tracks this pass's wall time (bounded at ~67% of the 30s busy_timeout, not a small fraction of it) — see "Write-lock defect" below for both metrics. Activation is still gated on the review/merge/deploy sequence. Stage 2 designed, not implemented.**
Branch: `worktree/crypto-coverage-repair`
Measured against EVO-X2 production DB on 2026-08-10 (the original finding, below); the write-lock fix itself has only been measured in the test suite (in-memory/file-backed SQLite) plus a separate reviewer's pinned-export benchmark (see "Write-lock defect" below), not yet re-measured on EVO-X2 itself.

## The finding

Survival horizons never mature. Not "24h is thin" — **nothing matures at all**.

| | value |
|---|---|
| birth events | 6,846 |
| survival outcome rows | 6,846 (100% present) |
| `survived_24h` non-null | **0** |
| `survived_6h` non-null | 20 |
| `survived_1h` non-null | 180 |
| `survived_15m` non-null | 192 |

The 192/180/20 are not a working pipeline. They are residue from manual
`crypto-tape-run-once` sessions in July, and the ticks that produced them have
since been pruned — their `details.tick_id` values reference rows that no
longer exist. Nothing has matured since.

## Two coverage metrics, deliberately separated

The defect proves why one number cannot describe this. A token can hold a real
tick inside the canonical window (**observation-covered**) and still carry no
label (**reconciliation-uncovered**), because reconciliation is never *selected*
to run on it.

Measured with `compute_survival`'s exact predicate (anchor =
`birth.first_evidence_at`; tick strictly later than the anchor and within
`±HORIZON_TOLERANCE × horizon`; nearest such tick must carry `liquidity_usd`;
`initial_liquidity_usd` must be truthy — i.e. the first tick's liquidity):

| horizon | due | observation-covered | sufficient evidence | label populated | **recoverable now** |
|---|---|---|---|---|---|
| 15m | 6,844 | 2,848 (41.6%) | 1,135 | 0 | **1,135** |
| 1h | 6,838 | 2,792 (40.8%) | 1,136 | 0 | **1,136** |
| 6h | 6,779 | 405 (6.0%) | 219 | 0 | **219** |
| 24h | 6,589 | 97 (1.5%) | 56 | 0 | **56** |

**Reconciliation coverage is 0.0000 at every horizon.** 2,546 labels are sitting
in evidence we already hold, and they expire.

Observation dies fast — birth → last tick: p50 **83 min**, p75 120, p90 351.

## Four causes, each verified from deployed code

1. **No persistent observation universe.** `crypto_scout._scan_once_unguarded`
   draws only from `fetch_latest_token_profiles()` and
   `fetch_latest_boosted_tokens()`. Ticks stop when DexScreener stops promoting
   the token. This is the upstream cause of the 6h/24h observation collapse.

2. **Recency-anchored selection.** `crypto_tape._universe` (lines 194-203)
   selects `first_seen_at >= cutoff` ordered newest-first with a limit. The
   repo's own coverage instrument agrees: `recent_first_starves_old_cohorts=True`
   and a due-token omission rate of **1.0** at both 6h and 24h.

3. **The windowed reconciler is never scheduled — the load-bearing cause.**
   `run_once` has exactly three callers, all manual: CLI `crypto-tape-run-once`
   (`cli.py:2825`) and the tmux session wrapper (`crypto_tape.py:1237,1273`).
   Production MarketOps (`marketops.py:885` → `:1097`) calls only
   `record_discovery_run`, which validates that every token was **first
   persisted by the originating discovery run**. By construction it therefore
   sees each token exactly once, at age ~0, when no horizon is due. That is why
   *every* horizon reads zero, not just 24h. Timer inventory on EVO-X2 confirms
   it: 10 user timers, none for the tape.

4. **Evidence is perishable.** `crypto_retention_days = 7` prunes
   `crypto_price_ticks`; live ticks span exactly 2026-08-03 → 2026-08-10. Every
   recoverable label is lost on a rolling 7-day basis if never reconciled. This
   is what makes the repair time-sensitive rather than merely desirable.

## Stage 1 — provider-free reconciliation (implemented, dark)

Scheduling and governance only. **No second reconciliation implementation** —
this is a thin wrapper over the existing, already-proven `run_once`.

- `enable_crypto_tape_reconciler` — **default OFF**; off is a clean no-op.
- `crypto_tape_reconciler_window_hours` = 48, `crypto_tape_reconciler_limit` = 1000.
- The window is **refused** if shorter than the longest horizon's closing edge
  (24h × (1 + 0.5) = 36h), so it can never quietly under-reconcile — the exact
  failure class this milestone exists to remove.
- `run_scheduled_reconciliation()` returns a structured `disabled`/`invalid_window`/`ok`
  result.
- CLI `crypto-tape-reconcile` (`--dry-run`, `--force`, `--hours`, `--limit`).
- User systemd timer at 03/09/15/21:20 UTC, not auto-installed. Chosen so any
  maturing horizon is reconciled within ≤6h of its window closing, with ~5 days
  of slack before pruning, and so it never lands near the 01:30 UTC backup.

**Measured dry run on EVO-X2, 48h window / 1000 limit: 105.2s, 819 tokens,
`external_calls=0`, zero writes.** It would populate `survived_24h=7`,
`survived_6h=33`, `survived_1h=225`, `survived_15m=283` (true counts) in that
window alone. At a 168h backfill window: 2,836 tokens, `survived_24h=48`.

### One-time backfill

Steady state at 48h covers all future maturation. Recovering the *existing*
denominator needs one wider pass, because 49 of the 56 recoverable 24h births
are already outside 48h.

**Corrected after review.** The originally documented command
(`--force --hours 168`, no `--limit`) was wrong: it inherits `limit=2000`
against a 2,836-token universe, and under the original newest-first ordering it
would have selected the *least* matured tokens and silently missed most of the
24h births the backfill exists to recover — while printing `status=ok`. The
pass now selects oldest-first, tops up from a state-driven backlog of still-open
outcomes, and refuses to report `ok` when it truncates. The backfill must still
name an explicit `--limit` covering the whole universe.

## Review outcome — three reviews, all REQUEST CHANGES

Nothing here was self-assessed. What the reviews found and what changed:

**Fixed in this milestone.** Silent limit truncation (the recency starvation
named as root cause #2 was NOT fixed by the first commit — a review proved 0 of
5 matured tokens reconciled at a binding cap, with `status=ok` and no truncation
field anywhere); `run_migrations()` running before the gate, so a dark timer
would have applied Alembic unattended every 6h outside the deploy runbook;
`--limit -1` reaching SQLite as *unlimited*; `invalid_window` exiting 0, so a
misconfigured unit would look green forever while reconciling nothing; a window
guard that ignored the scheduling interval; no MarketOps-degraded abort; no
gate-bypass marker or `mode`/`forced` stamp in the audit trail; window-driven
selection that loses a cohort permanently after two missed passes and never
reconciles the existing backlog; and two tests that could not fail for the
reason they existed.

**Write-lock defect — the HOLD DURATION is fixed and measured; the
COMPETING-WRITER WAIT is a separate metric that is NOT proportionally fixed
(third review, NEW-H2 — read this whole section before trusting any
percentage figure elsewhere in this doc).**
The pass used to be a single write transaction: `_assemble_pass` flushed the
run row before the token loop and committed once at the end. Measured at
production density (1,000 tokens) BEFORE the fix: **36.9s pass, competing
writer blocked 35.79s — 97% of it — exceeding the 30s busy timeout.** This was
the same single-commit shape OPS-012 hit and OPS-013 retired in favour of
per-sub-window commits.

The scheduled reconciliation path (`run_scheduled_reconciliation` →
`run_once(..., batch_size=..., max_duration_seconds=...)`) now:

- commits in bounded batches (`crypto_tape_reconciler_batch_size`, default
  `RECONCILE_BATCH_SIZE=25` tokens) instead of one transaction for the whole
  pass. **This genuinely collapses the maximum SINGLE write-lock HOLD**:
  measured 8.5-40.8s max hold in the legacy shape down to **0.16-1.73s at
  2000 tokens** with batching — that improvement is real and credited.
  **It does NOT proportionally reduce a competing writer's worst-case WAIT**,
  which tracks this pass's WALL TIME instead: a pinned-export benchmark
  measured comparable wall-clock competitor blocking between legacy and
  batched shapes (6.79s vs 6.75s, 8.10s vs 8.18s in two reps), and in a THIRD
  rep the **batched run blocked the competitor LONGER** than the legacy
  comparison (13.68s vs 9.88s). All of that wait was in `BEGIN IMMEDIATE`,
  never in `COMMIT`. Mechanism: ~80 back-to-back short write transactions
  give SQLite's sleeping busy handler ~80 chances to lose the lock race
  against this pass — classic writer starvation, not a hold-duration
  problem. (Control: a read-only `dry_run` of 9.12s produced a max
  competitor wait of only 0.076s, ruling out the read span as the cause.)
  The honest bound on competing-writer exposure is therefore
  `RECONCILE_MAX_DURATION_SECONDS` (20s) **plus one batch — i.e. >=67% of the
  30s busy_timeout, NOT a small fraction of it.** The scheduled path's
  END-TO-END wall time on EVO-X2 has not yet been re-measured after this fix
  — do that before flipping the flag, and do not rely on the sub-second
  per-batch hold figure as if it were the safety bound;
- stops at an internal wall-clock deadline
  (`crypto_tape_reconciler_max_duration_seconds`, default
  `RECONCILE_MAX_DURATION_SECONDS=20.0`) and reports `status="partial"` with
  `stop_reason="deadline"` rather than claiming `ok`; already-committed
  batches are durable, nothing is duplicated;
- selects state-driven, not just recency-driven: already-`final` tokens are
  excluded from the in-window query (`exclude_final=True`), so a
  deadline-stopped pass advances to a DIFFERENT set of tokens on its next
  invocation instead of re-selecting the identical oldest head forever; the
  aged-out backlog query is now an OUTER join, so a token that was NEVER
  reconciled (no outcome row at all) is still recoverable once it ages out of
  the window, not silently dropped;
- takes a non-blocking, per-chain overlap flock
  (`.crypto-tape-reconcile-{chain}.lock`, co-located with the SQLite file, or
  the system temp dir for non-SQLite backends) around the whole pass, so a
  second concurrent instance — another scheduled tick, a manual tape
  session, a stray CLI run — is refused loudly (`status="skipped_overlap"`)
  instead of racing the pre-transaction `existing_births` read and dying with
  an `IntegrityError`;
- reuses the bounded lock-retry ladder every other tape caller already uses
  (`max_lock_attempts`/`lock_retry_seconds`), applied per batch commit;
  exhausting it on the very first write yields `status="skipped_contention"`,
  exhausting it after some batches already committed yields
  `status="partial"`/`stop_reason="contention"`.

The **manual/CLI path** (`run_once` with its historical defaults — no
`batch_size`, no `max_duration_seconds`) is functionally unchanged for
persistence (still one commit for the whole pass, still no deadline), but it
now DOES take the overlap flock by default (`use_overlap_lock=True`) and
issues one extra `final_by_birth_id` SELECT per pass. That is a deliberate,
deployed behaviour change, not "unchanged byte-for-byte" as an earlier commit
message on this branch claimed — the flock exists specifically so a manual
pass can never race the scheduled reconciler (or another manual pass) on the
same window, which is the entire point of the B4 overlap guard; the cost is
one extra read-only SELECT and a non-blocking flock syscall, negligible next
to the manual path's own multi-second write phase. The one caller that
deliberately opts OUT of the flock is `record_discovery_run` (the exact-cycle
anchor feed) — it is a single bounded transaction over a validated ≤40-token
set that runs every MarketOps cycle, so being skipped by a held lock would
zero out an anchor-feed cycle for good, which the exact-cycle design cannot
tolerate.

**Required before the flag is flipped:** re-measure the scheduled path's
actual wall-clock and write-lock-hold behaviour on EVO-X2 (the 20s deadline
default has not been validated against a real end-to-end pass duration on
production density since the batching change), add the five tape tables to
`retention.py`, and re-run the two-connection file-backed lock test on the
real deploy target — the existing one does NOT reach the batch retry ladder
(the holder takes `RESERVED` before the pass starts, so the run-row commit
exhausts the ladder first; instrumented: `_process_batch` calls = 0,
`batch_size=5` inert, and it uses `connect_args timeout=0.2` rather than the
app's real 30s `sqlite_busy_timeout_ms`), so failure-mode 15 in
`docs/SQLITE_WRITER_TOPOLOGY_2026_07.md` stays OPEN, not closed. Row growth
is measured at **1.048 MiB per pass ≈ 4.19 MiB/day ≈ 1.5 GiB/year** on a DB
already past its 3072 MB gate — unchanged by this fix (it changes write-lock
hold duration, not row volume).

**Third review — additional findings, all fixed on this branch:**

- **A transient overlap used to kill an entire bounded manual tape session**
  (`crypto-tape-session`): `run_once`'s overlap flock now defaults ON, so
  `status="skipped_overlap"` became reachable on every capture, and the
  session unconditionally treated any non-`ok` capture status as fatal.
  Measured before the fix: a 6h/12-capture session died on its FIRST capture
  (`abort_reason="capture 1 status=skipped_overlap"`, `captures_run=1` of
  12) whenever the reconciler (fires 4x/day) happened to hold the lock. Now
  a `skipped_overlap` capture is skipped and the session continues; the
  result reports `overlap_skipped_captures`.
- **The overlap lock coupled the TEST SUITE to the PRODUCTION database
  directory.** `_resolve_lock_dir` derives the lock dir from
  `settings.database_url`; on a host where `.env` sets a real sqlite URL
  (verified: this exact machine, with the exact `Settings(...)` shape the
  crypto-tape test helpers build), the resolved lock dir was the REAL `data/`
  directory the production timer uses — running the suite there would
  create and contend on the production `.crypto-tape-reconcile-{chain}.lock`
  file. Fixed with an autouse fixture
  (`tests/conftest.py::_isolate_crypto_tape_overlap_lock`) that forces every
  lock the test suite takes into a per-test `tmp_path`, unconditionally.
- **A pass that committed ZERO batches was labelled `partial`** with a
  message claiming "already-committed batches are durable" describing zero
  batches. Now labelled `skipped_contention` (matching the run-row-creation
  contention failure, which already used that status for the same "nothing
  was written" shape), both at the point of the deadline/contention decision
  and at the (separate) finalize-commit-failure path.

## Stage 2 — sparse 6h/24h re-ticks (designed, NOT implemented)

Only after Stage 1, because Stage 1 is free and Stage 2 costs provider calls.
Solves genuinely missing evidence (cause 1), not selection.

Prospective only — no historical backlog. Use the existing governed DexScreener
adapter; **do not spend SolanaTracker risk credits for horizon price
measurement**. No new cohort, no arming, no second discovery scan; CANARY-003/004
are closed scheduler proof and CANARY-005 must not run.

### Provider budget

Births run ~819 per 48h ≈ **410/day**. Two sparse re-observations per birth
(one near 6h, one near 24h):

| | calls |
|---|---|
| per day, unbatched | ~820 |
| per day, batched 30 addresses/request | **~28 requests/day** |
| per month, batched | **~840 requests/month** |

DexScreener's token endpoint accepts up to 30 addresses per request, so the
batched figure is the one to budget. This is negligible against the free tier
and adds no SolanaTracker spend. Re-ticks must be skipped when an in-window tick
already exists, which removes ~41% of 15m/1h work outright.

## Scope boundaries

Zero external calls in Stage 1. No wallets, no keys, no swaps, no signing, no
orders, no execution capability, no billing change. No cohort, member, or
horizon-observation row is created — asserted by test. No price tick is ever
written by reconciliation — asserted by test.

## Tests

`tests/test_crypto_coverage_repair_001.py` — 62 tests (grew from an initial 25
across three review rounds), plus one new test in
`tests/test_crypto_tape_cadence_001.py` for the session-level overlap fix: the
default-OFF gate and its no-op guarantee, the window guard, per-horizon
maturation for all four horizons, recency-starvation resistance, idempotency
(in-place update, no duplicate outcome row), restart convergence, dry-run
inertness, provider-freeness (fails on any HTTP client construction),
no-cohort/no-tick-write, NULL preserved for absent evidence, no substitution
of a tick just outside tolerance, and a regression pin proving
`record_discovery_run` **cannot** mature a horizon — so the scheduled pass is
never removed as "redundant with the anchor feed". The write-lock-fix review
round added: bounded per-batch commits proven via a real SQLAlchemy
`after_commit` listener (not just loop-iteration counting), the overlap flock
(including that `record_discovery_run` must ignore it and that a degraded
underlying pass must never report fabricated anchor counts), state-driven
selection making forward progress under repeated deadline stops, the backlog
OUTER-join fix for never-reconciled tokens, a deadline-truncated dry run
reporting a distinct non-ok status and non-zero CLI exit code, and file-backed
two-connection durability/contention tests. The third (SQLite/concurrency)
review round added: a zero-batches-committed pass reporting
`skipped_contention` not `partial`, a transient session-level overlap
skipping one capture instead of aborting the whole session, and a regression
pin proving the test suite's own overlap lock is isolated from a real
production-shaped `database_url` (not just present in `conftest.py`'s
source). Crash safety and idempotency under SIGKILL (both between and
mid-batch — no duplicate births/snapshots/actors, no flipped labels), the
`_commit_with_retry` re-staging design, "deadline never splits a batch", and
legacy-path column-level equivalence were independently re-verified by the
third review and left unchanged.

## Activation order

**Stage 1**: enable the provider-free reconciler; measure newly populated
`survived_6h`/`survived_24h`, pass duration, lock events, MarketOps health, and
confirm `external_calls=0`.

**Stage 2**: only then, prospective sparse re-ticks; measure due / observed /
missed / provider failures / duplicates / incremental calls.

Free denominator first, purchased observations second.
