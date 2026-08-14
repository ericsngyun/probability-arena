"""CRYPTO-COVERAGE-REPAIR-002 — prospective sparse observation.

WHY THIS EXISTS
---------------
CRYPTO-COVERAGE-REPAIR-001 finished the *retrospective* half of the problem and
its own numbers closed the door on doing more of it. On production, of 1,182
finalized survival outcomes only 54 (4.57%) carry a real 24h observation and
1,026 (86.8%) are classified `permanently_missing_evidence`; coverage by horizon
is 15m 80.9%, 1h 81.1%, 6h 16.8%, 24h 4.6%. The cliff sits between 1h and 6h,
and it is not pruning and not reconciliation capacity: the median token's LAST
tick is ~83 minutes after birth. **Tokens simply stop being observed.** A single
bounded reconciliation pass already drained the recoverable pool from 1,043 to
~106 against an ~11,516-row backlog that is ~99% permanent write-offs — there is
nothing left to reconcile harder.

So this lane does the other half: it OBSERVES PROSPECTIVELY.

    birth -> scheduled 6h observation -> scheduled 24h observation -> reconciled

THE MECHANISM IN ONE PARAGRAPH
------------------------------
One standing, ROLLING cohort (`crypto_horizon_cohorts`, marked
`provenance["membership"] = MEMBERSHIP_ROLLING`) admits eligible new births.
Every scheduled pass (a) enrols newly-eligible births and (b) fetches
market/liquidity state via the existing read-only DexScreener adapter for every
member whose 6h or 24h SPARSE BAND is open right now, writing one ordinary
`crypto_price_tick` plus one `crypto_horizon_observations` audit row per
observation. At most TWO attempts per (token, horizon), ever, and the second
only when the first got no ANSWER (see `_is_retryable`). The pure planner
(`crypto_horizon.plan_observations`), the pair-selection policy
(`crypto_horizon.select_pair`) and the record/miss semantics
(`CryptoHorizonService._record_observation`) are REUSED unchanged — this module
is eligibility, governance, transaction shape and reporting, not a second
scheduler.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* No canary. No cohort arming, no one-shot timers, no orchestrator manifest.
  The standing cohort is explicitly refused by `build_arm_plan`.
* No second scheduler. No new planner, no new window arithmetic, no new
  observation table, no migration.
* No interpolation, no nearest-tick substitution, no backfill. An observation
  happened inside its band or it does not exist; a band that closes unobserved
  is a permanent, reported `scheduling_miss` and is never filled in later.
* No retry storm. A provider ANSWER (`observed`, `provider_no_pair`,
  `no_liquidity_state`, `token_inactive`) is terminal for this lane, exactly as
  `permanently_missing_evidence` is terminal for the tape. A failed REQUEST is
  not an answer — it stays re-plannable while its band is open, hard-capped at
  2 attempts per (token, horizon), so a rate-limit window cannot permanently
  burn every token in a pass.
* No SolanaTracker. Structurally, not by convention: the fetch phase runs
  inside a `provider_run` policy that ALLOWS only DexScreener and explicitly
  DENIES every other provider, so a paid-provider request raises
  `ProviderDeniedError` before any socket is opened.
* No reconciliation. This lane never computes, updates or reads a survival
  label. Whether the tick it bought matured a label is the RECONCILER's
  question and the tape coverage report's number, deliberately kept apart.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): market/liquidity
OBSERVATION only. Nothing here is EV, a side, a size, an order, a
recommendation, or a trade direction. No wallets, keys, swaps, signing, orders,
execution, or autonomy.
"""

import fcntl
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    CryptoHorizonCohort,
    CryptoHorizonCohortMember,
    CryptoHorizonObservation,
    CryptoTokenBirthEvent,
)
from app.services.crypto_horizon import (
    MEMBERSHIP_ROLLING,
    OBS_OBSERVED,
    OBS_REQUEST_FAILED,
    OBSERVE_MAX_CALLS,
    STATUS_DUE_NOW,
    CryptoHorizonService,
    HorizonPlanEntry,
    describe_pair,
    is_rolling_cohort,
    plan_observations,
    select_pair,
)
from app.services import crypto_tape as _tape
from app.services.crypto_tape import (
    DB_LOCKED_MAX_ATTEMPTS,
    DB_LOCKED_RETRY_SECONDS,
    HORIZON_TOLERANCE,
    HORIZONS,
    LockUnavailableError,
    _aware,
    _completeness_reason,
    _is_db_locked,
    _now,
    _reconciliation_should_abort,
)

logger = logging.getLogger(__name__)

SPARSE_NOTE = (
    "Prospective sparse observation: every eligible NEW birth gets exactly one "
    "governed 6h observation and one governed 24h observation, fetched via the "
    "existing read-only DexScreener adapter (no SolanaTracker, structurally "
    "denied). A provider ANSWER is terminal; a failed REQUEST may be "
    "re-attempted once while its band is open (hard cap 2 per token-horizon) "
    "— no polling, no backfill, no interpolation, no nearest-tick "
    "substitution. A band that "
    "closes unobserved stays a reported miss forever. Observation only — never "
    "EV, a side, a size, an order, or a recommendation. No wallets, keys, "
    "swaps, signing, or execution."
)

FLAG = "enable_crypto_sparse_observation"
LOCK_FILENAME = ".crypto-sparse-observe-{chain}.lock"
TICK_SOURCE = "crypto-sparse-obs"
COHORT_NOTE = "standing rolling cohort for prospective sparse observation"

# --- the two horizons this lane buys --------------------------------------------
# 15m and 1h are NOT bought. The measured production coverage at those horizons
# is already 80.9% / 81.1% — the background scout observes tokens densely for
# their first ~83 minutes (measured median last tick) and then stops. Buying
# observations there would be spend against a denominator that is already
# nearly full. The cliff is 6h (16.8%) and 24h (4.6%); that is where the
# denominator is.
SPARSE_HORIZONS: tuple[tuple[str, int], ...] = tuple(
    (label, minutes) for label, minutes in HORIZONS if label in ("6h", "24h")
)
SPARSE_HORIZON_LABELS: tuple[str, ...] = tuple(label for label, _m in SPARSE_HORIZONS)

# --- the two numbers that define the schedule (CHOSEN POLICY, not measured) -----
# SPARSE_BAND_MINUTES is the absolute half-width of the band around each
# horizon target inside which a scheduled pass may observe. SPARSE_CADENCE_
# MINUTES is the period of the host timer that drives the pass. They are chosen
# TOGETHER and two invariants — both asserted at import and pinned by test —
# make the pair safe:
#
#   (1) BAND CONTAINMENT. 2*BAND <= the tape's own tolerance at every sparse
#       horizon, so a tick written inside the band is ALWAYS inside
#       `compute_survival`'s window and can actually mature the label. 60 min
#       against 180 min (6h) and 720 min (24h) — a 3x and 12x margin.
#   (2) MISSED-PASS TOLERANCE. A closed interval of length 2*BAND contains at
#       least floor(2*BAND / CADENCE) points of a CADENCE-spaced lattice. At
#       120/60 that is 2 scheduled passes inside every band, so the lane still
#       observes after ONE missed pass (unit failure, host reboot, a
#       marketops_degraded skip).
#
# Neither number is measured on the target host and neither claims to be. They
# are policy, derived from the two invariants above plus the per-pass load
# arithmetic in the milestone doc.
SPARSE_BAND_MINUTES = 60.0
SPARSE_CADENCE_MINUTES = 60.0

_TIGHTEST_TAPE_TOLERANCE_MINUTES = min(
    minutes * HORIZON_TOLERANCE for _label, minutes in SPARSE_HORIZONS
)
assert SPARSE_HORIZONS, "sparse horizons must be a non-empty subset of HORIZONS"
assert SPARSE_BAND_MINUTES <= _TIGHTEST_TAPE_TOLERANCE_MINUTES, (
    "invariant (1) BAND CONTAINMENT violated: a sparse observation could land "
    "outside compute_survival's tolerance window and buy a tick that can never "
    "mature the label it was bought for"
)
assert int((2 * SPARSE_BAND_MINUTES) // SPARSE_CADENCE_MINUTES) >= 2, (
    "invariant (2) MISSED-PASS TOLERANCE violated: fewer than 2 scheduled "
    "passes fall inside a band, so one missed pass silently loses the horizon"
)

# The band closes at target + BAND; past that a member-horizon is permanently
# unobservable. A birth older than this at the LONGEST sparse horizon can never
# be enrolled usefully, which is exactly the enrolment window.
ENROL_WINDOW_MINUTES = max(m for _l, m in SPARSE_HORIZONS) + SPARSE_BAND_MINUTES

