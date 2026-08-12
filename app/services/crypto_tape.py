"""CRYPTO-TAPE-001 — read-only Solana memecoin lifecycle tape.

Moves the crypto lane from point-in-time scoring to REPLAYABLE TOKEN
LIFECYCLE TAPES: token birth, early holder/actor structure, risk-provider
enrichment, liquidity path, social metadata, and deterministic survival
outcomes over 15m/1h/6h/24h horizons.

The tape is DERIVED: one assembly pass consolidates rows the existing lanes
already persist (crypto_tokens/pairs/price_ticks/discovery_events/
risk_assessments + meme attention snapshots/catalyst events) into lifecycle
rows. It makes ZERO external calls and has ZERO provider-budget impact — the
scheduled scan lanes remain the only data collectors. Fields no source ever
provided stay NULL and are named in missing_info; nothing is fabricated.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): research infrastructure
only. A survival label is measured token behavior — never PnL, EV, a return,
a side, a size, or a recommendation. Actor observations hold public-chain
addresses already persisted by providers; no deanonymization. No wallets,
keys, swaps, signing, orders, execution, or autonomy anywhere. `--dry-run`
persists nothing; a real run persists ONLY lifecycle tape rows — never
signals, never MarketOps state.
"""

import fcntl
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, event, func, or_, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    CryptoPair,
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenActorObservation,
    CryptoTokenBirthEvent,
    CryptoTokenDiscoveryEvent,
    CryptoTokenLifecycleRun,
    CryptoTokenLifecycleSnapshot,
    CryptoTokenRiskAssessment,
    CryptoTokenSurvivalOutcome,
    CryptoWatcherRun,
    MemeAttentionSnapshot,
    MemeCatalystEvent,
)

def _completeness_reason(birth, min_liquidity: float) -> str | None:
    """None when the birth is a COMPLETE lifecycle anchor; else the rejection
    reason. Mirrors the --require-complete filter, per token. Canonical home
    (ANCHOR-FEED-MEASUREMENT-001): lives in this provider-free module so the
    exact-cycle anchor feed can classify births without importing anything
    network-capable; `crypto_horizon` re-exports it unchanged."""
    if not birth.first_pair_address:
        return "invalid_pair"
    if birth.initial_price_usd is None:
        return "missing_initial_price"
    if birth.initial_liquidity_usd is None:
        return "liquidity_or_initial_state_missing"
    if birth.initial_liquidity_usd <= 0:
        return "null_initial_liquidity"
    if birth.initial_liquidity_usd <= min_liquidity:
        return "below_min_liquidity"
    return None


# CRYPTO-HORIZON-ANCHOR-FEED-MEASUREMENT-001: hard cap on tokens the
# exact-cycle anchor feed will process from one natural discovery cycle.
# Internal safety constant (never an environment knob); comfortably above the
# marketops crypto scan's own bounded per-cycle output. An over-cap cycle is
# skipped loudly (`skipped_cap`) — never silently truncated.
MAX_ANCHOR_FEED_TOKENS_PER_CYCLE = 40

logger = logging.getLogger(__name__)

TAPE_NOTE = (
    "Read-only token lifecycle tape: birth, holder/actor structure, risk "
    "enrichment, liquidity path, social metadata, and survival outcomes, "
    "assembled from already-persisted surveillance rows (no external call, "
    "no provider-budget impact). Research infrastructure — a survival label "
    "is measured token behavior, never PnL, EV, a side, a size, or a "
    "recommendation. No wallets, keys, swaps, signing, orders, or execution."
)

# survival horizons: (label, minutes) — mirrors the MEME-SHADOW yardsticks
HORIZONS: tuple[tuple[str, int], ...] = (
    ("15m", 15), ("1h", 60), ("6h", 360), ("24h", 1440)
)
# an observation counts for a horizon if within +/- this fraction of it
HORIZON_TOLERANCE = 0.5
# CRYPTO-COVERAGE-REPAIR-001 HIGH-1: the SHORTEST horizon's own closing edge —
# derived from HORIZONS/HORIZON_TOLERANCE, never hardcoded. A token younger
# than this has no horizon due yet (not even 15m), so it cannot be finalized
# and re-selecting it wastes every scheduled pass forever (see
# `min_age_minutes` on `_universe`/`run_once`, opt-in on the scheduled path
# only).
SHORTEST_HORIZON_CLOSING_EDGE_MINUTES = min(m for _, m in HORIZONS) * (1 + HORIZON_TOLERANCE)
# CRYPTO-COVERAGE-REPAIR-001: the scheduled reconciler's interval, kept here
# rather than only in the systemd unit so the window guard can require that the
# window outlasts the closing edge PLUS one interval. If the unit's OnCalendar
# changes, change this with it.
RECONCILER_CADENCE_HOURS = 6
# liquidity below this fraction of the initial value => not survived / removed
SURVIVAL_LIQUIDITY_FRACTION = 0.3
# 24h volume below this at >=6h after birth => dead_volume
DEAD_VOLUME_24H_USD = 500.0
# bonding-curve launchpads; a later non-launchpad pair = graduated/migrated
LAUNCHPAD_DEXES = frozenset({"pumpfun", "moonshot", "launchlab"})

STATUS_OK = "ok"
STATUS_DRY_RUN = "dry_run"
# CRYPTO-COVERAGE-REPAIR-001 write-coordination hardening — statuses a caller
# must treat as "not fully done", distinct from ok/dry_run:
STATUS_PARTIAL = "partial"                       # stopped early (deadline or
    # exhausted lock retries) but committed whatever batches it finished;
    # restart-safe, the next pass continues via oldest-first + backlog.
STATUS_SKIPPED_OVERLAP = "skipped_overlap"        # another pass already holds
    # the per-chain overlap lock; nothing was read or written this call.
STATUS_SKIPPED_CONTENTION = "skipped_contention"  # the very first write of the
    # pass (the run row) never got a lock even after the full retry ladder;
    # nothing was written.
STATUS_DRY_RUN_PARTIAL = "dry_run_partial"        # a dry-run probe that was
    # ITSELF stopped early (deadline or exhausted lock retries) before it
    # could examine the whole selected set. Nothing is ever written by a dry
    # run, so this is distinct from STATUS_PARTIAL (which implies some
    # batches committed); it exists so a truncated probe never reports plain
    # "dry_run", which looks indistinguishable from a complete one.
STATUS_LOCK_UNAVAILABLE = "lock_unavailable"      # MEDIUM fix: the overlap
    # lock FILE itself could not be opened (unwritable/missing lock
    # directory, permission error) — distinct from `skipped_overlap` (another
    # pass legitimately holds the lock). Previously this raised an uncaught
    # traceback instead of a typed, refused status.
STATUS_CONCURRENT_WRITE_CONFLICT = "concurrent_write_conflict"  # NEW-H3 fix:
    # a real `IntegrityError` (unique-constraint violation), not an
    # `OperationalError` — the DB_LOCKED_* retry ladder never applies. This
    # is the exact shape a live race against `record_discovery_run` (which
    # deliberately opts out of the overlap lock) produces if the HIGH-1
    # age-exclusion mitigation is ever defeated by a clock/timing edge.
    # Caught and reported as a typed, non-zero-exit status instead of an
    # uncaught traceback that would kill the systemd unit.
STATUS_BACKLOG_EXPIRING = "backlog_expiring"  # NEW-HIGH-2 fix (second
    # re-review, convergence lens): the FRONTIER (oldest still-open/
    # never-reconciled token's age) has crossed
    # `crypto_retention_days*24 - RECONCILE_CADENCE_HOURS` — i.e. the
    # OLDEST unreconciled evidence is at risk of being pruned before the
    # NEXT scheduled pass can reach it. Deliberately distinct from the
    # routine `partial`/`truncated` that fires on every healthy pass at
    # production density (see the module note by
    # `RECONCILE_CADENCE_HOURS` below on why an always-on alarm carries no
    # information) — this status exists to be the rare, actionable one.

STATUS_UNSAFE_HOST_COST = "unsafe_host_cost"  # B3 terminal status: the
    # adaptive per-token cost estimate is so high, relative to
    # `time_budget_seconds` (the B1 SLO), that even a SINGLE-TOKEN
    # transaction is predicted to violate the write-time budget. This is
    # NOT "truncated" or "partial" — those imply a healthy pass simply ran
    # out of selection room or wall-clock time; this means the host is
    # currently too slow (or the seed/measured cost estimate too
    # conservative) for this SLO at ANY batch size, and continuing would
    # mean guessing rather than measuring. Batches already committed
    # earlier in the same pass, if any, remain durable — only forward
    # progress stops.

BONDING_LAUNCHPAD = "launchpad_curve"
BONDING_AMM = "amm_pool"

# CRYPTO-TAPE-CADENCE-002: SQLite write-lock resilience. On a shared host the
# baseline/watcher/MarketOps writers can hold the write lock past the DB busy
# timeout, so a capture's run-row INSERT raises "database is locked". A bounded
# app-level retry (mirrors the OPS-013 tick-aggregation idiom) recovers from
# transient contention; a persistent lock aborts loudly and CLEANLY (session
# rolled back first) so the summary path never hits PendingRollbackError.
# Defined here (not just near run_tape_session, its original home) because
# CRYPTO-COVERAGE-REPAIR-001's per-batch commit retry in `_assemble_pass` and
# `run_once` need it as a default argument value, which is evaluated at
# `def` time and must already exist.
DB_LOCKED_MAX_ATTEMPTS = 3       # total tries per capture (1 + 2 retries)
DB_LOCKED_RETRY_SECONDS = 3.0    # short wait between attempts
ABORT_DB_LOCKED = "database_locked"

# CRYPTO-COVERAGE-REPAIR-001 B1/B3 — measured blocker (MEDIUM: session-only
# evidence from an ad-hoc, non-committed benchmark script — see "evidentiary
# status" in docs/milestones/CRYPTO-COVERAGE-REPAIR-001.md's Write-lock
# defect section before citing exact figures): `_assemble_pass` used to
# be one write transaction for the whole pass (36.9s measured at production
# density, blocking a competing writer 97% of a 30s busy_timeout). Bounding
# each committed transaction to a small, fixed batch of tokens keeps the write
# lock held for a small fraction of a second at a time instead of for the
# whole pass. 25 is the shipped default from the B1 profile (see the
# CRYPTO-COVERAGE-REPAIR-001 debugging session): at ~51 ticks/28 discovery
# events/23 risk assessments/2 pairs per token, a 25-token batch's write phase
# measured well under one second.
#
# THIRD REVIEW (NEW-H2), restating what this batching does and does not
# bound, against a pinned measurement at 35dec32 vs. the batched version at
# 2000 tokens: batching genuinely collapses the max SINGLE WRITE-LOCK HOLD
# (8.5-40.8s -> 0.16-1.73s) — that improvement is real and unchanged by this
# note. But a competing writer's worst-case WAIT tracks the reconciler's PASS
# WALL TIME, not the batch hold: measured 6.79s vs 6.75s wall for legacy vs.
# batched, 8.10s vs 8.18s wall in a second rep, and in a THIRD rep the
# batched run blocked the competitor LONGER than the legacy comparison
# (13.68s vs 9.88s). All of the wait was in BEGIN IMMEDIATE, never in COMMIT.
# Mechanism: ~80 back-to-back short write transactions give SQLite's
# sleeping busy handler ~80 chances to lose the race for the lock against a
# reconciler that keeps re-acquiring it — classic writer starvation, not a
# hold-duration problem. (Control: a read-only dry_run of 9.12s produced a
# max competitor wait of 0.076s, ruling out the read span as the cause.) The
# honest bound on a competing writer's exposure is therefore
# RECONCILE_MAX_DURATION_SECONDS (below) plus one batch — NOT the per-batch
# hold duration, and NOT the "~3%" figure that per-batch hold alone would
# suggest.
#
# NEW-HIGH-1 fix (third Lane-B review, SQLite coexistence — DO NOT ACTIVATE
# reason). This is a per-TOKEN-COUNT constant, and the 20s deadline above
# CANNOT bound a single batch's hold — it is only evaluated BETWEEN batches
# (see the deadline check in `_assemble_pass_locked`'s batch loop), so with
# one batch in flight there is nothing for the deadline, or the post-batch
# yield, to interrupt. A batch's wall-clock hold tracks
# `batch_size x per-token cost`, and per-token cost is HOST-SPEED dependent,
# not something this constant can be calibrated against once and trusted
# everywhere: at the dev-Mac speed this shipped at, 25 tokens was measured
# safely under the 30s busy_timeout; at the reviewer's measured EVO-speed
# multiplier (~62x slower per token), the SAME batch_size=25 produced
# 26.3-36.5s holds — one of three trials exceeded the 30s busy_timeout
# outright — and a full pass converges only ~25 tokens/6h-tick against
# ~405 new births/day, so the reconciler could never converge on that host
# at this constant. Lowered to the reviewer's measured stopgap (5): at the
# same EVO-speed multiplier, batch_size=5 held 4.56-5.32s worst case (5-7x
# better), with unchanged competitor throughput and a better duty cycle.
# This is still a COUNT-based bound, not a time-based one — the reviewer's
# preferred long-term fix (check the deadline INSIDE the batch loop between
# tokens, or derive batch_size at runtime from a measured per-token wall
# time) is NOT implemented here; this constant change is a stopgap, not a
# structural fix. The pre-activation runbook step is, and must stay,
# "measure a real batch's hold on the target host and set batch_size so the
# hold stays comfortably under the busy_timeout" — never "trust this
# default on an unmeasured host".
RECONCILE_BATCH_SIZE = 5

# CRYPTO-COVERAGE-REPAIR-001 B1 — the write-time SLO. THE CORE PROBLEM this
# constant (and B3 below) exists to fix: `RECONCILE_BATCH_SIZE` above is a
# fixed TOKEN COUNT, and the measured relation on a slower host is
#     write-lock hold ~= tokens_in_batch x per-token write cost   (R ~ 1.0)
# A count is not a safety invariant by itself, because per-token cost is
# HOST-SPEED dependent (measured >60x slower on one EVO-class host than the
# dev Mac this repo is usually edited on — see RECONCILE_BATCH_SIZE's own
# history above: the same batch_size=25 was safe on one host and exceeded
# the 30s busy_timeout on the other).
#
# This constant is the operational TARGET for how long ANY ONE reconciler
# transaction may hold the SQLite write lock, chosen — not measured — with
# deliberately large margin below `busy_timeout=30s`, because:
#   * the always-on watcher and the 5-minute MarketOps timer can each want
#     the write lock at any moment, independent of the reconciler's own
#     schedule;
#   * the daily backup (SQLITE-BACKUP-COORDINATION-001) needs its own
#     window;
#   * host load and filesystem variability are exactly what made the fixed
#     batch_size=25 -> 5 stopgap necessary in the first place, and neither
#     is something this repo can measure without EVO access.
# 2.0s is under 7% of the 30s busy_timeout — comfortably small enough that a
# reconciler transaction is very unlikely to be the reason ANY other writer
# sees "database is locked", while still large enough that a correctly
# calibrated per-token cost estimate can batch more than one token per
# transaction on a healthy host. This is a POLICY choice (how much of the
# shared write-lock budget the reconciler may spend at once), not a host
# measurement — unlike `initial_per_token_cost_seconds` (B3 below), which
# MUST come from a real measurement and deliberately has no default.
RECONCILE_WRITE_TIME_SLO_SECONDS = 2.0
# Internal wall-clock deadline for one `run_once` call. None = unbounded
# (existing manual-path behaviour, unchanged). The scheduled path sets this so
# one pass can never run indefinitely; remaining tokens simply stay backlog
# for the next scheduled pass (oldest-first + state-driven selection already
# guarantee they are not starved — see `unreconciled_backlog`).
#
# This value (plus one batch's worth of wall time past it) is the honest
# bound on a competing writer's worst-case wait against this pass — see the
# NEW-H2 note on RECONCILE_BATCH_SIZE above. At the shipped default that is
# >=67% of the 30s SQLite busy_timeout, not a small fraction of it.
RECONCILE_MAX_DURATION_SECONDS = 20.0
# NEW-HIGH-2 fix (second re-review, convergence lens): `STATUS_BACKLOG_
# EXPIRING`'s threshold (see `run_scheduled_reconciliation`) is computed
# against the systemd timer's OWN cadence — reuses the pre-existing
# `RECONCILER_CADENCE_HOURS` (module top, used by the window-validation
# check above) rather than introducing a second, possibly-drifting cadence
# constant.
#
# Why `STATUS_BACKLOG_EXPIRING` exists as a SEPARATE status from
# `partial`/`truncated` at all: at production density EVERY pass exits
# non-zero via those two (app/cli.py's `crypto_tape_reconcile` returns -1 for
# both), so a oneshot unit driven by this reconciler sits permanently
# "failed" in `systemctl --user list-units`. An alarm that is always on
# carries no information — it is the SAME alarm in a healthy run and in a
# run silently losing 28% of its labels to pruning (the NEW-BLOCKING-2
# shape). `backlog_expiring` is reserved for the case that is actually rare
# and actually actionable: the frontier is close enough to
# `crypto_retention_days` that evidence will be pruned before the NEXT
# scheduled pass, regardless of whether THIS pass's own truncated/partial
# shortfall is otherwise routine.
# NEW-H1 fix (third re-review, SQLite/concurrency). Per-batch commit hold
# duration alone is not what starves a competing writer — SQLite's sleeping
# busy handler loses the lock race against ~80 back-to-back short write
# transactions almost as easily as against one long one, because each of
# this pass's re-acquisitions is another chance for the handler to lose.
# Applying a short sleep AFTER each batch commits (tried BEFORE the commit
# first — that made competitor waits WORSE, a harness-ordering mistake, not
# a design choice) gives a genuinely idle window for a waiting competitor to
# actually win the lock race, at the cost of fewer tokens processed per pass
# (recoverable by raising the deadline or lowering the cadence — the
# reconciler is the interruptible party here, the watcher is not).
#
# NEW-HIGH-4 fix (third Lane-B review, SQLite coexistence): the ORIGINAL
# specific figures here ("competitor max wait 7.49-12.68s -> 0.156-0.870s, a
# 10-40x reduction") were session-only ad-hoc benchmark evidence and did NOT
# reproduce under an independent 4-trial-each measurement: that review
# measured max wait 0.01-10.80s -> 0.47-4.47s (a ~2.4x reduction in the
# worst case, not 10-40x), competitor writes 84-261 -> 144-238 (LOWER than
# the monolithic comparison's 328-378 — no throughput improvement), and
# WORSE typical (p95) waits: 0.00-1.43s -> 0.42-0.61s. The qualitative shape
# is still believed correct — this converts a rare-and-huge wait
# distribution into a frequent-and-moderate one, which is probably the
# right trade for a shared host that needs bounded worst-case latency more
# than it needs zero average latency — but the specific numbers are NOT
# reproducible evidence and must not be cited as measured fact. No
# committed benchmark harness exists yet to re-derive them (see the
# milestone doc's evidentiary-status note); until one does, treat this as a
# qualitative, not quantitative, claim.
RECONCILE_POST_BATCH_YIELD_SECONDS = 0.05
# Overlap guard: a coordination-only flock file, one per chain, living next to
# the sqlite file itself (or the system temp dir for non-sqlite/in-memory
# configurations). Never touches the database's own locking; kernel-released
# if the process dies, so a crash can never leave a stale lock.
RECONCILE_LOCK_FILENAME = ".crypto-tape-reconcile-{chain}.lock"

# ---------------------------------------------------------------------------
# CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001 — the reconciler's own LOCK-WAIT
# budget. This is a DIFFERENT quantity from RECONCILE_WRITE_TIME_SLO_SECONDS
# above and the two must never be conflated:
#
#   * RECONCILE_WRITE_TIME_SLO_SECONDS bounds how long the reconciler may
#     HOLD the SQLite write lock (a courtesy to co-tenants).
#   * the constants below bound how long the reconciler may WAIT for it (its
#     own robustness — a blocked reconciler is the VICTIM, not the aggressor).
#
# MOTIVATING PRODUCTION MEASUREMENT (EVO, tape_run_id=3618, bounded `--force`
# run-once): the pass succeeded — external_calls=0, 236 batches committed,
# 1,182 final outcomes — but recorded `lock_retry_events=1`,
# `blocked_ms=45,744` and `duration_ms=61,047` against
# `--max-duration-seconds 30`. The entire 2x deadline overshoot was that one
# blocked window. A concurrent MarketOps run (#9405) completed `ok`, so the
# reconciler was purely the blocked party.
#
# WHY `sqlite_busy_timeout_ms=30000` did not bound it at 30s. SQLite's busy
# timeout is PER LOCK-ACQUISITION ATTEMPT, not per statement and not per
# transaction, and one blocked write statement can perform more than one such
# attempt. Measured on this repo's dev Mac with raw `sqlite3` (no SQLAlchemy
# in the path), a single competing connection holding RESERVED:
#
#     configured busy_timeout   observed wall time before "database is
#                               locked"        ratio
#     -----------------------   -----------------------------   -----
#     250 ms                    0.734 s / 0.663 s               2.93x / 2.65x
#     500 ms                    1.077 s / 1.151 s               2.15x / 2.30x
#     1000 ms                   1.779 s / 1.993 s               1.78x / 1.99x
#     2000 ms                   3.416 s / 3.373 s               1.71x / 1.69x
#     (BEGIN IMMEDIATE, i.e. a single nominal acquisition, behaves the same:
#      500 ms -> 1.131 s, 1000 ms -> 1.991 s, 2000 ms -> 3.300 s)
#
# THAT TABLE IS THE WRONG HOST, AND THE CORRECTION MATTERS (independent
# review of this branch). Re-derived ON EVO — SQLite 3.45.1, raw `sqlite3`,
# 2 reps per budget at 250/500/1000/2000/4000 ms, across `BEGIN IMMEDIATE`,
# autocommit INSERT, COMMIT-behind-SHARED and SELECT/temp-INSERT behind an
# EXCLUSIVE holder — the MAXIMUM ratio observed anywhere was **1.01x**. The
# dev-Mac asymptote above does not describe the target host; this repo has
# made exactly this mistake before (a per-token cost measured on the Mac and
# generalised to EVO), so the numbers are kept here as what they are: a
# dev-Mac measurement, not the deployment host's behaviour.
#
# The production figure does NOT require the 2x hypothesis either:
# 45.744 s / 30 s = 1.52x is fully explained by ~1.00x across TWO successive
# acquisitions.
#
# So 2.0 is NOT "the measured asymptote". It is a DELIBERATE SAFETY FACTOR,
# chosen and labelled as such:
#   * on the only host where it has been measured properly (EVO) the true
#     overshoot is ~1.0x, so this factor makes the derived budget up to 2x
#     TIGHTER than the pass's deadline could actually afford;
#   * that direction is the safe one for a gate whose whole purpose is a
#     BOUNDED wait distribution — the reconciler is the interruptible party;
#   * the cost is real and must be stated: a pass can fail into `partial`
#     (stop_reason=lock_wait_budget) on contention it had deadline left to
#     absorb. If the EVO lock-wait histogram this milestone persists shows
#     that happening, lowering this constant toward the measured ~1.0x is
#     the correct response — not raising the deadline.
# `test_one_blocked_statement_stays_within_the_assumed_overshoot_factor`
# defends the direction the DESIGN depends on (the factor is an UPPER bound
# on one acquisition's overshoot); it deliberately no longer asserts a
# lower bound, because at 1.01x on EVO any such assertion is satisfied by a
# millisecond of scheduler noise and therefore defends nothing.
RECONCILE_LOCK_WAIT_STATEMENT_OVERSHOOT = 2.0
# WRITE-lock acquisition points inside ONE batch attempt, in this database's
# rollback-journal mode (no `journal_mode=WAL` is set anywhere in this repo —
# see app/db.py): (1) the transaction's first write statement, acquiring
# RESERVED, and (2) COMMIT, acquiring PENDING/EXCLUSIVE behind any live
# reader. A cache spill would add a third, which a 5-token batch does not
# produce; if `crypto_tape_reconciler_batch_size` is ever raised far enough
# to spill the page cache mid-batch, this constant is the thing to revisit.
#
# READS ARE AN UNLISTED THIRD CLASS, and this comment used to omit them.
# `_process_batch` issues SELECTs inside the attempt, and a SELECT blocks
# behind an EXCLUSIVE holder — measured on EVO at 1.00x its configured
# busy_timeout, same as every write shape. They are not added to this
# constant because the shipped ATTEMPT_MULTIPLIER of 4 already covers three
# blocking points at the measured ~1.0x overshoot with margin to spare
# (3 x 1.01 = 3.03 < 4); that headroom is now the stated reason the read
# class is safe, rather than an accident of an unexamined constant.
RECONCILE_LOCK_WAIT_BLOCKING_POINTS = 2
# Worst-case multiple of the CONFIGURED per-acquisition budget that ONE batch
# attempt can spend blocked, under the safety factor above: 2 write-lock
# blocking points x 2.0. See the read-class note: at EVO's measured ~1.0x
# this value also covers the batch's reads.
RECONCILE_LOCK_WAIT_ATTEMPT_MULTIPLIER = (
    RECONCILE_LOCK_WAIT_BLOCKING_POINTS * RECONCILE_LOCK_WAIT_STATEMENT_OVERSHOOT
)
# The floor under the derived budget, so a pass that is already at (or past)
# its deadline still gets a real chance to take the lock rather than failing
# on contention it never actually waited for. It is fixed by the identity
# `floor x RECONCILE_LOCK_WAIT_ATTEMPT_MULTIPLIER ==
# RECONCILE_WRITE_TIME_SLO_SECONDS` — the reconciler may never wait longer,
# in the modelled worst case, than it is itself allowed to hold.
#
# HONESTY CORRECTION (same review as the overshoot constant): that identity
# is EXACT only if one attempt really can overshoot its budget by
# ATTEMPT_MULTIPLIER. At EVO's measured ~1.0x per acquisition the true
# worst-case wait at the floor is ~2 x 0.5 = 1.0 s, i.e. the identity is
# CONSERVATIVE BY ~2x rather than tight. It remains a defensible derivation
# of the floor (it can only make the reconciler wait less than its own hold
# SLO), but it must not be quoted as an exact equality of measured
# quantities.
RECONCILE_LOCK_WAIT_FLOOR_SECONDS = (
    RECONCILE_WRITE_TIME_SLO_SECONDS / RECONCILE_LOCK_WAIT_ATTEMPT_MULTIPLIER
)
# Fixed histogram edges (milliseconds) for the persisted lock-wait
# distribution. Fixed edges are what make per-pass histograms ADDABLE across
# many passes without keeping every raw sample on every run row — the
# downstream goal is a distribution of real lock waits, and a per-pass list
# of raw samples neither aggregates nor bounds its own size.
RECONCILE_LOCK_WAIT_BUCKET_EDGES_MS = (1, 10, 100, 1000, 5000, 15000, 30000)
# How many raw per-attempt samples the IN-MEMORY summary keeps (never
# persisted). Bounded so a 2000-token pass cannot build an unbounded list.
RECONCILE_LOCK_WAIT_SAMPLE_CAP = 256

_WRITE_STATEMENT_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")


def _is_write_statement(statement: str) -> bool:
    """True for the statement kinds that take SQLite's RESERVED write lock.

    LOW fix (independent review): the original classified on the first seven
    characters alone, so a CTE-wrapped write (`WITH x AS (...) INSERT ...`)
    read as a plain SELECT and its RESERVED acquisition would never have been
    timed. No such statement exists in this module today — SQLAlchemy emits
    plain INSERT/UPDATE/DELETE for these mappings — so this is a trap closed
    before it is stepped in, not a measured defect. A `WITH` prefix defers to
    a bounded keyword scan rather than widening the fast path for every
    statement."""
    head = statement.lstrip()[:7].upper()
    if head.startswith(_WRITE_STATEMENT_PREFIXES):
        return True
    if head.startswith("WITH"):
        upper = statement.upper()
        return any(f" {kw} " in upper for kw in _WRITE_STATEMENT_PREFIXES)
    return False


