"""CRYPTO-COVERAGE-REPAIR-002 tests — prospective sparse observation.

Every test here is written to FAIL ON REVERT of the behaviour it names; the
mutation used to prove each one is recorded in
`docs/milestones/CRYPTO-COVERAGE-REPAIR-002.md`. In-memory SQLite, a fake
adapter, no network anywhere.

Grouped by the property under test:

  * the schedule invariants (band containment, missed-pass tolerance)
  * eligibility (why a birth is or is not enrolled)
  * the gate, dry run and typed refusals
  * ONE attempt per (token, horizon) — no retries, no backfill
  * idempotency and restart survival
  * transaction shape: no write lock is held across network I/O
  * DexScreener only — SolanaTracker structurally unreachable
  * observation coverage vs reconciliation coverage: two distinct surfaces
  * the standing rolling cohort is not armable
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import (
    CryptoHorizonCohort,
    CryptoHorizonCohortMember,
    CryptoHorizonObservation,
    CryptoPriceTick,
    CryptoTokenBirthEvent,
    CryptoTokenSurvivalOutcome,
    MarketOpsRun,
)
from app.services import crypto_sparse_observation as sparse
from app.services.crypto_horizon import (
    MEMBERSHIP_ROLLING,
    OBS_OBSERVED,
    OBS_PROVIDER_NO_PAIR,
    OBSERVE_MAX_CALLS,
    CryptoHorizonService,
    is_rolling_cohort,
    plan_observations,
)
from app.services.crypto_tape import HORIZON_TOLERANCE, HORIZONS

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
CHAIN = "solana"


# --- fixtures / fakes -----------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


class FakeAdapter:
    """Canned pairs per token. Counts calls and records the exact call order so
    the fetch/write phase separation can be asserted against a real event log."""

    source_name = "dexscreener"

    def __init__(self, pairs_by_token=None, raise_for=None, events=None):
        self.pairs_by_token = pairs_by_token or {}
        self.raise_for = raise_for or set()
        self.calls = 0
        self.fetched: list[str] = []
        self.events = events

    async def fetch_pairs_for_token(self, token_address):
        self.calls += 1
        self.fetched.append(token_address)
        if self.events is not None:
            self.events.append(("fetch", token_address))
        if token_address in self.raise_for:
            raise RuntimeError("boom")
        return list(self.pairs_by_token.get(token_address, []))


class SolanaTrackerProbeAdapter(FakeAdapter):
    """A DexScreener stand-in that ALSO tries to reach SolanaTracker, exactly
    as a future careless edit would. It must be denied before any request."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.solana_tracker_error = None

    async def fetch_pairs_for_token(self, token_address):
        from app.services.crypto_provider_policy import (
            Provider,
            guard_provider_request,
        )

        try:
            await guard_provider_request(Provider.SOLANA_TRACKER)
            self.solana_tracker_error = "NOT DENIED"
        except Exception as exc:
            self.solana_tracker_error = type(exc).__name__
        return await super().fetch_pairs_for_token(token_address)


def pair(token, *, price=0.001, liq=10_000.0, address="PairA"):
    from app.adapters.dexscreener import PairData

    return PairData(
        chain=CHAIN, pair_address=address, base_token_address=token,
        base_token_symbol="TKN", quote_token_address="So11111111111111111111111111111111111111112",
        dex_id="raydium", price_usd=price, liquidity_usd=liq,
        volume_5m_usd=10.0, volume_1h_usd=100.0, volume_24h_usd=1000.0,
        market_cap=50_000.0, fdv=50_000.0,
        raw={"liquidity": {"usd": liq}, "txns": {"m5": {"buys": 3, "sells": 1}}},
    )


def token_id(n: int) -> str:
    """A canonical-shaped Solana base58 address, deterministic per n."""
    return f"So{n:04d}" + "T" * 34


def add_birth(
    session, n=1, *, anchor, complete=True, liq=5_000.0, chain=CHAIN,
):
    birth = CryptoTokenBirthEvent(
        chain=chain, token_address=token_id(n), symbol=f"T{n}",
        observed_at=anchor, first_evidence_at=anchor,
        launch_source="dexscreener:profile",
        first_pair_address=("Pair%04d" % n) if complete else None,
        first_dex_id="raydium",
        initial_price_usd=0.001 if complete else None,
        initial_liquidity_usd=(liq if complete else None),
        created_at=anchor,
    )
    session.add(birth)
    session.flush()
    return birth


def settings(**over) -> Settings:
    base = dict(
        database_url="sqlite://",
        crypto_chain=CHAIN,
        enable_crypto_sparse_observation=True,
    )
    base.update(over)
    return Settings(**base)


def config(**over) -> sparse.SparseObservationConfig:
    """A generous default deadline so only the deadline test exercises it;
    `max_duration_seconds=0.0` means "already past due" (the same sentinel the
    tape reconciler uses) and stops the fetch loop after exactly one token."""
    base = dict(chain=CHAIN, write_batch_size=25, max_duration_seconds=600.0)
    base.update(over)
    return sparse.SparseObservationConfig(**base)


async def run_pass(session, *, adapter=None, now=NOW, s=None, cfg=None, **kw):
    s = s or settings()
    service = CryptoHorizonService(adapter=adapter or FakeAdapter(), settings=s)
    return await sparse.run_scheduled_sparse_observation(
        session, settings=s, service=service,
        config=cfg or config(**{"chain": s.crypto_chain}), now=now,
        sleeper=lambda _s: None, **kw,
    )


def row_counts(session) -> dict:
    return {
        "cohorts": session.query(CryptoHorizonCohort).count(),
        "members": session.query(CryptoHorizonCohortMember).count(),
        "observations": session.query(CryptoHorizonObservation).count(),
        "ticks": session.query(CryptoPriceTick).count(),
    }


# --- 1. schedule invariants -----------------------------------------------------


