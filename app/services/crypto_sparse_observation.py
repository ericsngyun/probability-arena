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
  observation table. The ONLY schema change is migration 0029, one additive
  index on `crypto_horizon_cohort_members(cohort_id, added_at)` — no new
  table, no new column, no data change (see `_sparse_plan`).
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

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.config import Settings, get_settings
from app.models import (
    CryptoHorizonCohort,
    CryptoHorizonCohortMember,
    CryptoHorizonObservation,
    CryptoTokenBirthEvent,
)
from app.services.crypto_horizon import (
    MEMBERSHIP_ROLLING,
    OBS_IDENTITY_MISMATCH,
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
SPARSE_COHORT_SOURCE = "prospective_sparse_observation"


class AmbiguousCohortError(RuntimeError):
    """Refusal to guess which cohort owns the observation denominator."""


class ProviderPolicyViolation(RuntimeError):
    """A non-DexScreener provider reached a REQUEST inside this lane's fetch
    phase. The deny set is supposed to make that impossible before a socket
    opens, so reaching it means the structural guarantee is broken; it is a
    typed refusal (`provider_policy_violation`), never a reported metric."""

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

# `raise`, not `assert`: `python -O` strips assert statements outright, and
# these two are the invariants that keep the lane from buying ticks that can
# never mature a label.
if not SPARSE_HORIZONS:
    raise ValueError("sparse horizons must be a non-empty subset of HORIZONS")
_TIGHTEST_TAPE_TOLERANCE_MINUTES = min(
    minutes * HORIZON_TOLERANCE for _label, minutes in SPARSE_HORIZONS
)
if SPARSE_BAND_MINUTES > _TIGHTEST_TAPE_TOLERANCE_MINUTES:
    raise ValueError(
        "invariant (1) BAND CONTAINMENT violated: a sparse observation could "
        "land outside compute_survival's tolerance window and buy a tick that "
        "can never mature the label it was bought for"
    )
if int((2 * SPARSE_BAND_MINUTES) // SPARSE_CADENCE_MINUTES) < 2:
    raise ValueError(
        "invariant (2) MISSED-PASS TOLERANCE violated: fewer than 2 scheduled "
        "passes fall inside a band, so one missed pass silently loses the "
        "horizon"
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
# RAW-PAYLOAD-STORAGE-001 made and then REVERSED this exact decision six days
# before this lane was written: raw payloads were 27% of the production DB with
# zero readers, and `RAW_PAYLOAD_CAPTURE_MODE=none` cut ticks 2051B -> 118B.
# Keeping 3 per-candidate diagnostics here was measured at 424 MB/year of a
# ~750 MB/year total, i.e. 71% of this lane's growth, on a 4.55 GB database
# already past a 3,072 MB gate — for a blob nothing reads.
#
# 0 keeps the two fields that ARE read: `selected_pair_basis` (why this pair
# was chosen — the audit question) and `candidate_count` (how many there were).
# The per-candidate list is dropped.
AUDIT_CANDIDATE_LIMIT = 0

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
REJECT_NO_ANCHOR_TIMESTAMP = "no_first_evidence_at"
REJECT_ALL_BANDS_CLOSED = "all_sparse_bands_closed"
# not a per-birth reason: a marker that the candidate PAGE was filled with
# ineligible births, so eligible ones may be waiting behind them
REJECT_PAGE_EXHAUSTED = "enrolment_page_exhausted"


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
    whole eligible birth population.

    THE ANCHOR IS `first_evidence_at`, WITH NO FALLBACK. This used to coalesce
    to `observed_at`, but `CryptoLifecycleTapeRecorder.compute_survival`
    anchors STRICTLY on `first_evidence_at` and sets `provider_gap=True`
    immediately when it is NULL. Measured: a birth with NULL
    `first_evidence_at` was enrolled and observed, then scored
    `survived_6h=None, provider_gap=True` — provider spend with a provably
    zero denominator gain, the exact thing rule 1 above exists to prevent, one
    field over. The fallback was also wrong on its own terms: `observed_at` is
    the TAPE RUN time, not a birth time, so a single tape run backfilling
    older tokens would anchor them all at the same instant."""
    anchor = _aware(getattr(birth, "first_evidence_at", None))
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
    """Create THE standing cohort. Never a second one.

    The rolling marker is one unconstrained JSON key. Dropped from the existing
    cohort's provenance, the next pass silently created a SECOND rolling cohort
    and split the observation denominator in two; forged onto a frozen cohort,
    the lane wedges at `ambiguous_cohort` forever. The forged direction is
    already fail-closed and loud. This closes the dropped direction: a cohort
    carrying this lane's own `provenance["source"]` for this chain is treated
    as the standing cohort whether or not the membership key survived, and a
    second one is refused rather than created.

    Creation is a once-per-deployment event and is logged at WARNING so it can
    never happen quietly."""
    existing = session.execute(
        select(CryptoHorizonCohort).where(CryptoHorizonCohort.chain == chain)
    ).scalars().all()
    orphaned = [
        c for c in existing
        if (getattr(c, "provenance", None) or {}).get("source")
        == SPARSE_COHORT_SOURCE
    ]
    if orphaned:
        raise AmbiguousCohortError(
            f"chain {chain} already has {len(orphaned)} cohort(s) created by "
            f"this lane (ids {[c.id for c in orphaned]}) whose "
            f"provenance['membership'] is no longer {MEMBERSHIP_ROLLING!r}. "
            "Refusing to create a second standing cohort and split the "
            "observation denominator; repair the marker on the existing "
            "cohort instead."
        )
    logger.warning(
        "CRYPTO-COVERAGE-REPAIR-002: creating THE standing rolling sparse "
        "cohort for chain %s. This should happen exactly once per deployment; "
        "a second occurrence means the rolling marker was lost.", chain,
    )
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
            "source": SPARSE_COHORT_SOURCE,
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
        # None until the pass gets far enough to look (B5); never a false True.
        "working_set_index_present": None,
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
        # LOW: horizons whose band closed BETWEEN pass start and the fetch, so
        # the honest answer arrived out of band and was not written at all.
        "band_closed_during_pass": 0,
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
    # Gate 1: the provider answered with pairs, and NONE of them is about this
    # token. Distinct from `request_failed` (no answer at all) and never both.
    identity_mismatch: bool = False
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


def _identity_matched(pair, token: str) -> bool:
    """Is this pair's BASE token exactly the token we asked about?

    CRYPTO-COVERAGE-REPAIR-002 (Gate 1). `fetch_pairs_for_token` filters the
    provider's answer by CHAIN only (`dexscreener.py`: `pair.chain ==
    self.chain`), and `select_pair`'s quality policy gives an exact base-token
    match a +25 BONUS rather than making it a gate. So a chain-correct,
    well-formed, non-empty answer whose pairs belong to a DIFFERENT token parses
    cleanly, scores, is selectable, and lands as `OBS_OBSERVED` with a price
    tick filed under the wrong token address. For a milestone whose entire
    purpose is an honest denominator, a tick under the wrong token is worse than
    a missing one: a missing row is visibly missing, a wrong row is invisibly
    wrong and contaminates every survival label derived from it.

    THIS GATE LIVES IN THIS LANE, NOT IN THE SHARED ADAPTER. The adapter is used
    by the scout, meme, discovery and frozen-horizon lanes; the +25 bonus is a
    deliberate soft preference there (a quote-side or related pool can be the
    honest best OBSERVABLE price for a discovery-time liquidity read), and this
    lane is the only one that turns a selected pair into a per-token,
    per-horizon price tick that a survival label is computed from. Confining the
    hard gate here changes exactly the behaviour that needs changing.

    BASE ONLY, DELIBERATELY. A pair with `quote_token_address == token` prices
    something ELSE against our token: its `price_usd` is the BASE asset's price,
    not this token's. Accepting a quote-side match would write another token's
    price under our address — precisely the defect this gate exists to close,
    arriving by a slightly politer route. `quote_token_address` is therefore
    read nowhere in this predicate.

    A missing / renamed / null identity field cannot reach here: `_parse_pair`
    already returns None without `baseToken.address`, and Probe 15's
    outcome-based check turns a non-empty payload that parses to zero pairs into
    a failed REQUEST upstream of this function. This predicate closes the
    remaining case — the field is present, well-formed, and names a DIFFERENT
    token."""
    return pair.base_token_address == token


async def _fetch_phase(
    service: CryptoHorizonService,
    due_tokens: list[str],
    max_duration_seconds: float | None,
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
    the provider is being called.

    WHAT ACTUALLY GUARANTEES IT is STRUCTURAL, not the test: this function has
    no `session` parameter and `CryptoHorizonService` holds no session, so
    there is no session in scope to open a transaction with. The
    transaction-shape test is a smoke check, not the guarantee — injecting five
    `session.execute(select(...))` calls into this phase was measured to leave
    all four of its assertions passing, because it watches commits and ORM
    flushes rather than statement starts. The earlier docstring claimed the
    opposite.

    The headline property additionally depends on pysqlite's DEFERRED implicit
    BEGIN: a read does not open a write transaction. Nothing in this repo owns
    that assumption, so `tests/test_crypto_coverage_repair_002.py` asserts it at
    the `app/db.py` boundary.

    THE DEADLINE IS ANCHORED HERE, at the first line of the fetch phase — not
    at pass start. `.env.example`, `config.py` and `docs/FEATURE_FLAGS.md` all
    describe it as a budget on the FETCH phase, and anchoring it at pass start
    made the real fetch budget shrink by however long the prelude took, i.e.
    silently smaller as the cohort grew. Anchoring it here makes the three
    documents true and decouples the budget from prelude cost.

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
    deadline = (
        _now() + timedelta(seconds=max_duration_seconds)
        if max_duration_seconds is not None else None
    )
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
            except ProviderPolicyError as exc:
                # NEVER degrade an authorization failure into an ordinary
                # provider miss. The policy module says so explicitly, and the
                # whole "no SolanaTracker spend" guarantee depends on it: a
                # swallowed denial is indistinguishable from a token that
                # simply has no pairs.
                #
                # The LEDGER dies with the `provider_run` context, so it is
                # attached to the exception here. This is the one path whose
                # entire purpose is to prove what a provider did; it used to
                # report `external_calls: 0` and no ledger after real fetches.
                exc.provider_ledger = ctx.ledger.snapshot()
                exc.external_calls = calls
                exc.token_address = token
                raise
            except Exception as exc:  # adapter degrades to [], but be safe
                logger.warning(
                    "sparse observation fetch failed for %s: %s", token, exc
                )
                pairs = []
                request_failed = True
            calls += 1
            # GATE 1 — EXACT TOKEN IDENTITY, applied before selection and before
            # any diagnostic is built. `mine` is the ONLY list that reaches
            # `select_pair` or `describe_pair`, so a wrong-token pair cannot be
            # scored, cannot be selected, and cannot leak a price into the
            # observation row through the `no_liquidity_state` branch of
            # `_record_observation` (which harvests `price_usd` from
            # `candidates` when nothing is eligible).
            mine = [p for p in pairs if _identity_matched(p, token)]
            # An answer that named only OTHER tokens is not an answer about
            # THIS one. It must never reach `select_pair([])`'s
            # `candidate_count: 0`, which past 24h `_record_observation` reads
            # as the terminal, affirmative claim `token_inactive`.
            identity_mismatch = bool(pairs) and not mine and not request_failed
            selected, basis = select_pair(mine, token)
            if len(mine) != len(pairs):
                # The ONLY durable evidence of what the provider actually said,
                # because `AUDIT_CANDIDATE_LIMIT = 0` stores no per-candidate
                # diagnostics. Bounded at 5 addresses: a lane writing ~1,000
                # rows/day must not re-inflate `raw_payload` (RAW-PAYLOAD-
                # STORAGE-001), and 5 is enough to tell "one stray pool" from
                # "the endpoint is answering about a different token entirely".
                rejected = sorted({p.base_token_address for p in pairs} - {token})
                basis = {**basis, "identity_gate": {
                    "requested_token": token,
                    "pairs_returned": len(pairs),
                    "exact_base_matches": len(mine),
                    "rejected_base_tokens": rejected[:5],
                    "rejected_base_token_count": len(rejected),
                }}
            fetched.append(_Fetched(
                token_address=token,
                selected=selected,
                basis=basis,
                candidates=[describe_pair(p, token) for p in mine],
                request_failed=request_failed,
                identity_mismatch=identity_mismatch,
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
    if spent:
        # CRYPTO-COVERAGE-REPAIR-002 (B2): carry the EVIDENCE, exactly as the
        # `ProviderPolicyError` branch above does. This is the more severe of
        # the two paths — a paid request actually went out — and it used to
        # report `external_calls: 0`, `solana_tracker_calls: 0` and no ledger
        # at all, while the milder DENIED path (nothing spent) reported both.
        # The one path whose entire existence is to prove a paid request
        # happened must not be the one with the poorer record.
        violation = ProviderPolicyViolation(
            f"sparse observation issued a non-DexScreener provider request: {spent}"
        )
        violation.provider_ledger = ledger
        violation.external_calls = calls
        raise violation
    return fetched, calls, stop_reason, ledger


def _apply_provider_ledger(result: dict, ledger: dict, calls: int) -> None:
    """Populate every provider-spend receipt on `result` FROM the ledger.

    One implementation for all three exits — the healthy pass, the denied
    attempt, and the proven violation — so the severe path can never again
    report less than the mild one. Nothing here is hardcoded: the paid-provider
    count is derived from the same accounting the guard itself keeps, so a
    ledger that accounts a paid request always moves the number.
    """
    result["external_calls"] = calls
    result["provider_ledger"] = ledger
    result["solana_tracker_calls"] = sum(
        ledger.get("solana-tracker", {}).get(k, 0)
        for k in ("authorized", "started", "succeeded", "failed")
    )
    result["denied_provider_attempts"] = {
        name: entry["blocked_policy"]
        for name, entry in ledger.items()
        if name != "dexscreener" and entry.get("blocked_policy")
    }


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
    # ONE result dict for the whole pass, built up in place.
    #
    # `_refused` used to rebuild from `_base_result`, which DESTROYED the
    # evidence of what the pass had already done. Measured: a
    # `provider_policy_violation` after enrolment reported `enrolled: 0,
    # persisted: False, cohort_id: None` while the database held 1 cohort and 5
    # members; a violation on request 5 of 8 reported `external_calls: 0`,
    # `solana_tracker_calls: 0` and no ledger at all, after 4 real paid-free
    # fetches. The one path whose entire purpose is to prove what a provider
    # did understated real spend as zero. The `db_locked` WRITE path already
    # did this correctly with `result.update(...)`; every path now does.
    result = _base_result(STATUS_OK, cfg, started)
    result["gate_bypassed"] = bypass
    result["enrol_limit"] = cfg.enrol_limit
    result["observe_limit"] = cfg.observe_limit
    result["write_batch_size"] = cfg.write_batch_size
    result["max_duration_seconds"] = cfg.max_duration_seconds
    result["cohort_id"] = None
    result["cohort_created"] = False

    def _refused(status: str, error: str) -> dict:
        result["status"] = status
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
    result["cohort_id"] = cohort.id if cohort is not None else None

    # B5: report whether migration 0029's working-set index exists BEFORE the
    # plan runs, on both the dry-run and the live path. False means
    # `_sparse_plan` is about to take the un-indexed path; it is not a refusal,
    # because nothing here is unsafe without it — it is the one signal that
    # makes an otherwise silent, load-bearing absence visible on the receipt.
    result["working_set_index_present"] = _working_set_index_present(session)

    service = service or CryptoHorizonService(settings=s)

    # --- enrolment (pure DB, no provider) ---------------------------------
    candidates, rejections, considered = _enrolment_candidates(
        session, cfg, cohort, now, count_anchorless=dry_run,
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
        try:
            cohort = _create_rolling_cohort(session, cfg.chain, now)
        except AmbiguousCohortError as exc:
            session.rollback()
            return _refused(STATUS_AMBIGUOUS_COHORT, str(exc))
        result["cohort_created"] = True
    # `progress` is updated after each COMMITTED enrolment batch, so a refusal
    # mid-enrolment reports what is actually durable rather than 0.
    progress = {"enrolled": 0}

    def _settle_enrolment_state() -> None:
        """After a rollback, report what the DATABASE holds — the cohort row is
        only flushed, not committed, until the first enrolment batch lands, so
        a rollback can un-create it."""
        result["enrolled"] = progress["enrolled"]
        try:
            surviving = find_rolling_cohort(session, cfg.chain)
        except Exception:  # pragma: no cover - the DB is already unhappy
            return
        result["cohort_id"] = surviving[0].id if surviving else None
        result["cohort_created"] = bool(result["cohort_created"]) and bool(surviving)
        result["persisted"] = (
            progress["enrolled"] > 0 or bool(result["cohort_created"])
        )

    try:
        enrolled = _enrol(session, cfg, cohort, candidates, now, progress)
    except IntegrityError as exc:
        session.rollback()
        _settle_enrolment_state()
        return _refused(
            STATUS_CONCURRENT_WRITE_CONFLICT,
            f"enrolment raced another writer on the standing cohort: {exc}",
        )
    except Exception as exc:
        session.rollback()
        if _is_db_locked(exc):
            _settle_enrolment_state()
            return _refused(
                STATUS_DB_LOCKED,
                "database is locked; pass abandoned during enrolment",
            )
        raise
    result["cohort_id"] = cohort.id
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
    # report `ok` having fetched nothing. The budget is anchored inside
    # `_fetch_phase`, at fetch start — see its docstring.
    try:
        fetched, calls, stop_reason, ledger = await _fetch_phase(
            service, selected_tokens, cfg.max_duration_seconds, clock,
        )
    except ProviderPolicyViolation as exc:
        # The ledger PROVED a non-DexScreener request happened. Typed, loud,
        # non-zero — the structural guarantee this lane sells is broken.
        #
        # B2: this is the SEVERE branch, so it carries the FULL receipt. The
        # ledger dies with the `provider_run` context inside `_fetch_phase`, so
        # it travels on the exception exactly as the denied branch below does.
        _apply_provider_ledger(
            result,
            getattr(exc, "provider_ledger", {}) or {},
            getattr(exc, "external_calls", 0),
        )
        return _refused(STATUS_PROVIDER_POLICY_VIOLATION, str(exc))
    except Exception as exc:
        from app.services.crypto_provider_policy import ProviderPolicyError

        if isinstance(exc, ProviderPolicyError):
            # A provider this lane never authorizes was reached. Loud and
            # non-zero — never degraded into a provider miss. The evidence of
            # what HAD already been spent travels on the exception.
            _apply_provider_ledger(
                result,
                getattr(exc, "provider_ledger", {}) or {},
                getattr(exc, "external_calls", 0),
            )
            return _refused(
                STATUS_PROVIDER_POLICY_VIOLATION,
                f"a non-DexScreener provider was attempted from the sparse "
                f"observation path and was denied before any request: {exc}",
            )
        raise
    _apply_provider_ledger(result, ledger, calls)
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
    #
    # `fetched_tokens` is bounded by `observe_limit` (<= OBSERVE_MAX_CALLS =
    # 100), so this `.in_()` can never grow with the cohort. The `chain`
    # predicate is redundant with `cohort_id` — a cohort is single-chain by
    # construction — but it is stated so a future multi-chain cohort cannot
    # silently mix chains here.
    fetched_tokens = [item.token_address for item in fetched]
    members = {
        m.token_address: m
        for m in session.execute(
            select(CryptoHorizonCohortMember).where(
                CryptoHorizonCohortMember.cohort_id == cohort.id,
                CryptoHorizonCohortMember.chain == cfg.chain,
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
    band_closed = 0
    meter = _WriteMeter()

    def _stage(batch: list[_Fetched]):
        """Stage one batch's rows. Called once per commit ATTEMPT — see
        `_commit_with_retry`: a rollback expunges everything staged, so the
        rows must be rebuilt, not merely re-committed."""
        staged = {
            "outcomes": {}, "ticks": 0, "recorded": 0, "retried": 0,
            "band_closed": 0,
        }
        for item in batch:
            member = members.get(item.token_address)
            observed_at = item.fetched_at or clock()
            for entry in due_by_token.get(item.token_address, []):
                # THE LANE MUST NOT PENALISE ITS OWN BAND EDGE.
                #
                # The plan is fixed at pass START; the tick is stamped after the
                # FETCH. So a token planned with seconds of band left can be
                # answered honestly up to `max_duration_seconds` after its band
                # closed — measured overshoot 1.21s — and the row was written
                # with an `observed_at` outside its own band. The pass then said
                # `observed: 1` while the report said `out_of_band: 1` for the
                # same row.
                #
                # That matters beyond the disagreement. `out_of_band_rate` is
                # the governance signal for MANUAL-LANE CONTAMINATION (an
                # `observe_once` planning at the fractional tape tolerance), and
                # a signal that also fires benignly from this lane's own clock
                # cannot carry that meaning. So the band is re-checked against
                # the FETCH timestamp, and a horizon whose band closed during
                # the pass is not written at all: it becomes an honest
                # `scheduling_miss` — this lane genuinely did not look inside
                # the band — counted here so the skip is never silent.
                window_end = _aware(entry.window_end)
                if window_end is not None and observed_at > window_end:
                    staged["band_closed"] += 1
                    continue
                existing = retry_rows.get((item.token_address, entry.horizon))
                # read BEFORE the call: `_record_observation` replaces
                # `raw_payload` wholesale. After a rollback the row is expired
                # and re-read from the database, so this recounts correctly on
                # every staging attempt rather than compounding.
                attempts = _attempts_used(existing) if existing is not None else 0
                # GATE 1. An identity mismatch takes the same SHORT-CIRCUIT as a
                # failed request — no tick, no price, no `candidate_count: 0`
                # reading — but is stamped with its own cause, so a provider
                # CONTRACT violation is never filed in the rate-limit bucket.
                # `request_failed` wins when both are set (they cannot be: no
                # answer means no pairs to fail identity on) because "we never
                # heard back" is the stronger and earlier fact.
                short_circuit = item.request_failed or item.identity_mismatch
                failure_status = (
                    OBS_REQUEST_FAILED if item.request_failed
                    else OBS_IDENTITY_MISMATCH
                )
                status, _cause, tick = service._record_observation(
                    session, cohort.id, member, entry, item.selected, item.basis,
                    item.candidates, short_circuit,
                    observed_at, existing=existing,
                    audit_candidate_limit=AUDIT_CANDIDATE_LIMIT,
                    tick_source=TICK_SOURCE,
                    failure_status=failure_status,
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
            staged = _commit_with_retry(
                session, lambda b=batch: _stage(b), sleeper, meter,
            )
            batches += 1
            recorded += staged["recorded"]
            ticks += staged["ticks"]
            retried += staged["retried"]
            band_closed += staged["band_closed"]
            for name, count in staged["outcomes"].items():
                outcomes[name] = outcomes.get(name, 0) + count
    except IntegrityError as exc:
        session.rollback()
        _record_write_progress(
            result, batches, recorded, ticks, retried, outcomes, meter,
            band_closed,
        )
        result["error"] = (
            "an observation row for this (cohort, token, horizon) already "
            f"existed — another pass raced this one: {exc}"
        )
        result["status"] = STATUS_CONCURRENT_WRITE_CONFLICT
        return _finish(result, started)
    except Exception as exc:
        session.rollback()
        if _is_db_locked(exc):
            _record_write_progress(
                result, batches, recorded, ticks, retried, outcomes, meter,
                band_closed,
            )
            result["error"] = (
                "database is locked; observation write phase abandoned. The "
                "counts above are the batches that COMMITTED before the lock, "
                "and they are durable."
            )
            result["status"] = STATUS_DB_LOCKED
            return _finish(result, started)
        raise

    result["observations_recorded"] = recorded
    result["ticks_written"] = ticks
    result["outcome_counts"] = outcomes
    result["batches_committed"] = batches
    result["request_failures_reattempted"] = retried
    result["persisted"] = result["persisted"] or recorded > 0
    result["band_closed_during_pass"] = band_closed
    result["write_lock"] = meter.snapshot()
    if stop_reason != STOP_COMPLETE:
        result["status"] = STATUS_PARTIAL
    return _finish(result, started)


@dataclass
class _WriteMeter:
    """Per-pass write-phase lock instrumentation.

    The reconciler PERSISTS `lock_wait_ms` / `write_hold_ms_max` / `blocked_ms`
    / phase attribution — and its timer is disarmed precisely because those
    numbers are uncalibrated on EVO. This lane proposes an hourly unattended
    timer against the same file, and shipped with `duration_ms` and nothing
    else. These are the same measurements, IN THE RESULT.

    They are NOT persisted to a run table: that needs a new table and a
    migration decision this review round did not take. Until it does, this
    lane's timer must not be installed — the flag stays off and the CLI is the
    only way to read these numbers."""

    batches: int = 0
    retry_attempts: int = 0
    lock_failures: int = 0
    write_hold_ms_max: float = 0.0
    commit_ms_max: float = 0.0
    commit_ms_total: float = 0.0

    def record(self, *, attempts: int, hold_ms: float, commit_ms: float) -> None:
        self.batches += 1
        self.retry_attempts += attempts - 1
        self.write_hold_ms_max = max(self.write_hold_ms_max, hold_ms)
        self.commit_ms_max = max(self.commit_ms_max, commit_ms)
        self.commit_ms_total += commit_ms

    def record_failure(self, attempt: int) -> None:
        self.lock_failures += 1

    def snapshot(self) -> dict:
        return {
            "batches": self.batches,
            "retry_attempts": self.retry_attempts,
            "lock_failures": self.lock_failures,
            "write_hold_ms_max": round(self.write_hold_ms_max, 3),
            "commit_ms_max": round(self.commit_ms_max, 3),
            "commit_ms_total": round(self.commit_ms_total, 3),
            "persisted": False,
            "note": (
                "write-phase only; the fetch phase holds no transaction. NOT "
                "persisted to a run table — install no timer until it is"
            ),
        }


def _record_write_progress(
    result: dict, batches: int, recorded: int, ticks: int, retried: int,
    outcomes: dict, meter: "_WriteMeter", band_closed: int = 0,
) -> None:
    """Carry the DURABLE write-phase counts into a refusal result. Every one of
    these is incremented only after a batch's commit returned (see
    `_commit_with_retry`), so a partially-committed pass reports the rows it
    actually wrote instead of claiming zero."""
    result["batches_committed"] = batches
    result["observations_recorded"] = recorded
    result["ticks_written"] = ticks
    result["request_failures_reattempted"] = retried
    result["outcome_counts"] = dict(outcomes)
    result["persisted"] = bool(result.get("persisted")) or recorded > 0
    result["band_closed_during_pass"] = band_closed
    result["write_lock"] = meter.snapshot()


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


def _commit_with_retry(session: Session, prepare, sleeper=None, meter=None):
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
            hold_start = time.perf_counter()
            staged = prepare()
            commit_start = time.perf_counter()
            session.commit()
            if meter is not None:
                meter.record(
                    attempts=attempt,
                    hold_ms=(time.perf_counter() - hold_start) * 1000.0,
                    commit_ms=(time.perf_counter() - commit_start) * 1000.0,
                )
            return staged
        except Exception as exc:
            if not _is_db_locked(exc) or attempt == DB_LOCKED_MAX_ATTEMPTS:
                if meter is not None and _is_db_locked(exc):
                    meter.record_failure(attempt)
                raise
            session.rollback()
            if meter is not None:
                meter.record_failure(attempt)
            sleeper(DB_LOCKED_RETRY_SECONDS)
    raise RuntimeError(  # pragma: no cover - the loop always returns or raises
        "commit retry ladder exhausted without committing or raising"
    )


# --- enrolment ------------------------------------------------------------------


def _enrolment_candidates(
    session: Session, cfg: SparseObservationConfig, cohort, now: datetime,
    count_anchorless: bool = False,
) -> tuple[list, dict, int]:
    """Eligible, not-yet-enrolled births, OLDEST ANCHOR FIRST.

    Oldest-first, not newest-first: the birth closest to losing its remaining
    band is the one a bounded pass must not defer. This is the same
    starvation lesson CRYPTO-COVERAGE-REPAIR-001 learned twice (recency-anchored
    selection starved old cohorts; backlog appended after the in-window head
    was never reached).

    The candidate query is bounded by the enrolment window on both sides: a
    birth older than `ENROL_WINDOW_MINUTES` has no reachable band and is not a
    candidate at all.

    TWO QUERY-SHAPE FIXES, both measured with `EXPLAIN QUERY PLAN` at 193k
    members with `sqlite_stat1` present (668ms -> 0.8ms, 835x, no schema
    change):

    * SARGABLE ANCHOR. The predicate used to be
      `coalesce(first_evidence_at, observed_at) BETWEEN :cutoff AND :now`. A
      function of a column cannot use an index, so the existing
      `ix_crypto_token_birth_events_first_evidence_at` was unusable and this
      became a full scan of the birth table — the docstring's "never walks the
      whole birth table" was false. `first_evidence_at` is now required
      outright (see `enrolment_rejection_reason`), so the predicate is a bare
      indexed range.
    * NOT EXISTS, NOT `NOT IN`. `NOT IN (subquery)` made SQLite materialise
      LIST SUBQUERY 1 — ALL 193k member rows — into a temporary table on every
      pass, then sort into a temp B-tree. A correlated `NOT EXISTS` probes the
      unique `ix_horizon_member_cohort_token` index once per candidate row
      instead. This is the dominant win of the two."""
    anchor_col = CryptoTokenBirthEvent.first_evidence_at
    cutoff = now - timedelta(minutes=ENROL_WINDOW_MINUTES)
    conditions = [
        CryptoTokenBirthEvent.chain == cfg.chain,
        anchor_col >= cutoff,
        anchor_col <= now,
    ]
    if cohort is not None:
        member = aliased(CryptoHorizonCohortMember)
        conditions.append(
            ~select(member.id).where(
                member.cohort_id == cohort.id,
                member.token_address == CryptoTokenBirthEvent.token_address,
            ).exists()
        )
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
    # PAGE EXHAUSTION, reported not hidden. This reads `enrol_limit * 2` rows
    # and filters in Python, so a page dominated by ineligible births can
    # return fewer than `enrol_limit` eligible ones while eligible births wait
    # behind them — and the rejects are re-read at the head of every pass until
    # they age out of the window. The page bound stays (pushing
    # `_completeness_reason`'s full predicate into SQL would duplicate it in
    # two places, and paging until `enrol_limit` eligible rows are found makes
    # a bounded pass unbounded), but the condition is now visible instead of
    # looking like "there was nothing to enrol".
    page_exhausted = (
        len(births) >= cfg.enrol_limit * 2 and len(eligible) < cfg.enrol_limit
    )
    if page_exhausted:
        rejections[REJECT_PAGE_EXHAUSTED] = len(births) - len(eligible)
    if count_anchorless:
        # A birth with NULL `first_evidence_at` cannot appear in the indexed
        # range above at all, so the scheduled pass excludes it in SQL and it
        # never reaches the histogram. That is the right shape for an hourly
        # job — the alternative predicate is non-sargable and costs a full
        # birth-table scan every pass — but a SILENT exclusion is exactly what
        # this project keeps paying for, so the operator-facing DRY RUN counts
        # them with one bounded probe.
        anchorless = session.execute(
            select(func.count()).select_from(
                select(CryptoTokenBirthEvent.id).where(
                    CryptoTokenBirthEvent.chain == cfg.chain,
                    CryptoTokenBirthEvent.first_evidence_at.is_(None),
                    CryptoTokenBirthEvent.observed_at >= cutoff,
                    CryptoTokenBirthEvent.observed_at <= now,
                ).limit(cfg.enrol_limit * 2).subquery()
            )
        ).scalar_one()
        if anchorless:
            rejections[REJECT_NO_ANCHOR_TIMESTAMP] = (
                rejections.get(REJECT_NO_ANCHOR_TIMESTAMP, 0) + anchorless
            )
    return eligible[: cfg.enrol_limit], rejections, len(births)


def _enrol(
    session: Session, cfg: SparseObservationConfig, cohort, births: list,
    now: datetime, progress: dict | None = None,
) -> int:
    """Insert members in bounded batches. The unique index on
    (cohort_id, token_address) is what makes a re-run — or a restart mid-pass —
    incapable of double-enrolling; this function adds no bookkeeping of its
    own.

    `progress["enrolled"]` is advanced only after a batch's commit RETURNS, so
    a caller that catches a lock or a race mid-enrolment reports what is
    durable rather than what was staged.

    NO RETRY LADDER HERE, deliberately (resolved once B4's `_commit_with_retry`
    landed). Enrolment is idempotent by unique index and costs nothing to
    repeat: a lock refuses the pass, the already-committed batches stay, and
    the NEXT scheduled pass re-selects exactly the births that still have no
    member row. Retrying inline would extend a pass's hold on a contended
    database to buy something the cadence already provides for free. The WRITE
    phase is different — it has already spent real provider requests, so
    abandoning it wastes them, which is why the ladder lives there."""
    progress = progress if progress is not None else {}
    progress.setdefault("enrolled", 0)
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
        pending += 1
        if pending >= cfg.write_batch_size:
            session.commit()
            progress["enrolled"] += pending
            pending = 0
    if pending or progress["enrolled"] == 0:
        # the trailing partial batch — and, when there is nothing to enrol at
        # all, the commit that makes a freshly created cohort durable
        session.commit()
        progress["enrolled"] += pending
    return progress["enrolled"]


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
    retry storm is possible.

    CRYPTO-COVERAGE-REPAIR-002 (Gate 1) puts `identity_mismatch` on the SAME
    side, and the reason is the design's own definition of an answer. Terminal
    means "the provider was asked and it TOLD US SOMETHING ABOUT THIS TOKEN".
    A response that named only OTHER tokens told us nothing about this one; it
    is not a quieter `provider_no_pair`, it is a response to a question we did
    not ask. Probe 15 already settled the identical shape one level coarser —
    a non-empty payload yielding zero pairs FOR THIS CHAIN is a failed request,
    not an honest empty answer — and identity is that same rule at token
    granularity, so classifying it terminal would leave the two inconsistent.
    It is also indistinguishable from upstream field drift (the provider
    starting to put a pool or quote address in `baseToken.address`), which is a
    contract violation and exactly the correlated, whole-fleet failure the
    cause-based rule exists to keep re-plannable.

    RETRYABLE IS THE CONSERVATIVE CHOICE HERE, not the expensive one, and it
    costs nothing new: the cap is per (token, horizon) and counts ATTEMPTS
    regardless of cause, so a permanently mismatching token costs exactly one
    extra request before it is terminal forever — the same bound
    `request_failed` already carries, and the same 40-request ceiling for a
    20-token correlated outage. The band closing still ends it unconditionally:
    the planner, not this predicate, decides what is still due."""
    return (
        status in (OBS_REQUEST_FAILED, OBS_IDENTITY_MISMATCH)
        and attempts < SPARSE_MAX_ATTEMPTS
    )


WORKING_SET_INDEX = "ix_horizon_member_cohort_added_at"


def _working_set_index_present(session: Session) -> bool | None:
    """Is migration 0029's composite index actually on this database?

    CRYPTO-COVERAGE-REPAIR-002 (B5). The index is load-bearing — `_sparse_plan`
    measured 416 ms cold without it against 25 ms with it — and its absence is
    otherwise SILENT here. `crypto-sparse-observe` deliberately does not call
    `ensure_schema_current` (the same precedent as `crypto-tape-reconcile`), so
    it is the one command that runs happily against an un-migrated DB, on the
    slow path. The plan-assertion test cannot catch it either: it runs against a
    `create_all` schema, which always has the index.

    This project's own history is the argument. A missing `ANALYZE` stayed
    invisible for six sessions and cost 9.9x, because nothing in any pass's
    output said whether the statistics existed. One cheap query per pass, on a
    receipt the operator already reads, is what that costs to never repeat.

    Returns None when the question is not answerable (a non-SQLite backend, or
    the catalogue read itself failing) — never a false 'present'.
    """
    try:
        # Dialect-gated on purpose: `sqlite_master` does not exist elsewhere,
        # and a failed statement would poison the session's transaction for the
        # enrolment that follows. A probe must never be able to break a pass.
        if session.get_bind().dialect.name != "sqlite":
            return None
        row = session.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'index' AND name = :name"
            ),
            {"name": WORKING_SET_INDEX},
        ).first()
    except Exception as exc:  # pragma: no cover - catalogue read failure
        logger.debug("working-set index probe failed: %s", exc)
        return None
    return row is not None


def _sparse_plan(
    session: Session, cfg: SparseObservationConfig, cohort, now: datetime,
    extra_members: list | None = None,
) -> tuple[list[HorizonPlanEntry], dict[tuple[str, str], int]]:
    """The sparse plan: the SHARED pure planner restricted to this lane's
    horizons and band, with this lane's CAUSE-BASED terminality applied on top.

    Returns `(entries, retryable_ids)`, where `retryable_ids` maps
    (token_address, horizon) to the id of the non-terminal (`request_failed` or
    `identity_mismatch`) observation row that a re-attempt must UPDATE IN PLACE
    rather than insert beside (the
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
        # The cohort_id predicate alone has NO selectivity — there is exactly
        # one rolling cohort, so every member row matches it and SQLite chose a
        # bare `SCAN crypto_horizon_cohort_members` (measured at 193k members
        # with sqlite_stat1 present). The `coalesce(...)` anchor predicate is
        # non-sargable and could not rescue it.
        #
        # `member_cutoff <= added_at <= now` is IMPLIED, never a new filter: a
        # member is enrolled at `added_at = <that pass's now>` and eligibility
        # requires `anchor <= now`, so `anchor <= added_at <= now` always
        # holds and every row passing the anchor predicate passes this one.
        # What it adds is a two-sided sargable range that
        # `ix_horizon_member_cohort_added_at` (migration 0029) can drive.
        # Measured with EXPLAIN QUERY PLAN at 60k members after ANALYZE: the
        # ONE-SIDED form still plans as `SCAN crypto_horizon_cohort_members`
        # (SQLite's default selectivity guess for an open-ended range is 1/4 of
        # the table, which loses to a scan), the two-sided form plans as
        # `SEARCH ... USING INDEX ix_horizon_member_cohort_added_at`. Ordering
        # by (added_at, id) rather than id alone keeps the index's own order,
        # so there is no temp B-tree either.
        member_cutoff = now - timedelta(minutes=ENROL_WINDOW_MINUTES)
        anchor_col = func.coalesce(
            CryptoHorizonCohortMember.first_evidence_at,
            CryptoHorizonCohortMember.birth_observed_at,
        )
        persisted = list(session.execute(
            select(CryptoHorizonCohortMember)
            .where(
                CryptoHorizonCohortMember.cohort_id == cohort.id,
                CryptoHorizonCohortMember.chain == cfg.chain,
                CryptoHorizonCohortMember.added_at >= member_cutoff,
                CryptoHorizonCohortMember.added_at <= now,
                anchor_col >= member_cutoff,
            )
            .order_by(
                CryptoHorizonCohortMember.added_at,
                CryptoHorizonCohortMember.id,
            )
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

# CRYPTO-COVERAGE-REPAIR-002 (B6): the CLI's DEFAULT window, in hours.
#
# The unbounded form is not safe as a default. Measured at year 1 on a 754MB
# fixture, `--hours None` stalls a co-tenant writer for 3.0-3.7s — one dense
# column scan of the whole observation table. Three runs at a 2s busy timeout
# produced no hard failure, but that is sampling luck on a 754MB file: EVO's is
# 4.55GB, with 1.01x-5.80x load overshoot documented in this repo. `--hours 48`
# measures 0.6-1.2s with a 506ms stall.
#
# 168h = 7 days: >= MIN_REPORT_HOURS by a wide margin (so no denominator is
# structurally nulled), long enough that a full 24h band plus a week of passes
# is in view, and short enough to stay in the bounded regime. Full history is
# still available and still correct — it is now `--all`, an explicit choice
# rather than what an operator gets by typing the command's name.
DEFAULT_REPORT_HOURS = 168


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
                f"no member can satisfy both. {MIN_REPORT_HOURS} is the "
                "STRUCTURAL MINIMUM, not a safe value: it admits only members "
                "enrolled in the last "
                f"{MIN_REPORT_HOURS} hours, and a member enrolled N hours ago "
                f"needs --hours >= {MIN_REPORT_HOURS} + N to appear at all. Use "
                f"the default ({DEFAULT_REPORT_HOURS}) unless you know the "
                "window you want, or --all for every enrolled member."
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
    ).where(
        CryptoHorizonCohortMember.cohort_id == cohort.id,
        # The `chain` predicate matches `_sparse_plan` and the write-phase load,
        # both of which gained one. It is redundant with `cohort_id` — a cohort
        # is single-chain by construction — but the report is the surface an
        # operator trusts, and a cohort that ever acquired a foreign-chain row
        # should not be able to enter this denominator silently.
        CryptoHorizonCohortMember.chain == cfg.chain,
    )
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
                    CryptoHorizonCohortMember.chain == cfg.chain,
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
    # NAMED FOR WHAT IT IS (LOW). This was `latest_pass_at`, which it is not:
    # it is derived from MAX(id) over rows this lane WROTE, so a perfectly
    # healthy pass with nothing to enrol and nothing due does not advance it and
    # false-warns `cadence_warning: True`. A real pass heartbeat needs a run
    # table and a migration decision this round did not take; until it does, the
    # honest fix is the honest name — `latest_write_at`, with the warning
    # documented as "no WRITE in 1.5 cadences", which is a weaker claim than
    # "the timer stopped" and must be read as one.
    latest_write_at = max(
        [t for t in (latest_observation_at, latest_enrolment_at) if t is not None],
        default=None,
    )
    write_age_minutes = (
        round((now - latest_write_at).total_seconds() / 60.0, 1)
        if latest_write_at is not None else None
    )
    cadence_warning = (
        write_age_minutes is not None
        and write_age_minutes > 1.5 * SPARSE_CADENCE_MINUTES
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
            "latest_write_at": (
                latest_write_at.isoformat() if latest_write_at else None
            ),
            "latest_observation_at": (
                latest_observation_at.isoformat() if latest_observation_at else None
            ),
            "latest_enrolment_at": (
                latest_enrolment_at.isoformat() if latest_enrolment_at else None
            ),
            "previous_write_age_minutes": write_age_minutes,
            "cadence_minutes": SPARSE_CADENCE_MINUTES,
            "cadence_warning": cadence_warning,
            "cadence_warning_means": (
                "no WRITE from this lane in 1.5 cadences. It is a write proxy, "
                "NOT a pass heartbeat: a healthy pass with nothing to enrol and "
                "nothing due writes nothing and trips this. A real heartbeat "
                "needs a run table; there is none yet."
            ),
            "expected_timer_oncalendar": timer_oncalendar(),
        },
        "enrolment_lag_seconds": {
            "p50": pctile(enrolment_lags, 0.5),
            "p90": pctile(enrolment_lags, 0.9),
            # LOW. `never_had_a_chance_rate` DILUTES — 20 late enrolments among
            # 200 pending reads as 0.1 — and `p90` misses a 10% tail entirely,
            # so a run where every tenth member was enrolled after its band
            # closed looks clean at p90 while `max` screams. `p99` and an
            # explicit COUNT over the band half-width are the two numbers that
            # cannot be diluted by a large healthy denominator.
            "p99": pctile(enrolment_lags, 0.99),
            "max": (round(enrolment_lags[-1], 1) if enrolment_lags else None),
            "over_band_count": sum(
                1 for lag in enrolment_lags if lag > SPARSE_BAND_MINUTES * 60
            ),
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