class LockWaitMeter:
    """CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001 — measures the SQLite write-lock
    WAIT of one batch attempt, as distinct from the attempt's total wall time
    (`blocked_ms`, which has always included the attempt's real work).

    What it measures, precisely, and what it does not:

      * `lock_acquire_seconds` — the wall time of the transaction's FIRST
        write statement. In rollback-journal mode that statement is where
        RESERVED is acquired, so its duration is the RESERVED wait plus the
        cost of executing one small DML statement. Measured uncontended on
        the dev Mac that cost is ~1.3 ms, so this is a TIGHT UPPER BOUND on
        the wait, not an exact figure.
      * `commit_seconds` — the wall time of `session.commit()` MINUS the wall
        time of any statements SQLAlchemy emitted inside it (the final
        flush). What remains is the COMMIT itself: the PENDING/EXCLUSIVE wait
        plus the journal fsync. Again an upper bound, overstated by the
        fsync.
      * `hold_seconds` — from the moment RESERVED was acquired to the moment
        COMMIT returned: the reconciler's actual write-lock HOLD, the
        quantity RECONCILE_WRITE_TIME_SLO_SECONDS is about. Recorded, never
        enforced (enforcement needs adaptive batching, which needs a
        measured `initial_per_token_cost_seconds`).

    Python's `sqlite3` exposes no busy-handler callback, so there is no way
    from this process to split a SUCCEEDING statement's duration into "slept
    in the busy handler" and "did work". Timing the one statement whose only
    job is to take the lock is the closest honest approximation, and it costs
    no extra statement and no extra lock hold. On the FAILING path the
    statement did no work at all, so the measurement there is exact.
    """

    # Deliberately `time.monotonic`, not `time.perf_counter`: several
    # existing tests inject a scripted fake `time.perf_counter` on the
    # STDLIB module to drive `blocked_ms`/adaptive-cost arithmetic by
    # counting calls, and an instrumentation path that silently consumed
    # entries from that script would change what those tests measure. This
    # meter must observe the clock without perturbing anyone else's
    # measurement of it.
    def __init__(self):
        self.lock_acquire_seconds = 0.0
        self.commit_seconds = 0.0
        self.hold_seconds = 0.0
        self._statement_seconds = 0.0
        self._pending_t0: float | None = None
        self._pending_is_write = False
        self._acquired = False
        self._acquired_at: float | None = None
        self._commit_t0: float | None = None
        self._commit_statement_baseline = 0.0

    # -- engine event hooks -------------------------------------------------
    def _before(self, conn, cursor, statement, parameters, context, executemany):
        self._pending_t0 = time.monotonic()
        self._pending_is_write = _is_write_statement(statement)

    def _after(self, conn, cursor, statement, parameters, context, executemany):
        if self._pending_t0 is None:  # pragma: no cover - defensive
            return
        elapsed = time.monotonic() - self._pending_t0
        self._statement_seconds += elapsed
        if self._pending_is_write and not self._acquired:
            self._acquired = True
            self._acquired_at = time.monotonic()
            self.lock_acquire_seconds += elapsed
        self._pending_t0 = None
        self._pending_is_write = False

    # -- caller-driven markers ---------------------------------------------
    def mark_commit_start(self) -> None:
        self._commit_t0 = time.monotonic()
        self._commit_statement_baseline = self._statement_seconds

    def mark_commit_end(self) -> None:
        if self._commit_t0 is None:  # pragma: no cover - defensive
            return
        now = time.monotonic()
        inner_statements = self._statement_seconds - self._commit_statement_baseline
        self.commit_seconds += max(0.0, (now - self._commit_t0) - inner_statements)
        if self._acquired_at is not None:
            self.hold_seconds = max(0.0, now - self._acquired_at)
        self._commit_t0 = None

    def mark_failed(self) -> None:
        """A `database is locked` error ended this attempt. Attribute the
        elapsed time of whatever was in flight — a statement that never got
        its `after_cursor_execute`, or an in-flight COMMIT — to lock wait.
        On this path the elapsed time IS pure wait: the statement did no
        work, it timed out trying to start."""
        now = time.monotonic()
        if self._pending_t0 is not None:
            elapsed = now - self._pending_t0
            self._statement_seconds += elapsed
            self.lock_acquire_seconds += elapsed
            self._pending_t0 = None
            self._pending_is_write = False
        elif self._commit_t0 is not None:
            inner = self._statement_seconds - self._commit_statement_baseline
            self.commit_seconds += max(0.0, (now - self._commit_t0) - inner)
            self._commit_t0 = None

    @property
    def lock_wait_seconds(self) -> float:
        return self.lock_acquire_seconds + self.commit_seconds


@contextmanager
def _lock_wait_meter(session: Session, enabled: bool = True):
    """Attach a `LockWaitMeter` to THIS session's current connection for the
    duration of one batch attempt. Scoped to the Connection instance (not the
    Engine) so it can never observe, or slow down, any other session sharing
    the same engine. Degrades to an inert meter if the session cannot produce
    a connection — instrumentation must never be able to fail a pass.

    `enabled=False` yields an inert meter and never touches the session at
    all, so a caller that did not opt into lock-wait accounting keeps its
    exact pre-milestone connection behaviour."""
    meter = LockWaitMeter()
    conn = None
    if not enabled:
        yield meter
        return
    try:
        conn = session.connection()
        event.listen(conn, "before_cursor_execute", meter._before)
        event.listen(conn, "after_cursor_execute", meter._after)
    except Exception:  # pragma: no cover - defensive
        logger.debug("crypto tape: lock-wait meter could not attach", exc_info=True)
        conn = None
    try:
        yield meter
    finally:
        if conn is not None:
            for name, fn in (
                ("before_cursor_execute", meter._before),
                ("after_cursor_execute", meter._after),
            ):
                try:
                    event.remove(conn, name, fn)
                except Exception:  # pragma: no cover - defensive
                    pass


def derive_lock_wait_budget_seconds(
    explicit_budget_seconds: float | None,
    remaining_deadline_seconds: float | None,
) -> float | None:
    """The reconciler's per-lock-acquisition wait budget for the NEXT attempt.

    Returns `None` when there is nothing to derive from and nothing was
    configured — the caller then leaves the connection's busy timeout exactly
    as `app.db.connect_args_for` set it (today's behaviour, unchanged).

    Derivation, and why it is NOT a number invented against the contention
    distribution: one production data point (45.744 s of blocked time against
    a healthy concurrent MarketOps run) cannot establish a percentile of that
    distribution, so it cannot honestly set an absolute timeout. What CAN be
    derived without a distribution is the budget the pass's OWN wall-clock
    contract can afford: at most `remaining_deadline` may be spent waiting,
    and one attempt can spend up to
    `RECONCILE_LOCK_WAIT_ATTEMPT_MULTIPLIER x budget` (measured — see that
    constant), so

        budget = max(FLOOR, remaining_deadline / ATTEMPT_MULTIPLIER)

    This is self-tightening: as a pass burns its deadline the budget shrinks,
    so a late batch fails fast into `partial` (durable prior batches, retried
    next pass) instead of turning a 30 s run into a 61 s one.

    An explicit `crypto_tape_reconciler_lock_wait_budget_seconds` acts as a
    CAP on top of the derived value, never as a floor under it — that is the
    knob for a contention-informed number once the `lock_wait_ms` histogram
    this milestone records has produced a real distribution.
    """
    if remaining_deadline_seconds is None:
        return explicit_budget_seconds
    derived = max(
        RECONCILE_LOCK_WAIT_FLOOR_SECONDS,
        remaining_deadline_seconds / RECONCILE_LOCK_WAIT_ATTEMPT_MULTIPLIER,
    )
    if explicit_budget_seconds is None:
        return derived
    return min(explicit_budget_seconds, derived)


def _apply_lock_wait_budget(session: Session, budget_seconds: float | None) -> int | None:
    """Set `PRAGMA busy_timeout` on THIS session's SQLite connection to the
    reconciler's budget. Connection-scoped: no other writer's busy policy
    changes, and the process-wide `sqlite_busy_timeout_ms` is untouched.

    Returns the applied value in ms, or None if nothing was applied (no
    budget, non-SQLite backend, or the PRAGMA failed). A failure here
    degrades to today's behaviour and is never allowed to fail the pass."""
    if budget_seconds is None:
        return None
    ms = max(0, int(budget_seconds * 1000))
    try:
        conn = session.connection()
        if conn.dialect.name != "sqlite":
            return None
        conn.exec_driver_sql(f"PRAGMA busy_timeout = {ms}")
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "crypto tape: could not apply the reconciler lock-wait budget "
            "(%s ms); this pass keeps the connection's existing busy timeout",
            ms, exc_info=True,
        )
        return None
    return ms


def _read_busy_timeout_ms(session: Session) -> int | None:
    """The connection's CURRENT busy timeout, so the pass can put it back."""
    try:
        conn = session.connection()
        if conn.dialect.name != "sqlite":
            return None
        return int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar())
    except Exception:  # pragma: no cover - defensive
        return None


class LockWaitAccounting:
    """Per-pass aggregation of `LockWaitMeter` samples. Bounded by
    construction: a fixed histogram plus scalars, never an unbounded list of
    raw samples on the run row.

    THE SCALAR IS BIASED HIGH, AND BY HOW MUCH IS NOW REPORTED, NOT GUESSED
    (independent review of this branch). `LockWaitMeter` cannot split a
    SUCCEEDING statement's duration into "slept in SQLite's busy handler" and
    "did work" — Python's `sqlite3` exposes no busy-handler callback — so
    every attempt's reported wait carries that attempt's own DML plus its
    commit fsync. Measured on a pass with ZERO contention (no competing
    writer at all, so every millisecond is bias):

        lock_wait_ms=3380  lock_wait_measurements=402  lock_wait_ms_max=21
        blocked_ms=9578    histogram: {'1-10': 372, '10-100': 30, rest 0}

    i.e. ~8.4 ms per attempt against a TRUE wait of exactly zero — ~6x the
    "~1.3 ms DML" the meter's docstring used to imply, because the commit
    FSYNC dominates and was never quantified. That is 35% of `blocked_ms`
    under no contention, and it scales with batch count.

    Two consequences, both now handled here rather than in a reader's head:

      * the bias is per-ATTEMPT and near-constant, so the pass's own MINIMUM
        observed attempt is a direct, in-band estimate of it. It is reported
        as `lock_wait_ms_baseline_per_attempt`, and `lock_wait_ms_net`
        subtracts `baseline x measurements` from the total. On a
        zero-contention pass that drives the net to ~0, which is the correct
        answer; under contention it removes the fixed per-attempt floor and
        leaves the real waiting.
      * the HISTOGRAM survives the bias far better than any scalar, because
        the bias lands almost entirely in the `1-10` bucket. **The
        eventual gate threshold must be derived from the `>=100` buckets of
        `lock_wait_histogram_ms`, never from pass-total `lock_wait_ms`** —
        see docs/EVO_X2_RUNBOOK.md's lock-wait section.
    """

    def __init__(self):
        self.total_seconds = 0.0
        self.max_seconds = 0.0
        self.min_seconds: float | None = None
        self.measurements = 0
        self.samples_ms: list[int] = []
        self.buckets = [0] * (len(RECONCILE_LOCK_WAIT_BUCKET_EDGES_MS) + 1)
        self.budget_ms_applied: int | None = None
        self.budget_ms_min: int | None = None
        self.hold_seconds_max = 0.0
        self.hold_slo_violations = 0

    def record_budget(self, budget_ms: int | None) -> None:
        if budget_ms is None:
            return
        if self.budget_ms_applied is None:
            self.budget_ms_applied = budget_ms
        self.budget_ms_min = (
            budget_ms if self.budget_ms_min is None
            else min(self.budget_ms_min, budget_ms)
        )

    def record(self, meter: LockWaitMeter) -> None:
        wait = meter.lock_wait_seconds
        self.total_seconds += wait
        self.max_seconds = max(self.max_seconds, wait)
        self.min_seconds = (
            wait if self.min_seconds is None else min(self.min_seconds, wait)
        )
        self.measurements += 1
        ms = int(wait * 1000)
        if len(self.samples_ms) < RECONCILE_LOCK_WAIT_SAMPLE_CAP:
            self.samples_ms.append(ms)
        index = len(RECONCILE_LOCK_WAIT_BUCKET_EDGES_MS)
        for i, edge in enumerate(RECONCILE_LOCK_WAIT_BUCKET_EDGES_MS):
            if ms < edge:
                index = i
                break
        self.buckets[index] += 1
        if meter.hold_seconds > 0:
            self.hold_seconds_max = max(self.hold_seconds_max, meter.hold_seconds)
            if meter.hold_seconds > RECONCILE_WRITE_TIME_SLO_SECONDS:
                self.hold_slo_violations += 1

    def histogram(self) -> dict:
        edges = RECONCILE_LOCK_WAIT_BUCKET_EDGES_MS
        labels = [f"<{edges[0]}"]
        labels += [f"{edges[i]}-{edges[i + 1]}" for i in range(len(edges) - 1)]
        labels.append(f">={edges[-1]}")
        return {label: count for label, count in zip(labels, self.buckets)}

    @property
    def baseline_seconds(self) -> float:
        """The pass's own per-attempt measurement bias, estimated in band as
        the SMALLEST wait it observed. See the class docstring: the meter
        cannot separate a successful statement's wait from its work, and the
        residue (DML + commit fsync) is near-constant per attempt, so the
        cheapest attempt in a pass is that residue. A pass with a single
        measurement has no way to distinguish bias from signal, so it reports
        no baseline rather than declaring its only sample to be pure bias."""
        if self.min_seconds is None or self.measurements < 2:
            return 0.0
        return self.min_seconds

    def as_summary(self) -> dict:
        """The telemetry contract. `lock_wait_ms` is the pass total (biased
        HIGH — see the class docstring), `lock_wait_ms_net` is that total with
        the measured per-attempt baseline removed, and the histogram is what
        aggregates across passes into the distribution this milestone exists
        to make possible. Thresholds come from the histogram's `>=100`
        buckets, never from the scalar."""
        baseline_ms = int(self.baseline_seconds * 1000)
        total_ms = int(self.total_seconds * 1000)
        return {
            "lock_wait_ms": total_ms,
            "lock_wait_ms_max": int(self.max_seconds * 1000),
            "lock_wait_ms_min": (
                None if self.min_seconds is None else int(self.min_seconds * 1000)
            ),
            # The measured per-attempt bias floor, and the total with it
            # removed. Both are None-free zeros on a pass that measured
            # nothing, exactly like every other field here.
            "lock_wait_ms_baseline_per_attempt": baseline_ms,
            "lock_wait_ms_net": max(0, total_ms - baseline_ms * self.measurements),
            "lock_wait_measurements": self.measurements,
            "lock_wait_histogram_ms": self.histogram(),
            "lock_wait_budget_ms": self.budget_ms_applied,
            "lock_wait_budget_ms_min": self.budget_ms_min,
            "write_hold_ms_max": int(self.hold_seconds_max * 1000),
            "write_hold_slo_seconds": RECONCILE_WRITE_TIME_SLO_SECONDS,
            "write_hold_slo_violations": self.hold_slo_violations,
        }


LOCK_WAIT_EVIDENCE_ATTR = "_crypto_tape_lock_wait_evidence"


def _attach_lock_wait_evidence(
    exc: BaseException,
    accounting: "LockWaitAccounting",
    blocked_seconds: float,
    lock_retry_events: int,
) -> None:
    """BLOCKER-1(a) fix (independent review of this branch): carry the pass's
    lock-wait accounting OUT along the exception, so the abandon path can
    report it.

    The failure this closes: a pass that hit severe contention raised out of
    `_assemble_pass_locked`, was caught by `run_scheduled_reconciliation` and
    returned as `status="db_locked"`, `error="database is locked; pass
    abandoned"` — with EVERY lock-wait field absent, therefore printed as
    `None` by the CLI. That is precisely the tail of the distribution the
    lock-wait gate exists to measure, censored at exactly the passes that
    carry the most information. The overlap and run-row-contention branches
    already report a real (zero) accounting; this makes the abandon path
    report a real (non-zero) one instead of nothing.

    Best-effort by construction: some exception types forbid attribute
    assignment, and losing telemetry must never turn into losing the error."""
    try:
        setattr(exc, LOCK_WAIT_EVIDENCE_ATTR, {
            **accounting.as_summary(),
            "blocked_ms": int(blocked_seconds * 1000),
            "lock_retry_events": lock_retry_events,
        })
    except Exception:  # pragma: no cover - defensive
        pass


def _lock_wait_evidence(exc: BaseException | None) -> dict:
    """The accounting `_attach_lock_wait_evidence` carried out, or an
    all-zero accounting when the exception never reached instrumented code
    (e.g. a blocked read in the caller's own prelude). Zeros with
    `lock_wait_measurements=0` say "nothing was measured", which is a
    different and honest statement from a missing key printed as None."""
    evidence = getattr(exc, LOCK_WAIT_EVIDENCE_ATTR, None) if exc is not None else None
    if isinstance(evidence, dict):
        return dict(evidence)
    return {
        **LockWaitAccounting().as_summary(),
        "blocked_ms": 0,
        "lock_retry_events": 0,
    }


class AdaptiveBatchCostEstimate:
    """CRYPTO-COVERAGE-REPAIR-001 B3 — conservative (bias-HIGH) EWMA estimate
    of write-phase wall-clock cost per token, in seconds. This REPLACES the
    count-based invariant `RECONCILE_BATCH_SIZE` used to be: a fixed token
    count can never be a safety invariant on its own, because the write-lock
    hold it produces is `tokens_in_batch x per-token cost`, and per-token
    cost is host-speed dependent (measured >60x slower on one EVO-class host
    than the dev Mac this repo is usually edited on).

    Never trust a single observation. The estimate starts from an explicit,
    caller-supplied seed (`initial_per_token_cost_seconds` on the call
    site — an UNCALIBRATED placeholder until a real measurement exists on
    the target host; there is deliberately no built-in default, because
    guessing this number is exactly the failure this class exists to
    remove), then updates via EWMA after every committed batch's ACTUAL
    measured wall time. The raw EWMA is inflated by `bias_multiplier`
    before it is ever used to size the next batch, so a run of
    favourable/quiet batches can never creep the estimate down to
    something a single slow batch would then blow through. Deliberately
    asymmetric: the conservative estimate shrinks immediately when a batch
    comes in slower (the raw EWMA is bias-high and any slow sample pulls it
    up hard with `alpha`), and grows back only slowly (`alpha`-weighted
    average of a run of fast samples), and even then only within
    `bias_multiplier` of the true recent average.
    """

    def __init__(
        self,
        initial_per_token_cost_seconds: float,
        *,
        alpha: float = 0.3,
        bias_multiplier: float = 1.5,
    ):
        if initial_per_token_cost_seconds is None or initial_per_token_cost_seconds <= 0:
            raise ValueError(
                "initial_per_token_cost_seconds must be a positive, "
                "explicitly-measured value (UNCALIBRATED placeholders must "
                "be named as such by the caller, never invented here) — "
                f"got {initial_per_token_cost_seconds!r}"
            )
        if not (0.0 < alpha <= 1.0):
            raise ValueError(f"alpha must be in (0, 1], got {alpha!r}")
        if bias_multiplier < 1.0:
            raise ValueError(
                f"bias_multiplier must be >= 1.0 (never bias LOW), got {bias_multiplier!r}"
            )
        self._raw_ewma = float(initial_per_token_cost_seconds)
        self.alpha = alpha
        self.bias_multiplier = bias_multiplier
        self.observations = 0

    @property
    def conservative_estimate_seconds(self) -> float:
        """The number batch sizing must use — always >= the raw EWMA."""
        return self._raw_ewma * self.bias_multiplier

    def observe(self, batch_duration_seconds: float, token_count: int) -> None:
        """Update the raw EWMA from one committed batch's ACTUAL measured
        wall time. `token_count` must be the real number of tokens written
        in that batch (0 is a no-op — a zero-token 'batch' teaches nothing
        about per-token cost and would divide by zero)."""
        if token_count <= 0:
            return
        sample = max(0.0, batch_duration_seconds) / token_count
        self._raw_ewma = self.alpha * sample + (1 - self.alpha) * self._raw_ewma
        self.observations += 1