def test_band_is_contained_in_the_tape_tolerance_at_every_sparse_horizon():
    """A tick written inside the sparse band must ALWAYS be inside
    `compute_survival`'s own tolerance window, or this lane buys observations
    that can never mature the label they were bought for."""
    for label, minutes in sparse.SPARSE_HORIZONS:
        tape_tolerance = minutes * HORIZON_TOLERANCE
        assert sparse.SPARSE_BAND_MINUTES <= tape_tolerance, label
    # and the margin is real, not marginal
    assert sparse.SPARSE_BAND_MINUTES * 3 <= min(
        m * HORIZON_TOLERANCE for _l, m in sparse.SPARSE_HORIZONS
    )


def test_at_least_two_scheduled_passes_fall_inside_every_band():
    """Missed-pass tolerance: a closed interval of length 2*BAND contains at
    least floor(2*BAND/CADENCE) points of a CADENCE-spaced lattice. Fewer than
    2 means one missed pass silently loses the horizon."""
    passes = int((2 * sparse.SPARSE_BAND_MINUTES) // sparse.SPARSE_CADENCE_MINUTES)
    assert passes >= 2, passes
    # exhaustive check against a real lattice, not just the arithmetic
    for offset_tenths in range(0, int(sparse.SPARSE_CADENCE_MINUTES) * 10):
        offset = offset_tenths / 10
        lo = offset
        hi = offset + 2 * sparse.SPARSE_BAND_MINUTES
        hits = sum(
            1 for k in range(0, 200)
            if lo <= k * sparse.SPARSE_CADENCE_MINUTES <= hi
        )
        assert hits >= 2, (offset, hits)


def test_only_6h_and_24h_are_bought():
    """15m/1h production coverage is already 80.9%/81.1%; the cliff is 6h
    (16.8%) and 24h (4.6%). Buying the short horizons is spend against a full
    denominator."""
    assert sparse.SPARSE_HORIZON_LABELS == ("6h", "24h")
    assert set(sparse.SPARSE_HORIZONS) <= set(HORIZONS)


def test_shared_planner_default_behaviour_is_unchanged():
    """The two new `plan_observations` parameters must be inert by default —
    the OBS-001 manual lane still plans all four horizons at the fractional
    tape tolerance."""
    member = CryptoHorizonCohortMember(
        cohort_id=1, chain=CHAIN, token_address=token_id(1), symbol="T1",
        first_evidence_at=NOW - timedelta(hours=6), added_at=NOW,
    )
    default = plan_observations([member], {}, set(), NOW)
    assert [e.horizon for e in default] == [label for label, _m in HORIZONS]
    six = next(e for e in default if e.horizon == "6h")
    assert (six.window_end - six.target_at) == timedelta(minutes=360 * HORIZON_TOLERANCE)

    sparse_plan = plan_observations(
        [member], {}, set(), NOW,
        horizons=sparse.SPARSE_HORIZONS,
        window_minutes=sparse.SPARSE_BAND_MINUTES,
    )
    assert [e.horizon for e in sparse_plan] == ["6h", "24h"]
    six = next(e for e in sparse_plan if e.horizon == "6h")
    assert (six.window_end - six.target_at) == timedelta(
        minutes=sparse.SPARSE_BAND_MINUTES
    )


# --- 2. eligibility -------------------------------------------------------------


def test_incomplete_lifecycle_anchor_is_never_enrolled(session):
    """`compute_survival` gates EVERY horizon label on truthy
    `initial_liquidity_usd`, so a birth without one can never produce a label
    no matter how many observations are bought. Enrolling it is provider spend
    with a provably zero denominator gain."""
    good = add_birth(session, 1, anchor=NOW - timedelta(minutes=10))
    add_birth(session, 2, anchor=NOW - timedelta(minutes=10), complete=False)
    add_birth(session, 3, anchor=NOW - timedelta(minutes=10), liq=0.0)
    session.commit()

    assert sparse.enrolment_rejection_reason(good, NOW) is None
    cands, rejections, considered = sparse._enrolment_candidates(
        session, config(), None, NOW,
    )
    assert [b.token_address for b in cands] == [token_id(1)]
    assert rejections == {sparse.REJECT_INCOMPLETE_ANCHOR: 2}
    assert considered == 3


def test_a_birth_whose_last_band_has_closed_is_never_enrolled(session):
    """Enrolling a token with no reachable band could only ever manufacture
    scheduling misses, corrupting the observation denominator with tokens the
    lane never had a chance to observe."""
    add_birth(session, 1, anchor=NOW - timedelta(minutes=sparse.ENROL_WINDOW_MINUTES + 1))
    add_birth(session, 2, anchor=NOW - timedelta(minutes=sparse.ENROL_WINDOW_MINUTES - 1))
    session.commit()
    cands, _rej, _n = sparse._enrolment_candidates(session, config(), None, NOW)
    assert [b.token_address for b in cands] == [token_id(2)]


def test_a_birth_past_its_6h_band_is_still_enrolled_for_its_24h_band(session):
    """The 6h band closes at 7h; the 24h band stays reachable until 25h. Losing
    the 6h observation must not cost the 24h one."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=10))
    session.commit()
    cands, _rej, _n = sparse._enrolment_candidates(session, config(), None, NOW)
    assert [b.token_address for b in cands] == [token_id(1)]


def test_enrolment_is_oldest_anchor_first(session):
    """The birth closest to losing its remaining band is the one a bounded pass
    must not defer — the starvation lesson CRYPTO-COVERAGE-REPAIR-001 learned
    twice."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=1))
    add_birth(session, 2, anchor=NOW - timedelta(hours=20))
    add_birth(session, 3, anchor=NOW - timedelta(hours=10))
    session.commit()
    cands, _r, _n = sparse._enrolment_candidates(session, config(enrol_limit=2), None, NOW)
    assert [b.token_address for b in cands] == [token_id(2), token_id(3)]


def test_other_chains_are_never_enrolled(session):
    add_birth(session, 1, anchor=NOW - timedelta(hours=1), chain="ethereum")
    session.commit()
    cands, _r, _n = sparse._enrolment_candidates(session, config(), None, NOW)
    assert cands == []


def test_eligibility_applies_no_liquidity_or_quality_threshold(session):
    """Deliberately NOT a rule: anything that would make the observed
    population a SELECTED sample. A 1-dollar initial liquidity is eligible."""
    b = add_birth(session, 1, anchor=NOW - timedelta(minutes=5), liq=1.0)
    session.commit()
    assert sparse.enrolment_rejection_reason(b, NOW) is None


# --- 3. gate, dry run, typed refusals -------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_is_a_clean_no_op(session):
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    before = row_counts(session)
    adapter = FakeAdapter()
    r = await run_pass(
        session, adapter=adapter, s=settings(enable_crypto_sparse_observation=False),
    )
    assert r["status"] == sparse.STATUS_DISABLED
    assert r["flag"] == sparse.FLAG
    assert r["external_calls"] == 0 and adapter.calls == 0
    assert row_counts(session) == before


@pytest.mark.asyncio
async def test_dry_run_enrols_nothing_calls_nothing_and_writes_nothing(session):
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    add_birth(session, 2, anchor=NOW - timedelta(minutes=10))
    session.commit()
    before = row_counts(session)
    adapter = FakeAdapter({token_id(1): [pair(token_id(1))]})
    r = await run_pass(session, adapter=adapter, dry_run=True)
    assert r["status"] == sparse.STATUS_DRY_RUN
    assert r["external_calls"] == 0 and adapter.calls == 0
    assert r["would_create_cohort"] is True
    assert r["would_enrol"] == 2
    assert row_counts(session) == before
    assert r["persisted"] is False


@pytest.mark.asyncio
async def test_dry_run_reports_what_it_would_observe_for_an_existing_cohort(session):
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter(), s=settings())  # enrol + observe
    session.query(CryptoHorizonObservation).delete()
    session.commit()
    r = await run_pass(session, adapter=FakeAdapter(), dry_run=True)
    assert r["status"] == sparse.STATUS_DRY_RUN
    assert r["would_create_cohort"] is False
    assert r["due_observations"] == 1
    assert r["would_fetch_tokens"] == 1


