# CRYPTO-COVERAGE-REPAIR-001 — survival-horizon coverage repair

Status: **Stage 1 implemented and DEPLOYED DARK (default OFF). NOT ACTIVATED — blocked on a measured write-lock defect. Stage 2 designed, not implemented.**
Branch: `worktree/crypto-coverage-repair`
Measured against EVO-X2 production DB on 2026-08-10.

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

**BLOCKING activation, NOT fixed.** The pass is a single write transaction:
`_assemble_pass` flushes the run row before the token loop and commits once at
the end. Measured at production density (1,000 tokens): **36.9s pass, competing
writer blocked 35.79s — 97% of it — exceeding the 30s busy timeout.** This is
the same single-commit shape OPS-012 hit and OPS-013 retired in favour of
per-sub-window commits. Also unfixed: a concurrent manual CLI run races the
pre-transaction `existing_births` read and dies with an `IntegrityError` after
~45s of contention, discarding the whole pass; and there is no bounded
lock-retry ladder (the wrapper rolls back and returns `db_locked`, but does not
retry the way every other tape caller does).

**Required before the flag is flipped:** chunked commits holding under ~2s, an
overlap guard or in-transaction birth upsert, the retry ladder, an internal
deadline, skipping snapshot/actor writes for already-final outcomes, and adding
the five tape tables to `retention.py`. Row growth is measured at **1.048 MiB
per pass ≈ 4.19 MiB/day ≈ 1.5 GiB/year** on a DB already past its 3072 MB gate.
A two-connection file-backed lock test — which the repo's own SQLite topology
audit already asked for — must exist before enabling.

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

`tests/test_crypto_coverage_repair_001.py` — 25 tests: the default-OFF gate and
its no-op guarantee, the window guard, per-horizon maturation for all four
horizons, recency-starvation resistance, idempotency (in-place update, no
duplicate outcome row), restart convergence, dry-run inertness, provider-freeness
(fails on any HTTP client construction), no-cohort/no-tick-write, NULL preserved
for absent evidence, no substitution of a tick just outside tolerance, and a
regression pin proving `record_discovery_run` **cannot** mature a horizon — so
the scheduled pass is never removed as "redundant with the anchor feed".

## Activation order

**Stage 1**: enable the provider-free reconciler; measure newly populated
`survived_6h`/`survived_24h`, pass duration, lock events, MarketOps health, and
confirm `external_calls=0`.

**Stage 2**: only then, prospective sparse re-ticks; measure due / observed /
missed / provider failures / duplicates / incremental calls.

Free denominator first, purchased observations second.