def next_adaptive_batch_size(
    time_budget_seconds: float,
    cost_estimate: AdaptiveBatchCostEstimate,
    *,
    max_batch_size: int | None = None,
) -> int:
    """B3's batch-sizing DECISION, kept as a pure, independently testable
    function. Primary contract: DO NOT START another token/write group when
    the PREDICTED transaction duration would violate `time_budget_seconds`.

    Returns 0 when even a single-token transaction would violate the budget
    at the current conservative estimate — the caller MUST treat that as a
    terminal `STATUS_UNSAFE_HOST_COST` condition, never silently proceed
    with a batch smaller than 1.

    `max_batch_size`, if given, is a SEPARATE sanity ceiling (B11): it can
    only make the result SMALLER. It never overrides or relaxes the time
    budget — the time budget always dominates."""
    if time_budget_seconds <= 0:
        return 0
    conservative = cost_estimate.conservative_estimate_seconds
    if conservative <= 0 or conservative > time_budget_seconds:
        return 0
    size = max(1, int(time_budget_seconds // conservative))
    if max_batch_size is not None:
        size = min(size, max_batch_size)
    return max(0, size)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator, 4)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _split_backlog_budget(
    recoverable_n: int, writeoff_n: int, room: int,
) -> tuple[int, int]:
    """CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B1 — how much of one
    pass's backlog `room` goes to genuinely recoverable work versus
    write-offs (`RETENTION_LOST`/`MISSING_REQUIRED_INITIAL_STATE`).

    Write-offs are monotone and permanent (scripts/crypto_backlog_
    partition.py:292-297): a token in one of those classes never leaves it,
    so a plain oldest-first ordering over the WHOLE backlog buries
    recoverable work behind them forever once write-offs accumulate (the
    measured shape: 51 RECOVERABLE_NOW tokens at ranks 9,519-12,093 of
    12,100, behind ~9,500 write-offs). But write-offs must still be visited
    EVENTUALLY — until a pass actually runs `compute_survival` on one and
    persists `final=True`, it keeps occupying `unreconciled_backlog`
    forever too, competing for room on every future pass. Reserve a small,
    bounded slice (10%, floor 1 when any exist) for write-offs so they get
    flushed without ever consuming a whole pass; hand any slack in either
    pool to the other so `room` is never left idle.

    Returns `(writeoff_budget, recoverable_budget)`."""
    if room <= 0:
        return 0, 0
    writeoff_budget = min(writeoff_n, max(1, room // 10)) if writeoff_n else 0
    recoverable_budget = room - writeoff_budget
    if recoverable_budget > recoverable_n:
        slack = recoverable_budget - recoverable_n
        recoverable_budget = recoverable_n
        writeoff_budget = min(writeoff_n, writeoff_budget + slack)
    if writeoff_budget > writeoff_n:
        slack = writeoff_budget - writeoff_n
        writeoff_budget = writeoff_n
        recoverable_budget = min(recoverable_n, recoverable_budget + slack)
    return writeoff_budget, recoverable_budget


def _resolve_lock_dir(settings: Settings | None) -> Path:
    """Host-local directory to anchor the reconciliation overlap lock (B4).
    Prefers the directory the sqlite file itself lives in — co-located,
    host-scoped, and stable across process restarts. Falls back to a
    user-private subdirectory of the system temp dir for non-sqlite backends
    and the in-memory `sqlite://` tests use (no file to co-locate with).

    NEW-L3 fix: the fallback used to be the bare system temp dir, which on a
    shared host (AGENTS.md's documented topology) is world-writable —
    `O_NOFOLLOW` blocks a symlink attack but not another local user simply
    pre-creating the lock FILE itself and holding an flock on it, which would
    make this process see permanent `skipped_overlap` for no legitimate
    reason. A per-uid, owner-only (0o700) subdirectory keeps the lock file
    out of the shared, world-writable namespace."""
    s = settings or get_settings()
    try:
        url = make_url(s.database_url)
        if url.get_backend_name() == "sqlite" and url.database:
            return Path(url.database).resolve().parent
    except Exception:  # pragma: no cover - defensive (malformed URL, etc.)
        pass
    private_dir = Path(tempfile.gettempdir()) / f"probability-arena-crypto-tape-{os.getuid()}"
    try:
        private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(private_dir, 0o700)
    except OSError:  # pragma: no cover - defensive
        return Path(tempfile.gettempdir())
    return private_dir


class LockUnavailableError(Exception):
    """CRYPTO-COVERAGE-REPAIR-001 MEDIUM fix: raised by
    `_reconcile_overlap_lock` when the lock FILE itself cannot be opened
    (missing/unwritable lock directory, permission error, etc.) — distinct
    from `yield False` (another pass legitimately holds the lock). Callers
    must turn this into a typed `lock_unavailable` refused status, never let
    it surface as an uncaught traceback."""


@contextmanager
def _reconcile_overlap_lock(lock_dir: Path, chain: str):
    """Bounded, non-blocking, kernel-held (flock) overlap guard (B4) so the
    scheduled reconciler, a manual tape session, and a second concurrent
    instance can never mutate the same chain's reconciliation window at once.

    A coordination-only file — this never touches the database's own locking,
    and it is orthogonal to SQLite's busy_timeout/retry ladder (B5), which
    guards against unrelated writers (MarketOps, the watcher, ...). The kernel
    releases an flock automatically when the holding process dies, so a
    crashed pass can never leave a stale lock behind (no TOCTOU PID file, no
    unbounded wait). Yields True when acquired, False when another pass
    already holds it. Mirrors `app.services.backup._backup_lock`.

    Raises `LockUnavailableError` (not OSError directly) if the lock FILE
    itself cannot be opened — an unwritable/missing DB directory previously
    produced an uncaught traceback here instead of a typed status."""
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - defensive
        pass
    lock_path = lock_dir / RECONCILE_LOCK_FILENAME.format(chain=chain)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise LockUnavailableError(
            f"cannot open overlap lock file {lock_path}: {exc}"
        ) from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - defensive
                pass
    finally:
        os.close(fd)


@dataclass
class CryptoTapeConfig:
    chain: str = "solana"
    default_limit: int = 25
    default_window_hours: int = 48
    lock_dir: Path | None = None  # resolved lazily from settings if None
    # CRYPTO-COVERAGE-REPAIR-001 B5 — how long crypto_price_ticks survives
    # before retention.py prunes it (Settings.crypto_retention_days). Needed
    # by `compute_survival` to tell a token whose 24h evidence is genuinely,
    # permanently GONE apart from one whose evidence window has merely
    # closed but might still be un-reconciled, un-pruned evidence sitting in
    # the DB right now — see `compute_survival`'s finality classification.
    retention_days: int = 7

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "CryptoTapeConfig":
        s = settings or get_settings()
        return cls(
            chain=s.crypto_chain, lock_dir=_resolve_lock_dir(s),
            retention_days=int(getattr(s, "crypto_retention_days", 7)),
        )


@dataclass
class TokenSources:
    """Everything already persisted about one token, loaded once."""

    token: CryptoToken
    pairs: list[CryptoPair]
    ticks: list[CryptoPriceTick]  # ordered by observed_at asc
    assessments: list[CryptoTokenRiskAssessment]  # ordered by created_at asc
    discovery_events: list[CryptoTokenDiscoveryEvent]  # ordered by observed_at asc
    attention: MemeAttentionSnapshot | None  # latest, if the meme lane saw it
    catalyst_count_24h: int


def merged_assessment_flags(assessments: list[CryptoTokenRiskAssessment]) -> dict:
    """Latest-wins merge of persisted risk flags across assessment rows (the
    engine row usually carries the merged provider facts already)."""
    merged: dict = {}
    for row in assessments:
        merged.update(row.flags or {})
    return merged


def extract_creator_address(assessments: list[CryptoTokenRiskAssessment]) -> str | None:
    """Creator/deployer PUBLIC address if any persisted provider payload named
    one. Current providers rarely do — honest absence is the norm."""
    for row in reversed(assessments):
        for source in (row.flags or {}, row.raw_payload or {}):
            for key in ("creator_address", "creator", "deployer"):
                value = source.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


def extract_cohort_counts(assessments: list[CryptoTokenRiskAssessment]) -> dict:
    """{sniper|insider|bundler}_address_count from persisted raw provider
    payloads (SolanaTracker risk shape: {"snipers": {"count": N, ...}})."""
    counts: dict = {}
    for row in assessments:
        raw = row.raw_payload or {}
        for key, field_name in (
            ("snipers", "sniper_address_count"),
            ("insiders", "insider_address_count"),
            ("bundlers", "bundler_address_count"),
        ):
            entry = raw.get(key)
            if isinstance(entry, dict) and isinstance(entry.get("count"), int):
                counts[field_name] = entry["count"]
    return counts


class CryptoLifecycleTapeRecorder:
    """One derived tape assembly pass over already-persisted rows. Session-only
    (no adapter, no HTTP client); dry-run persists nothing."""

    # CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B1/B3 — the typed
    # backlog partition, read straight off `compute_survival`'s own decision
    # surface (see `classify_backlog`). Kept in insertion-priority order:
    # highest-value-first for selection (B1), and iterated in this order
    # wherever a caller needs "recoverable" as one group.
    BACKLOG_CLASS_RECOVERABLE_NOW = "RECOVERABLE_NOW"
    BACKLOG_CLASS_OTHER_TYPED_REASON = "OTHER_TYPED_REASON"
    BACKLOG_CLASS_UNRESOLVABLE_WINDOW_CLOSED = "UNRESOLVABLE_WINDOW_CLOSED"
    BACKLOG_CLASS_NOT_YET_DUE = "NOT_YET_DUE"
    BACKLOG_CLASS_RETENTION_LOST = "RETENTION_LOST"
    BACKLOG_CLASS_MISSING_REQUIRED_INITIAL_STATE = "MISSING_REQUIRED_INITIAL_STATE"
    # Monotone, permanent write-offs: cheap (timestamp/birth-join only, no
    # tick scan) and, once true, never leave the class — the same two
    # classes scripts/crypto_backlog_partition.py:292-305 documents as
    # "written off once and excluded from selection forever".
    BACKLOG_WRITEOFF_CLASSES = frozenset({
        BACKLOG_CLASS_RETENTION_LOST, BACKLOG_CLASS_MISSING_REQUIRED_INITIAL_STATE,
    })

    def __init__(self, config: CryptoTapeConfig | None = None):
        self.config = config or CryptoTapeConfig.from_settings()

    # --- source loading (read-only) ------------------------------------------

    def _universe(
        self, session: Session, limit: int, cutoff: datetime,
        *, oldest_first: bool = False, exclude_final: bool = False,
        max_first_seen_at: datetime | None = None,
    ) -> list[CryptoToken]:
        """Tokens in the window. `oldest_first` inverts the ordering for
        CRYPTO-COVERAGE-REPAIR-001: newest-first truncation drops exactly the
        MATURED tokens (the ones whose horizons have closed and whose evidence
        is about to be pruned), while the newest tokens it keeps have no due
        horizon at all. Oldest-first truncation drops the unmatured tail
        instead, which the next pass picks up anyway because those tokens are
        still inside the window. Default preserves the existing manual-path
        behaviour; only the scheduled reconciler opts in.

        `exclude_final` (CRYPTO-COVERAGE-REPAIR-001 B2 fix) — when True,
        tokens whose survival outcome is already `final` are dropped from
        this query entirely. Without this, a deadline-stopped pass over an
        oldest-first ordering RE-SELECTS THE IDENTICAL HEAD on every
        subsequent pass (nothing about `first_seen_at` changes when a token
        is reconciled), so a deadline stop never makes forward progress and
        the pass also wastes its wall-clock budget re-walking tokens that
        are already final and have nothing left to learn. Left OFF by
        default: the manual/CLI path intentionally keeps reprocessing every
        token in the window on every pass (see
        test_manual_path_still_appends_lifecycle_rows_unchanged); only the
        scheduled reconciler, whose whole point is state-driven selection,
        opts in.

        `max_first_seen_at` (CRYPTO-COVERAGE-REPAIR-001 HIGH-1) — when set,
        drops tokens born AFTER this timestamp from the selection entirely.
        Paired with `SHORTEST_HORIZON_CLOSING_EDGE_MINUTES` this excludes
        tokens too young to have ANY horizon due yet: `compute_survival` only
        sets `final=True` at the 24h+tolerance edge, so under
        `exclude_final=True` alone every token younger than that stays
        selectable and — because the youngest births vastly outnumber a
        20s-deadline pass's throughput — becomes the STRUCTURAL steady state,
        re-selected on every pass forever with nothing to learn. None keeps
        the existing behaviour (manual/CLI path); only the scheduled
        reconciler opts in."""
        order = (
            (CryptoToken.first_seen_at.asc(), CryptoToken.id.asc())
            if oldest_first
            else (CryptoToken.first_seen_at.desc(), CryptoToken.id.desc())
        )
        stmt = select(CryptoToken).where(
            CryptoToken.chain == self.config.chain,
            CryptoToken.first_seen_at >= cutoff,
        )
        if max_first_seen_at is not None:
            stmt = stmt.where(CryptoToken.first_seen_at <= max_first_seen_at)
        if exclude_final:
            stmt = stmt.outerjoin(
                CryptoTokenSurvivalOutcome,
                and_(
                    CryptoTokenSurvivalOutcome.token_address == CryptoToken.token_address,
                    CryptoTokenSurvivalOutcome.chain == CryptoToken.chain,
                ),
            ).where(
                or_(
                    CryptoTokenSurvivalOutcome.id.is_(None),
                    CryptoTokenSurvivalOutcome.final.is_(False),
                )
            ).distinct()
        stmt = stmt.order_by(*order).limit(limit)
        return list(session.execute(stmt).scalars().all())

    def _backlog_rows(self, session: Session, cutoff: datetime):
        """The raw still-open (or never-reconciled) backlog set, each row
        carrying enough to classify it WITHOUT a second round trip: the
        token, and — via an outer join, `None` when no birth event exists
        yet — the birth event's `first_evidence_at`/`initial_liquidity_usd`.
        Oldest-first, since that evidence is closest to being pruned. Shared
        by `classify_backlog` and (indirectly) everything downstream of it,
        so the OUTER-join predicate is written exactly once."""
        return session.execute(
            select(
                CryptoToken.token_address, CryptoToken.first_seen_at,
                CryptoTokenBirthEvent.first_evidence_at,
                CryptoTokenBirthEvent.initial_liquidity_usd,
            )
            .select_from(CryptoToken)
            .outerjoin(
                CryptoTokenSurvivalOutcome,
                and_(
                    CryptoTokenSurvivalOutcome.token_address == CryptoToken.token_address,
                    CryptoTokenSurvivalOutcome.chain == CryptoToken.chain,
                ),
            )
            .outerjoin(
                CryptoTokenBirthEvent,
                and_(
                    CryptoTokenBirthEvent.token_address == CryptoToken.token_address,
                    CryptoTokenBirthEvent.chain == CryptoToken.chain,
                ),
            )
            .where(
                CryptoToken.chain == self.config.chain,
                CryptoToken.first_seen_at < cutoff,
                or_(
                    CryptoTokenSurvivalOutcome.id.is_(None),
                    CryptoTokenSurvivalOutcome.final.is_(False),
                ),
            )
            .order_by(CryptoToken.first_seen_at.asc(), CryptoToken.id.asc())
            .distinct()
        ).all()

    def classify_backlog(
        self, session: Session, cutoff: datetime, *, now: datetime | None = None,
    ) -> dict[str, list[str]]:
        """CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B1/B3 — an EXACT,
        typed partition of the still-open backlog, read straight off
        `compute_survival`'s own decision surface (the same classes
        `scripts/crypto_backlog_partition.py` derives offline). Two classes
        are cheap and monotone WRITE-OFFS (no tick scan — a pure timestamp
        compare or one birth-table lookup); the rest need one batched,
        indexed tick probe to tell apart:

          RETENTION_LOST                   anchor + 36h is already older
                                            than `crypto_retention_days` —
                                            any tick that could have
                                            answered this is pruned.
          MISSING_REQUIRED_INITIAL_STATE   no birth row, or no
                                            `initial_liquidity_usd` — the
                                            survival fraction is
                                            unmeasurable no matter what
                                            evidence exists.
          RECOVERABLE_NOW                  window closed, a real tick sits
                                            inside the 24h tolerance, and
                                            initial liquidity is known — a
                                            pass right now produces a
                                            genuine `survived_24h` answer.
          OTHER_TYPED_REASON               window closed, no 24h tick, but
                                            a tick sits inside the 15m/1h/6h
                                            tolerance — a pass still learns
                                            real sub-horizon labels even
                                            though the 24h answer never
                                            will exist.
          UNRESOLVABLE_WINDOW_CLOSED       window closed, liquidity known,
                                            but NOT ONE tick lands in any
                                            horizon — the pass that visits
                                            this token proves the 24h label
                                            permanently unobtainable
                                            (`compute_survival`'s
                                            `permanently_missing_evidence`,
                                            see B2) the moment it runs, but
                                            has not run yet, so it is not a
                                            write-off UNTIL it does.
          NOT_YET_DUE                      the closing edge has not passed
                                            (should not occur past `cutoff`
                                            in practice — 36h < the 48h
                                            default window — kept for
                                            correctness if the window is
                                            ever configured narrower).

        Anchor approximation: uses the birth event's `first_evidence_at`
        when one already exists, else `CryptoToken.first_seen_at` — the
        same fallback `build_birth_event` itself uses when no discovery
        event/tick predates `first_seen_at`. This is a SELECTION-TIME
        estimate only; `compute_survival` derives the real anchor once a
        pass actually visits the token.

        Returns `{class_name: [token_address, ...]}`, oldest-first within
        each class, in selection-priority order (highest-value-first)."""
        now = now or _now()
        retention_cutoff = now - timedelta(days=self.config.retention_days)
        rows = self._backlog_rows(session, cutoff)

        classes: dict[str, list[str]] = {
            self.BACKLOG_CLASS_RECOVERABLE_NOW: [],
            self.BACKLOG_CLASS_OTHER_TYPED_REASON: [],
            self.BACKLOG_CLASS_UNRESOLVABLE_WINDOW_CLOSED: [],
            self.BACKLOG_CLASS_NOT_YET_DUE: [],
            self.BACKLOG_CLASS_RETENTION_LOST: [],
            self.BACKLOG_CLASS_MISSING_REQUIRED_INITIAL_STATE: [],
        }
        candidates: list[tuple[str, datetime]] = []
        # Tokens with NO birth event yet (the majority of a never-touched
        # backlog) always read `init_liq is None` from the outer join —
        # that is "no birth row exists", not "liquidity is unmeasurable".
        # `build_birth_event` (the function that WOULD create that row) sets
        # `initial_liquidity_usd` from the token's EARLIEST persisted tick
        # (`sources.ticks[0].liquidity_usd`), so treating "no birth row" as
        # an automatic MISSING_REQUIRED_INITIAL_STATE write-off would
        # misclassify almost the entire never-reconciled backlog — real
        # liquidity evidence already sits in `crypto_price_ticks`, simply
        # not yet consolidated. Defer these to a batched first-tick lookup
        # below instead of writing them off on birth-row absence alone.
        needs_liquidity_check: list[tuple[str, datetime]] = []
        for addr, first_seen, first_evidence_at, init_liq in rows:
            has_birth = first_evidence_at is not None or init_liq is not None
            anchor = _aware(first_evidence_at) or _aware(first_seen)
            if anchor is None:
                classes[self.BACKLOG_CLASS_MISSING_REQUIRED_INITIAL_STATE].append(addr)
                continue
            closing_edge = anchor + timedelta(minutes=1440 * (1 + HORIZON_TOLERANCE))
            if closing_edge < retention_cutoff:
                classes[self.BACKLOG_CLASS_RETENTION_LOST].append(addr)
                continue
            if has_birth:
                if init_liq is None:
                    classes[self.BACKLOG_CLASS_MISSING_REQUIRED_INITIAL_STATE].append(addr)
                    continue
                candidates.append((addr, anchor))
            else:
                needs_liquidity_check.append((addr, anchor))

        if needs_liquidity_check:
            conn = session.connection()
            conn.exec_driver_sql(
                "CREATE TEMP TABLE IF NOT EXISTS _crypto_backlog_liq_probe "
                "(addr TEXT PRIMARY KEY)"
            )
            conn.exec_driver_sql("DELETE FROM _crypto_backlog_liq_probe")
            conn.execute(
                text("INSERT INTO _crypto_backlog_liq_probe (addr) VALUES (:addr)"),
                [{"addr": addr} for addr, _ in needs_liquidity_check],
            )
            try:
                # Earliest tick per token (mirrors `build_birth_event`'s
                # `sources.ticks[0]`, which orders by `observed_at, id`) —
                # liquidity known iff that specific tick has a value.
                liq_rows = conn.execute(text(
                    "SELECT k.token_address, k.liquidity_usd "
                    "FROM crypto_price_ticks k "
                    "JOIN ("
                    "  SELECT token_address, MIN(observed_at) AS first_at "
                    "  FROM crypto_price_ticks "
                    "  WHERE chain = :chain "
                    "    AND token_address IN "
                    "      (SELECT addr FROM _crypto_backlog_liq_probe) "
                    "  GROUP BY token_address"
                    ") f ON f.token_address = k.token_address "
                    "     AND f.first_at = k.observed_at "
                    "WHERE k.chain = :chain"
                ), {"chain": self.config.chain}).fetchall()
            finally:
                conn.exec_driver_sql(
                    "DROP TABLE IF EXISTS _crypto_backlog_liq_probe"
                )
            liq_known = {addr for addr, liq in liq_rows if liq is not None}
            for addr, anchor in needs_liquidity_check:
                if addr in liq_known:
                    candidates.append((addr, anchor))
                else:
                    classes[self.BACKLOG_CLASS_MISSING_REQUIRED_INITIAL_STATE].append(addr)

        if not candidates:
            return classes
        # Restore oldest-first ordering: the liquidity-check deferral above
        # appends its candidates after the birth-backed ones, breaking the
        # otherwise-oldest-first order `_backlog_rows` provides.
        candidates.sort(key=lambda c: c[1])

        # One batched, indexed tick probe over a session-temp table — never
        # one round trip per candidate. Mirrors
        # scripts/crypto_backlog_partition.py's approach exactly (that
        # script uses an attached :memory: db; a session-scoped TEMP TABLE
        # does the same job inside an already-open ORM session/connection).
        conn = session.connection()
        conn.exec_driver_sql(
            "CREATE TEMP TABLE IF NOT EXISTS _crypto_backlog_anchor_probe "
            "(addr TEXT PRIMARY KEY, a TEXT)"
        )
        conn.exec_driver_sql("DELETE FROM _crypto_backlog_anchor_probe")
        conn.execute(
            text(
                "INSERT INTO _crypto_backlog_anchor_probe (addr, a) "
                "VALUES (:addr, :a)"
            ),
            [{"addr": addr, "a": _iso(anchor)} for addr, anchor in candidates],
        )
        try:
            hits_24h: set[str] = set()
            hits_sub_horizon: set[str] = set()
            for label, minutes in HORIZONS:
                tol = minutes * HORIZON_TOLERANCE
                rows_h = conn.execute(
                    text(
                        "SELECT DISTINCT p.addr FROM "
                        "_crypto_backlog_anchor_probe p "
                        "JOIN crypto_price_ticks k ON k.chain = :chain "
                        "  AND k.token_address = p.addr "
                        "WHERE k.observed_at > p.a "
                        f"  AND k.observed_at >= datetime(p.a, "
                        f"    '+{minutes - tol} minutes') "
                        f"  AND k.observed_at <= datetime(p.a, "
                        f"    '+{minutes + tol} minutes')"
                        + ("  AND k.liquidity_usd IS NOT NULL" if label == "24h" else "")
                    ),
                    {"chain": self.config.chain},
                ).fetchall()
                hit_set = hits_24h if label == "24h" else hits_sub_horizon
                hit_set.update(r[0] for r in rows_h)
        finally:
            conn.exec_driver_sql(
                "DROP TABLE IF EXISTS _crypto_backlog_anchor_probe"
            )

        for addr, anchor in candidates:
            closing_edge = anchor + timedelta(minutes=1440 * (1 + HORIZON_TOLERANCE))
            if now < closing_edge:
                classes[self.BACKLOG_CLASS_NOT_YET_DUE].append(addr)
            elif addr in hits_24h:
                classes[self.BACKLOG_CLASS_RECOVERABLE_NOW].append(addr)
            elif addr in hits_sub_horizon:
                classes[self.BACKLOG_CLASS_OTHER_TYPED_REASON].append(addr)
            else:
                classes[self.BACKLOG_CLASS_UNRESOLVABLE_WINDOW_CLOSED].append(addr)
        return classes

    def recoverable_backlog_summary(
        self, session: Session, cutoff: datetime, *, now: datetime | None = None,
        classes: dict[str, list[str]] | None = None,
    ) -> dict:
        """CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B3 — the
        RECOVERABLE frontier, as opposed to the raw backlog-row frontier
        `oldest_unreconciled_first_seen_at` reports. That raw frontier
        measures the oldest BACKLOG ROW regardless of whether anything can
        still be learned from it; once any `RETENTION_LOST`/
        `MISSING_REQUIRED_INITIAL_STATE` write-off sits in the backlog —
        which, once a pass ever falls behind by more than
        `crypto_retention_days`, it always will — that row is a permanent
        write-off and the raw frontier can NEVER recover, even on a
        perfectly healthy pass: "alarm always on" carries the same
        information as no alarm at all, exactly the failure
        `backlog_expiring` exists to avoid. Reuses `classify_backlog`
        (never re-derives the same predicate with different code).

        `classes` (CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R1) — an
        ALREADY-COMPUTED partition from `classify_backlog`, for callers that
        also call `unreconciled_backlog` in the same pass and would
        otherwise pay for the identical classification twice. Must have been
        derived from the SAME `session`/`cutoff`/`now`; passing a partition
        derived from anything else silently reports a frontier for a
        different instant. `None` (the default) classifies here, keeping
        every existing caller byte-for-byte unchanged."""
        now = now or _now()
        if classes is None:
            classes = self.classify_backlog(session, cutoff, now=now)
        writeoff_count = {
            cls: len(classes.get(cls, [])) for cls in self.BACKLOG_WRITEOFF_CLASSES
        }
        recoverable_addrs = [
            addr for cls, addrs in classes.items()
            if cls not in self.BACKLOG_WRITEOFF_CLASSES
            for addr in addrs
        ]
        result: dict = {
            "recoverable_backlog_count": len(recoverable_addrs),
            "oldest_recoverable_due_at": None,
            "oldest_recoverable_age_seconds": None,
            "recoverable_at_retention_risk": 0,
            "writeoff_count": writeoff_count,
        }
        if not recoverable_addrs:
            return result
        rows = session.execute(
            select(CryptoToken.token_address, CryptoToken.first_seen_at).where(
                CryptoToken.chain == self.config.chain,
                CryptoToken.token_address.in_(recoverable_addrs),
            )
        ).all()
        retention_cutoff = now - timedelta(days=self.config.retention_days)
        risk_cutoff = retention_cutoff + timedelta(hours=RECONCILER_CADENCE_HOURS)
        oldest_first_seen: datetime | None = None
        at_risk = 0
        for _, first_seen in rows:
            anchor = _aware(first_seen)
            if anchor is None:
                continue
            if oldest_first_seen is None or anchor < oldest_first_seen:
                oldest_first_seen = anchor
            closing_edge = anchor + timedelta(minutes=1440 * (1 + HORIZON_TOLERANCE))
            if closing_edge < risk_cutoff:
                at_risk += 1
        if oldest_first_seen is not None:
            result["oldest_recoverable_due_at"] = oldest_first_seen.isoformat()
            result["oldest_recoverable_age_seconds"] = (
                now - oldest_first_seen
            ).total_seconds()
        result["recoverable_at_retention_risk"] = at_risk
        return result

    def unreconciled_backlog(
        self, session: Session, cutoff: datetime, *, limit: int,
        now: datetime | None = None,
        classes: dict[str, list[str]] | None = None,
    ) -> list[CryptoToken]:
        """Tokens OLDER than the window whose outcome is still not final —
        including tokens that were NEVER reconciled at all (no outcome row
        yet). An INNER join here is a silent data-loss bug: a token that no
        pass has ever reached has no outcome row, so once it ages out of the
        window an INNER join makes it invisible to both this query and
        `backlog_size` forever, permanently capping how much pre-existing
        backlog can ever be recovered. The OUTER join fixes that.

        Window-driven selection alone is lossy: the window carries only
        `window_hours - closing_edge` of slack (12h at the shipped defaults),
        so two missed passes — host down, lock contention, a flag toggle —
        push a cohort out of the window permanently, and the same gap means the
        pre-existing backlog is never reconciled at first enablement. Selection
        must therefore be driven by STATE (is this outcome still open, or
        nonexistent?) and not only by recency.

        CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B1 — a naive
        oldest-first `LIMIT` over the raw backlog buries genuinely
        recoverable work behind permanent write-offs (measured: 51
        `RECOVERABLE_NOW` tokens at ranks 9,519-12,093 of 12,100, behind
        ~9,500 `RETENTION_LOST`/`MISSING_REQUIRED_INITIAL_STATE` rows that
        can never yield a real answer). `classify_backlog` partitions the
        FULL backlog — not just the head a naive `LIMIT` would have taken —
        BEFORE any per-pass budget is applied, and `_split_backlog_budget`
        reserves a small, bounded slice of `limit` for write-offs so they
        still get flushed (memorialised as `final=True`, and therefore
        excluded from every future call) without ever consuming a whole
        pass's budget.

        `classes` (CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R1) — see
        `recoverable_backlog_summary`'s note of the same name. `run_once`
        calls BOTH this method and that one on every backlog-enabled pass,
        and each used to classify the whole backlog independently: two
        identical full-backlog scans per pass, measured at 1.92 s + 1.94 s
        cold and 4.02 s + 3.21 s warm on a fast Mac. That cost is not
        merely wasted, it is UNBOUNDED with respect to
        `max_duration_seconds`: the deadline is anchored at pass start
        (`_assemble_pass_locked`'s `deadline = started + ...`) but only
        CHECKED between batches, so every second spent here is subtracted
        from the batch loop's budget before the first token is reconciled.
        Threading one partition through halves that prelude. `None` (the
        default) classifies here, keeping every existing caller
        byte-for-byte unchanged."""
        if classes is None:
            classes = self.classify_backlog(session, cutoff, now=now)
        writeoff_addrs = [
            addr
            for cls in (
                self.BACKLOG_CLASS_RETENTION_LOST,
                self.BACKLOG_CLASS_MISSING_REQUIRED_INITIAL_STATE,
            )
            for addr in classes.get(cls, [])
        ]
        recoverable_addrs = [
            addr for cls, addrs in classes.items()
            if cls not in self.BACKLOG_WRITEOFF_CLASSES
            for addr in addrs
        ]
        writeoff_budget, recoverable_budget = _split_backlog_budget(
            len(recoverable_addrs), len(writeoff_addrs), limit
        )
        selected_addrs = (
            recoverable_addrs[:recoverable_budget] + writeoff_addrs[:writeoff_budget]
        )
        if not selected_addrs:
            return []
        rows = session.execute(
            select(CryptoToken).where(
                CryptoToken.chain == self.config.chain,
                CryptoToken.token_address.in_(selected_addrs),
            )
        ).scalars().all()
        by_addr = {t.token_address: t for t in rows}
        return [by_addr[addr] for addr in selected_addrs if addr in by_addr]

    def backlog_size(self, session: Session, cutoff: datetime) -> int:
        """How many still-open (or never-reconciled) outcomes sit outside the
        window. Reported so a shortfall is visible rather than inferred. See
        `unreconciled_backlog` for why this must be an OUTER join."""
        return int(session.execute(
            select(func.count(func.distinct(CryptoToken.id)))
            .select_from(CryptoToken)
            .outerjoin(
                CryptoTokenSurvivalOutcome,
                and_(
                    CryptoTokenSurvivalOutcome.token_address == CryptoToken.token_address,
                    CryptoTokenSurvivalOutcome.chain == CryptoToken.chain,
                ),
            )
            .where(
                CryptoToken.chain == self.config.chain,
                CryptoToken.first_seen_at < cutoff,
                or_(
                    CryptoTokenSurvivalOutcome.id.is_(None),
                    CryptoTokenSurvivalOutcome.final.is_(False),
                ),
            )
        ).scalar() or 0)

    def oldest_unreconciled_first_seen_at(
        self, session: Session, cutoff: datetime
    ) -> datetime | None:
        """NEW-HIGH-2 fix (second re-review, convergence lens): the
        FRONTIER — the age of the single oldest still-open (or
        never-reconciled) token — is the metric that actually distinguishes
        a healthy pass from NEW-BLOCKING-2's partial starvation. Every
        pre-existing observable (`backlog_size` draining, a confident
        nonzero `backlog_processed`, `outcomes_updated`) can look identical
        in both cases while this number silently marches toward
        `crypto_retention_days * 24` in the starvation case. Same
        OUTER-join predicate as `backlog_size` (see its docstring for why an
        INNER join is a silent data-loss bug), just `MIN(first_seen_at)`
        instead of `COUNT`. `None` when there is nothing unreconciled at
        all."""
        return session.execute(
            select(func.min(CryptoToken.first_seen_at))
            .select_from(CryptoToken)
            .outerjoin(
                CryptoTokenSurvivalOutcome,
                and_(
                    CryptoTokenSurvivalOutcome.token_address == CryptoToken.token_address,
                    CryptoTokenSurvivalOutcome.chain == CryptoToken.chain,
                ),
            )
            .where(
                CryptoToken.chain == self.config.chain,
                CryptoToken.first_seen_at < cutoff,
                or_(
                    CryptoTokenSurvivalOutcome.id.is_(None),
                    CryptoTokenSurvivalOutcome.final.is_(False),
                ),
            )
        ).scalar()

    def universe_size(
        self, session: Session, cutoff: datetime, *, exclude_final: bool = False,
        max_first_seen_at: datetime | None = None,
    ) -> int:
        """How many tokens the window actually holds, independent of any limit.
        Without this a truncated pass is indistinguishable from a complete one,
        which is the silent-under-reconciliation class this milestone removes.

        `exclude_final` MUST mirror whatever value the caller passes to
        `_universe`/`run_once` — otherwise a fully-reconciled (all-final)
        window counts as "work remains" against a selection query that has
        already correctly excluded that work, and every subsequent pass
        reports a false `truncated`. `max_first_seen_at` must likewise mirror
        `_universe`'s age exclusion (HIGH-1), for the same reason."""
        count_col = (
            func.count(func.distinct(CryptoToken.id))
            if exclude_final else func.count()
        )
        stmt = select(count_col).select_from(CryptoToken).where(
            CryptoToken.chain == self.config.chain,
            CryptoToken.first_seen_at >= cutoff,
        )
        if max_first_seen_at is not None:
            stmt = stmt.where(CryptoToken.first_seen_at <= max_first_seen_at)
        if exclude_final:
            stmt = stmt.outerjoin(
                CryptoTokenSurvivalOutcome,
                and_(
                    CryptoTokenSurvivalOutcome.token_address == CryptoToken.token_address,
                    CryptoTokenSurvivalOutcome.chain == CryptoToken.chain,
                ),
            ).where(
                or_(
                    CryptoTokenSurvivalOutcome.id.is_(None),
                    CryptoTokenSurvivalOutcome.final.is_(False),
                )
            )
        return int(session.execute(stmt).scalar() or 0)

    def _load_sources(self, session: Session, token: CryptoToken, now: datetime) -> TokenSources:
        address = token.token_address
        pairs = list(session.execute(
            select(CryptoPair).where(
                CryptoPair.chain == self.config.chain,
                CryptoPair.base_token_address == address,
            ).order_by(CryptoPair.id)
        ).scalars().all())
        ticks = list(session.execute(
            select(CryptoPriceTick).where(
                CryptoPriceTick.chain == self.config.chain,
                CryptoPriceTick.token_address == address,
            ).order_by(CryptoPriceTick.observed_at, CryptoPriceTick.id)
        ).scalars().all())
        assessments = list(session.execute(
            select(CryptoTokenRiskAssessment).where(
                CryptoTokenRiskAssessment.chain == self.config.chain,
                CryptoTokenRiskAssessment.token_address == address,
            ).order_by(CryptoTokenRiskAssessment.created_at, CryptoTokenRiskAssessment.id)
        ).scalars().all())
        events = list(session.execute(
            select(CryptoTokenDiscoveryEvent).where(
                CryptoTokenDiscoveryEvent.chain == self.config.chain,
                CryptoTokenDiscoveryEvent.token_address == address,
            ).order_by(CryptoTokenDiscoveryEvent.observed_at, CryptoTokenDiscoveryEvent.id)
        ).scalars().all())
        attention = session.execute(
            select(MemeAttentionSnapshot)
            .where(MemeAttentionSnapshot.token_address == address)
            .order_by(MemeAttentionSnapshot.id.desc())
        ).scalars().first()
        catalyst_count = session.execute(
            select(func.count()).select_from(MemeCatalystEvent).where(
                MemeCatalystEvent.subject_ref == address,
                MemeCatalystEvent.observed_at >= now - timedelta(hours=24),
            )
        ).scalar() or 0
        return TokenSources(
            token=token, pairs=pairs, ticks=ticks, assessments=assessments,
            discovery_events=events, attention=attention,
            catalyst_count_24h=catalyst_count,
        )

    # --- birth event ----------------------------------------------------------

    def build_birth_event(self, sources: TokenSources, now: datetime) -> CryptoTokenBirthEvent:
        token = sources.token
        missing: list[str] = []
        first_event = sources.discovery_events[0] if sources.discovery_events else None
        first_tick = sources.ticks[0] if sources.ticks else None

        evidence_times = [
            t for t in (
                _aware(token.first_seen_at),
                _aware(first_event.observed_at) if first_event else None,
                _aware(first_tick.observed_at) if first_tick else None,
            ) if t is not None
        ]
        first_evidence_at = min(evidence_times) if evidence_times else None

        launch_source = None
        if first_event is not None:
            launch_source = f"{first_event.source}:{first_event.event_type}"
        else:
            missing.append("launch_source")

        # earliest pair by chain-side creation time, falling back to first seen
        first_pair = None
        if sources.pairs:
            first_pair = min(
                sources.pairs,
                key=lambda p: _aware(p.pair_created_at) or _aware(p.first_seen_at) or now,
            )
        else:
            missing.append("pair")

        creator = extract_creator_address(sources.assessments)
        if creator is None:
            missing.append("creator_address")
        flags = merged_assessment_flags(sources.assessments)
        mint_auth = flags.get("mint_authority_enabled")
        freeze_auth = flags.get("freeze_authority_enabled")
        if mint_auth is None:
            missing.append("mint_authority")
        if freeze_auth is None:
            missing.append("freeze_authority")

        metadata = dict(token.token_metadata or {})
        if sources.attention is not None:
            metadata.setdefault("has_social", sources.attention.has_social)
            metadata.setdefault("social_links_count", sources.attention.social_links_count)
        if not metadata:
            missing.append("metadata_links")

        if first_tick is None:
            missing.append("initial_market_state")
        bonding = None
        if first_pair is not None and first_pair.dex_id:
            bonding = (
                BONDING_LAUNCHPAD
                if first_pair.dex_id.lower() in LAUNCHPAD_DEXES
                else BONDING_AMM
            )
        else:
            missing.append("bonding_curve_state")

        provenance = {
            "derived_from": "persisted surveillance rows only (no external call)",
            "crypto_token_id": token.id,
            "discovery_event_ids": [e.id for e in sources.discovery_events[:10]],
            "first_tick_id": first_tick.id if first_tick else None,
            "risk_assessment_ids": [a.id for a in sources.assessments[-3:]],
            "attention_snapshot_id": sources.attention.id if sources.attention else None,
        }
        return CryptoTokenBirthEvent(
            chain=self.config.chain,
            token_address=token.token_address,
            symbol=token.symbol,
            name=token.name,
            observed_at=now,
            first_evidence_at=first_evidence_at,
            launch_source=launch_source,
            first_pair_address=first_pair.pair_address if first_pair else None,
            first_dex_id=first_pair.dex_id if first_pair else None,
            pair_created_at=_aware(first_pair.pair_created_at) if first_pair else None,
            creator_address=creator,
            mint_authority_enabled=mint_auth,
            freeze_authority_enabled=freeze_auth,
            metadata_links=metadata or None,
            initial_price_usd=first_tick.price_usd if first_tick else None,
            initial_liquidity_usd=first_tick.liquidity_usd if first_tick else None,
            initial_volume_24h_usd=first_tick.volume_24h_usd if first_tick else None,
            initial_market_cap=first_tick.market_cap if first_tick else None,
            initial_fdv=first_tick.fdv if first_tick else None,
            bonding_curve_state=bonding,
            provenance=provenance,
            missing_info=missing or None,
            raw_payload=(first_event.raw_payload if first_event else None),
            created_at=now,
        )

    # --- lifecycle snapshot ---------------------------------------------------

    def build_snapshot(
        self, sources: TokenSources, birth: CryptoTokenBirthEvent | None, now: datetime
    ) -> CryptoTokenLifecycleSnapshot:
        missing: list[str] = []
        coverage: list[str] = []

        # latest tick per pair; best pair = deepest liquidity
        latest_by_pair: dict[str, CryptoPriceTick] = {}
        for tick in sources.ticks:
            if tick.pair_address:
                latest_by_pair[tick.pair_address] = tick
        best_tick = max(
            latest_by_pair.values(), key=lambda t: t.liquidity_usd or 0, default=None
        ) or (sources.ticks[-1] if sources.ticks else None)
        if best_tick is not None:
            coverage.append("price_tick")
        else:
            missing.append("market_state")

        latest_assessment = sources.assessments[-1] if sources.assessments else None
        flags = merged_assessment_flags(sources.assessments)
        if latest_assessment is not None:
            coverage.append(f"risk:{latest_assessment.provider}")
            for provider_name in latest_assessment.provider_names or []:
                coverage.append(f"risk:{provider_name}")
        else:
            missing.append("risk_assessment")
        for key in ("top10_holder_pct", "sniper_pct", "insider_pct", "bundler_pct"):
            if key not in flags:
                missing.append(key)

        if sources.attention is not None:
            coverage.append("attention")
        else:
            missing.append("attention_snapshot")

        first_evidence = _aware(birth.first_evidence_at) if birth is not None else None
        age_seconds = (
            max(0, int((now - first_evidence).total_seconds()))
            if first_evidence is not None else None
        )
        tick_age = (
            max(0, int((now - _aware(best_tick.observed_at)).total_seconds()))
            if best_tick is not None else None
        )
        risk_score = risk_level = risk_reasons = None
        if latest_assessment is not None:
            risk_score = (
                latest_assessment.composite_risk_score
                if latest_assessment.composite_risk_score is not None
                else latest_assessment.risk_score
            )
            risk_level = (
                latest_assessment.composite_risk_level or latest_assessment.risk_level
            )
            risk_reasons = latest_assessment.risk_reasons

        return CryptoTokenLifecycleSnapshot(
            birth_event_id=birth.id if birth is not None else None,
            chain=self.config.chain,
            token_address=sources.token.token_address,
            observed_at=now,
            token_age_seconds=age_seconds,
            price_usd=best_tick.price_usd if best_tick else None,
            liquidity_usd=best_tick.liquidity_usd if best_tick else None,
            volume_5m_usd=best_tick.volume_5m_usd if best_tick else None,
            volume_1h_usd=best_tick.volume_1h_usd if best_tick else None,
            volume_24h_usd=best_tick.volume_24h_usd if best_tick else None,
            market_cap=best_tick.market_cap if best_tick else None,
            fdv=best_tick.fdv if best_tick else None,
            holder_count=flags.get("holder_count"),
            top10_holder_pct=flags.get("top10_holder_pct"),
            sniper_pct=flags.get("sniper_pct"),
            insider_pct=flags.get("insider_pct"),
            bundler_pct=flags.get("bundler_pct"),
            creator_pct=flags.get("creator_pct"),
            risk_score=risk_score,
            risk_level=risk_level,
            risk_reasons=risk_reasons,
            boost_amount=(
                sources.attention.boost_amount if sources.attention else None
            ),
            attention_score=(
                sources.attention.attention_score if sources.attention else None
            ),
            has_social=(sources.attention.has_social if sources.attention else None),
            social_links_count=(
                sources.attention.social_links_count if sources.attention else None
            ),
            catalyst_count_24h=sources.catalyst_count_24h,
            pair_count=len(sources.pairs),
            best_pair_address=best_tick.pair_address if best_tick else None,
            best_dex_id=(
                (best_tick.raw_payload or {}).get("dex_id") if best_tick else None
            ),
            volume_to_liquidity_24h=_ratio(
                best_tick.volume_24h_usd if best_tick else None,
                best_tick.liquidity_usd if best_tick else None,
            ),
            single_venue=(len(sources.pairs) == 1) if sources.pairs else None,
            source_tick_id=best_tick.id if best_tick else None,
            source_risk_assessment_id=(
                latest_assessment.id if latest_assessment else None
            ),
            source_attention_snapshot_id=(
                sources.attention.id if sources.attention else None
            ),
            source_tick_age_seconds=tick_age,
            provider_coverage=sorted(set(coverage)) or None,
            missing_info=missing or None,
            created_at=now,
        )

    # --- actor observation ----------------------------------------------------

    def build_actor_observation(
        self, sources: TokenSources, birth: CryptoTokenBirthEvent | None, now: datetime
    ) -> CryptoTokenActorObservation:
        missing: list[str] = []
        obs_sources: list[str] = []
        flags = merged_assessment_flags(sources.assessments)
        creator = extract_creator_address(sources.assessments)
        if creator is None:
            missing.append("creator_address")
        counts = extract_cohort_counts(sources.assessments)
        for field_name in (
            "sniper_address_count", "insider_address_count", "bundler_address_count"
        ):
            if field_name not in counts:
                missing.append(field_name)
        # no configured source exposes an ordered first-buyer list today —
        # the column is an honest placeholder until one legitimately does
        missing.append("first_buyer_addresses")
        if sources.assessments:
            obs_sources.append("crypto_token_risk_assessments")
        holder_distribution = {
            key: flags[key]
            for key in (
                "holder_count", "top10_holder_pct", "sniper_pct",
                "insider_pct", "bundler_pct", "creator_pct",
            )
            if key in flags
        }
        if not holder_distribution:
            missing.append("holder_distribution")
        return CryptoTokenActorObservation(
            birth_event_id=birth.id if birth is not None else None,
            chain=self.config.chain,
            token_address=sources.token.token_address,
            observed_at=now,
            creator_address=creator,
            creator_holding_pct=flags.get("creator_pct"),
            first_buyer_addresses=None,
            sniper_address_count=counts.get("sniper_address_count"),
            insider_address_count=counts.get("insider_address_count"),
            bundler_address_count=counts.get("bundler_address_count"),
            repeated_cohort_ref=None,          # cross-token cohorting: later milestone
            known_creator_cluster_ref=None,    # creator clustering: later milestone
            holder_distribution=holder_distribution or None,
            observation_sources=obs_sources or None,
            missing_info=missing or None,
            created_at=now,
        )

    # --- survival outcome -----------------------------------------------------

    def compute_survival(
        self, birth: CryptoTokenBirthEvent, sources: TokenSources, now: datetime
    ) -> dict:
        """Deterministic survival labels from the token's persisted trajectory.
        Pure computation; None = not yet measurable or source gap."""
        anchor = _aware(birth.first_evidence_at)
        initial_liquidity = birth.initial_liquidity_usd
        details: dict = {"horizons": {}, "anchor": anchor.isoformat() if anchor else None}
        labels: dict = {label: None for label in (
            "survived_15m", "survived_1h", "survived_6h", "survived_24h",
            "liquidity_removed", "dead_volume", "severe_risk",
            "graduated_or_migrated", "provider_gap",
        )}
        if anchor is None:
            details["reason"] = "no first-evidence timestamp"
            labels["provider_gap"] = True
            return {"labels": labels, "details": details, "final": False}

        later = [
            t for t in sources.ticks
            if _aware(t.observed_at) is not None and _aware(t.observed_at) > anchor
        ]
        gap_reasons: list[str] = []

        # per-horizon survival: nearest later observation inside tolerance
        for label, minutes in HORIZONS:
            key = f"survived_{label}"
            target = anchor + timedelta(minutes=minutes)
            tolerance = timedelta(minutes=minutes * HORIZON_TOLERANCE)
            if now < target - tolerance:
                details["horizons"][label] = "not_yet_mature"
                continue
            candidates = [
                t for t in later if abs(_aware(t.observed_at) - target) <= tolerance
            ]
            if not candidates:
                details["horizons"][label] = "no_observation_in_window"
                gap_reasons.append(f"no_tick_at_{label}")
                continue
            nearest = min(candidates, key=lambda t: abs(_aware(t.observed_at) - target))
            if initial_liquidity and nearest.liquidity_usd is not None:
                survived = (
                    nearest.liquidity_usd
                    >= SURVIVAL_LIQUIDITY_FRACTION * initial_liquidity
                )
                labels[key] = bool(survived)
                details["horizons"][label] = {
                    "tick_id": nearest.id,
                    "liquidity_usd": nearest.liquidity_usd,
                    "initial_liquidity_usd": initial_liquidity,
                }
            else:
                details["horizons"][label] = "liquidity_unmeasurable"
                gap_reasons.append(f"liquidity_unmeasurable_at_{label}")

        # liquidity_removed: any later observation below the survival fraction
        if initial_liquidity and later:
            removed_tick = next(
                (
                    t for t in later
                    if t.liquidity_usd is not None
                    and t.liquidity_usd < SURVIVAL_LIQUIDITY_FRACTION * initial_liquidity
                ),
                None,
            )
            labels["liquidity_removed"] = removed_tick is not None
            if removed_tick is not None:
                details["liquidity_removed_tick_id"] = removed_tick.id
        elif not initial_liquidity:
            gap_reasons.append("no_initial_liquidity")

        # dead_volume: latest observation at >=6h after birth with negligible 24h volume
        matured = [t for t in later if _aware(t.observed_at) >= anchor + timedelta(hours=6)]
        if matured:
            last = matured[-1]
            if last.volume_24h_usd is not None:
                labels["dead_volume"] = last.volume_24h_usd < DEAD_VOLUME_24H_USD
                details["dead_volume_basis"] = {
                    "tick_id": last.id, "volume_24h_usd": last.volume_24h_usd,
                }
        elif now >= anchor + timedelta(hours=6):
            gap_reasons.append("no_tick_after_6h")

        # severe_risk: any assessment after birth landed severe
        post_birth = [
            a for a in sources.assessments
            if _aware(a.created_at) is not None and _aware(a.created_at) >= anchor
        ]
        if post_birth:
            labels["severe_risk"] = any(
                (a.composite_risk_level or a.risk_level or "").lower() == "severe"
                for a in post_birth
            )
        else:
            gap_reasons.append("no_risk_assessment")

        # graduated_or_migrated: launchpad-born token later seen on a non-launchpad venue
        if birth.first_dex_id and birth.first_dex_id.lower() in LAUNCHPAD_DEXES:
            labels["graduated_or_migrated"] = any(
                (p.dex_id or "").lower() not in LAUNCHPAD_DEXES and p.dex_id
                for p in sources.pairs
            )
        elif not birth.first_dex_id:
            gap_reasons.append("launch_venue_unknown")
        # else: born on an AMM — graduation does not apply; stays None

        provider_backed = any(
            a.provider_names for a in sources.assessments
        ) or any(a.provider not in ("risk-engine", "mock") for a in sources.assessments)
        if not provider_backed:
            gap_reasons.append("no_provider_backed_risk_read")
        labels["provider_gap"] = bool(gap_reasons)
        if gap_reasons:
            details["gap_reasons"] = sorted(set(gap_reasons))

        # CRYPTO-COVERAGE-REPAIR-001 B5 — finality classification.
        #
        # Before this fix, `final` was AGE ALONE: `now >= anchor + 36h`, full
        # stop. That conflates three genuinely different situations under
        # one flag, and because `exclude_final=True` is exactly what the
        # scheduled reconciler uses to select which tokens to (re)visit, a
        # wrongly-final token is NEVER looked at again:
        #   * OBSERVED TERMINAL — `survived_24h` actually got a real answer.
        #     The measurement is done; `final=True` is correct.
        #   * RETENTION-LOST — the 24h window closed, no answer was ever
        #     recorded, AND even the latest possible qualifying tick (one
        #     landing right at the closing edge, `anchor + 36h`) would by now
        #     be older than `crypto_retention_days` and therefore already
        #     pruned by `retention.py`. This evidence is truly, permanently
        #     gone — `final=True` is the honest (if unhappy) answer, not a
        #     guess, and is worth its own classification so a reader can
        #     tell "measured: no" apart from "we gave up".
        #   * PERMANENTLY-MISSING-EVIDENCE (CRYPTO-BACKLOG-SELECTION-AND-
        #     OPERATOR-PATH-001 B2 fix, replacing a disproved
        #     "still_recoverable" branch below) — the 24h window closed, no
        #     answer was recorded, and the closing edge has NOT yet aged past
        #     `crypto_retention_days`. Before this fix `final` stayed False
        #     here on the theory that "a future pass could still recover
        #     it" — but `_load_sources` (this call's own caller) already
        #     loaded EVERY tick this token will ever have as of `now`, with
        #     no time filter at all, and every tick writer sets
        #     `observed_at=now()` at write time. Once `now >= closing_edge`,
        #     any tick written AFTER this moment has `observed_at > now >=
        #     closing_edge`, so it can never land inside the 24h tolerance
        #     window either — the window is BEHIND `now`, not ahead of it.
        #     That means this exact call has already exhaustively searched
        #     every tick that could ever satisfy `survived_24h` and found
        #     none: the label is PROVEN unobtainable this instant, not
        #     merely "not yet reconciled". Re-selecting it (the old
        #     `final=False` behaviour) burns budget re-deriving the
        #     identical negative result forever — it never once produces a
        #     different answer. `final=True` here is therefore honest, not
        #     a guess; `finality="permanently_missing_evidence"` reuses the
        #     existing per-horizon missing-cause vocabulary
        #     (`details["horizons"]["24h"] == "no_observation_in_window"`,
        #     set above) to say WHY, and is kept distinct from
        #     `retention_lost` (evidence was pruned) and `observed_terminal`
        #     (a real answer exists) so a reader can always tell which of
        #     the three actually happened.
        closing_edge = anchor + timedelta(minutes=1440 * (1 + HORIZON_TOLERANCE))
        window_closed = now >= closing_edge
        if not window_closed:
            final = False
            finality = "not_yet_due"
        elif labels["survived_24h"] is not None:
            final = True
            finality = "observed_terminal"
        else:
            retention_days = getattr(self, "config", None) and self.config.retention_days
            retention_days = retention_days if retention_days is not None else 7
            retention_cutoff = now - timedelta(days=retention_days)
            if closing_edge < retention_cutoff:
                final = True
                finality = "retention_lost"
            else:
                final = True
                finality = "permanently_missing_evidence"
        details["finality"] = finality
        return {"labels": labels, "details": details, "final": final}

    # --- one assembly pass ----------------------------------------------------

    def run_once(
        self,
        session: Session,
        limit: int | None = None,
        hours: int | None = None,
        dry_run: bool = False,
        *,
        oldest_first: bool = False,
        include_backlog: bool = False,
        exclude_final: bool = False,
        min_age_minutes: float | None = None,
        run_config_extra: dict | None = None,
        skip_redundant_when_final: bool = False,
        batch_size: int | None = None,
        max_duration_seconds: float | None = None,
        max_lock_attempts: int = DB_LOCKED_MAX_ATTEMPTS,
        lock_retry_seconds: float = DB_LOCKED_RETRY_SECONDS,
        use_overlap_lock: bool = True,
        sleeper=time.sleep,
        time_budget_seconds: float | None = None,
        initial_per_token_cost_seconds: float | None = None,
        lock_wait_budget_seconds: float | None = None,
        finalize_lock_wait_budget_seconds: float | None = None,
    ) -> dict:
        """One bounded reconciliation pass.

        CRYPTO-COVERAGE-REPAIR-001 write-coordination parameters (all default
        to the pre-existing manual-path behaviour so nothing changes unless a
        caller opts in — `run_scheduled_reconciliation` is the only caller
        that does):

        * `skip_redundant_when_final` (B2) — once a token's outcome is
          already final, its window is closed and re-appending a lifecycle
          snapshot/actor-observation row teaches nothing new; skip them.
        * `batch_size` (B3) — commit in bounded batches of this many tokens
          instead of one commit for the whole pass, so the write lock is held
          for a small fraction of a second at a time. None keeps the old
          single-commit behaviour (needed by dry-run and by tests asserting
          nothing is ever partially persisted).
        * `max_duration_seconds` (B6) — internal wall-clock deadline; the pass
          stops after whichever batch is in flight when the deadline is
          crossed and reports `stop_reason="deadline"`. None = unbounded.
        * `max_lock_attempts` / `lock_retry_seconds` (B5) — reuses the same
          DB_LOCKED_* retry ladder `run_tape_session` already uses, applied
          per batch commit. Exhausting it on the very first write yields
          `status="skipped_contention"`; exhausting it after some batches
          already committed yields `status="partial"`,
          `stop_reason="contention"` — the batches that already committed are
          real, durable work, not corruption.
        * `use_overlap_lock` (B4) — a non-blocking, per-chain flock guard so
          two reconciliation passes (scheduled, manual, or a second instance)
          can never mutate the same window concurrently. Disabled only by
          tests exercising `_assemble_pass` directly under a lock they hold
          themselves.
        * `exclude_final` (B2 fix) — drop already-final tokens from the
          in-window selection entirely, so a deadline-stopped, oldest-first
          pass makes forward progress on its NEXT invocation instead of
          re-selecting the identical head. False keeps the manual path's
          historical "reprocess everything in the window, every pass"
          behaviour; only `run_scheduled_reconciliation` opts in.
        * `min_age_minutes` (HIGH-1) — drop tokens younger than this from the
          in-window selection entirely. `exclude_final` alone is not enough:
          `compute_survival` only marks a token `final` at the 24h+tolerance
          edge, so every token younger than that (the whole recent tail) stays
          selectable forever and — since fresh births vastly outnumber a
          bounded pass's throughput — becomes the structural steady state.
          None keeps the manual path unchanged; only
          `run_scheduled_reconciliation` opts in, with
          `SHORTEST_HORIZON_CLOSING_EDGE_MINUTES` (no horizon can be due
          before that age, so there is nothing yet to reconcile).
        * `initial_per_token_cost_seconds` (B3) — supplying this ACTIVATES
          adaptive time-budgeted batching: `batch_size` (if also given)
          becomes only a MAXIMUM sanity ceiling (B11), and the actual next
          batch size is derived every commit from
          `time_budget_seconds / conservative_per_token_cost_estimate`
          (see `AdaptiveBatchCostEstimate`/`next_adaptive_batch_size`).
          `None` (the default) keeps the pre-existing fixed-`batch_size`
          behaviour byte-for-byte unchanged — this value is deliberately an
          UNCALIBRATED placeholder until a real per-token cost is measured
          on the target host; nothing in this module invents a default for
          it, and passing it in is the caller's explicit statement "I have
          measured this".
        * `time_budget_seconds` (B1) — the write-time SLO adaptive batching
          sizes against; only consulted when `initial_per_token_cost_seconds`
          is set. Defaults to `RECONCILE_WRITE_TIME_SLO_SECONDS` when
          adaptive mode is active and this is left `None`.
        """
        started = _now()
        limit = limit if limit is not None else self.config.default_limit
        hours = hours if hours is not None else self.config.default_window_hours
        cutoff = started - timedelta(hours=hours)
        max_first_seen_at = (
            started - timedelta(minutes=min_age_minutes)
            if min_age_minutes is not None else None
        )
        # NEW-BLOCKING-1 fix (second re-review, convergence lens): the
        # trapdoor has a SECOND DOOR beyond backlog-FIRST ordering. Before
        # this fix, `_universe` below was called with the FULL `limit`
        # BEFORE any backlog budget existed — `room = max(0, limit -
        # len(tokens))` was computed only AFTER that call, so whenever
        # in-window eligible tokens alone reached (or exceeded) `limit`,
        # `room` was 0 and `unreconciled_backlog` was NEVER QUERIED, on
        # this and every future pass, because fresh births refill the
        # in-window head forever. Reproduced at `--limit 500`: 24 passes,
        # `backlog_processed=0` every pass, backlog frozen, frontier aged
        # 168h -> 312h. The ordering fix alone (backlog placed FIRST in the
        # selected list, see below) only helps once backlog tokens are
        # actually IN that list — it does nothing if `room` was 0 to begin
        # with. Reserve a backlog budget of `min(backlog_size, limit // 2)`
        # BEFORE calling `_universe`, so the in-window query itself is
        # capped below `limit` and always leaves room. Verified by the
        # reviewer: with this reserve, `--limit 500` and `--limit 850` both
        # reproduce the default run's convergence exactly
        # (`backlog_processed=175` every pass, frontier 157.8h -> 59.8h).
        backlog_total = 0
        reserved_backlog_budget = 0
        # CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R1 — classify the
        # backlog ONCE per pass. `unreconciled_backlog` and
        # `recoverable_backlog_summary` are both called below with the same
        # `session`, `cutoff` and `now=started`, and each used to run its
        # own full-backlog `classify_backlog`: two identical scans, measured
        # at 1.92 s + 1.94 s cold and 4.02 s + 3.21 s warm on a fast Mac
        # (10-33 % of a 20 s pass budget; the code's own ~62x per-token EVO
        # slowdown note means the prelude alone can consume a whole EVO
        # deadline before one token is reconciled). Nothing writes between
        # the two call sites, so one partition is exactly what both used to
        # derive separately.
        #
        # This does NOT bound the residual: see `classify_ms` below.
        backlog_classes: dict[str, list[str]] | None = None
        classify_ms: float | None = None
        if include_backlog:
            backlog_total = self.backlog_size(session, cutoff)
            reserved_backlog_budget = min(backlog_total, limit // 2)
            _classify_started = time.monotonic()
            backlog_classes = self.classify_backlog(session, cutoff, now=started)
            classify_ms = (time.monotonic() - _classify_started) * 1000.0
        in_window_limit = max(0, limit - reserved_backlog_budget)
        tokens = self._universe(
            session, in_window_limit, cutoff, oldest_first=oldest_first,
            exclude_final=exclude_final, max_first_seen_at=max_first_seen_at,
        )
        total = self.universe_size(
            session, cutoff, exclude_final=exclude_final,
            max_first_seen_at=max_first_seen_at,
        )
        backlog_selected = 0
        extra: list = []
        if include_backlog:
            # State-driven top-up: still-open outcomes that have aged out of the
            # window. Without this a missed pass loses a cohort permanently.
            # `room` is now guaranteed >= `reserved_backlog_budget` (the
            # in-window query above was capped at `in_window_limit = limit -
            # reserved_backlog_budget`, so it can consume at most that many
            # slots) — and can be even larger if the in-window head came back
            # short of its own budget, so a genuinely quiet in-window period
            # still lets backlog use the full remaining limit, exactly as
            # before this fix.
            room = max(0, limit - len(tokens))
            if room:
                seen = {t.token_address for t in tokens}
                extra = [
                    t for t in self.unreconciled_backlog(
                        session, cutoff, limit=room, now=started,
                        classes=backlog_classes,
                    )
                    if t.token_address not in seen
                ]
                backlog_selected = len(extra)
                # NEW-H1 fix: backlog tokens are the OLDEST evidence, closest
                # to `crypto_retention_days` pruning (see `unreconciled_backlog`
                # above) — that is the whole reason they exist as a top-up.
                # Appending them AFTER the in-window head meant a
                # deadline-stopped, chunked pass (the scheduled path always
                # is one) processed batches in list order and could NEVER
                # reach them: the in-window head alone (~615 non-final
                # tokens at steady state) already exceeds one pass's
                # throughput, so the backlog queued behind it forever — a
                # one-way trapdoor where aged-out tokens accumulate and their
                # ticks get pruned before a pass ever revisits them.
                # Processing backlog FIRST guarantees forward progress on the
                # evidence that is actually expiring, at the cost of the
                # in-window head (which is not yet at risk of pruning and
                # will still be picked up, oldest-first, by a future pass).
                tokens = extra + tokens
        # NEW-HIGH-2 fix (second re-review, convergence lens): tell
        # `_assemble_pass` which selected tokens are backlog, so it can
        # ground `backlog_processed` in outcome rows ACTUALLY WRITTEN
        # rather than in a prefix-length calc over two selection counts —
        # see the rationale on `_process_batch`'s local of the same name.
        backlog_addresses = (
            frozenset(t.token_address for t in extra) if include_backlog else None
        )
        # NEW-HIGH-2 fix: the FRONTIER metric — see
        # `oldest_unreconciled_first_seen_at`'s docstring. Computed here
        # (not persisted-only inside `_assemble_pass`) so it lands in
        # `config` below and is therefore carried into `run.config` by the
        # existing BLOCKING-1-fixed finalize path, without a second DB
        # round trip inside the write-lock-sensitive part of the pass.
        oldest_unreconciled_first_seen_at = None
        oldest_unreconciled_age_hours = None
        # CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B3 — computed
        # PRE-PASS, same as the raw frontier above and for the same reason:
        # this pass is about to reconcile (and finalise) some of these exact
        # tokens, so measuring the recoverable frontier AFTER `_assemble_pass`
        # would report "how good things look now that we just fixed it"
        # instead of "how far behind was the reconciler when this pass
        # started" — the actual health signal `backlog_expiring` needs.
        recoverable_backlog = None
        if include_backlog:
            oldest_unreconciled_first_seen_at = self.oldest_unreconciled_first_seen_at(
                session, cutoff
            )
            if oldest_unreconciled_first_seen_at is not None:
                oldest_unreconciled_age_hours = (
                    started - _aware(oldest_unreconciled_first_seen_at)
                ).total_seconds() / 3600.0
            recoverable_backlog = self.recoverable_backlog_summary(
                session, cutoff, now=started, classes=backlog_classes,
            )
        config = {"limit": limit, "hours": hours, "chain": self.config.chain}
        config.update(run_config_extra or {})
        if include_backlog:
            config["frontier"] = {
                "oldest_unreconciled_first_seen_at": (
                    oldest_unreconciled_first_seen_at.isoformat()
                    if oldest_unreconciled_first_seen_at is not None else None
                ),
                "oldest_unreconciled_age_hours": oldest_unreconciled_age_hours,
            }
            # CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R7 — the
            # RECOVERABLE frontier belongs in the durable run row too, not
            # only in the `backlog_expiring` error string. The raw
            # `oldest_unreconciled_age_hours` above reads ~204h regardless
            # of pass health once any permanent write-off exists (B3), so a
            # run row carrying only that number cannot distinguish a healthy
            # pass from a starving one after the fact. These four can.
            if recoverable_backlog is not None:
                config["frontier"].update({
                    "recoverable_backlog_count":
                        recoverable_backlog["recoverable_backlog_count"],
                    "oldest_recoverable_due_at":
                        recoverable_backlog["oldest_recoverable_due_at"],
                    "oldest_recoverable_age_seconds":
                        recoverable_backlog["oldest_recoverable_age_seconds"],
                    "recoverable_at_retention_risk":
                        recoverable_backlog["recoverable_at_retention_risk"],
                    "writeoff_count": recoverable_backlog["writeoff_count"],
                })
            # R1 — the residual, made OBSERVABLE. This is the one-and-only
            # full-backlog classification this pass ran, and it is spent
            # BEFORE `_assemble_pass` starts checking the deadline, so it is
            # the part of `duration_ms` that `max_duration_seconds` cannot
            # reach. See the note on `unreconciled_backlog`'s `classes`.
            config["frontier"]["classify_ms"] = classify_ms
        summary = self._assemble_pass(
            session, tokens, started=started, dry_run=dry_run,
            window_hours=hours,
            run_config=config,
            skip_redundant_when_final=skip_redundant_when_final,
            batch_size=batch_size,
            max_duration_seconds=max_duration_seconds,
            max_lock_attempts=max_lock_attempts,
            lock_retry_seconds=lock_retry_seconds,
            use_overlap_lock=use_overlap_lock,
            sleeper=sleeper,
            backlog_token_addresses=backlog_addresses,
            time_budget_seconds=time_budget_seconds,
            initial_per_token_cost_seconds=initial_per_token_cost_seconds,
            lock_wait_budget_seconds=lock_wait_budget_seconds,
            finalize_lock_wait_budget_seconds=finalize_lock_wait_budget_seconds,
        )
        # A cap that silently drops work reads as "complete" to every caller.
        summary["universe_size"] = total
        summary["backlog_size"] = backlog_total
        summary["work_available"] = total + backlog_total
        if recoverable_backlog is not None:
            summary["recoverable_backlog_count"] = recoverable_backlog["recoverable_backlog_count"]
            summary["oldest_recoverable_due_at"] = recoverable_backlog["oldest_recoverable_due_at"]
            summary["oldest_recoverable_age_seconds"] = recoverable_backlog["oldest_recoverable_age_seconds"]
            summary["recoverable_at_retention_risk"] = recoverable_backlog["recoverable_at_retention_risk"]
            summary["writeoff_count"] = recoverable_backlog["writeoff_count"]
        # R1 — surfaced on the summary as well as in `config["frontier"]`, so
        # the CLI can print it without re-reading the run row.
        summary["classify_ms"] = classify_ms
        summary["oldest_unreconciled_first_seen_at"] = oldest_unreconciled_first_seen_at
        summary["oldest_unreconciled_age_hours"] = oldest_unreconciled_age_hours
        tokens_accounted = summary.get("tokens_processed", len(tokens))
        # NEW-HIGH-2 fix: `backlog_processed` used to be
        # `min(backlog_selected, tokens_accounted)` — a prefix-length calc
        # over two SELECTION counts, not grounded in any outcome actually
        # written. It caught NEW-BLOCKING-1 (reads 0 when room==0) but
        # CANNOT catch NEW-BLOCKING-2's partial starvation (`room > 0` but
        # `room < per-pass throughput`), which reads a confident, nonzero
        # value here every pass while the frontier actually retreats —
        # `compute_survival` finalizes tokens on AGE ALONE once ticks are
        # pruned, which is not the same as this pass having done real work
        # on them. Use the grounded, per-batch-committed count from
        # `_assemble_pass` instead: how many backlog tokens' survival
        # outcome rows this pass actually wrote.
        summary["backlog_processed"] = summary.get(
            "backlog_outcomes_written", min(backlog_selected, tokens_accounted)
        )
        # MEDIUM fix (third re-review, M5): `truncated` and `tokens_omitted`
        # answer two DIFFERENT questions and can legitimately disagree —
        # `truncated` is SELECTION-limit-specific (did the query itself cap
        # us below available work?), while `tokens_omitted` counts unreached
        # work regardless of WHY (a `skipped_overlap`/`skipped_contention`
        # pass reaches `tokens_accounted=0` — nothing was read or written —
        # so `tokens_omitted` can be large while `truncated` stays False
        # because the SELECTION itself was never capped). Read `truncated`
        # as "raise --limit", and `tokens_omitted` as "how much reconcilable
        # work exists right now, regardless of cause" — a caller that wants
        # "why is work not getting done" should look at `status`/
        # `stop_reason` first, not infer it from these two counts alone.
        summary["truncated"] = (total + backlog_total) > len(tokens)
        summary["tokens_omitted"] = max(0, (total + backlog_total) - tokens_accounted)
        summary.pop("_births", None)  # internal accounting, not part of the contract
        return summary

    def record_discovery_run(
        self,
        session: Session,
        crypto_run_id: int,
        token_ids,
        *,
        dry_run: bool = False,
    ) -> dict:
        """CRYPTO-HORIZON-ANCHOR-FEED-MEASUREMENT-001 — exact-cycle anchor
        materialization. Consolidates EXACTLY the given canonical token ids —
        which must all have been first persisted by crypto discovery run
        `crypto_run_id` — into lifecycle rows via the same `_assemble_pass`
        the manual tape uses (no second lifecycle-anchor implementation).

        Guarantees: exact canonical ids only (no symbol/partial/freshest
        fallback, no substitution); membership validated against the exact
        originating run BEFORE any write (fail-closed — validation failure
        persists nothing); input order preserved; existing anchors
        deduplicated idempotently; one bounded transaction; zero provider
        access; hard-capped at MAX_ANCHOR_FEED_TOKENS_PER_CYCLE (an over-cap
        cycle is skipped loudly, never truncated silently). Measurement
        only — never advice."""
        started = _now()
        received = list(token_ids)  # materialize once (generator-safe)

        def _result(status: str, **extra) -> dict:
            base = {
                "status": status,
                "note": TAPE_NOTE,
                "mode": "exact_cycle",
                "source_crypto_run_id": crypto_run_id,
                "external_calls": 0,
                "tokens_received": len(received),
                "tokens_validated": 0,
                "anchors_attempted": 0,
                "anchors_created": 0,
                "anchors_existing": 0,
                "complete_anchors": 0,
                "incomplete_anchors": 0,
                "skipped_cap": 0,
                "error": None,
                "duration_ms": max(0, int((_now() - started).total_seconds() * 1000)),
            }
            base.update(extra)
            return base

        run = session.get(CryptoWatcherRun, crypto_run_id)
        if run is None:
            return _result("unknown_run", error="crypto discovery run not found")

        ordered = list(dict.fromkeys(received))  # dedupe, preserve input order
        if not ordered:
            return _result("no_new_tokens")
        if len(ordered) > MAX_ANCHOR_FEED_TOKENS_PER_CYCLE:
            return _result(
                "skipped_cap", skipped_cap=len(ordered),
                error=(
                    f"cycle produced {len(ordered)} tokens > cap "
                    f"{MAX_ANCHOR_FEED_TOKENS_PER_CYCLE}; no anchors created"
                ),
            )

        window_start = _aware(run.started_at)
        window_end = _aware(run.finished_at) or _now()
        tokens: list[CryptoToken] = []
        for token_id in ordered:
            if not isinstance(token_id, str) or not token_id.strip() or len(token_id) > 64:
                return _result("invalid_token", error="malformed canonical token id")
            token = session.execute(
                select(CryptoToken).where(
                    CryptoToken.chain == self.config.chain,
                    CryptoToken.token_address == token_id,
                )
            ).scalars().first()
            if token is None:
                return _result(
                    "invalid_token",
                    error="canonical token id not persisted for this chain",
                )
            if not (window_start <= _aware(token.first_seen_at) <= window_end):
                return _result(
                    "membership_mismatch",
                    error="token was not first persisted by the given discovery run",
                )
            tokens.append(token)

        try:
            summary = self._assemble_pass(
                session, tokens, started=started, dry_run=dry_run,
                window_hours=None,
                run_config={
                    "mode": "exact_cycle",
                    "source_crypto_run_id": crypto_run_id,
                    "chain": self.config.chain,
                },
                # CRYPTO-COVERAGE-REPAIR-001 B1 fix: this is a single bounded
                # transaction over a validated <=MAX_ANCHOR_FEED_TOKENS_PER_CYCLE
                # token set that `_assemble_pass` never chunks/retries for this
                # caller (batch_size stays None => legacy single-commit mode), so
                # it does not need — and must not take — the B4 overlap flock.
                # The anchor feed is EXACT-CYCLE: a skipped cycle is never
                # retried, so silently deferring to another lock holder here
                # would zero out an anchor-feed cycle for good.
                use_overlap_lock=False,
            )
        except IntegrityError as exc:
            # HIGH fix (fourth re-review): this method opts OUT of the
            # overlap lock (see above), so a residual race against another
            # concurrent tape writer surfaces here as a real IntegrityError
            # — the exact same failure class `run_scheduled_reconciliation`
            # already maps to `concurrent_write_conflict` at :2542. Before
            # this fix, `record_discovery_run` had NO such mapping: the CLI
            # calls it directly (app/cli.py:2795) with no surrounding
            # try/except, so this would propagate uncaught all the way to
            # an unhandled traceback. The marketops anchor-feed hook
            # (app/services/marketops.py:1128) separately catches
            # `Exception` around its OWN call and isolates it as
            # `anchor_feed.status="error"` — that isolation is real, but it
            # only covers the anchor-feed hook's caller, not this method's
            # contract, and it discards the specific, actionable
            # "concurrent_write_conflict" classification in favour of a
            # generic error string. Mapping it here fixes BOTH callers at
            # the source and keeps the same typed vocabulary as the
            # reconciler.
            try:
                session.rollback()
            except Exception:  # pragma: no cover - defensive
                pass
            return _result(
                STATUS_CONCURRENT_WRITE_CONFLICT,
                error=(
                    f"unique-constraint conflict during anchor-feed "
                    f"reconciliation, most likely a race with a concurrent "
                    f"tape writer: {exc}"
                ),
            )
        # Defensive: `_assemble_pass` can still, in principle, return a
        # non-normal summary (e.g. if a future change adds a path that skips
        # or truncates work here). A skipped/degraded summary must never be
        # allowed to serialize as "ok, all anchors already existed" — that is
        # exactly the fabricated-anchors-existing failure this fix removes.
        pass_status = summary.get("status")
        if pass_status not in (STATUS_OK, STATUS_DRY_RUN):
            return _result(
                "degraded",
                error=(
                    f"underlying reconciliation pass returned "
                    f"status={pass_status!r} instead of ok/dry_run; refusing "
                    "to report anchor counts derived from that result "
                    f"(pass error: {summary.get('error')})"
                ),
            )
        births = summary.pop("_births", [])
        complete = incomplete = 0
        for birth in births:
            if _completeness_reason(birth, 0.0) is None:
                complete += 1
            else:
                incomplete += 1
        created = summary["birth_events_created"]
        return _result(
            "dry_run" if dry_run else "ok",
            tokens_validated=len(tokens),
            anchors_attempted=len(tokens),
            anchors_created=created,
            anchors_existing=len(tokens) - created,
            complete_anchors=complete,
            incomplete_anchors=incomplete,
            tape_run_id=summary.get("tape_run_id"),
            tokens_considered=summary["tokens_considered"],
            snapshots_created=summary["snapshots_created"],
            outcomes_updated=summary["outcomes_updated"],
        )

    def _commit_with_retry(
        self, session: Session, prepare, max_attempts: int, retry_seconds: float,
        sleeper=time.sleep,
        *,
        budget_provider=None,
        accounting: "LockWaitAccounting | None" = None,
    ) -> tuple[bool, int]:
        """CRYPTO-COVERAGE-REPAIR-001 B5 — bounded retry ladder for one commit,
        reusing the DB_LOCKED_* constants `run_tape_session` already uses.
        `prepare()` is called before every attempt (including the first) and
        must (re)stage this attempt's writes via `session.add(...)` /
        attribute assignment — required because a rolled-back SQLAlchemy
        session EXPIRES already-persistent objects, silently discarding any
        staged-but-uncommitted attribute change, so simply retrying
        `session.commit()` after a rollback would commit stale/empty state.
        Returns (committed, attempts_used); never raises for a lock error —
        a non-lock error still propagates so the caller's normal error
        handling (mark the run row, re-raise) applies unchanged. A bounded
        for-loop over a small, explicit `max_attempts`, never an unbounded
        loop — this module is audited for autonomy vocabulary (see
        test_no_timer_or_daemon_vocabulary_in_session_code).

        NEW-B1 fix: `prepare()` itself can hit the real DB lock, not just
        `session.commit()`. After a rollback the ORM object(s) `prepare()`
        mutates are EXPIRED, so a later ATTRIBUTE READ inside `prepare()`
        (not just a write) lazy-loads from the DB — and because `prepare()`
        typically stages OTHER dirty attribute writes first, SQLAlchemy's
        autoflush fires before that lazy-load SELECT, emitting a real UPDATE
        that can itself hit the lock. Before this fix `prepare()` was called
        OUTSIDE the `try`, so that OperationalError propagated straight past
        this ladder — measured: a real second-process lock holder raised
        past this function with batches already durably committed, leaving
        the run row orphaned at status='running' and the typed-result
        contract broken on exactly the failure path this ladder exists for.
        `prepare()` must be INSIDE the try so a lock hit during PREPARE gets
        exactly the same retry/rollback treatment as a lock hit during
        commit.

        CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001: `budget_provider`, when given,
        is called before EVERY attempt and returns this attempt's per-lock-
        acquisition wait budget in seconds (or None). It is re-applied per
        attempt on purpose — `session.commit()`/`rollback()` return the
        connection to the pool, so a budget set once cannot be assumed to
        survive to the next attempt, and the derived budget shrinks as the
        pass's deadline is consumed. `accounting`, when given, receives the
        measured lock wait of each attempt. Both default to None, which keeps
        this method byte-identical to its pre-milestone behaviour."""
        for attempt in range(1, max(1, max_attempts) + 1):
            if budget_provider is not None:
                budget_ms = _apply_lock_wait_budget(session, budget_provider())
                if accounting is not None:
                    accounting.record_budget(budget_ms)
            with _lock_wait_meter(session, accounting is not None) as meter:
                try:
                    prepare()
                    meter.mark_commit_start()
                    session.commit()
                    meter.mark_commit_end()
                    if accounting is not None:
                        accounting.record(meter)
                    return True, attempt
                except OperationalError as exc:
                    meter.mark_failed()
                    if accounting is not None:
                        accounting.record(meter)
                    session.rollback()
                    if _is_db_locked(exc) and attempt < max_attempts:
                        sleeper(retry_seconds)
                        continue
                    if _is_db_locked(exc):
                        return False, attempt
                    raise
        return False, max(1, max_attempts)  # pragma: no cover - defensive

    def _process_batch(
        self,
        session: Session,
        chunk: list,
        *,
        run: "CryptoTokenLifecycleRun | None",
        started: datetime,
        dry_run: bool,
        existing_births_snapshot: dict,
        final_by_birth_id: dict,
        skip_redundant_when_final: bool,
        backlog_token_addresses: frozenset[str] | None = None,
    ) -> dict:
        """Process one bounded chunk of tokens: reads (`_load_sources`),
        object construction, and the `session.add()`/`flush()` calls this
        chunk needs. Does NOT commit — the caller commits (with retry) once
        per chunk. Deliberately pure with respect to the CALLER's running
        totals: everything this chunk did is returned as a delta, not
        mutated in place, so a rolled-back retry can call this again from
        scratch with zero double-counting.

        CRYPTO-COVERAGE-REPAIR-001 B2 write classification applied here:
          * birth event   — REQUIRED_FOR_OUTCOME (the anchor everything else
            hangs off); always written when new.
          * survival outcome — REQUIRED_FOR_OUTCOME; always upserted, but a
            row whose `final` is already True is never rewritten (pre-existing
            behaviour, unchanged).
          * lifecycle snapshot / actor observation — REQUIRED_FOR_AUDIT the
            FIRST time a token is seen, DERIVED/HISTORICAL_ARTIFACT on every
            later pass once the outcome is final (the window is closed; nothing
            new can be learned). `skip_redundant_when_final` skips exactly
            that redundant case; the manual path never sets it, so its
            row-append behaviour is byte-for-byte unchanged.
        """
        new_births = snapshots = actors = outcomes = 0
        snapshots_skipped = actors_skipped = 0
        # NEW-HIGH-2 fix (second re-review, convergence lens):
        # `backlog_processed` used to be `min(backlog_selected,
        # tokens_accounted)` — a prefix-length calc over two SELECTION
        # counts, grounded in nothing that was actually WRITTEN. It caught
        # NEW-BLOCKING-1 (reads 0 when room==0) but structurally CANNOT
        # catch NEW-BLOCKING-2's partial starvation, where `room > 0` but
        # `room < per-pass throughput`: that shape reads a confident,
        # nonzero `backlog_processed` (bounded by the prefix-count
        # arithmetic) every pass while the frontier actually retreats,
        # because outcome rows finalize on AGE ALONE once ticks are pruned
        # — not because real work was reaching that specific token. Ground
        # this in reality: count backlog tokens whose survival OUTCOME ROW
        # was actually written (upserted, not skipped-because-already-
        # final) in THIS chunk.
        backlog_outcomes_written = 0
        backlog_token_addresses = backlog_token_addresses or frozenset()
        coverage_delta = {
            "tokens_with_ticks": 0,
            "tokens_with_risk": 0,
            "tokens_with_provider_backed_risk": 0,
            "tokens_with_attention": 0,
            "tokens_without_any_source": 0,
        }
        survival_delta: dict[str, int] = {}
        new_examples: list[dict] = []
        births_seen: list[CryptoTokenBirthEvent] = []
        existing_births = dict(existing_births_snapshot)  # chunk-local copy

        for token in chunk:
            sources = self._load_sources(session, token, started)
            if sources.ticks:
                coverage_delta["tokens_with_ticks"] += 1
            if sources.assessments:
                coverage_delta["tokens_with_risk"] += 1
                if any(a.provider_names for a in sources.assessments):
                    coverage_delta["tokens_with_provider_backed_risk"] += 1
            if sources.attention is not None:
                coverage_delta["tokens_with_attention"] += 1
            if not (sources.ticks or sources.assessments or sources.attention):
                coverage_delta["tokens_without_any_source"] += 1

            birth = existing_births.get(token.token_address)
            is_new_birth = birth is None
            if birth is None:
                birth = self.build_birth_event(sources, started)
                new_births += 1
                if not dry_run:
                    birth.run_id = run.id
                    session.add(birth)
                    session.flush()
                    existing_births[token.token_address] = birth
            births_seen.append(birth)

            already_final = (
                not is_new_birth and birth.id is not None
                and final_by_birth_id.get(birth.id, False)
            )
            skip_snapshot_actor = skip_redundant_when_final and already_final

            snapshot = None
            if skip_snapshot_actor:
                snapshots_skipped += 1
                actors_skipped += 1
            else:
                snapshot = self.build_snapshot(
                    sources, birth if birth.id is not None else None, started
                )
                snapshots += 1
                actor = self.build_actor_observation(
                    sources, birth if birth.id is not None else None, started
                )
                actors += 1
                if not dry_run:
                    snapshot.run_id = run.id
                    actor.run_id = run.id
                    session.add(snapshot)
                    session.add(actor)

            survival = self.compute_survival(birth, sources, started)
            for label, value in survival["labels"].items():
                if value is True:
                    survival_delta[label] = survival_delta.get(label, 0) + 1
            if not dry_run and birth.id is not None:
                outcome = session.execute(
                    select(CryptoTokenSurvivalOutcome).where(
                        CryptoTokenSurvivalOutcome.birth_event_id == birth.id
                    )
                ).scalar_one_or_none()
                if outcome is None:
                    outcome = CryptoTokenSurvivalOutcome(
                        birth_event_id=birth.id,
                        chain=self.config.chain,
                        token_address=token.token_address,
                        created_at=started,
                    )
                    session.add(outcome)
                if not outcome.final:
                    for label, value in survival["labels"].items():
                        setattr(outcome, label, value)
                    outcome.details = survival["details"]
                    outcome.final = survival["final"]
                    outcome.last_run_id = run.id
                    outcome.computed_at = started
                    outcomes += 1
                    if token.token_address in backlog_token_addresses:
                        backlog_outcomes_written += 1
            elif dry_run and not already_final:
                # NEW-M3 fix: mirror the non-dry-run "skip if already final,
                # nothing to update" rule. Without `and not already_final` a
                # dry-run probe overstated `outcomes_updated` for every
                # already-final token it merely re-examined (harmless on the
                # scheduled path only because exclude_final already removes
                # those tokens from selection; a manual dry-run without
                # exclude_final would overcount).
                outcomes += 1
                if token.token_address in backlog_token_addresses:
                    backlog_outcomes_written += 1

            if len(new_examples) < 5:
                new_examples.append({
                    "token": token.token_address[:16],
                    "symbol": token.symbol,
                    "launch_source": birth.launch_source,
                    "risk_level": snapshot.risk_level if snapshot is not None else None,
                    "top10_holder_pct": (
                        snapshot.top10_holder_pct if snapshot is not None else None
                    ),
                    "labels": {
                        k: v for k, v in survival["labels"].items() if v is not None
                    },
                })

        return {
            "new_births": new_births, "snapshots": snapshots, "actors": actors,
            "outcomes": outcomes, "snapshots_skipped": snapshots_skipped,
            "actors_skipped": actors_skipped, "coverage_delta": coverage_delta,
            "survival_delta": survival_delta, "examples": new_examples,
            "births_seen": births_seen, "existing_births": existing_births,
            "backlog_outcomes_written": backlog_outcomes_written,
        }

    def _assemble_pass(
        self,
        session: Session,
        tokens: list,
        *,
        started: datetime,
        dry_run: bool,
        window_hours: int | None,
        run_config: dict,
        skip_redundant_when_final: bool = False,
        batch_size: int | None = None,
        max_duration_seconds: float | None = None,
        max_lock_attempts: int = DB_LOCKED_MAX_ATTEMPTS,
        lock_retry_seconds: float = DB_LOCKED_RETRY_SECONDS,
        use_overlap_lock: bool = True,
        sleeper=time.sleep,
        backlog_token_addresses: frozenset[str] | None = None,
        time_budget_seconds: float | None = None,
        initial_per_token_cost_seconds: float | None = None,
        lock_wait_budget_seconds: float | None = None,
        finalize_lock_wait_budget_seconds: float | None = None,
    ) -> dict:
        """B4 overlap guard entry point. A non-blocking, per-chain flock (see
        `_reconcile_overlap_lock`) wraps the ENTIRE pass — from the
        `existing_births` read through the last commit — because the
        measured race this milestone found was exactly a concurrent pass's
        pre-transaction `existing_births` read colliding with another pass's
        insert, producing an IntegrityError that discarded the whole pass.
        Dry-run never mutates anything, so it never takes the lock (two
        concurrent dry probes are harmless)."""
        if dry_run or not use_overlap_lock:
            return self._assemble_pass_locked(
                session, tokens, started=started, dry_run=dry_run,
                window_hours=window_hours, run_config=run_config,
                skip_redundant_when_final=skip_redundant_when_final,
                batch_size=batch_size, max_duration_seconds=max_duration_seconds,
                max_lock_attempts=max_lock_attempts,
                lock_retry_seconds=lock_retry_seconds, sleeper=sleeper,
                backlog_token_addresses=backlog_token_addresses,
                time_budget_seconds=time_budget_seconds,
                initial_per_token_cost_seconds=initial_per_token_cost_seconds,
                lock_wait_budget_seconds=lock_wait_budget_seconds,
                finalize_lock_wait_budget_seconds=finalize_lock_wait_budget_seconds,
            )
        lock_dir = self.config.lock_dir or _resolve_lock_dir(None)
        with _reconcile_overlap_lock(lock_dir, self.config.chain) as acquired:
            if not acquired:
                return {
                    "status": STATUS_SKIPPED_OVERLAP,
                    "note": TAPE_NOTE,
                    "external_calls": 0,
                    "window_hours": window_hours,
                    "tokens_considered": 0,
                    "tokens_processed": 0,
                    "birth_events_created": 0,
                    "snapshots_created": 0,
                    "actor_observations_created": 0,
                    "outcomes_updated": 0,
                    "snapshots_skipped_redundant": 0,
                    "actor_observations_skipped_redundant": 0,
                    "provider_coverage": {}, "survival_label_mix": {},
                    "examples": [], "batches_committed": 0,
                    "batch_size": batch_size, "stop_reason": "overlap",
                    "lock_retry_events": 0,
                    "blocked_ms": 0,
                    # Nothing was read or written, so every lock-wait figure
                    # is a real zero — not a missing key the CLI would print
                    # as None.
                    **LockWaitAccounting().as_summary(),
                    "backlog_outcomes_written": 0,
                    "error": (
                        f"another crypto-tape reconciliation pass already "
                        f"holds the {self.config.chain} overlap lock at "
                        f"{lock_dir / RECONCILE_LOCK_FILENAME.format(chain=self.config.chain)}; "
                        "nothing was read or written"
                    ),
                    "_births": [],
                }
            return self._assemble_pass_locked(
                session, tokens, started=started, dry_run=dry_run,
                window_hours=window_hours, run_config=run_config,
                skip_redundant_when_final=skip_redundant_when_final,
                batch_size=batch_size, max_duration_seconds=max_duration_seconds,
                max_lock_attempts=max_lock_attempts,
                lock_retry_seconds=lock_retry_seconds, sleeper=sleeper,
                backlog_token_addresses=backlog_token_addresses,
                time_budget_seconds=time_budget_seconds,
                initial_per_token_cost_seconds=initial_per_token_cost_seconds,
                lock_wait_budget_seconds=lock_wait_budget_seconds,
                finalize_lock_wait_budget_seconds=finalize_lock_wait_budget_seconds,
            )

    def _assemble_pass_locked(
        self,
        session: Session,
        tokens: list,
        *,
        started: datetime,
        dry_run: bool,
        window_hours: int | None,
        run_config: dict,
        skip_redundant_when_final: bool,
        batch_size: int | None,
        max_duration_seconds: float | None,
        max_lock_attempts: int,
        lock_retry_seconds: float,
        sleeper=time.sleep,
        backlog_token_addresses: frozenset[str] | None = None,
        time_budget_seconds: float | None = None,
        initial_per_token_cost_seconds: float | None = None,
        lock_wait_budget_seconds: float | None = None,
        finalize_lock_wait_budget_seconds: float | None = None,
    ) -> dict:
        """The actual assembly work, run with the overlap lock already held
        (or dry-run, which needs none).

        `batch_size` is the fork point between two modes, and this is
        deliberate — every existing caller before CRYPTO-COVERAGE-REPAIR-001
        leaves it `None` and must see byte-identical behaviour:

        * `batch_size is None` (LEGACY, unchanged) — one flush for the run
          row, one pass over all tokens, one commit at the end, no retry
          ladder around any of it. This is exactly the pre-milestone
          `_assemble_pass` body, including "any exception (lock or not)
          raises straight through, caught only by the outer error-row
          handler" — `record_discovery_run`'s "one bounded transaction"
          guarantee (pinned by
          test_crypto_anchor_feed_measurement_001::test_one_bounded_
          transaction) depends on this.
        * `batch_size` given (B3, opt-in — only `run_scheduled_reconciliation`
          sets it) — the run row is created in its own short retried
          transaction, each token batch commits (with retry) on its own, and
          the run row is finalized in a last short retried transaction. B6's
          wall-clock deadline only applies in this mode, between batches,
          never mid-batch.
        * `initial_per_token_cost_seconds` given (B3 ADAPTIVE mode, opt-in) —
          implies chunked mode regardless of `batch_size`, and `batch_size`
          (if also given) becomes ONLY a sanity MAXIMUM (B11) — the actual
          next batch size is derived every commit from
          `time_budget_seconds / conservative_per_token_cost_estimate`
          (see `AdaptiveBatchCostEstimate`). If the estimate ever predicts
          that even a single-token transaction would violate the budget,
          the pass stops with `status=STATUS_UNSAFE_HOST_COST` rather than
          guess a smaller-than-1 batch. This is the mechanism THE CORE
          PROBLEM (a fixed token count is not a safety invariant) requires;
          it is inert unless a caller explicitly supplies a measured cost."""
        hours = window_hours
        adaptive = initial_per_token_cost_seconds is not None
        if adaptive and initial_per_token_cost_seconds <= 0:
            raise ValueError(
                "initial_per_token_cost_seconds must be > 0 — refusing to "
                "activate adaptive batching with a non-positive UNCALIBRATED "
                f"value ({initial_per_token_cost_seconds!r})"
            )
        effective_time_budget_seconds = (
            time_budget_seconds
            if time_budget_seconds is not None
            else RECONCILE_WRITE_TIME_SLO_SECONDS
        ) if adaptive else None
        if adaptive and effective_time_budget_seconds <= 0:
            raise ValueError(
                "time_budget_seconds must be > 0, got "
                f"{effective_time_budget_seconds!r}"
            )
        cost_estimate = (
            AdaptiveBatchCostEstimate(initial_per_token_cost_seconds)
            if adaptive else None
        )
        chunked = batch_size is not None or adaptive
        deadline = (
            started + timedelta(seconds=max_duration_seconds)
            if (chunked and max_duration_seconds is not None) else None
        )
        # CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001 — the reconciler's OWN
        # lock-wait budget, live only on the chunked WRITE path. A dry run
        # never commits, and legacy single-transaction mode must stay
        # byte-identical (`record_discovery_run`'s one-bounded-transaction
        # guarantee), so neither gets a budget or a meter.
        lock_budget_active = chunked and not dry_run
        lock_accounting = LockWaitAccounting()
        original_busy_timeout_ms = (
            _read_busy_timeout_ms(session) if lock_budget_active else None
        )

        def _remaining_deadline_seconds() -> float | None:
            if deadline is None:
                return None
            return (deadline - _now()).total_seconds()

        def _next_lock_wait_budget() -> float | None:
            """This attempt's per-lock-acquisition wait budget. Re-derived
            per attempt so it tightens as the pass's deadline is spent."""
            if not lock_budget_active:
                return None
            return derive_lock_wait_budget_seconds(
                lock_wait_budget_seconds, _remaining_deadline_seconds()
            )

        def _finalize_lock_wait_budget() -> float | None:
            """The run row's finalize commit gets its OWN budget, not the
            pass's data-work budget.

            CORRECTED REASONING (independent review of this branch). The
            original justification here was that the finalize "runs AFTER the
            deadline has normally been consumed, so a deadline-derived budget
            would be the bare floor anyway" — which conflates two different
            quantities. `max_duration_seconds` bounds the pass's DATA WORK;
            the finalize is BOOKKEEPING, one small row written once, and it is
            not data work. Nothing about a spent data deadline says anything
            about what this commit may wait.

            What the 0.5s floor actually cost, measured on the same fixture
            and the same 20s competing holder, floor being the only variable:

                floor=0.5s   wall= 5.6s  status=partial/lock_wait_budget
                             run_row=running  histogram_persisted=False
                floor=30s    wall=20.2s  status=ok
                             run_row=ok       histogram_persisted=True

            `write_coordination` — the persisted lock-wait scalars AND the
            whole histogram — is staged inside `_prepare_finalize` and written
            by this one commit. Cutting its budget 60x therefore threw away
            the evidence on exactly the contended passes the lock-wait gate
            exists to measure. The 5.6s-vs-20.2s improvement is real and is
            the milestone's point, but it belongs to the BATCH LOOP, which is
            unbounded in how much work it can attempt; this commit writes one
            small row and holds the lock for microseconds, so it is bounded by
            construction and does not need the batch loop's protection.

            So: the finalize keeps the connection's ORIGINAL busy timeout —
            i.e. exactly what it had before this milestone existed
            (`sqlite_busy_timeout_ms`, 30s in production) — capped by an
            operator-set budget if one exists, and floored so a host with a
            pathologically small busy timeout still gets a real attempt. The
            residual this adds to the pass's wall time is stated, not hidden:
            see RESIDUAL OVERSHOOT in the milestone commit message and the
            runbook."""
            if not lock_budget_active:
                return None
            base = (
                finalize_lock_wait_budget_seconds
                if finalize_lock_wait_budget_seconds is not None
                else (
                    original_busy_timeout_ms / 1000.0
                    if original_busy_timeout_ms else None
                )
            )
            if base is None:
                # No connection-level timeout to inherit (non-SQLite backend,
                # or the PRAGMA read failed): fall back to the floor rather
                # than to an unbounded wait.
                base = RECONCILE_LOCK_WAIT_FLOOR_SECONDS
            base = max(base, RECONCILE_LOCK_WAIT_FLOOR_SECONDS)
            if lock_wait_budget_seconds is None:
                return base
            # An operator budget is a CAP everywhere it applies, here too.
            return min(lock_wait_budget_seconds, base)

        def _restore_busy_timeout() -> None:
            """Put the connection's busy timeout back where the pass found
            it. The budget is applied per attempt on whatever connection the
            session currently holds; this restores the same way. Caveat,
            stated rather than hidden: with a connection POOL, a pass that
            touched more than one pooled connection restores only the one it
            ends on. In production the reconciler is a single-threaded
            one-shot CLI process that ends immediately afterwards, so the
            residue is unobservable; in-process (tests, a long-lived engine)
            it is bounded by `sqlite_busy_timeout_ms` being re-applied on
            every new connection by `app.db.connect_args_for`."""
            if original_busy_timeout_ms is not None:
                _apply_lock_wait_budget(session, original_busy_timeout_ms / 1000.0)

        # --- read phase: no write transaction is open for any of this -------
        # BLOCKER-2 fix (independent review of this branch): READS ARE A
        # BLOCKING POINT TOO, and before this the budget was applied ONLY
        # inside the chunked write loop and the finalize. Everything before
        # that ran at the process-wide `sqlite_busy_timeout_ms` (30s), so a
        # real `run_scheduled_reconciliation` with `--max-duration-seconds 30`
        # and a claimed 42.0s bound was measured at **60.11s** behind a 45s
        # competing EXCLUSIVE holder, with `lock_retry_events=0` — the budget
        # machinery never engaged at all; 60.09s is simply two successive read
        # acquisitions each burning the full process timeout. A SELECT blocks
        # behind an EXCLUSIVE holder (measured on EVO at 1.00x its configured
        # busy_timeout), so the reads must be budgeted like everything else.
        # The governed caller budgets its own prelude the same way — see
        # `run_scheduled_reconciliation`.
        _read_phase_budget_ms = _apply_lock_wait_budget(
            session, _next_lock_wait_budget()
        )
        lock_accounting.record_budget(_read_phase_budget_ms)
        try:
            existing_births = {
                b.token_address: b
                for b in session.execute(
                    select(CryptoTokenBirthEvent).where(
                        CryptoTokenBirthEvent.chain == self.config.chain,
                        CryptoTokenBirthEvent.token_address.in_(
                            [t.token_address for t in tokens]
                        ),
                    )
                ).scalars().all()
            } if tokens else {}
            existing_birth_ids = [
                b.id for b in existing_births.values() if b.id is not None
            ]
            final_by_birth_id: dict[int, bool] = {
                row[0]: bool(row[1])
                for row in session.execute(
                    select(
                        CryptoTokenSurvivalOutcome.birth_event_id,
                        CryptoTokenSurvivalOutcome.final,
                    ).where(
                        CryptoTokenSurvivalOutcome.birth_event_id.in_(existing_birth_ids)
                    )
                )
            } if existing_birth_ids else {}
        except Exception as exc:
            # The read phase sits OUTSIDE the pass's main try/finally, so a
            # blocked read that now fails fast (it previously waited the full
            # process busy timeout) must still put the connection back and
            # must still carry its accounting out — see `_lock_wait_carrier`.
            _attach_lock_wait_evidence(exc, lock_accounting, 0.0, 0)
            _restore_busy_timeout()
            raise

        new_births = snapshots = actors = outcomes = 0
        snapshots_skipped = actors_skipped = 0
        coverage_summary = {
            "tokens_with_ticks": 0,
            "tokens_with_risk": 0,
            "tokens_with_provider_backed_risk": 0,
            "tokens_with_attention": 0,
            "tokens_without_any_source": 0,
        }
        survival_mix: dict[str, int] = {}
        examples: list[dict] = []
        births_seen: list[CryptoTokenBirthEvent] = []
        lock_retry_events = 0
        batches_committed = 0
        tokens_processed = 0
        # NEW-HIGH-2 fix: grounded, per-batch backlog outcome-write count
        # (see `_process_batch`'s own comment) — accumulated across every
        # chunk actually committed in this pass.
        backlog_outcomes_written_total = 0
        # NEW-MEDIUM-2 fix (third Lane-B review, SQLite coexistence):
        # `lock_retry_events` only increments on a CAUGHT-and-retried
        # OperationalError — a pass genuinely blocked by a real external
        # holder for the ENTIRE duration of `sqlite_busy_timeout_ms` on a
        # single attempt (no second attempt ever needed because the first
        # one, eventually, succeeds) reports `lock_retry_events=0` while
        # having actually spent up to 30s blocked inside SQLite's busy
        # handler. Measured: a 40s external hold produced `status=partial
        # (stop_reason=deadline)` with `lock_retry_events=0`, understating
        # how much of that 43.9s pass wall time was spent blocked, not
        # doing work — and this same commit newly PERSISTS
        # `lock_retry_events` to `run.config.write_coordination` and PRINTS
        # it, turning a known-undercounting signal into an active
        # misinformation surface. `blocked_seconds` is measured directly
        # with `time.perf_counter()` around every commit/prepare attempt
        # (successful or not) — it counts ALL time spent waiting on SQLite,
        # not just attempts that were retried.
        blocked_seconds = 0.0
        stop_reason: str | None = None

        run: CryptoTokenLifecycleRun | None = None
        # BLOCKING-2 fix: `run.id` is an immutable PK once assigned, but
        # reading it as a SQLAlchemy-instance attribute is not — every
        # commit/rollback later in this pass EXPIRES `run`, and reading an
        # expired attribute under a real second-connection lock is a live,
        # uncaught `SELECT ... WHERE id = ?` that can raise OperationalError
        # outside any retry ladder (the same escape class as BLOCKING-1, on
        # the PK column instead of `config`). Capture it once into this
        # plain int local, the moment it first exists in each mode below,
        # and use the local everywhere later in this method instead of
        # `run.id`.
        run_id: int | None = None
        if not dry_run:
            run = CryptoTokenLifecycleRun(
                status="running", started_at=started, window_hours=hours,
                config=run_config, created_at=started,
            )
            if chunked:
                def _prepare_run_creation() -> None:
                    session.add(run)

                _t0 = time.perf_counter()
                ok, attempts = self._commit_with_retry(
                    session, _prepare_run_creation, max_lock_attempts,
                    lock_retry_seconds, sleeper,
                    budget_provider=_next_lock_wait_budget,
                    accounting=lock_accounting if lock_budget_active else None,
                )
                blocked_seconds += time.perf_counter() - _t0
                lock_retry_events += max(0, attempts - 1)
                # Capture the PK right after the run-row creation commit —
                # see the module-level comment on `run_id` above.
                run_id = run.id if ok else None
                if not ok:
                    _restore_busy_timeout()
                    return {
                        "status": STATUS_SKIPPED_CONTENTION,
                        "note": TAPE_NOTE,
                        "external_calls": 0,
                        "window_hours": hours,
                        "tokens_considered": 0,
                        "tokens_processed": 0,
                        "birth_events_created": 0,
                        "snapshots_created": 0,
                        "actor_observations_created": 0,
                        "outcomes_updated": 0,
                        "snapshots_skipped_redundant": 0,
                        "actor_observations_skipped_redundant": 0,
                        "provider_coverage": coverage_summary,
                        "survival_label_mix": {},
                        "examples": [], "batches_committed": 0,
                        "batch_size": batch_size, "stop_reason": "contention",
                        "lock_retry_events": lock_retry_events,
                        "blocked_ms": int(blocked_seconds * 1000),
                        **lock_accounting.as_summary(),
                        "backlog_outcomes_written": 0,
                        "error": (
                            f"database is locked; exhausted {max_lock_attempts} "
                            "attempts before the run row could even be created "
                            "— nothing was written"
                        ),
                        "_births": [],
                    }
            else:
                # LEGACY single-transaction mode: flush only (no commit, no
                # retry) — identical to the pre-milestone `_assemble_pass`.
                session.add(run)
                session.flush()
                # `flush()` (unlike `commit()`) does not expire attributes,
                # so `run.id` is safe to read here — capture it into the
                # same `run_id` local used by the chunked branch so both
                # modes feed the same later `run_id` reads.
                run_id = run.id

        effective_batch = batch_size or max(len(tokens), 1)

        def _iter_chunks():
            """B3 — the SELECTION of what goes in each batch is decided lazily,
            one batch at a time, so an adaptive pass can react to what the
            PREVIOUS batch's real commit actually cost. Non-adaptive
            (fixed-`batch_size`/legacy) mode keeps its pre-existing, eagerly
            equivalent fixed-size slicing — yielding lazily changes nothing
            observable there. Yields `("chunk", list_of_tokens)` or
            `("unsafe", None)` — the latter means even a single-token
            transaction is predicted to violate the write-time budget at the
            current conservative cost estimate; the caller must stop, not
            proceed with a smaller-than-1 batch."""
            if not tokens:
                return
            if not adaptive:
                for i in range(0, len(tokens), effective_batch):
                    yield "chunk", tokens[i:i + effective_batch]
                return
            remaining = list(tokens)
            while remaining:
                size = next_adaptive_batch_size(
                    effective_time_budget_seconds, cost_estimate,
                    max_batch_size=batch_size,
                )
                if size < 1:
                    yield "unsafe", None
                    return
                yield "chunk", remaining[:size]
                remaining = remaining[size:]

        try:
            for kind, chunk in _iter_chunks():
                if kind == "unsafe":
                    stop_reason = "unsafe_host_cost"
                    break
                if chunked and deadline is not None and _now() >= deadline and tokens_processed > 0:
                    stop_reason = "deadline"
                    break

                if chunked:
                    result = None
                    lock_budget_expired = False
                    for attempt in range(1, max_lock_attempts + 1):
                        # CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001: re-derive
                        # and re-apply THIS attempt's lock-wait budget. Per
                        # attempt, not once per pass, for two reasons: the
                        # previous attempt's commit/rollback returned the
                        # connection to the pool (a PRAGMA set on one
                        # checkout cannot be assumed to survive), and the
                        # derived budget shrinks as the deadline is spent.
                        _attempt_budget_ms = _apply_lock_wait_budget(
                            session, _next_lock_wait_budget()
                        )
                        lock_accounting.record_budget(_attempt_budget_ms)
                        # NEW-MEDIUM-2 fix: measure wall time spent in THIS
                        # attempt's process+commit — including a successful
                        # first attempt that itself spent real time inside
                        # SQLite's busy handler before winning the lock —
                        # not just attempts that were caught and retried.
                        _attempt_t0 = time.perf_counter()
                        with _lock_wait_meter(session, lock_budget_active) as _meter:
                            try:
                                result = self._process_batch(
                                    session, chunk, run=run, started=started,
                                    dry_run=dry_run,
                                    existing_births_snapshot=existing_births,
                                    final_by_birth_id=final_by_birth_id,
                                    skip_redundant_when_final=skip_redundant_when_final,
                                    backlog_token_addresses=backlog_token_addresses,
                                )
                                if not dry_run:
                                    _meter.mark_commit_start()
                                    session.commit()
                                    _meter.mark_commit_end()
                                    lock_accounting.record(_meter)
                                    _attempt_duration = time.perf_counter() - _attempt_t0
                                    blocked_seconds += _attempt_duration
                                    # B3: feed the ACTUAL measured commit wall time
                                    # back into the conservative cost estimate
                                    # before sizing the NEXT batch (the generator
                                    # above reads `cost_estimate` again on its
                                    # next iteration). Only successful commits
                                    # teach real per-token cost; a retried/failed
                                    # attempt's wait time is contention, not
                                    # per-token write cost.
                                    if adaptive:
                                        cost_estimate.observe(_attempt_duration, len(chunk))
                                    # NEW-H1 fix: yield the write lock briefly
                                    # AFTER a real commit so a waiting competing
                                    # writer gets a genuine chance to win the
                                    # race — see RECONCILE_POST_BATCH_YIELD_SECONDS.
                                    sleeper(RECONCILE_POST_BATCH_YIELD_SECONDS)
                                break
                            except OperationalError as exc:
                                _meter.mark_failed()
                                lock_accounting.record(_meter)
                                blocked_seconds += time.perf_counter() - _attempt_t0
                                session.rollback()
                                result = None
                                if _is_db_locked(exc):
                                    lock_retry_events += 1
                                    # THE BUDGET-EXPIRY DECISION. Only the
                                    # CURRENT batch is discarded (the rollback
                                    # above); every batch committed earlier in
                                    # this pass is already durable and stays
                                    # durable. Before this milestone the ladder
                                    # slept and retried regardless of the
                                    # deadline, so a pass could keep spending
                                    # wall-clock time it no longer had — the
                                    # 61s-against-30s shape. Now: if the
                                    # deadline is already gone, stop here and
                                    # let the typed status say so.
                                    _remaining = _remaining_deadline_seconds()
                                    if _remaining is not None and _remaining <= 0:
                                        lock_budget_expired = True
                                        break
                                    if attempt < max_lock_attempts:
                                        # Never sleep past the deadline either.
                                        sleeper(
                                            lock_retry_seconds if _remaining is None
                                            else min(lock_retry_seconds, _remaining)
                                        )
                                        continue
                                    break  # exhausted — handled below as contention
                                raise  # a real DB error, not lock contention

                    if result is None:
                        stop_reason = (
                            "lock_wait_budget" if lock_budget_expired else "contention"
                        )
                        break
                else:
                    # LEGACY: no retry, no intermediate commit — any exception
                    # (lock or not) propagates straight to the outer handler,
                    # exactly like the pre-milestone single-transaction pass.
                    result = self._process_batch(
                        session, chunk, run=run, started=started, dry_run=dry_run,
                        existing_births_snapshot=existing_births,
                        final_by_birth_id=final_by_birth_id,
                        skip_redundant_when_final=skip_redundant_when_final,
                        backlog_token_addresses=backlog_token_addresses,
                    )

                existing_births.update(result["existing_births"])
                new_births += result["new_births"]
                snapshots += result["snapshots"]
                actors += result["actors"]
                outcomes += result["outcomes"]
                snapshots_skipped += result["snapshots_skipped"]
                actors_skipped += result["actors_skipped"]
                backlog_outcomes_written_total += result.get(
                    "backlog_outcomes_written", 0
                )
                for key, delta in result["coverage_delta"].items():
                    coverage_summary[key] += delta
                for label, delta in result["survival_delta"].items():
                    survival_mix[label] = survival_mix.get(label, 0) + delta
                if len(examples) < 5:
                    examples.extend(result["examples"][: 5 - len(examples)])
                births_seen.extend(result["births_seen"])
                tokens_processed += len(chunk)
                # NEW-M2 fix: `batches_committed` must count actual commits,
                # not loop iterations. In chunked mode a dry run NEVER calls
                # `session.commit()` (guarded by `if not dry_run` above), so
                # counting the iteration itself overstated committed batches
                # for a dry-run probe (observed: dry_run status=dry_run
                # batches_committed=3 while a real after_commit listener saw
                # 0 commits). Legacy (unbatched) mode still commits once at
                # the very end in the real-run branch below, so incrementing
                # here for a non-dry-run pass reflects real, imminent work.
                if not dry_run:
                    batches_committed += 1

            summary = {
                "status": STATUS_DRY_RUN if dry_run else STATUS_OK,
                "note": TAPE_NOTE,
                "external_calls": 0,
                "window_hours": hours,
                "tokens_considered": tokens_processed,
                "tokens_processed": tokens_processed,
                "birth_events_created": new_births,
                "snapshots_created": snapshots,
                "actor_observations_created": actors,
                "outcomes_updated": outcomes,
                "snapshots_skipped_redundant": snapshots_skipped,
                "actor_observations_skipped_redundant": actors_skipped,
                "provider_coverage": coverage_summary,
                "survival_label_mix": dict(sorted(survival_mix.items())),
                "examples": examples,
                "batches_committed": batches_committed,
                "batch_size": batch_size,
                "stop_reason": stop_reason,
                "lock_retry_events": lock_retry_events,
                # `blocked_ms` is the attempt's TOTAL wall time (wait + real
                # work); `lock_wait_ms` below is the measured WAIT alone. The
                # two are deliberately both reported — their difference is
                # the pass's own write cost.
                "blocked_ms": int(blocked_seconds * 1000),
                **lock_accounting.as_summary(),
                "backlog_outcomes_written": backlog_outcomes_written_total,
                "_births": births_seen,
            }
            if stop_reason is not None:
                if stop_reason == "unsafe_host_cost":
                    # B3 — the adaptive cost estimate predicts even a
                    # single-token transaction would violate the write-time
                    # SLO. Distinct from every other stop reason: those mean
                    # "ran out of selection room/wall-clock budget/lock
                    # attempts"; this means "the host is currently too slow
                    # for this budget at ANY batch size" and must not be
                    # silently reported as a routine partial/dry-run-partial.
                    # Batches committed earlier in THIS pass, if any, remain
                    # durable — only forward progress stopped.
                    summary["status"] = STATUS_UNSAFE_HOST_COST
                    summary["error"] = (
                        "adaptive batching refused to start another batch: "
                        f"the conservative per-token cost estimate "
                        f"({cost_estimate.conservative_estimate_seconds:.4f}s) "
                        f"exceeds the write-time budget "
                        f"({effective_time_budget_seconds:.4f}s) — even a "
                        "single-token transaction is predicted to violate "
                        f"it. {batches_committed} batch(es) / "
                        f"{tokens_processed} of {len(tokens)} selected "
                        "tokens were processed before this pass stopped; "
                        "already-committed batches are durable. This "
                        "requires either a real re-measurement of "
                        "per-token cost on this host or a larger "
                        "time_budget_seconds — never a smaller batch size, "
                        "which this mechanism already tried."
                    )
                elif dry_run:
                    # H1 fix: a dry-run probe truncated by the deadline (or
                    # exhausted lock retries) must never report plain
                    # "dry_run" — that is indistinguishable from a complete
                    # probe to every caller, including the CLI exit code.
                    summary["status"] = STATUS_DRY_RUN_PARTIAL
                    summary["error"] = (
                        f"dry-run probe stopped early (stop_reason={stop_reason}) "
                        f"after examining {tokens_processed} of {len(tokens)} "
                        "selected tokens; nothing was written (dry-run never "
                        "writes) and nothing beyond the examined tokens was "
                        "measured"
                    )
                elif stop_reason == "lock_wait_budget":
                    # CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001 — the pass's
                    # lock-wait budget (and with it the wall-clock deadline)
                    # was exhausted while a batch was blocked. Same typed
                    # vocabulary as every other contention stop: `partial`
                    # when earlier batches are durable, `skipped_contention`
                    # when nothing was written at all. Only the CURRENT
                    # batch was rolled back.
                    summary["status"] = (
                        STATUS_PARTIAL if batches_committed > 0
                        else STATUS_SKIPPED_CONTENTION
                    )
                    summary["error"] = (
                        "lock-wait budget exhausted: the pass was blocked on "
                        "the SQLite write lock with no wall-clock budget left "
                        f"(lock_wait_ms={int(lock_accounting.total_seconds * 1000)}, "
                        f"budget_ms={lock_accounting.budget_ms_min}). "
                        f"{batches_committed} batch(es) / {tokens_processed} of "
                        f"{len(tokens)} selected tokens were committed before "
                        "this pass stopped; those batches are durable and the "
                        "rolled-back batch is neither duplicated nor lost — it "
                        "returns to the backlog for the next pass"
                    )
                elif stop_reason == "contention" and batches_committed == 0:
                    # LOW-3 fix: the first token batch itself exhausted the
                    # retry ladder before committing anything. "partial" with
                    # "already-committed batches are durable" would describe
                    # zero batches — a lie. This is the same "nothing was
                    # written this pass" shape as the run-row-creation
                    # contention failure above; label it the same way.
                    summary["status"] = STATUS_SKIPPED_CONTENTION
                    summary["error"] = (
                        "database is locked; the first token batch exhausted "
                        f"{max_lock_attempts} commit attempts before anything "
                        "was written — nothing was reconciled this pass"
                    )
                else:
                    summary["status"] = STATUS_PARTIAL
                    # NEW-HIGH-1(a) fix (second re-review, convergence
                    # lens): this method has no visibility into the
                    # caller's backlog-budget allocation (`run_once`/
                    # `run_scheduled_reconciliation` sit a layer above it),
                    # so it previously overclaimed "the remaining tokens
                    # stay eligible for a future pass" unconditionally —
                    # measured FALSE under the budget-starvation shape this
                    # milestone's second re-review found (NEW-BLOCKING-1/2):
                    # a token can remain technically re-selectable by the
                    # query on every future pass while never actually
                    # RECEIVING budget, which is a durable-in-practice
                    # exclusion, not eligibility. Only claim what this layer
                    # actually knows: the committed batches are real and
                    # durable, and unreached tokens are neither lost nor
                    # duplicated by THIS pass. Whether they get budget on
                    # the NEXT pass is the caller's concern (see
                    # `backlog_processed` / the frontier fields on the
                    # scheduled-reconciliation result).
                    summary["error"] = (
                        f"pass stopped early (stop_reason={stop_reason}) after "
                        f"{batches_committed} batch(es) / {tokens_processed} of "
                        f"{len(tokens)} selected tokens; already-committed "
                        "batches are durable — nothing is duplicated or lost "
                        "by this pass; whether the remaining tokens actually "
                        "receive budget on a future pass depends on the "
                        "caller's selection/backlog policy, not on anything "
                        "this pass guarantees"
                    )
            if dry_run:
                return summary

            finished = _now()

            if chunked:
                # NEW-B1 fix (third review, corrected per BLOCKING-1 from a
                # fourth review): `run.config` used to be read INSIDE
                # `_prepare_finalize`, below. After any prior commit/rollback
                # in this pass, `run` is EXPIRED (SQLAlchemy's default
                # `expire_on_commit=True`, plus every rollback expires too) —
                # so once `_prepare_finalize` had already staged OTHER dirty
                # `run.*` attribute writes above this read, the read of the
                # expired `run.config` attribute lazy-loads, autoflush fires
                # on the now-dirty session, and that autoflush's UPDATE could
                # itself hit a real lock — raising OperationalError from
                # INSIDE `prepare()`, which (before the companion fix moving
                # `prepare()` inside `_commit_with_retry`'s try) escaped the
                # retry ladder entirely.
                #
                # The original companion fix moved the read here, still as
                # `run.config`, which merely relocated the same expired-
                # instance access to a point that is ALSO reachable after a
                # rollback (this line runs after the batch loop, which can
                # have rolled back `session` on a caught OperationalError at
                # :1743) — a real second-connection lock at that moment still
                # produces a live `SELECT ... WHERE id = ?` that can itself
                # raise, uncaught, outside any retry ladder. The run row's
                # config was never actually mutated by anything above this
                # point in this pass, so `run.config` is guaranteed to equal
                # the exact dict this method was called with at :1669
                # (`config=run_config`) — read that caller-supplied local
                # directly instead. Zero DB access, byte-identical value, and
                # it is `dict`-copied here (not just referenced) so later
                # mutation of `run_config` by any other caller can never leak
                # into this pass's snapshot.
                existing_config = dict(run_config or {})

                def _prepare_finalize() -> None:
                    with session.no_autoflush:
                        run.status = summary["status"]
                        run.finished_at = finished
                        run.duration_ms = max(
                            0, int((finished - started).total_seconds() * 1000)
                        )
                        run.tokens_considered = tokens_processed
                        run.birth_events_created = new_births
                        run.snapshots_created = snapshots
                        run.actor_observations_created = actors
                        run.outcomes_updated = outcomes
                        run.provider_coverage = coverage_summary
                        # NEW-H1 fix: B2's skip-when-final path makes
                        # `snapshots_created`/`actor_observations_created`
                        # mean different things depending on whether this run
                        # skipped redundant writes for already-final tokens —
                        # without recording that classification on the run
                        # row itself, a later reader of the DB (e.g.
                        # build_tape_report) has no way to tell "all tokens
                        # got a fresh snapshot" apart from "only non-final
                        # tokens did".
                        run.config = {
                            **existing_config,
                            "write_classification": {
                                "skip_redundant_when_final": skip_redundant_when_final,
                                "snapshots_skipped_redundant": snapshots_skipped,
                                "actor_observations_skipped_redundant": actors_skipped,
                            },
                            # MEDIUM fix: lock_retry_events/batches_committed
                            # were only ever printed by the CLI, so
                            # contention history died with journal rotation
                            # on a host with documented lock contention.
                            # Persisted here so it survives.
                            #
                            # NEW-MEDIUM-2 fix (third Lane-B review, SQLite
                            # coexistence): `lock_retry_events` alone
                            # UNDERCOUNTS real blocked time — a pass fully
                            # blocked for up to `sqlite_busy_timeout_ms` on a
                            # single attempt that eventually succeeds
                            # reports 0 retries despite real, substantial
                            # blocked wall time (measured: a 40s external
                            # hold produced `lock_retry_events=0`).
                            # `blocked_ms_before_finalize` is the directly
                            # `perf_counter`-measured lower bound as of this
                            # point in the pass (run-row creation + every
                            # batch attempt) — it does NOT yet include this
                            # finalize commit's own attempt time, since that
                            # has not happened yet when this closure runs;
                            # the fully-inclusive `blocked_ms` is on the
                            # RETURNED summary dict (see `run_once`'s
                            # in-memory result), just not re-persisted here
                            # to avoid a second write after this same commit.
                            #
                            # CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001: the
                            # measured lock WAIT (not the attempt's total
                            # wall time) is persisted here too, as scalars
                            # plus a FIXED-EDGE histogram. Fixed edges are
                            # the point: per-pass histograms add up across
                            # many passes into the `lock_wait_ms`
                            # DISTRIBUTION this milestone exists to make
                            # possible, without keeping an unbounded list of
                            # raw samples on every run row. Same "before
                            # finalize" caveat as `blocked_ms_before_
                            # finalize`: this closure runs BEFORE the
                            # finalize commit, so the finalize's own wait is
                            # on the returned summary, not in this column.
                            "write_coordination": {
                                "lock_retry_events": lock_retry_events,
                                "batches_committed": batches_committed,
                                "blocked_ms_before_finalize": int(
                                    blocked_seconds * 1000
                                ),
                                **{
                                    f"{k}_before_finalize" if k in (
                                        "lock_wait_ms", "lock_wait_measurements"
                                    ) else k: v
                                    for k, v in lock_accounting.as_summary().items()
                                },
                            },
                        }
                        session.add(run)

                _finalize_t0 = time.perf_counter()
                ok, attempts = self._commit_with_retry(
                    session, _prepare_finalize, max_lock_attempts,
                    lock_retry_seconds, sleeper,
                    budget_provider=_finalize_lock_wait_budget,
                    accounting=lock_accounting if lock_budget_active else None,
                )
                blocked_seconds += time.perf_counter() - _finalize_t0
                lock_retry_events += max(0, attempts - 1)
                summary["lock_retry_events"] = lock_retry_events
                summary["blocked_ms"] = int(blocked_seconds * 1000)
                summary.update(lock_accounting.as_summary())
                if not ok:
                    # The token batches already committed are real, durable,
                    # correct work — only the run row's own bookkeeping commit
                    # lost the lock race. Never raise: the reconciliation
                    # itself already succeeded (or partially succeeded);
                    # losing the summary row is not the same failure as
                    # losing data.
                    #
                    # LOW-3: if zero batches actually committed (e.g. the
                    # very first batch already exhausted its own retry ladder
                    # and set skipped_contention above), the finalize failure
                    # must NOT overwrite that with "partial" — "partial"
                    # implies some batches are durable, which would be false
                    # here on top of false.
                    summary["status"] = (
                        STATUS_PARTIAL if batches_committed > 0
                        else STATUS_SKIPPED_CONTENTION
                    )
                    summary["stop_reason"] = summary["stop_reason"] or "contention"
                    # BLOCKING-2 fix: use the captured local, not `run.id` —
                    # `_commit_with_retry` just rolled back internally on
                    # this failure path, so `run` is expired here.
                    summary["tape_run_id"] = run_id
                    finalize_error = (
                        "reconciliation batches committed, but the run row's "
                        "own finalize commit could not acquire the lock; the "
                        "run row stays status=running"
                    ) if batches_committed > 0 else (
                        "the run row's own finalize commit could not acquire "
                        "the lock either; the run row stays status=running"
                    )
                    # Append, don't replace: a deadline/contention stop
                    # earlier in the pass already set an "N batches / X of Y
                    # tokens" data-shortfall message, and that is different,
                    # equally real information from the finalize-commit
                    # failure — losing either one is a loss of signal.
                    summary["error"] = (
                        f"{summary['error']}; additionally, {finalize_error}"
                        if summary.get("error") else finalize_error
                    )
                    return summary
            else:
                # LEGACY single-transaction mode: one commit, no retry —
                # identical to the pre-milestone `_assemble_pass`.
                run.status = summary["status"]
                run.finished_at = finished
                run.duration_ms = max(
                    0, int((finished - started).total_seconds() * 1000)
                )
                run.tokens_considered = tokens_processed
                run.birth_events_created = new_births
                run.snapshots_created = snapshots
                run.actor_observations_created = actors
                run.outcomes_updated = outcomes
                run.provider_coverage = coverage_summary
                run.config = {
                    **(run.config or {}),
                    "write_classification": {
                        "skip_redundant_when_final": skip_redundant_when_final,
                        "snapshots_skipped_redundant": snapshots_skipped,
                        "actor_observations_skipped_redundant": actors_skipped,
                    },
                }
                session.commit()
            # BLOCKING-2 fix: use the captured local, not `run.id` — both
            # the chunked-success path (finalize just committed) and the
            # legacy path (`session.commit()` immediately above) expire
            # `run`, so a real second-connection lock at this exact line
            # would otherwise be a live, uncaught PK SELECT.
            summary["tape_run_id"] = run_id
            return summary
        except Exception as exc:
            # BLOCKER-1(a): the accounting dies with this frame unless it
            # rides out on the exception — see `_attach_lock_wait_evidence`.
            # Attached BEFORE the dry-run re-raise so both paths carry it.
            _attach_lock_wait_evidence(
                exc, lock_accounting, blocked_seconds, lock_retry_events
            )
            if dry_run:
                raise
            session.rollback()
            logger.exception("crypto lifecycle tape pass failed")
            # Best-effort error-row record. Under DB-lock contention the
            # error-recording commit can ITSELF fail; that must never mask the
            # original exception or leave the session in a pending-rollback
            # state (the CRYPTO-TAPE-CADENCE-002 crash). After the rollback the
            # run row is detached, so re-add before committing; swallow a
            # second failure and always re-raise the ORIGINAL error so the
            # caller can classify it (e.g. as database_locked).
            try:
                run.status = "error"
                run.error_type = type(exc).__name__
                run.error_message = str(exc)[:2000]
                run.finished_at = _now()
                run.duration_ms = max(
                    0, int((run.finished_at - started).total_seconds() * 1000)
                )
                session.add(run)
                session.commit()
            except Exception:
                session.rollback()
            raise
        finally:
            # CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001 — the lock-wait budget is
            # this pass's, not the process's. Put the connection's busy
            # timeout back on every exit path, including the error one.
            _restore_busy_timeout()


# --- report -------------------------------------------------------------------


def build_tape_report(session: Session, hours: int = 24, top: int = 5) -> dict:
    """DB-only lifecycle tape report: volumes, provider coverage, survival
    label distribution, risk distribution, actor-pattern examples, missing
    data. Read-only; no external call; never advice."""
    now = _now()
    cutoff = now - timedelta(hours=hours)
    runs = list(session.execute(
        select(CryptoTokenLifecycleRun)
        .where(CryptoTokenLifecycleRun.started_at >= cutoff)
        .order_by(CryptoTokenLifecycleRun.id.desc())
    ).scalars().all())
    run_ids = [r.id for r in runs]
    births = list(session.execute(
        select(CryptoTokenBirthEvent)
        .where(CryptoTokenBirthEvent.observed_at >= cutoff)
    ).scalars().all())
    snaps = list(session.execute(
        select(CryptoTokenLifecycleSnapshot)
        .where(CryptoTokenLifecycleSnapshot.run_id.in_(run_ids))
    ).scalars().all()) if run_ids else []
    actor_rows = list(session.execute(
        select(CryptoTokenActorObservation)
        .where(CryptoTokenActorObservation.run_id.in_(run_ids))
    ).scalars().all()) if run_ids else []
    outcomes = list(session.execute(
        select(CryptoTokenSurvivalOutcome)
        .where(CryptoTokenSurvivalOutcome.computed_at >= cutoff)
    ).scalars().all())

    coverage_mix: dict[str, int] = {}
    risk_mix: dict[str, int] = {}
    missing_mix: dict[str, int] = {}
    for snap in snaps:
        for item in snap.provider_coverage or []:
            coverage_mix[item] = coverage_mix.get(item, 0) + 1
        risk_mix[snap.risk_level or "unknown"] = risk_mix.get(
            snap.risk_level or "unknown", 0
        ) + 1
        for item in snap.missing_info or []:
            missing_mix[item] = missing_mix.get(item, 0) + 1

    label_mix: dict[str, dict] = {}
    for label in (
        "survived_15m", "survived_1h", "survived_6h", "survived_24h",
        "liquidity_removed", "dead_volume", "severe_risk",
        "graduated_or_migrated", "provider_gap",
    ):
        values = [getattr(o, label) for o in outcomes]
        label_mix[label] = {
            "true": sum(1 for v in values if v is True),
            "false": sum(1 for v in values if v is False),
            "unknown": sum(1 for v in values if v is None),
        }

    # actor-pattern examples: most concentrated holder structures observed
    concentrated = sorted(
        (a for a in actor_rows if (a.holder_distribution or {}).get("top10_holder_pct")),
        key=lambda a: -(a.holder_distribution or {}).get("top10_holder_pct", 0),
    )
    actor_examples = [
        {
            "token": a.token_address[:16],
            "top10_holder_pct": (a.holder_distribution or {}).get("top10_holder_pct"),
            "sniper_pct": (a.holder_distribution or {}).get("sniper_pct"),
            "insider_pct": (a.holder_distribution or {}).get("insider_pct"),
            "bundler_pct": (a.holder_distribution or {}).get("bundler_pct"),
            "creator_address_known": a.creator_address is not None,
        }
        for a in concentrated[:top]
    ]

    # NEW-H1 fix: CRYPTO-COVERAGE-REPAIR-001's B2 skip-when-final option makes
    # `len(snaps)`/distinct-snapshot-addresses an unreliable measure of how
    # many tokens a pass actually considered — a run using skip mode appends
    # NO snapshot/actor row for a token whose outcome was already final, which
    # visibly (and misleadingly) shrinks/reshapes coverage_mix/risk_mix/
    # tokens_observed for the SAME underlying tokens versus a non-skip run.
    # `run.tokens_considered` is recorded independently of whether a snapshot
    # was written, so it is the honest count. Skip totals and which runs used
    # skip mode are surfaced explicitly so a reader is never left guessing
    # whether "N snapshots" means "every considered token" or "only the
    # non-final ones".
    skip_mode_runs = sum(
        1 for r in runs
        if (r.config or {}).get("write_classification", {}).get(
            "skip_redundant_when_final"
        )
    )
    snapshots_skipped_redundant = sum(
        (r.config or {}).get("write_classification", {}).get(
            "snapshots_skipped_redundant", 0
        ) or 0
        for r in runs
    )
    actor_observations_skipped_redundant = sum(
        (r.config or {}).get("write_classification", {}).get(
            "actor_observations_skipped_redundant", 0
        ) or 0
        for r in runs
    )

    return {
        "note": TAPE_NOTE,
        "window_hours": hours,
        "generated_at": now.isoformat(),
        "tape_runs": len(runs),
        "tokens_observed": sum(r.tokens_considered for r in runs),
        "birth_events_in_window": len(births),
        "snapshots_recorded": len(snaps),
        "actor_observations_recorded": len(actor_rows),
        "outcomes_computed": len(outcomes),
        "outcomes_final": sum(1 for o in outcomes if o.final),
        "write_classification": {
            "skip_redundant_when_final_runs": skip_mode_runs,
            "snapshots_skipped_redundant": snapshots_skipped_redundant,
            "actor_observations_skipped_redundant": actor_observations_skipped_redundant,
        },
        "provider_coverage_mix": dict(
            sorted(coverage_mix.items(), key=lambda kv: -kv[1])
        ),
        "risk_level_mix": dict(sorted(risk_mix.items(), key=lambda kv: -kv[1])),
        "survival_labels": label_mix,
        "actor_pattern_examples": actor_examples,
        "missing_data_mix": dict(
            sorted(missing_mix.items(), key=lambda kv: -kv[1])[:10]
        ),
        "db_impact_rows": len(runs) + len(births) + len(snaps)
        + len(actor_rows) + len(outcomes),
        "disclaimer": (
            "research tape only — replayable lifecycle observation; labels are "
            "measured token behavior, never advice; no EV, no recommendation, "
            "no sizing, no orders, no wallets, no execution"
        ),
    }


# --- CRYPTO-TAPE-CADENCE-001: bounded manual tape session ----------------------
# A convenience wrapper over run_once so repeated passes can MATURE the
# 15m/1h/6h/24h survival horizons (CRYPTO-RETROSPECT-001 found provider_gap
# dominating precisely because horizons lacked observations). NOT a timer,
# NOT a daemon, NOT autonomous: one invocation runs a fixed, hard-capped
# number of derived (zero-external-call) passes with a sleep between, then
# exits. Aborts on abnormal capture status or a detectable MarketOps error.
# Dry-run persists nothing: it prints the planned schedule and runs exactly
# ONE dry probe pass — it never sleeps and never loops.

SESSION_MAX_DURATION_HOURS = 36
SESSION_INTERVAL_MIN_MINUTES = 15
SESSION_INTERVAL_MAX_MINUTES = 120
SESSION_MAX_CAPTURES = 144  # 36h at the 15-minute floor

SESSION_OK = "ok"
SESSION_DRY_RUN = "dry_run"
SESSION_ABORTED = "aborted"

# DB_LOCKED_MAX_ATTEMPTS / DB_LOCKED_RETRY_SECONDS / ABORT_DB_LOCKED now live
# near the top of this module (CRYPTO-COVERAGE-REPAIR-001) so `run_once` and
# `_assemble_pass` can use them as default argument values too.

SESSION_NOTE = (
    "Bounded manual tape session: repeated derived lifecycle passes so the "
    "15m/1h/6h/24h survival horizons can mature. Zero external calls, zero "
    "provider-budget impact (each pass reads persisted rows only). Not a "
    "timer, not a daemon, never autonomous; measurement only, never advice."
)


def new_token_ids_for_run(
    session: Session, crypto_run_id: int, chain: str = "solana"
) -> list[str]:
    """Canonical token ids FIRST persisted by exactly the given crypto
    discovery run (first_seen_at within the run's own start/finish window),
    in persistence order. Read-only; zero provider access. Returns [] for an
    unknown run — `record_discovery_run` re-validates and fails closed."""
    run = session.get(CryptoWatcherRun, crypto_run_id)
    if run is None:
        return []
    window_end = run.finished_at or _now().replace(tzinfo=None)
    return list(session.execute(
        select(CryptoToken.token_address).where(
            CryptoToken.chain == chain,
            CryptoToken.first_seen_at >= run.started_at,
            CryptoToken.first_seen_at <= window_end,
        ).order_by(CryptoToken.first_seen_at, CryptoToken.id)
    ).scalars().all())


def _is_db_locked(exc: BaseException | None) -> bool:
    """True when `exc` is (or wraps) a SQLite write-contention error that
    the DB_LOCKED_* retry ladder can plausibly recover from by waiting and
    retrying. Handles both SQLAlchemy OperationalError (via .orig) and raw
    sqlite3.OperationalError.

    NEW-MEDIUM-1 fix (third Lane-B review, SQLite coexistence): this
    predicate used to match ONLY "database is locked" / "database table is
    locked" (SQLITE_BUSY). A real competing writer under rollback-journal
    contention was measured to also produce "unable to open database file"
    (SQLITE_CANTOPEN) — busy_timeout does not retry that error class, this
    predicate did not match it either, so it fell through every typed
    handler (not locked, not LockUnavailableError, not IntegrityError) and
    re-raised as an uncaught Python traceback — with batches already
    durably committed and the run row orphaned at status='running': the
    same shape as the BLOCKING-1/2 lazy-load escapes, on a sibling error
    class. Broadened to also match "unable to open database file", "disk
    i/o error", and "database schema has changed" — all transient,
    contention-adjacent SQLite failure modes worth one more retry attempt
    rather than an immediate crash. (Also see `_handle_batch_operational_
    error`'s final catch-all, which now maps ANY OperationalError this
    predicate does not recognize to a typed `status="db_error"` result
    instead of letting it escape uncaught.)"""
    if exc is None:
        return False
    parts = [str(exc)]
    orig = getattr(exc, "orig", None)
    if orig is not None:
        parts.append(str(orig))
    text = " ".join(parts).lower()
    return any(
        marker in text
        for marker in (
            "database is locked",
            "database table is locked",
            "unable to open database file",
            "disk i/o error",
            "database schema has changed",
        )
    )


def _marketops_degraded(session: Session) -> bool:
    """Cheap detectable health check: latest MarketOps run errored. Mirrors
    the tennis session helper; kept local so the crypto lane imports no
    tennis/adapter modules."""
    try:
        from app.models import MarketOpsRun

        latest = session.execute(
            select(MarketOpsRun).order_by(MarketOpsRun.id.desc()).limit(1)
        ).scalars().first()
        return bool(latest is not None and latest.status == "error")
    except Exception:  # pragma: no cover - defensive
        return False


def summarize_tape_session(session: Session, run_ids: list[int]) -> dict:
    """Post-session maturity view over the runs this session persisted.
    Read-only; empty when no run committed (dry-run or abort before the first
    commit). Defensive: if the session is in a bad state after an abort, it
    rolls back and returns gracefully — it NEVER raises (the CADENCE-002
    PendingRollbackError-from-summary crash)."""
    if not run_ids:
        return {
            "available": False,
            "reason": "no runs committed (dry-run, or aborted before first capture)",
        }
    try:
        runs = list(session.execute(
            select(CryptoTokenLifecycleRun).where(
                CryptoTokenLifecycleRun.id.in_(run_ids)
            )
        ).scalars().all())
        outcomes = list(session.execute(
            select(CryptoTokenSurvivalOutcome).where(
                CryptoTokenSurvivalOutcome.last_run_id.in_(run_ids)
            )
        ).scalars().all())
    except Exception:  # a poisoned session must not crash the summary
        try:
            session.rollback()
        except Exception:
            pass
        return {
            "available": False,
            "reason": "summary unavailable (session error after abort; rolled back)",
        }
    totals = {
        "birth_events": sum(r.birth_events_created for r in runs),
        "snapshots": sum(r.snapshots_created for r in runs),
        "actor_observations": sum(r.actor_observations_created for r in runs),
        "outcomes_updated": sum(r.outcomes_updated for r in runs),
    }
    horizon_maturity = {}
    for label, _ in HORIZONS:
        key = f"survived_{label}"
        known = sum(1 for o in outcomes if getattr(o, key) is not None)
        horizon_maturity[key] = {"known": known, "unknown": len(outcomes) - known}
    return {
        "available": True,
        "runs": len(runs),
        "totals": totals,
        "outcomes_tracked": len(outcomes),
        "outcomes_final": sum(1 for o in outcomes if o.final),
        "horizon_maturity": horizon_maturity,
        "provider_gap_true": sum(1 for o in outcomes if o.provider_gap is True),
        "db_impact_rows": len(runs) + totals["birth_events"] + totals["snapshots"]
        + totals["actor_observations"] + totals["outcomes_updated"],
    }


def run_scheduled_reconciliation(
    session: Session,
    recorder: CryptoLifecycleTapeRecorder | None = None,
    *,
    settings=None,
    dry_run: bool = False,
    force: bool = False,
    window_hours: int | None = None,
    limit: int | None = None,
    batch_size: int | None = None,
    max_duration_seconds: float | None = None,
    sleeper=time.sleep,
    time_budget_seconds: float | None = None,
    initial_per_token_cost_seconds: float | None = None,
    lock_wait_budget_seconds: float | None = None,
) -> dict:
    """CRYPTO-COVERAGE-REPAIR-001 — one bounded, provider-free reconciliation
    pass over already-persisted tokens whose survival horizons have matured.

    Why this exists: `record_discovery_run` (the only tape path production
    actually runs) validates that every token was FIRST PERSISTED by the
    originating discovery run, so it sees each token exactly once, at age ~0,
    when no horizon is due. `run_once` — the windowed reconciler that would
    revisit matured tokens — is CLI-only and nothing schedules it. The result
    is that survival horizons never mature at all, and the ticks needed to
    mature them are pruned after `crypto_retention_days`.

    This is a THIN GOVERNED WRAPPER over the existing, already-proven
    `run_once`; it deliberately does not reimplement reconciliation. It adds
    only: a default-OFF gate, explicit bounds, and a structured disabled/ok
    result so a scheduled unit is a clean no-op when the flag is off.

    Guarantees: zero external calls (no provider, no discovery scan, no second
    universe fetch), zero provider-budget impact, no cohort, no arming, no
    observation unit, and no trade-execution capability of any kind. The
    canonical boundary vocabulary for this lane is TAPE_NOTE, which is carried
    in the `note` field of EVERY result this function returns, including the
    disabled and refused ones — see docs/SAFETY_BOUNDARIES.md.

    Honest limits, stated because overclaiming here has bitten this repo before:

    * Labels are idempotent — reconciliation recomputes deterministic labels
      from persisted rows, updating the outcome row in place. The pass is
      MOSTLY, not fully, row-idempotent: `_assemble_pass` APPENDS one
      lifecycle snapshot and one actor observation per token considered on
      every pass. `skip_redundant_when_final` (B2) exists to skip that append
      once a token's outcome is already final, but on THIS path it is
      structurally INERT: this call always pairs it with `exclude_final=True`
      (below), which already drops every already-final token from the
      selected set at the query level — `final_by_birth_id` is therefore
      all-False for every token this pass ever considers, so the
      already-final branch can never trigger and
      `snapshots_skipped_redundant`/`actor_observations_skipped_redundant`
      stay 0 on every scheduled pass (see
      test_scheduled_pass_is_row_idempotent_once_final and
      test_scheduled_path_skip_redundant_structurally_inert). Neither table
      is covered by `retention.py`. Budget `2 x tokens_processed` permanently
      retained rows per pass (not reduced by any already-final skip on this
      path).
    * Restart-safe (B3/B6): `run_once` commits in bounded batches
      (`crypto_tape_reconciler_batch_size`, default `RECONCILE_BATCH_SIZE`)
      instead of one transaction for the whole pass, and stops at an internal
      wall-clock deadline (`crypto_tape_reconciler_max_duration_seconds`,
      default `RECONCILE_MAX_DURATION_SECONDS`). A crash or a hit deadline
      after batch N leaves batches 1..N durably committed and nothing
      duplicated; the next pass continues via oldest-first + state-driven
      backlog selection. `status="partial"` (with `stop_reason`) reports this
      explicitly rather than claiming `status="ok"`.
    * Overlap-safe (B4): a non-blocking, per-chain flock guards the whole
      pass, so a second concurrent instance (another scheduled tick, a manual
      tape session, a stray CLI run) never races this one on the same window;
      it gets `status="skipped_overlap"` instead of the IntegrityError this
      milestone measured. NEW-H3 CORRECTION (third re-review): this flock
      does NOT, by itself, close the race against the exact-cycle anchor
      feed (`record_discovery_run`) — that caller deliberately opts OUT of
      the overlap lock (`use_overlap_lock=False`, a single bounded
      transaction over a validated ≤40-token set that must never be skipped
      by a held lock). What actually keeps the two callers' token sets
      disjoint is the HIGH-1 age exclusion: `record_discovery_run` only ever
      consolidates tokens at age ~0 (first persisted by the originating
      discovery run, by construction), and this function excludes any token
      younger than `SHORTEST_HORIZON_CLOSING_EDGE_MINUTES` from selection —
      so under normal operation the sets never overlap. That is a strong
      mitigation, not a proof for every possible clock/timing edge, so an
      `IntegrityError` (the exact failure a live race produces — a raw
      unique-constraint violation, NOT an `OperationalError`, so the
      DB-locked retry ladder does not apply to it) is still caught below and
      turned into a typed, non-zero-exit `status="concurrent_write_conflict"`
      result rather than an uncaught traceback that would kill the unit.
    * Contention-safe (B5): reuses the same DB_LOCKED_* bounded retry ladder
      `run_tape_session` already uses, per batch commit. Exhausting it before
      even the run row can be created yields `status="skipped_contention"`
      (nothing written); exhausting it after some batches already committed
      yields `status="partial"`, `stop_reason="contention"`.
    * The gate is honest about being bypassed: `force` and `dry_run` both run a
      pass while the flag is off, and the result says which.

    Measurement only, never advice."""
    s = settings if settings is not None else get_settings()
    started = _now()
    enabled = bool(getattr(s, "enable_crypto_tape_reconciler", False))
    bypass = "force" if force else ("dry_run" if dry_run else None)
    if not (enabled or force or dry_run):
        return {
            "status": "disabled",
            "note": TAPE_NOTE,
            "mode": "scheduled_reconciliation",
            "external_calls": 0,
            "tokens_considered": 0,
            "outcomes_updated": 0,
            "flag": "enable_crypto_tape_reconciler",
            "gate_bypassed": None,
            "duration_ms": 0,
        }

    hours = window_hours if window_hours is not None else int(
        getattr(s, "crypto_tape_reconciler_window_hours", 48)
    )
    cap = limit if limit is not None else int(
        getattr(s, "crypto_tape_reconciler_limit", 2000)
    )

    def _refused(status: str, error: str, exc: BaseException | None = None) -> dict:
        # BLOCKER-1(a) fix: every refusal carries the lock-wait telemetry
        # contract, never a set of missing keys the CLI prints as `None`.
        # For a validation refusal the zeros are literally true (nothing was
        # read or written); for `db_locked` — the one refusal that IS a
        # contention outcome, and therefore the one the lock-wait gate most
        # needs — `_lock_wait_evidence` returns what the abandoned pass had
        # actually measured before it gave up.
        #
        # `status` must fit `CryptoTokenLifecycleRun.status` (VARCHAR(32)) in
        # case a future refusal ever reaches a run row; today none does. The
        # longest shipped value, `invalid_lock_wait_budget_seconds`, is
        # exactly 32 — zero headroom, pinned by
        # test_every_refused_status_fits_the_run_row_status_column.
        return {
            "status": status,
            "note": TAPE_NOTE,
            "mode": "scheduled_reconciliation",
            "external_calls": 0,
            "tokens_considered": 0,
            "outcomes_updated": 0,
            "gate_bypassed": bypass,
            "error": error,
            **_lock_wait_evidence(exc),
            "duration_ms": max(0, int((_now() - started).total_seconds() * 1000)),
        }

    if cap < 1:
        # SQLite treats LIMIT -1 as "no limit", so an unvalidated cap is an
        # unbounded pass wearing a bound's clothing.
        return _refused("invalid_limit", f"limit {cap} must be >= 1")

    # The window must outlast the longest horizon's closing edge PLUS one
    # scheduling interval, or a token can mature and fall out of the window
    # between two passes without ever being reconciled.
    closing_edge_hours = int(max(m for _, m in HORIZONS) * (1 + HORIZON_TOLERANCE) / 60)
    required_hours = closing_edge_hours + RECONCILER_CADENCE_HOURS
    if hours < required_hours:
        return _refused(
            "invalid_window",
            f"window {hours}h is shorter than the longest horizon closing edge "
            f"{closing_edge_hours}h plus one {RECONCILER_CADENCE_HOURS}h "
            f"scheduling interval; matured outcomes would be missed",
        )

    # BLOCKER-2 fix (independent review of this branch): the MarketOps-health
    # check is a READ, and reads block behind an EXCLUSIVE holder. It used to
    # run here, ahead of every bound this function resolves, at the
    # process-wide `sqlite_busy_timeout_ms`. It now runs below, after the
    # bounds are known, INSIDE the lock-wait budget that covers the whole
    # prelude. Moving it also means a misconfigured pass is refused for the
    # misconfiguration (which is always actionable) rather than for host
    # state that will have changed by the next tick.
    rec = recorder or CryptoLifecycleTapeRecorder(CryptoTapeConfig.from_settings(s))
    batch = batch_size if batch_size is not None else int(
        getattr(s, "crypto_tape_reconciler_batch_size", RECONCILE_BATCH_SIZE)
    )
    deadline_seconds = max_duration_seconds if max_duration_seconds is not None else float(
        getattr(
            s, "crypto_tape_reconciler_max_duration_seconds",
            RECONCILE_MAX_DURATION_SECONDS,
        )
    )
    # NEW-HIGH-2 fix (third Lane-B review, SQLite coexistence).
    # `batch_size` IS the write-lock safety argument this whole milestone
    # rests on, and — unlike `limit` (validated at :2716 above) — it was
    # completely unvalidated. Measured: `batch_size=0` -> one "batch" is the
    # WHOLE pass (Python slicing `tokens[i:i+0]` never advances, so the
    # chunking loop degenerates); `batch_size=-5` -> `status="ok"`,
    # `tokens_processed=0`, CLI exit 0 — a green unit that reconciled
    # NOTHING, the exact failure class this milestone exists to remove;
    # `batch_size=5000` (>= a typical `limit`) -> same monolithic-pass
    # collapse the `limit` check above already guards against on the OTHER
    # side of the ratio. `app/cli.py` added two new ways to set this
    # (`--batch-size` and the `.env`/Settings default), which is what made
    # this reachable by any operator, not just direct callers.
    if batch < 1:
        return _refused(
            "invalid_batch_size", f"batch_size {batch} must be >= 1"
        )
    if batch >= cap:
        return _refused(
            "invalid_batch_size",
            f"batch_size {batch} >= selection limit {cap} restores the "
            "single-transaction pass this milestone exists to remove",
        )
    # LOW-3 fix (same review): `max_duration_seconds <= 0` degenerates the
    # deadline check (`_now() >= deadline` is true before the first batch
    # even starts, in the `chunked and ... and tokens_processed > 0` guard —
    # actually the `tokens_processed > 0` guard means a <=0 deadline is
    # SAFE at the first batch, but `0.0` is also the intentional
    # "already-past-due" sentinel several existing tests use deliberately
    # (`test_deadline_stops_the_pass_between_batches_not_mid_batch`,
    # `test_scheduled_reconciliation_reports_partial_status_not_ok`) to stop
    # after exactly one batch. A NEGATIVE deadline has no such legitimate
    # use and is nonsensical (a deadline before the pass even started);
    # reject only that, not 0.0.
    if deadline_seconds is not None and deadline_seconds < 0:
        return _refused(
            "invalid_max_duration_seconds",
            f"max_duration_seconds {deadline_seconds} must be >= 0",
        )
    # CRYPTO-COVERAGE-REPAIR-001 B1/B3/B11 — the adaptive time-budgeted
    # batching knobs. Both default to `None` (mechanism OFF, `batch` above
    # keeps governing byte-for-byte as before) because
    # `initial_per_token_cost_seconds` is an UNCALIBRATED value with
    # deliberately no built-in fallback (see the Settings field docstring) —
    # only an explicit, positive value (caller kwarg or
    # `crypto_tape_reconciler_initial_per_token_cost_seconds`) activates it.
    # Once active, `batch` (validated above) becomes ONLY a sanity MAXIMUM
    # (B11) — the write-time budget always dominates batch sizing.
    resolved_initial_cost = (
        initial_per_token_cost_seconds if initial_per_token_cost_seconds is not None
        else getattr(s, "crypto_tape_reconciler_initial_per_token_cost_seconds", None)
    )
    resolved_time_budget = (
        time_budget_seconds if time_budget_seconds is not None
        else getattr(s, "crypto_tape_reconciler_time_budget_seconds", None)
    )
    if resolved_initial_cost is not None and resolved_initial_cost <= 0:
        return _refused(
            "invalid_initial_per_token_cost_seconds",
            f"initial_per_token_cost_seconds {resolved_initial_cost} must be "
            "> 0 — refusing to activate adaptive batching with a "
            "non-positive UNCALIBRATED value",
        )
    if resolved_time_budget is not None and resolved_time_budget <= 0:
        return _refused(
            "invalid_time_budget_seconds",
            f"time_budget_seconds {resolved_time_budget} must be > 0",
        )
    # CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001 — the LOCK-WAIT budget. This is
    # NOT `time_budget_seconds` (which bounds how long a transaction may
    # HOLD the write lock) and the two must never be conflated. None keeps
    # the deadline-derived budget (see `derive_lock_wait_budget_seconds`),
    # which is what ships; a positive value is an operator CAP on top of it,
    # for the day the persisted `lock_wait_ms` histogram has produced a real
    # contention distribution to set it from.
    resolved_lock_wait_budget = (
        lock_wait_budget_seconds if lock_wait_budget_seconds is not None
        else getattr(s, "crypto_tape_reconciler_lock_wait_budget_seconds", None)
    )
    if resolved_lock_wait_budget is not None and resolved_lock_wait_budget <= 0:
        return _refused(
            "invalid_lock_wait_budget_seconds",
            f"lock_wait_budget_seconds {resolved_lock_wait_budget} must be "
            "> 0 — a zero/negative wait budget makes every contended batch "
            "fail before it has waited at all",
        )
    try:
        summary = rec.run_once(
            session, limit=cap, hours=hours, dry_run=dry_run,
            oldest_first=True,
            include_backlog=True,
            # B2 fix: state-driven selection — already-final tokens are
            # dropped from the query so a deadline-stopped pass advances on
            # its next invocation, and the wall-clock budget is never spent
            # re-walking tokens with nothing left to learn.
            exclude_final=True,
            # HIGH-1 fix: tokens younger than the shortest horizon's own
            # closing edge have NO horizon due yet (compute_survival can only
            # set `final` at the 24h edge, so exclude_final alone leaves the
            # whole recent tail selectable forever). Excluding them bounds
            # selection to genuinely reconcilable tokens and removes the
            # age-0 overlap with the exact-cycle anchor feed entirely.
            min_age_minutes=SHORTEST_HORIZON_CLOSING_EDGE_MINUTES,
            run_config_extra={
                "mode": "scheduled_reconciliation",
                "forced": bool(force),
            },
            # B2: once a token's outcome is final, this path (only this path)
            # skips the now-redundant snapshot/actor rows. The manual path
            # (run_tape_session/CLI) never sets this, so its behaviour is
            # unchanged.
            skip_redundant_when_final=True,
            # B3: bounded commits instead of one transaction for the pass.
            batch_size=batch,
            # B6: internal wall-clock deadline; the rest becomes backlog.
            max_duration_seconds=deadline_seconds,
            sleeper=sleeper,
            # B1/B3: inert unless resolved_initial_cost is an explicit,
            # positive, measured value — see the resolution block above.
            time_budget_seconds=resolved_time_budget,
            initial_per_token_cost_seconds=resolved_initial_cost,
            # CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001: an operator-set CAP on
            # the deadline-derived per-lock-acquisition wait budget. None
            # (the default) means the derived budget governs alone.
            lock_wait_budget_seconds=resolved_lock_wait_budget,
        )
    except Exception as exc:
        # A poisoned transaction must not leak into the caller's session, and a
        # locked database is an expected operational condition on this host,
        # not a crash — the manual path already treats it that way.
        try:
            session.rollback()
        except Exception:  # pragma: no cover - defensive
            pass
        if _is_db_locked(exc):
            return _refused("db_locked", "database is locked; pass abandoned")
        if isinstance(exc, LockUnavailableError):
            # MEDIUM fix: an unwritable/missing lock directory previously
            # produced an uncaught traceback here; report a typed refused
            # status instead (the CLI turns this into a non-zero exit, same
            # as db_locked/skipped_overlap/skipped_contention).
            return _refused(STATUS_LOCK_UNAVAILABLE, str(exc))
        if isinstance(exc, IntegrityError):
            # NEW-H3 fix: the exact-cycle anchor feed (`record_discovery_run`)
            # deliberately opts out of the overlap lock, so a residual race
            # (if the HIGH-1 age-disjointness mitigation is ever defeated by
            # a clock/timing edge) surfaces as a real IntegrityError, not an
            # OperationalError — the DB-locked retry ladder never applies to
            # it. This used to propagate uncaught and kill the systemd unit;
            # now it is a typed, non-zero-exit refused status.
            return _refused(
                STATUS_CONCURRENT_WRITE_CONFLICT,
                f"unique-constraint conflict during reconciliation, most "
                f"likely a race with a concurrent tape writer outside the "
                f"overlap lock (e.g. the exact-cycle anchor feed): {exc}",
            )
        if isinstance(exc, OperationalError):
            # NEW-MEDIUM-1 fix (third Lane-B review, SQLite coexistence).
            # `_is_db_locked` is a string-match HEURISTIC over known SQLite
            # error text; it cannot enumerate every OperationalError shape a
            # competing writer under real contention can produce. Before
            # this fix, any OperationalError that predicate did not
            # recognize fell through every typed handler above (not locked,
            # not LockUnavailableError, not IntegrityError) and re-raised as
            # an uncaught Python traceback — with batches already durably
            # committed and the run row orphaned at status='running': the
            # same shape as the BLOCKING-1/2 lazy-load escapes fixed
            # earlier, on a sibling error path. This is deliberately the
            # LAST arm, after every more-specific OperationalError shape
            # above has had a chance to match, so it never masks a more
            # actionable classification.
            return _refused(
                "db_error",
                f"unrecognized SQLite OperationalError during reconciliation "
                f"(batches already committed, if any, are durable): {exc}",
            )
        raise

    # `run_once` may already have returned a terminal, non-"ok" status
    # (skipped_overlap / skipped_contention / partial — B3/B4/B5/B6). Those
    # must NOT be silently overwritten with "ok"/"dry_run"; that is exactly
    # the "a unit that reconciles nothing must never look healthy" failure
    # class this milestone exists to remove.
    terminal_statuses = {
        STATUS_SKIPPED_OVERLAP, STATUS_SKIPPED_CONTENTION, STATUS_PARTIAL,
        STATUS_DRY_RUN_PARTIAL, STATUS_UNSAFE_HOST_COST,
    }
    pass_status = summary.get("status")
    resolved_status = (
        pass_status if pass_status in terminal_statuses
        else ("dry_run" if dry_run else "ok")
    )
    summary.update({
        "status": resolved_status,
        "mode": "scheduled_reconciliation",
        "external_calls": 0,
        "window_hours": hours,
        "selection_limit": cap,
        "batch_size": batch,
        "max_duration_seconds": deadline_seconds,
        "lock_wait_budget_seconds": resolved_lock_wait_budget,
        "gate_bypassed": bypass,
        "duration_ms": max(0, int((_now() - started).total_seconds() * 1000)),
    })
    if summary.get("truncated"):
        # Loud, not silent: this is the exact failure this milestone exists to
        # remove, so it must be visible in the result AND in the log. A
        # deadline/contention stop (status already partial/skipped_*) is
        # reported through `stop_reason`/`error` instead of being relabelled
        # "truncated", which is specifically the SELECTION-limit case.
        if resolved_status not in terminal_statuses:
            summary["status"] = "truncated"
            # MEDIUM fix (fourth re-review): `run_once`'s own finalize step
            # (crypto_tape.py, `_prepare_finalize`/legacy commit) already
            # persisted the run row with whatever status IT computed —
            # which, for a pass that completed fully with no deadline/
            # contention stop, is "ok". The "truncated" relabelling above
            # happens entirely out here, in the caller, using information
            # (`universe_size`/`backlog_size`/`cap`) `run_once` never had.
            # Before this fix that relabelling was IN-MEMORY ONLY: the
            # durable row stayed status="ok" forever, so `build_tape_report`
            # (which reads that table, not this return value) told a
            # reader a pass that dropped a real fraction of its work was
            # healthy. Best-effort, bounded, retried UPDATE of just the one
            # column on just that one row — never raises: losing this
            # correction is a real but strictly smaller loss than losing
            # the reconciliation work itself, so it must never crash the
            # caller or overwrite `summary["status"]` back to something the
            # caller didn't ask for.
            tape_run_id = summary.get("tape_run_id")
            if tape_run_id is not None:
                for attempt in range(1, DB_LOCKED_MAX_ATTEMPTS + 1):
                    try:
                        session.execute(
                            update(CryptoTokenLifecycleRun)
                            .where(CryptoTokenLifecycleRun.id == tape_run_id)
                            .values(status="truncated")
                        )
                        session.commit()
                        break
                    except OperationalError as exc:
                        session.rollback()
                        if _is_db_locked(exc) and attempt < DB_LOCKED_MAX_ATTEMPTS:
                            time.sleep(DB_LOCKED_RETRY_SECONDS)
                            continue
                        logger.warning(
                            "crypto reconciliation: could not persist the "
                            "truncated relabel onto run row %s (%s); the "
                            "returned status is still correctly "
                            "'truncated', only the durable row's status "
                            "column stays stale", tape_run_id, exc,
                        )
                        break
        # NEW-HIGH-1(b)/(c) fix (second re-review, convergence lens):
        # `summary.get("error") or (...)` meant that whenever a pass was
        # BOTH deadline/contention-stopped AND selection-limit-truncated —
        # measured to be EVERY pass at production density — the
        # deadline/contention text (set inside `_assemble_pass_locked`, and
        # already present in `summary["error"]` by this point) always won,
        # and the truncation text below — the ONLY message naming
        # `universe_size`/`backlog_size`/`tokens_omitted`, i.e. the frozen
        # backlog and how much of it went unworked — was silently thrown
        # away. It never even reached the log line just below, which
        # therefore mentioned neither the frozen backlog nor
        # `backlog_processed`. APPEND instead of `or`, so both pieces of
        # real, distinct information survive together.
        truncation_note = (
            f"window holds {summary['universe_size']} tokens plus "
            f"{summary['backlog_size']} aged-out unreconciled, but the limit is "
            f"{cap}; {summary['tokens_omitted']} were not reconciled "
            f"(backlog_processed={summary.get('backlog_processed')}). Raise "
            f"--limit (or crypto_tape_reconciler_limit) to cover the window."
        )
        existing_error = summary.get("error")
        summary["error"] = (
            f"{existing_error}; additionally, {truncation_note}"
            if existing_error else truncation_note
        )
        logger.warning("crypto reconciliation truncated: %s", summary["error"])
    if resolved_status in (
        STATUS_SKIPPED_OVERLAP, STATUS_SKIPPED_CONTENTION, STATUS_PARTIAL,
        STATUS_DRY_RUN_PARTIAL,
    ):
        logger.warning(
            "crypto reconciliation %s: %s", resolved_status, summary.get("error")
        )
    # NEW-HIGH-2 fix (second re-review, convergence lens): the routine
    # `partial`/`truncated` statuses fire on EVERY pass at production
    # density (see `RECONCILE_CADENCE_HOURS`'s note above) and carry no
    # signal on their own about whether the frontier is actually at risk.
    # `backlog_expiring` is the separable, rare, actionable status: the
    # oldest still-open/never-reconciled token's age has crossed
    # `crypto_retention_days*24 - RECONCILER_CADENCE_HOURS` — i.e. it will
    # be pruned before the NEXT scheduled pass can reach it, regardless of
    # whether THIS pass's own truncated/partial shortfall is otherwise
    # ordinary. Deliberately does not override the hard-refused statuses
    # (those already returned above, before this point is ever reached).
    # CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B3 — the RAW frontier
    # above (`oldest_unreconciled_age_hours`) measures the oldest BACKLOG
    # ROW regardless of whether anything can still be learned from it. Once
    # any `RETENTION_LOST`/`MISSING_REQUIRED_INITIAL_STATE` write-off sits
    # in the backlog — which, once a pass ever falls behind by more than
    # `crypto_retention_days`, it always will — that row is a PERMANENT
    # write-off, so the raw frontier can never again drop below the
    # threshold even on a perfectly healthy pass: `backlog_expiring` was
    # measured permanently ON (frontier ~204h vs a 162h threshold, the
    # oneshot unit sitting permanently failed) — exactly the "alarm always
    # on carries no information" failure this status exists to avoid.
    # `recoverable_backlog_summary` (reusing `classify_backlog`, never
    # re-deriving the predicate) gives the frontier that actually answers
    # "is there real, at-risk work this pass could still do" — write-offs
    # excluded, because no amount of scheduling saves them.
    # `run_once` already computed this PRE-PASS (see its own comment) and
    # put it straight on `summary` — recomputing here would measure the
    # backlog AFTER this pass's own writes, hiding exactly the shortfall
    # this status exists to report.
    frontier_seconds = summary.get("oldest_recoverable_age_seconds")
    if frontier_seconds is not None:
        frontier_hours = frontier_seconds / 3600.0
        retention_days = int(getattr(s, "crypto_retention_days", 7))
        frontier_threshold_hours = (
            retention_days * 24 - RECONCILER_CADENCE_HOURS
        )
        if frontier_hours >= frontier_threshold_hours:
            summary["status"] = STATUS_BACKLOG_EXPIRING
            summary["frontier_threshold_hours"] = frontier_threshold_hours
            frontier_note = (
                f"the oldest RECOVERABLE unreconciled token is "
                f"{frontier_hours:.1f}h old, >= the "
                f"{frontier_threshold_hours:.1f}h frontier threshold "
                f"(crypto_retention_days={retention_days}d minus one "
                f"{RECONCILER_CADENCE_HOURS}h cadence interval); it may be "
                "pruned before the next scheduled pass reaches it "
                f"(write-offs excluded: {summary.get('writeoff_count')})"
            )
            summary["error"] = (
                f"{summary['error']}; additionally, {frontier_note}"
                if summary.get("error") else frontier_note
            )
            logger.warning("crypto reconciliation backlog_expiring: %s", frontier_note)
    return summary


def _reconciliation_should_abort(session: Session) -> bool:
    """Do not add write pressure while MarketOps is already unhealthy. Mirrors
    the manual session path's abort, which the scheduled path previously
    lacked — the scheduled path must not be less careful than the manual one."""
    return _marketops_degraded(session)


async def run_tape_session(
    session: Session,
    recorder: CryptoLifecycleTapeRecorder | None = None,
    duration_hours: int = 6,
    interval_min: int = 30,
    limit: int | None = None,
    hours: int | None = None,
    dry_run: bool = False,
    sleeper=None,
    max_lock_attempts: int = DB_LOCKED_MAX_ATTEMPTS,
    lock_retry_seconds: float = DB_LOCKED_RETRY_SECONDS,
) -> dict:
    """Bounded manual tape session: a fixed, hard-capped number of derived
    run_once passes with a sleep between, then exit. Aborts on abnormal pass
    status or a detectable MarketOps error — EXCEPT a transient overlap
    (`status="skipped_overlap"`, the scheduled reconciler holding the
    per-chain lock for this one capture), which is expected routine
    contention between two legitimate passes: that capture is skipped and
    the session continues (see `overlap_skipped_captures` in the result).
    Lock-safe: a capture that hits a locked DB is rolled back and retried up
    to `max_lock_attempts`; a persistent lock aborts cleanly
    (reason=database_locked) with the session already rolled back.
    Measurement only — never advice."""
    import asyncio

    sleeper = sleeper or asyncio.sleep
    duration_hours = max(1, min(duration_hours, SESSION_MAX_DURATION_HOURS))
    interval_min = max(
        SESSION_INTERVAL_MIN_MINUTES, min(interval_min, SESSION_INTERVAL_MAX_MINUTES)
    )
    captures_planned = min(
        max(1, (duration_hours * 60) // interval_min), SESSION_MAX_CAPTURES
    )
    recorder = recorder or CryptoLifecycleTapeRecorder()
    started = _now()
    planned_schedule_min = [i * interval_min for i in range(captures_planned)]

    if dry_run:
        # one dry probe proves the pass works; nothing persisted, no sleeping
        probe = recorder.run_once(session, limit=limit, hours=hours, dry_run=True)
        return {
            "note": SESSION_NOTE,
            "status": SESSION_DRY_RUN,
            "aborted": False,
            "abort_reason": None,
            "failed_capture_index": None,
            "started_at": started.isoformat(),
            "duration_hours": duration_hours,
            "interval_min": interval_min,
            "captures_planned": captures_planned,
            "captures_run": 1,
            "capture_statuses": [probe["status"]],
            "overlap_skipped_captures": 0,
            "planned_schedule_min": planned_schedule_min,
            "rows_written_before_abort": 0,
            "probe": {
                "tokens_considered": probe["tokens_considered"],
                "external_calls": probe["external_calls"],
                "survival_label_mix": probe["survival_label_mix"],
            },
            "provider_gap_trend": None,
            "session_summary": {"available": False,
                                "reason": "no persisted runs (dry-run session)"},
            "tape_run_ids": [],
        }

    captures: list[dict] = []
    run_ids: list[int] = []
    abort_reason = None
    failed_capture_index: int | None = None
    overlap_skipped_captures = 0
    for i in range(captures_planned):
        # --- one capture with bounded, lock-safe retry -------------------------
        result = None
        last_exc: BaseException | None = None
        for attempt in range(1, max_lock_attempts + 1):
            try:
                result = recorder.run_once(session, limit=limit, hours=hours)
                break
            except Exception as exc:
                last_exc = exc
                # ANY failed flush/commit poisons the transaction — always
                # rollback so the session stays usable (and the summary path
                # never hits PendingRollbackError).
                try:
                    session.rollback()
                except Exception:  # pragma: no cover - defensive
                    pass
                if _is_db_locked(exc) and attempt < max_lock_attempts:
                    logger.warning(
                        "crypto tape session: capture %d hit a locked database — "
                        "retry %d/%d in %.1fs",
                        i + 1, attempt, max_lock_attempts - 1, lock_retry_seconds,
                    )
                    await sleeper(lock_retry_seconds)
                    continue
                break  # non-locked error, or lock retries exhausted
        if result is None:
            failed_capture_index = i
            abort_reason = (
                ABORT_DB_LOCKED if _is_db_locked(last_exc)
                else f"capture {i + 1} raised {type(last_exc).__name__}"
            )
            break

        captures.append(result)
        if result.get("tape_run_id"):
            run_ids.append(result["tape_run_id"])
        if result["status"] == STATUS_SKIPPED_OVERLAP:
            # NEW-H3 fix: `run_once`'s overlap flock now defaults ON, so a
            # scheduled reconciliation pass (fires up to 4x/day) can
            # transiently hold the lock during exactly one capture window of
            # a bounded manual session. That is expected, routine contention
            # between two legitimate passes — not a reason to kill an entire
            # 6h/36h session. Skip THIS capture and continue; every other
            # non-ok status still aborts the session below, unchanged.
            overlap_skipped_captures += 1
            logger.warning(
                "crypto tape session: capture %d skipped (overlap lock held "
                "by another reconciliation pass); continuing", i + 1,
            )
            if i < captures_planned - 1:
                await sleeper(interval_min * 60)
            continue
        if result["status"] != STATUS_OK:
            failed_capture_index = i
            abort_reason = f"capture {i + 1} status={result['status']}"
            break
        if _marketops_degraded(session):
            failed_capture_index = i
            abort_reason = "latest MarketOps run errored"
            break
        if i < captures_planned - 1:
            await sleeper(interval_min * 60)

    def gap_share(capture: dict) -> float | None:
        tokens = capture.get("tokens_considered") or 0
        if not tokens:
            return None
        return round(capture["survival_label_mix"].get("provider_gap", 0) / tokens, 4)

    trend = None
    if len(captures) >= 2:
        first, last = gap_share(captures[0]), gap_share(captures[-1])
        if first is not None and last is not None:
            trend = {
                "first_capture_gap_share": first,
                "last_capture_gap_share": last,
                "direction": (
                    "improving" if last < first
                    else ("worsening" if last > first else "flat")
                ),
            }

    # rows written before an abort, computed from the successful captures
    # (independent of the DB summary, so it survives a poisoned session).
    # NEW-L1 fix: a `skipped_overlap` capture writes NOTHING (the pass never
    # got past the flock) — it must not count "1" for a run row that was
    # never created; only real (non-skipped-overlap) captures wrote a run row.
    rows_written_before_abort = sum(
        (1 if c.get("status") != STATUS_SKIPPED_OVERLAP else 0)  # the run row
        + c.get("birth_events_created", 0)
        + c.get("snapshots_created", 0)
        + c.get("actor_observations_created", 0)
        + c.get("outcomes_updated", 0)
        for c in captures
    )

    return {
        "note": SESSION_NOTE,
        "status": SESSION_ABORTED if abort_reason else SESSION_OK,
        "aborted": bool(abort_reason),
        "abort_reason": abort_reason,
        "failed_capture_index": failed_capture_index,
        "started_at": started.isoformat(),
        "duration_hours": duration_hours,
        "interval_min": interval_min,
        "captures_planned": captures_planned,
        "captures_run": len(captures),
        "capture_statuses": [c["status"] for c in captures],
        "overlap_skipped_captures": overlap_skipped_captures,
        "planned_schedule_min": planned_schedule_min,
        "provider_gap_trend": trend,
        "rows_written_before_abort": rows_written_before_abort,
        "session_summary": summarize_tape_session(session, run_ids),
        "tape_run_ids": run_ids,
    }