@pytest.mark.asyncio
async def test_force_runs_one_pass_while_the_flag_is_off(session):
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    r = await run_pass(
        session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}),
        s=settings(enable_crypto_sparse_observation=False), force=True,
    )
    assert r["status"] == sparse.STATUS_OK
    assert r["gate_bypassed"] == "force"
    assert r["observations_recorded"] == 1


@pytest.mark.asyncio
async def test_a_degraded_marketops_run_aborts_before_any_write(session):
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.add(MarketOpsRun(status="error", started_at=NOW))
    session.commit()
    before = row_counts(session)
    adapter = FakeAdapter({token_id(1): [pair(token_id(1))]})
    r = await run_pass(session, adapter=adapter)
    assert r["status"] == sparse.STATUS_MARKETOPS_DEGRADED
    assert adapter.calls == 0
    assert row_counts(session) == before


@pytest.mark.asyncio
async def test_a_dry_run_is_exempt_from_the_marketops_health_abort(session):
    """A dry run adds no write pressure at all, so a degraded host is not a
    reason to refuse the operator the diagnosis."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.add(MarketOpsRun(status="error", started_at=NOW))
    session.commit()
    r = await run_pass(session, adapter=FakeAdapter(), dry_run=True)
    assert r["status"] == sparse.STATUS_DRY_RUN


@pytest.mark.parametrize(
    "over,expected",
    [
        ({"enrol_limit": 0}, "invalid_enrol_limit"),
        ({"observe_limit": 0}, "invalid_observe_limit"),
        ({"observe_limit": OBSERVE_MAX_CALLS + 1}, "invalid_observe_limit"),
        ({"write_batch_size": 0}, "invalid_write_batch_size"),
        ({"max_duration_seconds": -1.0}, "invalid_max_duration_seconds"),
    ],
)
@pytest.mark.asyncio
async def test_every_invalid_bound_is_refused_loudly(session, over, expected):
    """A bound that is silently coerced produces a green pass that does no
    work — the failure class CRYPTO-COVERAGE-REPAIR-001 spent four rounds
    removing."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    r = await run_pass(session, cfg=config(**over))
    assert r["status"] == expected
    assert r["status"] not in sparse.HEALTHY_STATUSES
    assert row_counts(session)["members"] == 0


@pytest.mark.asyncio
async def test_a_second_concurrent_pass_is_refused_by_the_overlap_lock(session):
    from app.services.crypto_tape import _resolve_lock_dir

    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    lock_dir = _resolve_lock_dir(settings())
    with sparse._sparse_overlap_lock(lock_dir, CHAIN) as held:
        assert held
        adapter = FakeAdapter()
        r = await run_pass(session, adapter=adapter)
    assert r["status"] == sparse.STATUS_SKIPPED_OVERLAP
    assert adapter.calls == 0
    assert row_counts(session)["members"] == 0


@pytest.mark.asyncio
async def test_two_rolling_cohorts_are_refused_never_guessed(session):
    for _ in range(2):
        session.add(CryptoHorizonCohort(
            chain=CHAIN, member_limit=0, window_hours=25, note="x",
            provenance={"membership": MEMBERSHIP_ROLLING}, created_at=NOW,
        ))
    session.commit()
    r = await run_pass(session)
    assert r["status"] == sparse.STATUS_AMBIGUOUS_COHORT


# --- 4. exactly ONE attempt per (token, horizon) --------------------------------