# --- per-pass bounds (CHOSEN, with stated margins over MEASURED arrivals) -------
# Measured births/day on EVO (CRYPTO-COVERAGE-REPAIR-001 B7, 2026-08-11):
#   14d 392.6 | 7d 417.3 | 3d 441.3 | 24h 517.0  (rising), planning rate ~530.
# At CADENCE=60min that is <=530/24 ~= 22 enrolments and, since each birth is
# observed exactly twice and the 6h/24h bands never overlap, <=44 fetches per
# pass in steady state.
DEFAULT_ENROL_LIMIT = 200          # 9x the ~22/pass steady-state enrolment rate
DEFAULT_OBSERVE_LIMIT = 100        # 2.27x the ~44/pass steady-state fetch rate
DEFAULT_WRITE_BATCH_SIZE = 25      # tokens committed per write transaction
DEFAULT_MAX_DURATION_SECONDS = 90.0  # deadline on the FETCH phase only
# RAW-PAYLOAD-STORAGE-001: raw payloads were 27% of the production DB with zero
# readers. This lane writes ~1,000 observation rows/day, so it keeps 3 candidate
# diagnostics, not the manual lane's 12.
AUDIT_CANDIDATE_LIMIT = 3

# --- statuses -------------------------------------------------------------------
STATUS_DISABLED = "disabled"
STATUS_DRY_RUN = "dry_run"
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_SKIPPED_OVERLAP = "skipped_overlap"
STATUS_LOCK_UNAVAILABLE = "lock_unavailable"
STATUS_MARKETOPS_DEGRADED = "marketops_degraded"
STATUS_DB_LOCKED = "db_locked"
STATUS_AMBIGUOUS_COHORT = "ambiguous_cohort"
STATUS_CONCURRENT_WRITE_CONFLICT = "concurrent_write_conflict"
STATUS_PROVIDER_POLICY_VIOLATION = "provider_policy_violation"

# every status that means "this pass did its job" (CLI exit 0)
HEALTHY_STATUSES = frozenset({STATUS_DRY_RUN, STATUS_OK, STATUS_PARTIAL})

STOP_DEADLINE = "deadline"
STOP_OBSERVE_LIMIT = "observe_limit"
STOP_COMPLETE = "complete"

# --- enrolment rejection reasons (honest, never silent) -------------------------
REJECT_INCOMPLETE_ANCHOR = "incomplete_lifecycle_anchor"
REJECT_NO_ANCHOR_TIMESTAMP = "no_anchor_timestamp"
REJECT_ALL_BANDS_CLOSED = "all_sparse_bands_closed"


# --- eligibility ----------------------------------------------------------------


def enrolment_rejection_reason(birth, now: datetime) -> str | None:
    """None when this birth is ELIGIBLE for prospective sparse observation;
    otherwise the reason it is not. Two rules, both derived from deployed code
    rather than intuition:

    1. COMPLETE LIFECYCLE ANCHOR (`_completeness_reason(birth, 0.0)`).
       `CryptoLifecycleTapeRecorder.compute_survival` gates EVERY horizon label
       on `if initial_liquidity and nearest.liquidity_usd is not None:` — a
       birth whose `initial_liquidity_usd` is NULL or <= 0 can never produce a
       survival label at ANY horizon, no matter how many observations are
       bought for it. Observing such a birth is pure provider spend with a
       provably zero denominator gain. The same predicate already gates the
       `--require-complete` cohort filter (CRYPTO-HORIZON-COHORT-SELECT-001)
       and the anchor feed's completeness accounting, so this is a reuse, not a
       new rule.

    2. AT LEAST ONE BAND STILL REACHABLE. A birth whose 24h band has already
       closed (`anchor + 24h + BAND < now`) has no observable horizon left.
       Enrolling it would create a member that can only ever produce
       scheduling misses, corrupting the observation denominator with tokens
       the lane never had a chance to observe.

    Deliberately NOT a rule: liquidity/volume/risk thresholds, launchpad venue,
    boost state, or anything else that would make the observed population a
    SELECTED sample. The denominator this lane exists to repair must stay the
    whole eligible birth population."""
    anchor = _aware(getattr(birth, "first_evidence_at", None)) or _aware(
        getattr(birth, "observed_at", None)
    )
    if anchor is None:
        return REJECT_NO_ANCHOR_TIMESTAMP
    if _completeness_reason(birth, 0.0) is not None:
        return REJECT_INCOMPLETE_ANCHOR
    if now > anchor + timedelta(minutes=ENROL_WINDOW_MINUTES):
        return REJECT_ALL_BANDS_CLOSED
    return None


# --- configuration --------------------------------------------------------------


@dataclass(frozen=True)
class SparseObservationConfig:
    chain: str
    enrol_limit: int = DEFAULT_ENROL_LIMIT
    observe_limit: int = DEFAULT_OBSERVE_LIMIT
    write_batch_size: int = DEFAULT_WRITE_BATCH_SIZE
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "SparseObservationConfig":
        s = settings or get_settings()
        return cls(
            chain=s.crypto_chain,
            enrol_limit=int(
                getattr(s, "crypto_sparse_observation_enrol_limit", DEFAULT_ENROL_LIMIT)
            ),
            observe_limit=int(
                getattr(
                    s, "crypto_sparse_observation_observe_limit", DEFAULT_OBSERVE_LIMIT
                )
            ),
            write_batch_size=int(
                getattr(
                    s, "crypto_sparse_observation_write_batch_size",
                    DEFAULT_WRITE_BATCH_SIZE,
                )
            ),
            max_duration_seconds=float(
                getattr(
                    s, "crypto_sparse_observation_max_duration_seconds",
                    DEFAULT_MAX_DURATION_SECONDS,
                )
            ),
        )

    def validation_error(self) -> tuple[str, str] | None:
        """(status, message) for an invalid bound, else None. Every bound is
        refused explicitly — never silently coerced into a green pass that does
        no work, which is the failure class CRYPTO-COVERAGE-REPAIR-001 spent
        four review rounds removing."""
        if self.enrol_limit < 1:
            return ("invalid_enrol_limit", f"enrol_limit {self.enrol_limit} must be >= 1")
        if self.observe_limit < 1:
            return (
                "invalid_observe_limit",
                f"observe_limit {self.observe_limit} must be >= 1",
            )
        if self.observe_limit > OBSERVE_MAX_CALLS:
            return (
                "invalid_observe_limit",
                f"observe_limit {self.observe_limit} exceeds the horizon lane's "
                f"hard per-pass adapter cap OBSERVE_MAX_CALLS={OBSERVE_MAX_CALLS}",
            )
        if self.write_batch_size < 1:
            return (
                "invalid_write_batch_size",
                f"write_batch_size {self.write_batch_size} must be >= 1",
            )
        if self.max_duration_seconds < 0:
            return (
                "invalid_max_duration_seconds",
                f"max_duration_seconds {self.max_duration_seconds} must be >= 0",
            )
        return None


# --- the standing rolling cohort ------------------------------------------------


def find_rolling_cohort(session: Session, chain: str) -> list[CryptoHorizonCohort]:
    """Every rolling sparse cohort for this chain. There must be exactly one;
    the caller refuses ambiguity rather than silently picking."""
    rows = list(session.execute(
        select(CryptoHorizonCohort)
        .where(CryptoHorizonCohort.chain == chain)
        .order_by(CryptoHorizonCohort.id)
    ).scalars().all())
    return [c for c in rows if is_rolling_cohort(c)]