@pytest.mark.asyncio
async def test_one_birth_gets_exactly_one_6h_and_one_24h_observation(session):
    """The whole contract in one test: birth -> one 6h look -> one 24h look."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    adapter = FakeAdapter({token_id(1): [pair(token_id(1))]})
    r = await run_pass(session, adapter=adapter, now=NOW)
    assert r["observations_recorded"] == 1
    assert adapter.calls == 1

    # 24h later the second (and last) observation happens
    later = NOW + timedelta(hours=18)
    r2 = await run_pass(session, adapter=adapter, now=later)
    assert r2["observations_recorded"] == 1
    rows = session.query(CryptoHorizonObservation).all()
    assert sorted(o.horizon for o in rows) == ["24h", "6h"]
    assert adapter.calls == 2

    # and never again, at any later time
    r3 = await run_pass(session, adapter=adapter, now=later + timedelta(hours=6))
    assert r3["observations_recorded"] == 0
    assert adapter.calls == 2


@pytest.mark.asyncio
async def test_a_miss_is_terminal_and_is_never_retried(session):
    """One attempt each — not a window, not a retry storm. The manual lane
    retries a failed row in place; this lane must not, or a token with no
    provider pair would be re-fetched at every pass for the whole band."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    adapter = FakeAdapter({})  # provider returns no pairs
    r = await run_pass(session, adapter=adapter, now=NOW)
    assert r["outcome_counts"] == {OBS_PROVIDER_NO_PAIR: 1}
    assert adapter.calls == 1

    # still inside the same 6h band, one cadence later
    r2 = await run_pass(
        session, adapter=adapter,
        now=NOW + timedelta(minutes=sparse.SPARSE_CADENCE_MINUTES),
    )
    assert r2["observations_recorded"] == 0
    assert adapter.calls == 1, "a miss was retried — this lane buys ONE attempt"
    assert session.query(CryptoHorizonObservation).count() == 1


@pytest.mark.asyncio
async def test_a_band_that_closes_unobserved_is_never_backfilled(session):
    """Absent evidence stays absent. No interpolation, no nearest tick, no
    late observation stamped with an in-band timestamp."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    adapter = FakeAdapter({token_id(1): [pair(token_id(1))]})
    # first pass runs only after the 6h band has closed (6h + 60min)
    r = await run_pass(session, adapter=adapter, now=NOW + timedelta(minutes=61))
    assert r["due_observations"] == 0
    assert adapter.calls == 0
    assert session.query(CryptoHorizonObservation).count() == 0
    report = sparse.build_observation_coverage_report(
        session, settings=settings(), now=NOW + timedelta(minutes=61),
    )
    six = report["by_horizon"]["6h"]
    assert six["observed"] == 0
    # this member was enrolled AFTER its 6h band closed, so it is
    # `enrolled_after_band_closed`, not a scheduling failure — and either way
    # it is never observed and never backfilled, which is what this test is
    # about. (The scheduling_miss/enrolled_too_late split has its own two
    # tests below.)
    assert six[sparse.OBS_STATE_ENROLLED_TOO_LATE] == 1
    assert six["scheduling_miss"] == 0

    # and a LATER pass still refuses to fill it in
    adapter2 = FakeAdapter({token_id(1): [pair(token_id(1))]})
    r2 = await run_pass(session, adapter=adapter2, now=NOW + timedelta(minutes=120))
    assert r2["due_observations"] == 0
    assert adapter2.calls == 0
    assert session.query(CryptoHorizonObservation).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["no_pair_at_all", "pairs_but_none_eligible"])
async def test_no_tick_is_written_for_a_miss(session, shape):
    """Both miss shapes, because they take different branches: no pair from
    the provider at all, and pairs that exist but carry no usable liquidity.
    Neither may produce a tick — never a null-liquidity tick, never liquidity
    fabricated from FDV/market cap/volume."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    pairs = (
        {} if shape == "no_pair_at_all"
        else {token_id(1): [pair(token_id(1), liq=0.0)]}
    )
    r = await run_pass(session, adapter=FakeAdapter(pairs), now=NOW)
    assert r["ticks_written"] == 0
    assert session.query(CryptoPriceTick).count() == 0
    obs = session.query(CryptoHorizonObservation).one()
    assert obs.status != OBS_OBSERVED
    assert obs.tick_id is None
    assert obs.liquidity_usd is None


@pytest.mark.asyncio
async def test_an_observed_tick_lands_inside_the_tape_survival_window(session):
    """The point of the whole lane: the tick must be usable by
    `compute_survival`, not merely written."""
    anchor = NOW - timedelta(hours=6)
    add_birth(session, 1, anchor=anchor)
    session.commit()
    await run_pass(
        session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}), now=NOW,
    )
    tick = session.query(CryptoPriceTick).one()
    target = anchor + timedelta(minutes=360)
    tolerance = timedelta(minutes=360 * HORIZON_TOLERANCE)
    observed = tick.observed_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    assert abs(observed - target) <= tolerance
    assert tick.liquidity_usd == 10_000.0
    assert tick.raw_payload["source"] == sparse.TICK_SOURCE


# --- 5. idempotency and restart survival ----------------------------------------


@pytest.mark.asyncio
async def test_rerunning_the_pass_double_enrols_and_double_observes_nothing(session):
    for n in (1, 2, 3):
        add_birth(session, n, anchor=NOW - timedelta(hours=6))
    session.commit()
    pairs = {token_id(n): [pair(token_id(n))] for n in (1, 2, 3)}
    adapter = FakeAdapter(pairs)
    first = await run_pass(session, adapter=adapter, now=NOW)
    after_first = row_counts(session)
    second = await run_pass(session, adapter=adapter, now=NOW)
    assert first["status"] == sparse.STATUS_OK
    # The second pass must be HEALTHY, not merely harmless. Without this the
    # test cannot tell correct idempotency apart from an IntegrityError caught
    # and rolled back into `concurrent_write_conflict` — which also leaves the
    # row counts unchanged and `enrolled` at 0. (Proven: mutating away the
    # already-enrolled exclusion left the weaker version of this test green.)
    assert second["status"] == sparse.STATUS_OK, second.get("error")
    assert first["enrolled"] == 3 and second["enrolled"] == 0
    assert second["observations_recorded"] == 0
    assert second["due_observations"] == 0
    assert row_counts(session) == after_first
    assert adapter.calls == 3


@pytest.mark.asyncio
async def test_a_pass_killed_mid_cycle_converges_with_no_duplicates(session):
    """Restart safety, proven by actually killing a pass mid-write-phase: the
    committed batch survives, and the next pass completes exactly the rest."""
    for n in range(1, 6):
        add_birth(session, n, anchor=NOW - timedelta(hours=6))
    session.commit()
    pairs = {token_id(n): [pair(token_id(n))] for n in range(1, 6)}

    boom_after = {"n": 0}
    real_record = CryptoHorizonService._record_observation

    def exploding(self, *args, **kwargs):
        boom_after["n"] += 1
        if boom_after["n"] > 3:
            raise KeyboardInterrupt("SIGKILL stand-in, mid write phase")
        return real_record(self, *args, **kwargs)

    s = settings()
    service = CryptoHorizonService(adapter=FakeAdapter(pairs), settings=s)
    cfg = config(write_batch_size=2)
    CryptoHorizonService._record_observation = exploding
    try:
        with pytest.raises(KeyboardInterrupt):
            await sparse.run_scheduled_sparse_observation(
                session, settings=s, service=service, config=cfg, now=NOW,
                sleeper=lambda _s: None,
            )
    finally:
        CryptoHorizonService._record_observation = real_record
    session.rollback()
    committed = session.query(CryptoHorizonObservation).count()
    assert 0 < committed < 5

    adapter2 = FakeAdapter(pairs)
    r = await run_pass(session, adapter=adapter2, now=NOW, cfg=config())
    assert r["enrolled"] == 0
    assert session.query(CryptoHorizonObservation).count() == 5
    assert adapter2.calls == 5 - committed
    keys = [
        (o.token_address, o.horizon)
        for o in session.query(CryptoHorizonObservation).all()
    ]
    assert len(keys) == len(set(keys))


@pytest.mark.asyncio
async def test_the_observe_limit_defers_rather_than_dropping(session):
    for n in range(1, 6):
        add_birth(session, n, anchor=NOW - timedelta(hours=6))
    session.commit()
    pairs = {token_id(n): [pair(token_id(n))] for n in range(1, 6)}
    adapter = FakeAdapter(pairs)
    r = await run_pass(session, adapter=adapter, now=NOW, cfg=config(observe_limit=2))
    assert r["status"] == sparse.STATUS_PARTIAL
    assert r["stop_reason"] == sparse.STOP_OBSERVE_LIMIT
    assert r["observations_recorded"] == 2 and r["deferred_observations"] == 3
    # the deferred ones are still selectable while their band is open
    r2 = await run_pass(session, adapter=adapter, now=NOW, cfg=config())
    assert r2["observations_recorded"] == 3


@pytest.mark.asyncio
async def test_the_fetch_deadline_reports_partial_and_defers_the_rest(session):
    for n in range(1, 4):
        add_birth(session, n, anchor=NOW - timedelta(hours=6))
    session.commit()
    pairs = {token_id(n): [pair(token_id(n))] for n in range(1, 4)}
    adapter = FakeAdapter(pairs)
    r = await run_pass(
        session, adapter=adapter, now=NOW, cfg=config(max_duration_seconds=0.0),
    )
    assert r["status"] == sparse.STATUS_PARTIAL
    assert r["stop_reason"] == sparse.STOP_DEADLINE
    assert r["observations_recorded"] == 1
    assert r["deferred_observations"] == 2
    assert session.query(CryptoHorizonObservation).count() == 1


@pytest.mark.asyncio
async def test_the_pass_working_set_is_bounded_by_the_enrolment_window(session):
    """The standing cohort is ROLLING: at ~530 births/day it accrues ~193k
    members and ~387k observation rows per year. A pass that loaded all of them
    would get slower every day, forever — the unbounded-growth failure this
    project has already paid for once. A member's last band closes at
    anchor + 24h + BAND, so anything older must be excluded IN SQL, not
    filtered in Python after loading it."""
    from sqlalchemy import event as sa_event

    # one live member, plus a large aged-out tail that must never be loaded
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter({}), now=NOW)
    cohort = session.query(CryptoHorizonCohort).one()
    for n in range(100, 200):
        old_anchor = NOW - timedelta(days=30 + n % 7)
        session.add(CryptoHorizonCohortMember(
            cohort_id=cohort.id, chain=CHAIN, token_address=token_id(n),
            symbol=f"T{n}", first_evidence_at=old_anchor,
            birth_observed_at=old_anchor, added_at=old_anchor,
        ))
    session.commit()
    assert session.query(CryptoHorizonCohortMember).count() == 101

    loaded: list[int] = []

    @sa_event.listens_for(CryptoHorizonCohortMember, "load")
    def _seen(target, ctx):
        loaded.append(target.id)

    try:
        plan = sparse._sparse_plan(session, config(), cohort, NOW + timedelta(hours=1))
    finally:
        sa_event.remove(CryptoHorizonCohortMember, "load", _seen)

    assert len(loaded) == 1, f"loaded {len(loaded)} members; the query is unbounded"
    # the aged-out tail contributes nothing to the plan either
    assert {e.token_address for e in plan} == {token_id(1)}