def _create_rolling_cohort(session: Session, chain: str, now: datetime):
    cohort = CryptoHorizonCohort(
        chain=chain,
        # A rolling cohort has no member limit by construction. 0 records that
        # honestly rather than inventing a cap that nothing enforces; the real
        # per-pass bound is `enrol_limit`, which lives in the pass result and
        # in Settings, not here.
        member_limit=0,
        window_hours=int(ENROL_WINDOW_MINUTES // 60),
        note=COHORT_NOTE,
        provenance={
            "source": "prospective_sparse_observation",
            "milestone": "CRYPTO-COVERAGE-REPAIR-002",
            "membership": MEMBERSHIP_ROLLING,
            "horizons": list(SPARSE_HORIZON_LABELS),
            "band_minutes": SPARSE_BAND_MINUTES,
            "cadence_minutes": SPARSE_CADENCE_MINUTES,
            "eligibility": "complete lifecycle anchor + at least one band reachable",
            "created_at": now.isoformat(),
        },
        created_at=now,
    )
    session.add(cohort)
    session.flush()
    return cohort


# --- pass result plumbing -------------------------------------------------------


def _base_result(status: str, config: SparseObservationConfig, started: datetime) -> dict:
    return {
        "status": status,
        "note": SPARSE_NOTE,
        "mode": "scheduled_sparse_observation",
        "chain": config.chain,
        "horizons": list(SPARSE_HORIZON_LABELS),
        "band_minutes": SPARSE_BAND_MINUTES,
        "cadence_minutes": SPARSE_CADENCE_MINUTES,
        "external_calls": 0,
        "provider": "dexscreener",
        "solana_tracker_calls": 0,
        "births_considered": 0,
        "enrolled": 0,
        "enrolment_rejections": {},
        "due_observations": 0,
        "observations_recorded": 0,
        "ticks_written": 0,
        "outcome_counts": {},
        "batches_committed": 0,
        "deferred_observations": 0,
        "retryable_request_failures": 0,
        "request_failures_reattempted": 0,
        "stop_reason": None,
        "persisted": False,
        "duration_ms": 0,
    }


def _finish(result: dict, started: datetime) -> dict:
    result["duration_ms"] = max(0, int((_now() - started).total_seconds() * 1000))
    return result


# --- the fetch phase ------------------------------------------------------------


@dataclass
class _Fetched:
    """One token's provider result, reduced to what the write phase needs.

    Deliberately NOT the raw `PairData` list: the fetch phase can hold up to
    `observe_limit` of these in memory at once, and the write phase must not
    re-touch the network. `selected` is the single chosen pair (the only object
    whose numeric fields are persisted); `candidates` are already-compacted
    diagnostics."""

    token_address: str
    selected: object | None
    basis: dict
    candidates: list = field(default_factory=list)
    request_failed: bool = False
    fetched_at: datetime | None = None


def _dexscreener_only_policy(run_id: str, cap: int):
    """A run-scoped policy that ALLOWS DexScreener and explicitly DENIES every
    other provider.

    This is the structural form of "no SolanaTracker spend". `deny` wins over
    everything in `ProviderPolicy.authorization`, so a SolanaTracker (or GoPlus,
    or Birdeye) request issued from anywhere inside this pass raises
    `ProviderDeniedError` in `guard_provider_request` — BEFORE a client is
    constructed or a socket opened — rather than relying on nobody having added
    a call. `cap` is a second, independent ceiling on DexScreener requests: the
    fetch loop stops at `observe_limit` on its own, so reaching the policy cap
    would mean a bug, and the result reports `skipped_cap` so it can never be
    mistaken for a provider miss."""
    from app.services.crypto_provider_policy import Provider, ProviderPolicy

    return ProviderPolicy(
        run_id=run_id,
        allowed=frozenset({Provider.DEXSCREENER}),
        denied=frozenset(p for p in Provider if p is not Provider.DEXSCREENER),
        caps={Provider.DEXSCREENER: cap},
        paid_confirmed=frozenset(),
    )


def _transport_failures(ctx) -> int:
    """How many DexScreener requests this run has failed to get an ANSWER for.

    `failed` covers 429 / timeout / connection error / 5xx / undecodable JSON
    (the adapter marks all of them). `skipped_cap` / `skipped_budget` cover the
    deterministic guard skips, which also return None and also are not an
    answer. Read as a delta across a single call, this is the transport-failure
    signal the adapter's return value deliberately erases."""
    from app.services.crypto_provider_policy import Provider

    ledger = ctx.ledger
    return sum(
        bucket.get(Provider.DEXSCREENER, 0)
        for bucket in (ledger.failed, ledger.skipped_cap, ledger.skipped_budget)
    )


async def _fetch_phase(
    service: CryptoHorizonService,
    due_tokens: list[str],
    deadline: datetime | None,
    clock,
) -> tuple[list[_Fetched], int, str, dict]:
    """Fetch every due token's pairs. **Opens no transaction and writes
    nothing.**

    Separating the network phase from the write phase entirely is the central
    transaction-shape decision of this milestone. `CryptoHorizonService.
    observe_once` interleaves `session.add`/`flush` with `await
    fetch_pairs_for_token` and commits once at the end — which, at this lane's
    scheduled cadence and token count, would hold the SQLite write lock across
    tens of seconds of network I/O on a shared host. That is precisely the
    single-transaction shape OPS-013 retired and CRYPTO-COVERAGE-REPAIR-001
    spent five review rounds on. Here the write lock is not held at all while
    the provider is being called; the property is pinned by test, not asserted
    in a comment.

    Returns (fetched, calls, stop_reason, provider_ledger)."""
    from app.services.crypto_provider_policy import (
        Provider,
        ProviderPolicyError,
        new_run_id,
        provider_run,
    )

    fetched: list[_Fetched] = []
    calls = 0
    stop_reason = STOP_COMPLETE
    policy = _dexscreener_only_policy(new_run_id(), len(due_tokens))
    with provider_run(policy) as ctx:
        for token in due_tokens:
            if deadline is not None and _now() >= deadline and fetched:
                stop_reason = STOP_DEADLINE
                break
            before = _transport_failures(ctx)
            try:
                pairs = await service.adapter.fetch_pairs_for_token(token)
                # THE REAL ADAPTER NEVER RAISES (dexscreener.py: "never raise on
                # network/HTTP/schema problems" — 429, timeout, 5xx and JSON
                # errors all `return None`, so `fetch_pairs_for_token` returns
                # []). An empty list therefore does NOT mean "the provider
                # answered and had no pair"; without this delta the write phase
                # recorded `provider_no_pair` — or, past 24h where `aged` is
                # true by construction, the TERMINAL `token_inactive`, an
                # affirmative claim that the token is dead derived from a
                # request that never returned an answer.
                #
                # The provider ledger is the one place that already knows.
                # `_get` calls `mark_failed` on every transport/schema failure
                # and the cap/budget skips are accounted there too, so a
                # non-zero delta across this one call means "no answer", for
                # any adapter that participates in the policy.
                request_failed = _transport_failures(ctx) > before
            except ProviderPolicyError:
                # NEVER degrade an authorization failure into an ordinary
                # provider miss. The policy module says so explicitly, and the
                # whole "no SolanaTracker spend" guarantee depends on it: a
                # swallowed denial is indistinguishable from a token that
                # simply has no pairs.
                raise
            except Exception as exc:  # adapter degrades to [], but be safe
                logger.warning(
                    "sparse observation fetch failed for %s: %s", token, exc
                )
                pairs = []
                request_failed = True
            calls += 1
            selected, basis = select_pair(pairs, token)
            fetched.append(_Fetched(
                token_address=token,
                selected=selected,
                basis=basis,
                candidates=[describe_pair(p, token) for p in pairs],
                request_failed=request_failed,
                # the LOGICAL observation time (see `_logical_clock`): real
                # wall clock in production, the injected clock under test, so
                # the tick lands where the caller's clock says it did
                fetched_at=clock(),
            ))
        ledger = ctx.ledger.snapshot()
    # A non-DexScreener provider may legitimately APPEAR in this ledger — with
    # `blocked_policy` incremented, which is the denial working and is exactly
    # the evidence worth keeping. What it may never have is a REQUEST: an
    # authorized, started, succeeded or failed count. That would mean the deny
    # set was bypassed, and it is fatal rather than reported.
    spent = {
        name: entry for name, entry in ledger.items()
        if name != Provider.DEXSCREENER.value
        and any(entry.get(k) for k in ("authorized", "started", "succeeded", "failed"))
    }
    if spent:  # pragma: no cover - structurally unreachable
        raise RuntimeError(
            f"sparse observation issued a non-DexScreener provider request: {spent}"
        )
    return fetched, calls, stop_reason, ledger


# --- the pass -------------------------------------------------------------------


async def run_scheduled_sparse_observation(
    session: Session,
    *,
    dry_run: bool = False,
    force: bool = False,
    settings: Settings | None = None,
    service: CryptoHorizonService | None = None,
    config: SparseObservationConfig | None = None,
    now: datetime | None = None,
    sleeper=None,
) -> dict:
    """One bounded, governed prospective sparse-observation pass.

    Sequence, in order, each step refusing loudly rather than degrading:

      gate -> overlap flock -> MarketOps health -> resolve standing cohort ->
      enrol (DB only) -> plan (pure) -> FETCH (network, no DB write) ->
      WRITE (batched commits, no network)

    Governance:

    * DEFAULT OFF (`enable_crypto_sparse_observation`). Off is a clean no-op:
      no read, no write, no call. `--force` and `--dry-run` bypass the gate
      attended and the result says which.
    * `--dry-run` enrols nothing, calls nothing and writes nothing; it reports
      exactly what it WOULD enrol and observe.
    * A degraded MarketOps run aborts the pass before any write, reusing
      `crypto_tape._reconciliation_should_abort` — the same health predicate
      the scheduled reconciler uses, not a second implementation of it.
    * A non-blocking per-chain flock makes a second concurrent instance refuse
      loudly (`skipped_overlap`) instead of racing enrolment.
    * Every failure is typed. Nothing returns `ok` having done no work.

    Idempotency and restart safety are enforced by the DATABASE, not by
    bookkeeping: `ix_horizon_member_cohort_token` makes double-enrolment
    impossible and `ix_horizon_obs_cohort_token_horizon` makes double-
    observation impossible. A pass killed mid-cycle leaves its already-
    committed batches durable; the next pass re-plans from persisted state and
    picks up exactly what has no row yet.

    Measurement only, never advice."""
    s = settings if settings is not None else get_settings()
    cfg = config if config is not None else SparseObservationConfig.from_settings(s)
    started = _now()
    now = _aware(now) or started
    enabled = bool(getattr(s, FLAG, False))
    bypass = "force" if force else ("dry_run" if dry_run else None)

    if not (enabled or force or dry_run):
        result = _base_result(STATUS_DISABLED, cfg, started)
        result["flag"] = FLAG
        result["gate_bypassed"] = None
        return result

    def _refused(status: str, error: str) -> dict:
        result = _base_result(status, cfg, started)
        result["gate_bypassed"] = bypass
        result["error"] = error
        return _finish(result, started)

    invalid = cfg.validation_error()
    if invalid is not None:
        return _refused(*invalid)

    # Module-attribute lookup, deliberately not a from-import: the test
    # suite's session-scoped `_isolate_crypto_tape_overlap_lock` fixture
    # monkeypatches `crypto_tape._resolve_lock_dir`, and a from-import bound
    # at module load would silently bypass it and take a lock inside whatever
    # DATABASE_URL the host's .env points at.
    lock_dir = _tape._resolve_lock_dir(s)
    try:
        with _sparse_overlap_lock(lock_dir, cfg.chain) as acquired:
            if not acquired:
                return _refused(
                    STATUS_SKIPPED_OVERLAP,
                    "another sparse-observation pass holds the per-chain lock; "
                    "nothing was read or written",
                )
            return await _run_locked(
                session, s, cfg, service=service, dry_run=dry_run, force=force,
                bypass=bypass, now=now, started=started, sleeper=sleeper,
            )
    except LockUnavailableError as exc:
        return _refused(STATUS_LOCK_UNAVAILABLE, str(exc))


@contextmanager
def _sparse_overlap_lock(lock_dir: Path, chain: str):
    """Non-blocking, kernel-held (flock) overlap guard for THIS lane.

    Same mechanism and same failure contract as the reconciler's
    `crypto_tape._reconcile_overlap_lock` (kernel releases on process death, so
    a crashed pass never leaves a stale lock; `LockUnavailableError` — never a
    raw `OSError` — when the lock FILE itself cannot be opened), but a distinct
    filename on purpose: the sparse observer and the tape reconciler are
    different work on different rows and must not block each other.

    The DIRECTORY comes from `crypto_tape._resolve_lock_dir`, which the test
    suite's session-scoped `_isolate_crypto_tape_overlap_lock` fixture already
    redirects into a tmp dir — so no test can ever take a lock inside a real
    deployment's data directory."""
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - defensive
        pass
    lock_path = lock_dir / LOCK_FILENAME.format(chain=chain)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise LockUnavailableError(
            f"cannot open sparse-observation lock file {lock_path}: {exc}"
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


_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def _earliest_deadline_first(entry: HorizonPlanEntry):
    """Selection order under `observe_limit`: the member-horizon whose BAND
    CLOSES SOONEST is served first.

    The original key was the ABSOLUTE distance to the horizon target, which
    conflates "59 minutes of runway left" with "closes in 1 minute" and served
    the LEAST urgent first — measured: with three staggered births and
    `observe_limit=1`, the one with 1 minute of band left was deferred and
    permanently lost, while a member with an hour of runway was observed. That
    directly contradicts the starvation reasoning `_enrolment_candidates`
    states one function away. `target_distance_s` survives only as the
    tiebreak among members whose bands close at the same instant."""
    window_end = _aware(entry.window_end) or _FAR_FUTURE
    distance = (
        entry.target_distance_s if entry.target_distance_s is not None else 1e18
    )
    return (window_end, distance, entry.token_address, entry.horizon)


async def _run_locked(
    session: Session,
    s: Settings,
    cfg: SparseObservationConfig,
    *,
    service: CryptoHorizonService | None,
    dry_run: bool,
    force: bool,
    bypass: str | None,
    now: datetime,
    started: datetime,
    sleeper,
) -> dict:
    def _refused(status: str, error: str) -> dict:
        result = _base_result(status, cfg, started)
        result["gate_bypassed"] = bypass
        result["error"] = error
        return _finish(result, started)

    clock = _logical_clock(now, started)

    # --- MarketOps health -------------------------------------------------
    # Reuses the reconciler's predicate rather than reimplementing it: do not
    # add write pressure to a host whose latest MarketOps run errored. A
    # dry-run reads and writes nothing, so it is exempt.
    if not dry_run:
        try:
            degraded = _reconciliation_should_abort(session)
        except Exception as exc:
            try:
                session.rollback()
            except Exception:  # pragma: no cover - defensive
                pass
            if _is_db_locked(exc):
                return _refused(
                    STATUS_DB_LOCKED,
                    "database is locked; pass abandoned at the MarketOps health "
                    "read, before any enrolment or observation work",
                )
            raise
        if degraded:
            return _refused(
                STATUS_MARKETOPS_DEGRADED,
                "latest MarketOps run errored; not adding write pressure",
            )

    # --- standing cohort --------------------------------------------------
    cohorts = find_rolling_cohort(session, cfg.chain)
    if len(cohorts) > 1:
        return _refused(
            STATUS_AMBIGUOUS_COHORT,
            f"{len(cohorts)} rolling sparse cohorts exist for chain "
            f"{cfg.chain} (ids {[c.id for c in cohorts]}); refusing to guess "
            "which one owns the observation denominator",
        )
    cohort = cohorts[0] if cohorts else None

    result = _base_result(STATUS_OK, cfg, started)
    result["gate_bypassed"] = bypass
    result["enrol_limit"] = cfg.enrol_limit
    result["observe_limit"] = cfg.observe_limit
    result["write_batch_size"] = cfg.write_batch_size
    result["max_duration_seconds"] = cfg.max_duration_seconds
    result["cohort_id"] = cohort.id if cohort is not None else None
    result["cohort_created"] = False

    service = service or CryptoHorizonService(settings=s)

    # --- enrolment (pure DB, no provider) ---------------------------------
    candidates, rejections, considered = _enrolment_candidates(
        session, cfg, cohort, now,
    )
    result["births_considered"] = considered
    result["enrolment_rejections"] = rejections

    if dry_run:
        result["status"] = STATUS_DRY_RUN
        result["would_create_cohort"] = cohort is None
        result["would_enrol"] = len(candidates)
        result["would_enrol_preview"] = [
            {"token": b.token_address[:16], "symbol": b.symbol}
            for b in candidates[:10]
        ]
        plan, retryable_ids = _sparse_plan(
            session, cfg, cohort, now,
            extra_members=_transient_members(cfg, candidates),
        )
        due = sorted(
            (e for e in plan if e.status == STATUS_DUE_NOW),
            key=_earliest_deadline_first,
        )
        result["retryable_request_failures"] = sum(
            1 for e in due if (e.token_address, e.horizon) in retryable_ids
        )
        result["due_observations"] = len(due)
        result["would_fetch_tokens"] = len({e.token_address for e in due})
        result["would_observe"] = [
            {"token": e.token_address[:16], "horizon": e.horizon,
             "target_at": e.target_at.isoformat() if e.target_at else None}
            for e in due[:10]
        ]
        result["plan_status_counts"] = _plan_counts(plan)
        return _finish(result, started)

    if cohort is None:
        cohort = _create_rolling_cohort(session, cfg.chain, now)
        result["cohort_created"] = True
    enrolled = 0
    try:
        enrolled = _enrol(session, cfg, cohort, candidates, now)
        result["cohort_id"] = cohort.id
    except IntegrityError as exc:
        session.rollback()
        return _refused(
            STATUS_CONCURRENT_WRITE_CONFLICT,
            f"enrolment raced another writer on the standing cohort: {exc}",
        )
    except Exception as exc:
        session.rollback()
        if _is_db_locked(exc):
            return _refused(
                STATUS_DB_LOCKED,
                "database is locked; pass abandoned during enrolment",
            )
        raise
    result["enrolled"] = enrolled
    result["persisted"] = enrolled > 0 or result["cohort_created"]

    # --- plan (pure) ------------------------------------------------------
    plan, retryable_ids = _sparse_plan(session, cfg, cohort, now)
    due = [e for e in plan if e.status == STATUS_DUE_NOW]
    result["plan_status_counts"] = _plan_counts(plan)
    result["due_observations"] = len(due)
    result["retryable_request_failures"] = sum(
        1 for e in due if (e.token_address, e.horizon) in retryable_ids
    )
    due_by_token: dict[str, list[HorizonPlanEntry]] = {}
    for entry in sorted(due, key=_earliest_deadline_first):
        due_by_token.setdefault(entry.token_address, []).append(entry)
    ordered_tokens = list(due_by_token)
    selected_tokens = ordered_tokens[: cfg.observe_limit]
    deferred = sum(
        len(due_by_token[t]) for t in ordered_tokens[cfg.observe_limit:]
    )

    # --- fetch (network, no DB write) -------------------------------------
    # 0.0 is the deliberate "already past due" sentinel (same convention as
    # the tape reconciler's `max_duration_seconds`): the fetch loop then stops
    # after exactly one token, never before the first, so a pass can never
    # report `ok` having fetched nothing.
    deadline = (
        started + timedelta(seconds=cfg.max_duration_seconds)
        if cfg.max_duration_seconds is not None else None
    )
    try:
        fetched, calls, stop_reason, ledger = await _fetch_phase(
            service, selected_tokens, deadline, clock,
        )
    except Exception as exc:
        from app.services.crypto_provider_policy import ProviderPolicyError

        if isinstance(exc, ProviderPolicyError):
            # A provider this lane never authorizes was reached. Loud and
            # non-zero — never degraded into a provider miss.
            return _refused(
                STATUS_PROVIDER_POLICY_VIOLATION,
                f"a non-DexScreener provider was attempted from the sparse "
                f"observation path and was denied before any request: {exc}",
            )
        raise
    result["external_calls"] = calls
    result["provider_ledger"] = ledger
    # Derived from the ledger, never hardcoded: the number a reader wants is
    # "did this lane spend paid-provider budget", and it must come from the
    # same accounting the guard itself keeps.
    result["solana_tracker_calls"] = sum(
        ledger.get("solana-tracker", {}).get(k, 0)
        for k in ("authorized", "started", "succeeded", "failed")
    )
    result["denied_provider_attempts"] = {
        name: entry["blocked_policy"]
        for name, entry in ledger.items()
        if name != "dexscreener" and entry.get("blocked_policy")
    }
    result["provider"] = getattr(service.adapter, "source_name", "dexscreener")

    if stop_reason == STOP_DEADLINE:
        deferred += sum(
            len(due_by_token[t]) for t in selected_tokens[len(fetched):]
        )
    elif deferred:
        stop_reason = STOP_OBSERVE_LIMIT
    result["stop_reason"] = stop_reason
    result["deferred_observations"] = deferred

    # --- write (batched commits, no network) ------------------------------
    # Only the members this pass actually fetched — never the whole cohort (see
    # `_sparse_plan`'s bounding note; the same unbounded-growth hazard applies
    # here, and `service._members` deliberately has no filter because the
    # frozen lane it was written for has at most 100 members).
    fetched_tokens = [item.token_address for item in fetched]
    members = {
        m.token_address: m
        for m in session.execute(
            select(CryptoHorizonCohortMember).where(
                CryptoHorizonCohortMember.cohort_id == cohort.id,
                CryptoHorizonCohortMember.token_address.in_(fetched_tokens),
            )
        ).scalars().all()
    } if fetched_tokens else {}
    # A re-attempt must UPDATE the existing `request_failed` row, never insert
    # beside it — the unique index on (cohort, token, horizon) makes that
    # mandatory. Only the rows for tokens this pass actually fetched are
    # loaded, and only when there are any.
    needed_ids = [
        obs_id for (token, _horizon), obs_id in retryable_ids.items()
        if token in due_by_token and token in set(fetched_tokens)
    ]
    retry_rows = {
        (row.token_address, row.horizon): row
        for row in session.execute(
            select(CryptoHorizonObservation).where(
                CryptoHorizonObservation.id.in_(needed_ids)
            )
        ).scalars().all()
    } if needed_ids else {}
    outcomes: dict[str, int] = {}
    ticks = 0
    recorded = 0
    batches = 0
    retried = 0

    def _stage(batch: list[_Fetched]):
        """Stage one batch's rows. Called once per commit ATTEMPT — see
        `_commit_with_retry`: a rollback expunges everything staged, so the
        rows must be rebuilt, not merely re-committed."""
        staged = {"outcomes": {}, "ticks": 0, "recorded": 0, "retried": 0}
        for item in batch:
            member = members.get(item.token_address)
            for entry in due_by_token.get(item.token_address, []):
                existing = retry_rows.get((item.token_address, entry.horizon))
                # read BEFORE the call: `_record_observation` replaces
                # `raw_payload` wholesale. After a rollback the row is expired
                # and re-read from the database, so this recounts correctly on
                # every staging attempt rather than compounding.
                attempts = _attempts_used(existing) if existing is not None else 0
                status, _cause, tick = service._record_observation(
                    session, cohort.id, member, entry, item.selected, item.basis,
                    item.candidates, item.request_failed,
                    item.fetched_at or clock(), existing=existing,
                    audit_candidate_limit=AUDIT_CANDIDATE_LIMIT,
                    tick_source=TICK_SOURCE,
                )
                if existing is not None:
                    payload = dict(existing.raw_payload or {})
                    payload[ATTEMPTS_KEY] = attempts + 1
                    existing.raw_payload = payload
                    staged["retried"] += 1
                staged["outcomes"][status] = staged["outcomes"].get(status, 0) + 1
                staged["recorded"] += 1
                if tick is not None:
                    staged["ticks"] += 1
        return staged

    try:
        for start in range(0, len(fetched), cfg.write_batch_size):
            batch = fetched[start:start + cfg.write_batch_size]
            # NOTHING is counted until the batch's commit has RETURNED. The
            # counters used to be incremented as rows were staged, so a pass
            # whose commit failed still reported the full `observations_
            # recorded` — a green pass that wrote nothing after spending real
            # provider requests.
            staged = _commit_with_retry(session, lambda b=batch: _stage(b), sleeper)
            batches += 1
            recorded += staged["recorded"]
            ticks += staged["ticks"]
            retried += staged["retried"]
            for name, count in staged["outcomes"].items():
                outcomes[name] = outcomes.get(name, 0) + count
    except IntegrityError as exc:
        session.rollback()
        result.update({
            "status": STATUS_CONCURRENT_WRITE_CONFLICT,
            "error": (
                "an observation row for this (cohort, token, horizon) already "
                f"existed — another pass raced this one: {exc}"
            ),
            "batches_committed": batches,
        })
        return _finish(result, started)
    except Exception as exc:
        session.rollback()
        if _is_db_locked(exc):
            result.update({
                "status": STATUS_DB_LOCKED,
                "error": "database is locked; observation write phase abandoned",
                "batches_committed": batches,
            })
            return _finish(result, started)
        raise

    result["observations_recorded"] = recorded
    result["ticks_written"] = ticks
    result["outcome_counts"] = outcomes
    result["batches_committed"] = batches
    result["request_failures_reattempted"] = retried
    result["persisted"] = result["persisted"] or recorded > 0
    if stop_reason != STOP_COMPLETE:
        result["status"] = STATUS_PARTIAL
    return _finish(result, started)


def _logical_clock(now: datetime, started: datetime):
    """The pass's clock, advanced by REAL elapsed time from an explicit base.

    In production `now` defaults to the pass start, so this returns exactly
    `_now()` — an observation is stamped with the instant it actually happened,
    which is what `compute_survival`'s nearest-tick-in-tolerance search and the
    report's target-distance both read. Under test an injected `now` becomes
    the base instead, so a fixture can place a pass at a chosen point in a
    token's life without the wall clock overwriting it."""
    def _clock() -> datetime:
        return now + (_now() - started)

    return _clock


def _commit_with_retry(session: Session, prepare, sleeper=None):
    """Bounded lock-retry ladder around one batch commit, with the batch
    RE-STAGED after every rollback.

    `prepare` must stage the batch (and may return an arbitrary summary of what
    it staged). It is called once before the first attempt and AGAIN after each
    rollback, because `session.rollback()` expunges every pending object and
    expires every persistent one: retrying a bare `session.commit()` after a
    rollback commits an EMPTY transaction and returns successfully having
    written nothing. `crypto_tape.py` (see `_commit_batch_with_retry`'s
    `prepare` contract) documents this exact hazard verbatim; the first version
    of this function claimed "the same shape the tape reconciler uses" while
    omitting the mechanism that makes it correct, and silently discarded whole
    batches while reporting `status=ok`.

    A persistent lock re-raises so the caller turns it into a typed `db_locked`
    result with the already-committed batches intact. Returns `prepare`'s last
    return value, which the caller counts ONLY after this returns."""
    sleeper = sleeper or time.sleep
    for attempt in range(1, DB_LOCKED_MAX_ATTEMPTS + 1):
        try:
            # INSIDE the try, exactly as the reconciler's ladder does it: after
            # a rollback the objects `prepare()` touches are expired, so
            # staging can itself emit SQL (autoflush, lazy-load) and hit the
            # lock. Staging outside the try would send that straight past this
            # ladder.
            staged = prepare()
            session.commit()
            return staged
        except Exception as exc:
            if not _is_db_locked(exc) or attempt == DB_LOCKED_MAX_ATTEMPTS:
                raise
            session.rollback()
            sleeper(DB_LOCKED_RETRY_SECONDS)
    raise RuntimeError(  # pragma: no cover - the loop always returns or raises
        "commit retry ladder exhausted without committing or raising"
    )


# --- enrolment ------------------------------------------------------------------


def _enrolment_candidates(
    session: Session, cfg: SparseObservationConfig, cohort, now: datetime,
) -> tuple[list, dict, int]:
    """Eligible, not-yet-enrolled births, OLDEST ANCHOR FIRST.

    Oldest-first, not newest-first: the birth closest to losing its remaining
    band is the one a bounded pass must not defer. This is the same
    starvation lesson CRYPTO-COVERAGE-REPAIR-001 learned twice (recency-anchored
    selection starved old cohorts; backlog appended after the in-window head
    was never reached).

    The candidate query is bounded by the enrolment window on both sides: a
    birth older than `ENROL_WINDOW_MINUTES` has no reachable band and is not a
    candidate at all, so this never walks the whole birth table."""
    anchor_col = func.coalesce(
        CryptoTokenBirthEvent.first_evidence_at, CryptoTokenBirthEvent.observed_at,
    )
    cutoff = now - timedelta(minutes=ENROL_WINDOW_MINUTES)
    conditions = [
        CryptoTokenBirthEvent.chain == cfg.chain,
        anchor_col >= cutoff,
        anchor_col <= now,
    ]
    if cohort is not None:
        already = select(CryptoHorizonCohortMember.token_address).where(
            CryptoHorizonCohortMember.cohort_id == cohort.id
        )
        conditions.append(CryptoTokenBirthEvent.token_address.notin_(already))
    births = list(session.execute(
        select(CryptoTokenBirthEvent)
        .where(*conditions)
        .order_by(anchor_col.asc(), CryptoTokenBirthEvent.id.asc())
        # read one page beyond the enrolment limit so `births_considered` and
        # the rejection histogram stay honest when the limit binds
        .limit(cfg.enrol_limit * 2)
    ).scalars().all())

    eligible: list = []
    rejections: dict[str, int] = {}
    for birth in births:
        reason = enrolment_rejection_reason(birth, now)
        if reason is None:
            eligible.append(birth)
        else:
            rejections[reason] = rejections.get(reason, 0) + 1
    return eligible[: cfg.enrol_limit], rejections, len(births)


def _enrol(
    session: Session, cfg: SparseObservationConfig, cohort, births: list,
    now: datetime,
) -> int:
    """Insert members in bounded batches. The unique index on
    (cohort_id, token_address) is what makes a re-run — or a restart mid-pass —
    incapable of double-enrolling; this function adds no bookkeeping of its
    own."""
    added = 0
    pending = 0
    for birth in births:
        session.add(CryptoHorizonCohortMember(
            cohort_id=cohort.id, chain=cfg.chain,
            token_address=birth.token_address, symbol=birth.symbol,
            birth_event_id=birth.id,
            birth_observed_at=_aware(birth.observed_at),
            first_evidence_at=_aware(birth.first_evidence_at),
            added_at=now,
        ))
        added += 1
        pending += 1
        if pending >= cfg.write_batch_size:
            session.commit()
            pending = 0
    if pending or added == 0:
        session.commit()
    return added


# --- planning -------------------------------------------------------------------


def _transient_members(cfg: SparseObservationConfig, births: list) -> list:
    """Un-persisted members standing in for births a dry run WOULD enrol, so
    the dry run plans over exactly the member set the real pass would have."""
    return [
        CryptoHorizonCohortMember(
            cohort_id=None, chain=cfg.chain, token_address=b.token_address,
            symbol=b.symbol, birth_event_id=b.id,
            birth_observed_at=_aware(b.observed_at),
            first_evidence_at=_aware(b.first_evidence_at),
        )
        for b in births
    ]


SPARSE_MAX_ATTEMPTS = 2
ATTEMPTS_KEY = "sparse_attempts"


def _attempts_used(row) -> int:
    """How many attempts a persisted observation row represents.

    A row written by this lane's FIRST attempt carries no marker, so absence
    means 1. Anything unparsable is treated as EXHAUSTED — a corrupt marker
    must not become an unbounded retry licence."""
    payload = row.raw_payload if isinstance(row.raw_payload, dict) else {}
    try:
        return max(1, int(payload.get(ATTEMPTS_KEY, 1)))
    except (TypeError, ValueError):
        return SPARSE_MAX_ATTEMPTS


def _is_retryable(status: str, attempts: int) -> bool:
    """Terminality depends on the miss CAUSE, not on a row merely existing.

    A provider ANSWER — `observed`, `provider_no_pair`, `no_liquidity_state`,
    `token_inactive` — is terminal. That is the honest one-shot semantic this
    lane promises: the provider was asked and it told us something.

    A failed REQUEST is not an answer. Treating it as terminal permanently
    burned the horizon while the band was still open, and did so in CORRELATED
    fashion: one DexScreener rate-limit window burned every token in the pass
    (up to `observe_limit`) simultaneously and forever. So a `request_failed`
    row stays re-plannable WHILE ITS BAND IS OPEN (the planner alone decides
    that), hard-capped at SPARSE_MAX_ATTEMPTS per (token, horizon) so spend
    stays bounded — at most 2 requests per birth-horizon, 4 per birth — and no
    retry storm is possible."""
    return status == OBS_REQUEST_FAILED and attempts < SPARSE_MAX_ATTEMPTS


def _sparse_plan(
    session: Session, cfg: SparseObservationConfig, cohort, now: datetime,
    extra_members: list | None = None,
) -> tuple[list[HorizonPlanEntry], dict[tuple[str, str], int]]:
    """The sparse plan: the SHARED pure planner restricted to this lane's
    horizons and band, with this lane's CAUSE-BASED terminality applied on top.

    Returns `(entries, retryable_ids)`, where `retryable_ids` maps
    (token_address, horizon) to the id of the `request_failed` observation row
    that a re-attempt must UPDATE IN PLACE rather than insert beside (the
    unique index on (cohort, token, horizon) makes that mandatory, not
    optional).

    `existing` is passed EMPTY to `plan_observations` and the filter happens
    here instead: the shared planner treats only an `observed` row as terminal
    and leaves every MISSED row retryable in place, unbounded — correct for a
    manual cohort pass an operator re-runs deliberately, wrong for a lane that
    fires every hour. See `_is_retryable` for the rule this lane applies
    instead."""
    members = list(extra_members or [])
    attempted: set = set()
    retryable_ids: dict[tuple[str, str], int] = {}
    if cohort is not None:
        # BOUNDED BY THE ENROLMENT WINDOW, not by cohort size.
        #
        # This cohort is standing and rolling: at ~530 births/day it accrues
        # ~193k members and ~387k observation rows per year. Loading all of
        # them every pass would make an hourly job slower every single day,
        # forever — the unbounded-growth failure this project has already paid
        # for once (`_universe`'s recency starvation) and the reason
        # CRYPTO-COVERAGE-REPAIR-001 insists a scheduled pass state its
        # capacity against its arrival rate.
        #
        # A member's LAST band closes at `anchor + 24h + BAND`. Anything older
        # than that has nothing due and never will, so it is excluded in SQL —
        # the working set is bounded at roughly (arrival rate x 25h), i.e.
        # ~550 members, whatever the cohort's lifetime size.
        member_cutoff = now - timedelta(minutes=ENROL_WINDOW_MINUTES)
        anchor_col = func.coalesce(
            CryptoHorizonCohortMember.first_evidence_at,
            CryptoHorizonCohortMember.birth_observed_at,
        )
        persisted = list(session.execute(
            select(CryptoHorizonCohortMember)
            .where(
                CryptoHorizonCohortMember.cohort_id == cohort.id,
                anchor_col >= member_cutoff,
            )
            .order_by(CryptoHorizonCohortMember.id)
        ).scalars().all())
        members = persisted + members
        if not members:
            return [], {}
        tokens = [m.token_address for m in members]
        # Columns, not entities: this reads 5 of 22 fields and never needs the
        # rest. `raw_payload` is read only for the attempt marker.
        for row in session.execute(
            select(
                CryptoHorizonObservation.id,
                CryptoHorizonObservation.token_address,
                CryptoHorizonObservation.horizon,
                CryptoHorizonObservation.status,
                CryptoHorizonObservation.raw_payload,
            ).where(
                CryptoHorizonObservation.cohort_id == cohort.id,
                CryptoHorizonObservation.token_address.in_(tokens),
            )
        ).all():
            key = (row.token_address, row.horizon)
            if _is_retryable(row.status, _attempts_used(row)):
                retryable_ids[key] = row.id
            else:
                attempted.add(key)
    if not members:
        return [], {}
    plan = plan_observations(
        members, {}, set(), now,
        horizons=SPARSE_HORIZONS, window_minutes=SPARSE_BAND_MINUTES,
    )
    entries = [e for e in plan if (e.token_address, e.horizon) not in attempted]
    return entries, retryable_ids


def _plan_counts(plan: list[HorizonPlanEntry]) -> dict:
    counts: dict[str, dict] = {}
    for entry in plan:
        counts.setdefault(entry.horizon, {})
        counts[entry.horizon][entry.status] = (
            counts[entry.horizon].get(entry.status, 0) + 1
        )
    return counts


# --- OBSERVATION coverage report ------------------------------------------------
# Read this header before adding a field.
#
# This report answers ONE question: DID WE LOOK? It never answers "could we
# score it?" — that is RECONCILIATION coverage and it lives in
# `crypto-tape-coverage-report` / `crypto_coverage.build_coverage_report`,
# against a different denominator (birth events with a matured horizon vs.
# populated survival labels).
#
# Conflating the two is how production's real 4.57% 24h coverage stayed
# invisible for months, so the separation here is structural, not stylistic:
# every metric name in this report is unique to it (pinned by test), every rate
# carries its denominator inline, and no survival label is read, computed or
# reported anywhere in this module.

OBSERVATION_REPORT_KIND = "observation_coverage"
OBSERVATION_DENOMINATOR = "member_horizons_whose_band_has_closed"

OBS_STATE_OBSERVED = "observed"
OBS_STATE_ATTEMPTED_MISSED = "attempted_missed"
OBS_STATE_OUT_OF_BAND = "out_of_band"
OBS_STATE_SCHEDULING_MISS = "scheduling_miss"
OBS_STATE_ENROLLED_TOO_LATE = "enrolled_after_band_closed"
OBS_STATE_BAND_OPEN = "band_open"
OBS_STATE_BAND_NOT_OPEN = "band_not_open_yet"

# The minimum window this report will accept. A closed 24h band needs
# `now > anchor + 24h + BAND` while the member filter needs `added_at >=
# now - hours` and `added_at >= anchor`; with `hours` below that, NO member can
# satisfy both and the 24h denominator is structurally zero — measured:
# `hours=24` gave `bands_closed=0, look_completion_rate=None`, a silent empty
# answer that reads like "nothing to report".
LONGEST_SPARSE_HORIZON = max(SPARSE_HORIZONS, key=lambda pair: pair[1])[0]
MIN_REPORT_HOURS = int(
    (max(m for _l, m in SPARSE_HORIZONS) + SPARSE_BAND_MINUTES + 59) // 60
)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def timer_oncalendar() -> str:
    """The exact systemd `OnCalendar=` line this lane's cadence implies.

    Derived from SPARSE_CADENCE_MINUTES rather than written down twice: the
    cadence constant and the installed timer must not be able to drift apart,
    and the operator installing the unit should not have to translate minutes
    into calendar syntax by hand."""
    minutes = int(SPARSE_CADENCE_MINUTES)
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return "hourly" if hours == 1 else f"*-*-* 00/{hours}:00:00"
    return f"*-*-* *:00/{minutes}:00"


def build_observation_coverage_report(
    session: Session,
    settings: Settings | None = None,
    hours: int | None = None,
    now: datetime | None = None,
    top: int = 5,
) -> dict:
    """OBSERVATION coverage: of the member-horizons whose observation band has
    CLOSED, how many did this lane actually look at?

    Denominator (`member_horizons_whose_band_has_closed`): every (member,
    horizon) pair for which `now > anchor + horizon + band`. A band that is
    still open, or not yet open, is NOT in the denominator — it is pending, and
    counting a pending horizon as a miss would be the same lie as counting an
    absent tick as an observation.

    States are disjoint and exhaustive:
      * `observed`          — a real fetch inside the band produced usable state
      * `attempted_missed`  — we looked; the provider had nothing usable
                              (token_inactive / provider_no_pair /
                              no_liquidity_state / request_failed)
      * `out_of_band`       — an `observed` row whose `observed_at` is NOT
                              inside this member-horizon's band. This lane
                              cannot produce one; a manual `observe_once`
                              against the standing cohort could (it plans at
                              the fractional tape tolerance), and before this
                              state existed such a row scored as a clean
                              `observed` and drove `look_completion_rate` to
                              1.0 while `target_distance max` printed 7200s
                              against a 3600s half-width. Counted in the
                              closed-band denominator, never in `observed`,
                              never in `target_distance_seconds`.
      * `scheduling_miss`   — the band closed while this member was enrolled
                              and this lane never looked. THIS is the number
                              that proves the mechanism ran; it is not
                              recoverable and is never backfilled.
      * `enrolled_after_band_closed` — the band had already closed when the
                              member was enrolled, so the lane never had a
                              chance. Structural (eligibility admits a birth
                              past its 6h band to catch its 24h band), counted
                              separately, EXCLUDED from every rate.
      * `band_open` / `band_not_open_yet` — pending, excluded from every rate.

    Compute-on-demand: persists nothing, makes zero external calls, reads no
    survival outcome."""
    s = settings or get_settings()
    cfg = SparseObservationConfig.from_settings(s)
    now = _aware(now) or _now()
    cohorts = find_rolling_cohort(session, cfg.chain)
    base = {
        "kind": OBSERVATION_REPORT_KIND,
        "note": SPARSE_NOTE,
        "chain": cfg.chain,
        "generated_at": now.isoformat(),
        "horizons": list(SPARSE_HORIZON_LABELS),
        "band_minutes": SPARSE_BAND_MINUTES,
        "cadence_minutes": SPARSE_CADENCE_MINUTES,
        "external_calls": 0,
        "persisted": False,
        "this_report_measures": (
            "OBSERVATION coverage — whether a scheduled look actually happened "
            "inside its horizon band. Denominator: "
            f"{OBSERVATION_DENOMINATOR}."
        ),
        "this_report_does_not_measure": (
            "RECONCILIATION coverage — whether a survival label could be "
            "computed from the evidence held. That is a different question "
            "against a different denominator (birth events with a matured "
            "horizon); run crypto-tape-coverage-report for it. No survival "
            "label is read or reported here."
        ),
    }
    if len(cohorts) > 1:
        return {
            **base,
            "status": STATUS_AMBIGUOUS_COHORT,
            "cohort_ids": [c.id for c in cohorts],
        }
    if hours is not None and hours < MIN_REPORT_HOURS:
        return {
            **base,
            "status": "window_too_short",
            "window_hours": hours,
            "minimum_window_hours": MIN_REPORT_HOURS,
            "error": (
                f"--hours {hours} structurally nulls the "
                f"{LONGEST_SPARSE_HORIZON} denominator: a closed "
                f"{LONGEST_SPARSE_HORIZON} band needs now > anchor + horizon + "
                "band, while a member's added_at is never before its anchor, so "
                f"no member can satisfy both. Use --hours >= "
                f"{MIN_REPORT_HOURS} (or omit it for every enrolled member)."
            ),
        }
    if not cohorts:
        return {**base, "status": "no_cohort", "enrolled_members": 0}
    cohort = cohorts[0]

    # COLUMNS, NOT ENTITIES, AND BOTH SIDES WINDOWED.
    #
    # Measured at one year of this lane's own output (193,450 members /
    # 386,900 observations, ANALYZE run): the members query took 25.1s and the
    # observations query — which had NO window filter at all, only
    # `cohort_id = :id` — took 368.3s at 692MB peak RSS, holding SHARED for
    # 14.7s and causing 3 hard `database is locked` failures in a competing
    # writer at a 2s busy timeout. `--hours 48` left the 368s query untouched.
    # This is the operator's ONLY verification surface and the CLI default is
    # `--hours None`.
    #
    # The report reads 4 member fields of 9 and 5 observation fields of 22, and
    # never needs `raw_payload`, so both queries select columns. When `hours`
    # is given the observation side is scoped to the SAME member window via a
    # token subquery; when it is not, the extra subquery would be pure cost
    # over the identical row set, so it is omitted.
    member_window = None
    member_q = select(
        CryptoHorizonCohortMember.token_address,
        CryptoHorizonCohortMember.first_evidence_at,
        CryptoHorizonCohortMember.birth_observed_at,
        CryptoHorizonCohortMember.added_at,
    ).where(CryptoHorizonCohortMember.cohort_id == cohort.id)
    if hours:
        member_window = now - timedelta(hours=hours)
        member_q = member_q.where(
            CryptoHorizonCohortMember.added_at >= member_window
        )
    members = list(session.execute(
        member_q.order_by(CryptoHorizonCohortMember.id)
    ).all())

    obs_q = select(
        CryptoHorizonObservation.token_address,
        CryptoHorizonObservation.horizon,
        CryptoHorizonObservation.status,
        CryptoHorizonObservation.missing_cause,
        CryptoHorizonObservation.observed_at,
    ).where(CryptoHorizonObservation.cohort_id == cohort.id)
    if member_window is not None:
        obs_q = obs_q.where(
            CryptoHorizonObservation.token_address.in_(
                select(CryptoHorizonCohortMember.token_address).where(
                    CryptoHorizonCohortMember.cohort_id == cohort.id,
                    CryptoHorizonCohortMember.added_at >= member_window,
                )
            )
        )
    obs_by_key = {
        (o.token_address, o.horizon): o
        for o in session.execute(obs_q).all()
    }

    by_horizon: dict[str, dict] = {}
    misses: list[dict] = []
    distances: list[float] = []
    enrolment_lags: list[float] = []
    for label, minutes in SPARSE_HORIZONS:
        states = {
            OBS_STATE_OBSERVED: 0, OBS_STATE_ATTEMPTED_MISSED: 0,
            OBS_STATE_OUT_OF_BAND: 0,
            OBS_STATE_SCHEDULING_MISS: 0, OBS_STATE_ENROLLED_TOO_LATE: 0,
            OBS_STATE_BAND_OPEN: 0, OBS_STATE_BAND_NOT_OPEN: 0,
        }
        causes: dict[str, int] = {}
        for member in members:
            anchor = _aware(member.first_evidence_at) or _aware(
                member.birth_observed_at
            )
            if anchor is None:
                continue
            target = anchor + timedelta(minutes=minutes)
            band_start = target - timedelta(minutes=SPARSE_BAND_MINUTES)
            band_end = target + timedelta(minutes=SPARSE_BAND_MINUTES)
            added_at = _aware(member.added_at)
            if label == SPARSE_HORIZON_LABELS[0] and added_at is not None:
                enrolment_lags.append((added_at - anchor).total_seconds())
            obs = obs_by_key.get((member.token_address, label))
            if obs is not None:
                if obs.status == OBS_OBSERVED:
                    # The band is recomputed HERE from the member's own anchor
                    # and this lane's fixed half-width — never taken from the
                    # row, which a manual `observe_once` pass would have
                    # written at the much wider fractional tape tolerance.
                    observed_at = _aware(obs.observed_at)
                    if observed_at is None or not (
                        band_start <= observed_at <= band_end
                    ):
                        states[OBS_STATE_OUT_OF_BAND] += 1
                        continue
                    states[OBS_STATE_OBSERVED] += 1
                    distances.append(abs((observed_at - target).total_seconds()))
                else:
                    states[OBS_STATE_ATTEMPTED_MISSED] += 1
                    causes[obs.missing_cause or obs.status] = (
                        causes.get(obs.missing_cause or obs.status, 0) + 1
                    )
                continue
            if now > band_end:
                # A band that closed BEFORE this member was even enrolled is
                # not a scheduling failure — the lane never had the chance to
                # look. Eligibility deliberately admits a birth past its 6h
                # band so its 24h band can still be caught (see
                # `enrolment_rejection_reason`), so this state is structural,
                # not exceptional. Conflating it with a real miss would
                # inflate `scheduling_miss_rate` with tokens that predate
                # enrolment — the same denominator conflation this whole
                # milestone exists to stop. Counted separately and excluded
                # from every rate — but `never_had_a_chance_rate` below reports
                # it against the whole member-horizon population, so a lane
                # that accomplishes nothing cannot read clean.
                if added_at is not None and added_at > band_end:
                    states[OBS_STATE_ENROLLED_TOO_LATE] += 1
                    continue
                states[OBS_STATE_SCHEDULING_MISS] += 1
                if len(misses) < top:
                    misses.append({
                        "token": member.token_address[:16],
                        "horizon": label,
                        "band_closed_at": band_end.isoformat(),
                        "enrolled_at": added_at.isoformat() if added_at else None,
                    })
            elif now >= band_start:
                states[OBS_STATE_BAND_OPEN] += 1
            else:
                states[OBS_STATE_BAND_NOT_OPEN] += 1
        closed = (
            states[OBS_STATE_OBSERVED]
            + states[OBS_STATE_ATTEMPTED_MISSED]
            + states[OBS_STATE_OUT_OF_BAND]
            + states[OBS_STATE_SCHEDULING_MISS]
        )
        attempted = states[OBS_STATE_OBSERVED] + states[OBS_STATE_ATTEMPTED_MISSED]
        # every member-horizon this report saw, pending ones included: the
        # denominator `never_had_a_chance_rate` needs, because
        # `enrolled_after_band_closed` is excluded from all the others and a
        # lane that enrolled everything too late would otherwise read clean.
        population = sum(states.values())
        by_horizon[label] = {
            **states,
            "bands_closed": closed,
            "attempted": attempted,
            "member_horizons": population,
            "miss_causes": causes,
            "observation_attempt_rate": _rate(attempted, closed),
            "observation_success_rate": _rate(states[OBS_STATE_OBSERVED], attempted),
            "look_completion_rate": _rate(states[OBS_STATE_OBSERVED], closed),
            "scheduling_miss_rate": _rate(states[OBS_STATE_SCHEDULING_MISS], closed),
            "out_of_band_rate": _rate(states[OBS_STATE_OUT_OF_BAND], closed),
            "never_had_a_chance_rate": _rate(
                states[OBS_STATE_ENROLLED_TOO_LATE], population
            ),
            "attempt_denominator": OBSERVATION_DENOMINATOR,
            "success_denominator": "attempted_member_horizons",
            "never_had_a_chance_denominator": "member_horizons_in_window",
        }

    distances.sort()
    enrolment_lags.sort()

    def pctile(values: list[float], p: float):
        if not values:
            return None
        idx = min(len(values) - 1, int(p * (len(values) - 1)))
        return round(values[idx], 1)

    # LIVENESS. `scheduling_miss_rate` detects a SLOW timer but not a STOPPED
    # one: once the timer stops, late births land in the excluded
    # `enrolled_after_band_closed` bucket or (past the enrolment window) are
    # never enrolled at all, and the rate lags a full band before it moves.
    # These two ages move within one cadence. Read by MAX(id), which walks the
    # primary key backwards, never a scan over `observed_at`/`added_at`.
    latest_observation_at = _aware(session.execute(
        select(CryptoHorizonObservation.observed_at)
        .where(CryptoHorizonObservation.cohort_id == cohort.id)
        .order_by(CryptoHorizonObservation.id.desc())
        .limit(1)
    ).scalar())
    latest_enrolment_at = _aware(session.execute(
        select(CryptoHorizonCohortMember.added_at)
        .where(CryptoHorizonCohortMember.cohort_id == cohort.id)
        .order_by(CryptoHorizonCohortMember.id.desc())
        .limit(1)
    ).scalar())
    latest_pass_at = max(
        [t for t in (latest_observation_at, latest_enrolment_at) if t is not None],
        default=None,
    )
    pass_age_minutes = (
        round((now - latest_pass_at).total_seconds() / 60.0, 1)
        if latest_pass_at is not None else None
    )
    cadence_warning = (
        pass_age_minutes is not None
        and pass_age_minutes > 1.5 * SPARSE_CADENCE_MINUTES
    )

    return {
        **base,
        "status": "ok",
        "cohort_id": cohort.id,
        "enrolled_members": len(members),
        "window_hours": hours,
        "by_horizon": by_horizon,
        "scheduling_miss_examples": misses,
        "liveness": {
            "latest_pass_at": (
                latest_pass_at.isoformat() if latest_pass_at else None
            ),
            "latest_observation_at": (
                latest_observation_at.isoformat() if latest_observation_at else None
            ),
            "latest_enrolment_at": (
                latest_enrolment_at.isoformat() if latest_enrolment_at else None
            ),
            "previous_pass_age_minutes": pass_age_minutes,
            "cadence_minutes": SPARSE_CADENCE_MINUTES,
            "cadence_warning": cadence_warning,
            "expected_timer_oncalendar": timer_oncalendar(),
        },
        "enrolment_lag_seconds": {
            "p50": pctile(enrolment_lags, 0.5),
            "p90": pctile(enrolment_lags, 0.9),
            "max": (round(enrolment_lags[-1], 1) if enrolment_lags else None),
            "band_half_width_seconds": SPARSE_BAND_MINUTES * 60,
            "measures": "member.added_at - birth anchor, per enrolled member",
        },
        "target_distance_seconds": {
            "p50": pctile(distances, 0.5), "p90": pctile(distances, 0.9),
            "max": (round(distances[-1], 1) if distances else None),
            "band_half_width_seconds": SPARSE_BAND_MINUTES * 60,
        },
        "disclaimer": (
            "observation coverage — did a scheduled look happen; NOT whether a "
            "survival label could be computed. Misses are recorded honestly and "
            "never backfilled, interpolated, or served from a nearby tick. "
            "Never advice, no EV, no recommendation, no sizing, no orders, no "
            "wallets, no execution"
        ),
    }