@pytest.mark.asyncio
async def test_a_member_whose_bands_have_all_closed_is_never_replanned(session):
    """Convergence: once both bands are behind it, a member drops out of the
    working set permanently rather than being re-walked at every pass."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}), now=NOW)
    cohort = session.query(CryptoHorizonCohort).one()
    long_after = NOW + timedelta(days=3)
    assert sparse._sparse_plan(session, config(), cohort, long_after) == []
    adapter = FakeAdapter({token_id(1): [pair(token_id(1))]})
    r = await run_pass(session, adapter=adapter, now=long_after)
    assert r["due_observations"] == 0 and adapter.calls == 0


# --- 6. transaction shape -------------------------------------------------------


@pytest.mark.asyncio
async def test_no_write_transaction_is_held_across_network_io(session):
    """The central transaction-shape decision. `observe_once` interleaves
    session writes with `await fetch_pairs_for_token` and commits once at the
    end — at this lane's cadence and token count that would hold SQLite's write
    lock across tens of seconds of network I/O on a shared host, the exact
    single-transaction shape OPS-013 retired.

    Pinned against a real event log: no commit may occur between the first and
    last provider call, and no provider call may occur after the first
    observation write."""
    for n in range(1, 6):
        add_birth(session, n, anchor=NOW - timedelta(hours=6))
    session.commit()
    events: list = []
    pairs = {token_id(n): [pair(token_id(n))] for n in range(1, 6)}
    adapter = FakeAdapter(pairs, events=events)

    @event.listens_for(session, "after_flush")
    def _flush(sess, ctx):
        for obj in sess.new:
            if isinstance(obj, (CryptoHorizonObservation, CryptoPriceTick)):
                events.append(("write", type(obj).__name__))
                break

    @event.listens_for(session, "after_commit")
    def _commit(sess):
        events.append(("commit", None))

    try:
        await run_pass(session, adapter=adapter, now=NOW, cfg=config(write_batch_size=2))
    finally:
        event.remove(session, "after_flush", _flush)
        event.remove(session, "after_commit", _commit)

    kinds = [k for k, _ in events]
    first_fetch = kinds.index("fetch")
    last_fetch = len(kinds) - 1 - kinds[::-1].index("fetch")
    assert "commit" not in kinds[first_fetch:last_fetch], events
    assert "write" not in kinds[first_fetch:last_fetch], events
    first_write = kinds.index("write")
    assert "fetch" not in kinds[first_write:], events
    # and the write phase really did commit in bounded batches
    assert kinds[first_write:].count("commit") >= 2, events


@pytest.mark.asyncio
async def test_the_audit_payload_is_bounded_to_three_candidates(session):
    """RAW-PAYLOAD-STORAGE-001: raw payloads were 27% of the production DB with
    zero readers. This lane writes ~1,000 observation rows/day, so it must not
    inherit the manual lane's 12-candidate default."""
    import json

    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    many = [pair(token_id(1), address=f"Pair{i}", liq=1000.0 + i) for i in range(20)]
    await run_pass(session, adapter=FakeAdapter({token_id(1): many}), now=NOW)
    obs = session.query(CryptoHorizonObservation).one()
    assert len(obs.raw_payload["candidates"]) == sparse.AUDIT_CANDIDATE_LIMIT
    assert obs.raw_payload["candidate_count"] == 20
    # a fixture-derived size bound, not a host measurement: it exists to catch
    # an unbounded payload creeping back in, not to predict production bytes
    assert len(json.dumps(obs.raw_payload)) < 4096


# --- 7. DexScreener only --------------------------------------------------------


@pytest.mark.asyncio
async def test_solana_tracker_is_structurally_denied_from_this_path(session):
    """Not "nobody calls it" — the run-scoped policy DENIES it, so any request
    from anywhere inside the pass raises before a socket is opened."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    adapter = SolanaTrackerProbeAdapter({token_id(1): [pair(token_id(1))]})
    r = await run_pass(session, adapter=adapter, now=NOW)
    assert adapter.solana_tracker_error == "ProviderDeniedError"
    # the denial is accounted, not merely raised: zero paid requests, one
    # policy block. A ledger that showed a started/succeeded SolanaTracker
    # request would make `_fetch_phase` raise outright.
    assert r["solana_tracker_calls"] == 0
    assert r["denied_provider_attempts"] == {"solana-tracker": 1}
    ledger = r["provider_ledger"]
    for key in ("authorized", "started", "succeeded", "failed"):
        assert ledger["solana-tracker"][key] == 0, key
    # DexScreener is absent from the ledger only because the fake adapter does
    # not go through the real `_get` guard; the pass's own `external_calls` is
    # the count that matters here, and the loop cap — not the policy cap — is
    # this lane's primary request bound.
    assert r["external_calls"] == 1
    assert r["provider"] == "dexscreener"
    assert r["observations_recorded"] == 1


def test_the_policy_denies_every_provider_except_dexscreener():
    from app.services.crypto_provider_policy import (
        Authorization,
        Provider,
    )

    policy = sparse._dexscreener_only_policy("test-run", 10)
    assert policy.authorization(Provider.DEXSCREENER) is Authorization.ALLOWED
    for provider in Provider:
        if provider is Provider.DEXSCREENER:
            continue
        assert policy.authorization(provider) is Authorization.DENIED, provider
    assert policy.paid_confirmed == frozenset()


@pytest.mark.asyncio
async def test_a_provider_policy_violation_is_a_loud_typed_refusal(session):
    """If a future edit reaches a denied provider, the pass must fail non-zero
    — never degrade the denial into an ordinary provider miss."""
    from app.services.crypto_provider_policy import Provider, guard_provider_request

    class Violating(FakeAdapter):
        async def fetch_pairs_for_token(self, token_address):
            await guard_provider_request(Provider.SOLANA_TRACKER)
            return []

    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    r = await run_pass(session, adapter=Violating(), now=NOW)
    assert r["status"] == sparse.STATUS_PROVIDER_POLICY_VIOLATION
    assert r["status"] not in sparse.HEALTHY_STATUSES
    assert session.query(CryptoHorizonObservation).count() == 0


def _module_identifiers(path: str) -> set[str]:
    """Every NAME the module actually references — imports, attributes, calls.

    Deliberately AST-based, not a text grep: this module's own boundary
    docstrings say the words "SolanaTracker" and "wallets" (they are the
    documented, AGENTS.md-acceptable kind of hit), and a text grep that trips
    on prose is a test that gets weakened rather than fixed. Identifiers are
    the thing that can actually reach a paid provider."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(path).read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.update(node.name.split("."))
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((getattr(node, "module", None) or "").split("."))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


@pytest.mark.asyncio
async def test_the_real_adapter_reaches_only_dexscreener_urls(session):
    """End-to-end network-surface proof, with the REAL `DexScreenerAdapter`
    (not a fake) and `httpx` intercepted: every URL this pass requests must be
    a DexScreener token-pairs URL, and the pass must succeed — which also
    proves the deny-all-but-DexScreener policy still AUTHORIZES the one
    provider it needs (a policy that denied everything would be trivially
    "safe" and useless)."""
    import httpx

    from app.adapters.dexscreener import DexScreenerAdapter

    requested: list[str] = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [{
                "chainId": CHAIN,
                "pairAddress": "PairReal",
                "baseToken": {"address": token_id(1), "symbol": "T1"},
                "quoteToken": {"address": "So11111111111111111111111111111111111111112"},
                "dexId": "raydium",
                "priceUsd": "0.001",
                "liquidity": {"usd": 12345.0},
                "volume": {"h24": 100.0},
                "txns": {"m5": {"buys": 2, "sells": 1}},
            }]

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            requested.append(url)
            return FakeResponse()

    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    original = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    try:
        s = settings()
        service = CryptoHorizonService(
            adapter=DexScreenerAdapter(settings=s), settings=s,
        )
        r = await sparse.run_scheduled_sparse_observation(
            session, settings=s, service=service, config=config(), now=NOW,
            sleeper=lambda _x: None,
        )
    finally:
        httpx.AsyncClient = original

    assert r["status"] == sparse.STATUS_OK, r.get("error")
    assert r["observations_recorded"] == 1
    assert requested, "the real adapter issued no request at all"
    for url in requested:
        assert url.startswith("https://api.dexscreener.com/token-pairs/"), url
    # the DexScreener guard really ran: the ledger accounts a real request
    assert r["provider_ledger"]["dexscreener"]["succeeded"] == len(requested)
    assert r["solana_tracker_calls"] == 0
    assert r["denied_provider_attempts"] == {}


def test_the_module_references_no_paid_provider_identifier():
    names = {n.lower() for n in _module_identifiers(
        "app/services/crypto_sparse_observation.py"
    )}
    for banned in (
        "solana_tracker", "solanatracker", "solana_tracker_risk", "birdeye",
        "goplus", "crypto_risk_engine", "enable_crypto_risk_provider",
    ):
        assert banned not in names, banned


# --- 8. two coverage surfaces ---------------------------------------------------


def test_the_observation_report_names_its_denominator_and_disclaims_the_other(session):
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    r = sparse.build_observation_coverage_report(session, settings=settings(), now=NOW)
    assert r["kind"] == sparse.OBSERVATION_REPORT_KIND
    assert "OBSERVATION coverage" in r["this_report_measures"]
    assert "RECONCILIATION coverage" in r["this_report_does_not_measure"]
    assert "crypto-tape-coverage-report" in r["this_report_does_not_measure"]


@pytest.mark.asyncio
async def test_observation_and_reconciliation_reports_share_no_metric_name(session):
    """Two distinct surfaces, distinct names — it must be impossible to read
    one as the other. Conflating them is how production's real 4.57% 24h
    coverage stayed invisible for months."""
    from app.services.crypto_coverage import build_coverage_report

    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}))
    obs = sparse.build_observation_coverage_report(
        session, settings=settings(), now=NOW,
    )
    rec = build_coverage_report(session, hours=168)

    def metric_names(node, out):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out.add(k)
                metric_names(v, out)
        elif isinstance(node, list):
            for v in node:
                metric_names(v, out)
        return out

    obs_metrics = metric_names(obs, set())
    rec_metrics = metric_names(rec, set())
    # the words that would let a reader mistake one for the other
    assert "coverage_rate" not in obs_metrics
    assert not any(m.startswith("coverage_") for m in obs_metrics), obs_metrics
    assert not any(m.startswith("observation_") for m in rec_metrics), rec_metrics
    assert not any(m.startswith("look_") for m in rec_metrics), rec_metrics
    assert not any(m.startswith("scheduling_miss") for m in rec_metrics), rec_metrics
    for name in (
        "observation_attempt_rate", "observation_success_rate",
        "look_completion_rate", "scheduling_miss_rate",
    ):
        assert name in obs_metrics, name


@pytest.mark.asyncio
async def test_pending_bands_are_excluded_from_every_rate(session):
    """Counting a still-open band as a miss would be the same lie as counting
    an absent tick as an observation."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}))
    r = sparse.build_observation_coverage_report(
        session, settings=settings(), now=NOW,
    )
    six = r["by_horizon"]["6h"]
    day = r["by_horizon"]["24h"]
    assert six["observed"] == 1 and six["bands_closed"] == 1
    assert six["observation_attempt_rate"] == 1.0
    assert day["band_not_open_yet"] == 1
    assert day["bands_closed"] == 0
    assert day["observation_attempt_rate"] is None
    assert day["scheduling_miss_rate"] is None


@pytest.mark.asyncio
async def test_the_observation_report_reads_no_survival_label(session):
    """It answers "did we look?", never "could we score it?". A survival
    outcome row must not change a single number in it."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}))
    before = sparse.build_observation_coverage_report(
        session, settings=settings(), now=NOW,
    )
    birth = session.query(CryptoTokenBirthEvent).one()
    session.add(CryptoTokenSurvivalOutcome(
        birth_event_id=birth.id, chain=CHAIN, token_address=birth.token_address,
        survived_6h=True, survived_24h=False, final=True, computed_at=NOW,
    ))
    session.commit()
    after = sparse.build_observation_coverage_report(
        session, settings=settings(), now=NOW,
    )
    before.pop("generated_at"), after.pop("generated_at")
    assert before == after


@pytest.mark.asyncio
async def test_attempted_misses_are_separated_from_scheduling_misses(session):
    """"We looked and found nothing" and "we never looked" are different
    failures with different owners; one report must never merge them."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    add_birth(session, 2, anchor=NOW - timedelta(hours=6))
    session.commit()
    # token 1 is fetched and misses; token 2 is never reached (limit binds)
    adapter = FakeAdapter({})
    await run_pass(session, adapter=adapter, now=NOW, cfg=config(observe_limit=1))
    after_band_closed = NOW + timedelta(minutes=61)
    r = sparse.build_observation_coverage_report(
        session, settings=settings(), now=after_band_closed,
    )
    six = r["by_horizon"]["6h"]
    assert six["attempted_missed"] == 1
    assert six["scheduling_miss"] == 1
    assert six["bands_closed"] == 2
    assert six["observation_attempt_rate"] == 0.5
    assert six["observation_success_rate"] == 0.0
    assert six["miss_causes"] == {OBS_PROVIDER_NO_PAIR: 1}


@pytest.mark.asyncio
async def test_a_band_that_closed_before_enrolment_is_not_a_scheduling_miss(session):
    """Found by a real CLI smoke run, not by review. Eligibility deliberately
    admits a birth past its 6h band so its 24h band can still be caught — so
    that member's 6h band closed before the lane ever saw it. Counting it as a
    `scheduling_miss` would inflate the one number that is supposed to mean
    "the mechanism failed to look when it could have" with tokens that predate
    enrolment: exactly the denominator conflation this milestone exists to
    stop."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=24))  # 6h band closed 17h ago
    add_birth(session, 2, anchor=NOW - timedelta(hours=6))   # 6h band open now
    session.commit()
    # enrol both, observe only token 2 (token 1's 6h band is long closed)
    r = await run_pass(
        session, adapter=FakeAdapter({token_id(2): [pair(token_id(2))]}), now=NOW,
    )
    assert r["enrolled"] == 2
    later = NOW + timedelta(minutes=61)  # token 2's 6h band has now closed too
    rep = sparse.build_observation_coverage_report(
        session, settings=settings(), now=later,
    )
    six = rep["by_horizon"]["6h"]
    assert six[sparse.OBS_STATE_ENROLLED_TOO_LATE] == 1
    assert six["scheduling_miss"] == 0
    assert six["observed"] == 1
    # the never-had-a-chance member is OUT of the denominator entirely
    assert six["bands_closed"] == 1
    assert six["scheduling_miss_rate"] == 0.0
    assert six["look_completion_rate"] == 1.0


@pytest.mark.asyncio
async def test_a_band_that_closed_after_enrolment_IS_a_scheduling_miss(session):
    """The compensating half: the exclusion above must not swallow a real
    failure. Same member, enrolled while its band was open, never observed."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    # enrol, but the provider pass observes nothing (limit 0 is refused, so use
    # a cohort-only enrolment by making the token not yet due at enrol time)
    r = await run_pass(session, adapter=FakeAdapter({}), now=NOW)
    assert r["enrolled"] == 1
    session.query(CryptoHorizonObservation).delete()
    session.commit()
    rep = sparse.build_observation_coverage_report(
        session, settings=settings(), now=NOW + timedelta(minutes=61),
    )
    six = rep["by_horizon"]["6h"]
    assert six["scheduling_miss"] == 1
    assert six[sparse.OBS_STATE_ENROLLED_TOO_LATE] == 0
    assert six["bands_closed"] == 1
    assert six["scheduling_miss_rate"] == 1.0
    assert rep["scheduling_miss_examples"][0]["enrolled_at"] is not None


def test_the_report_is_compute_on_demand(session):
    before = row_counts(session)
    r = sparse.build_observation_coverage_report(session, settings=settings(), now=NOW)
    assert r["external_calls"] == 0 and r["persisted"] is False
    assert row_counts(session) == before


# --- 9. the standing cohort is not a canary -------------------------------------


@pytest.mark.asyncio
async def test_the_standing_cohort_is_marked_rolling(session):
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter())
    cohort = session.query(CryptoHorizonCohort).one()
    assert is_rolling_cohort(cohort)
    assert cohort.provenance["membership"] == MEMBERSHIP_ROLLING
    assert cohort.provenance["horizons"] == ["6h", "24h"]


@pytest.mark.asyncio
async def test_a_rolling_cohort_can_never_be_armed(session):
    """No canary. The orchestrator's arming plan assumes membership is FROZEN;
    a rolling cohort would produce a plan stale the moment it is written, and
    would re-enter the canary governance this lane exists to stay out of."""
    from app.services.crypto_horizon_orchestrator import build_arm_plan

    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter())
    cohort = session.query(CryptoHorizonCohort).one()
    plan = build_arm_plan(session, cohort.id, now=NOW)
    assert plan["status"] == "rolling_cohort_not_armable"
    assert plan["jobs"] == [] and plan["installed"] is False


def test_a_frozen_cohort_is_still_armable(session):
    """The compensating half: the refusal must not break the existing lane."""
    from app.services.crypto_horizon_orchestrator import build_arm_plan

    cohort = CryptoHorizonCohort(
        chain=CHAIN, member_limit=5, window_hours=48, note="frozen",
        provenance={"membership": "frozen"}, created_at=NOW,
    )
    session.add(cohort)
    session.flush()
    session.add(CryptoHorizonCohortMember(
        cohort_id=cohort.id, chain=CHAIN, token_address=token_id(1), symbol="T1",
        first_evidence_at=NOW - timedelta(minutes=5), added_at=NOW,
    ))
    session.commit()
    plan = build_arm_plan(session, cohort.id, now=NOW)
    assert plan["status"] != "rolling_cohort_not_armable"
    assert is_rolling_cohort(cohort) is False


def test_is_rolling_cohort_is_false_for_legacy_and_missing_cohorts():
    assert is_rolling_cohort(None) is False
    assert is_rolling_cohort(
        CryptoHorizonCohort(chain=CHAIN, member_limit=5, window_hours=48)
    ) is False


# --- 10. safety boundary --------------------------------------------------------


def test_no_forbidden_capability_identifier_in_the_new_surface():
    """The AGENTS.md safety grep, at IDENTIFIER level (the stricter check the
    frontier-eval safety audit uses). Boundary-statement prose naming
    `wallets`/`orders`/`execution` is the documented acceptable hit; an
    identifier is not."""
    import re

    banned = re.compile(
        r"expected_value|kelly|position_siz|paper_trad|place_order|submit_order|"
        r"create_order|wallet|recommended_side|trade_recommend|execute_trade",
        re.I,
    )
    names = _module_identifiers("app/services/crypto_sparse_observation.py")
    offenders = sorted(n for n in names if banned.search(n))
    assert offenders == [], offenders


def test_no_migration_was_required():
    """This lane reuses `crypto_horizon_cohorts` / `_cohort_members` /
    `_observations` unchanged. A new migration here would mean the reuse claim
    is false."""
    import pathlib

    versions = sorted(
        p.name for p in pathlib.Path("alembic/versions").glob("*.py")
    )
    assert not any("sparse" in v for v in versions), versions


def test_the_cli_commands_are_registered():
    from app import cli

    parser = cli.build_parser()
    names = set(parser._subparsers._group_actions[0].choices)
    assert "crypto-sparse-observe" in names
    assert "crypto-observation-coverage-report" in names
