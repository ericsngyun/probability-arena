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
  * GATE 1 — exact token identity: no tick from another token's pair
  * observation coverage vs reconciliation coverage: two distinct surfaces
  * the standing rolling cohort is not armable
"""

import time
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
    OBS_IDENTITY_MISMATCH,
    OBS_OBSERVED,
    OBS_PROVIDER_NO_PAIR,
    OBS_REQUEST_FAILED,
    OBS_TOKEN_INACTIVE,
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
    first_evidence=True,
):
    birth = CryptoTokenBirthEvent(
        chain=chain, token_address=token_id(n), symbol=f"T{n}",
        observed_at=anchor, first_evidence_at=(anchor if first_evidence else None),
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


WSOL = "So11111111111111111111111111111111111111112"
# A different, equally valid Solana token. Same chain, same shape, not ours.
FOREIGN_TOKEN = "So9999" + "F" * 34


def _foreign_pair_json(pair_address: str) -> dict:
    """A perfectly healthy DEX Screener pair entry for a token that is NOT the
    one we asked about: right chain, every required field, huge liquidity, real
    activity — and a price six orders of magnitude away from ours."""
    return {
        "chainId": CHAIN,
        "pairAddress": pair_address,
        "baseToken": {"address": FOREIGN_TOKEN, "symbol": "OTHER"},
        "quoteToken": {"address": WSOL},
        "dexId": "raydium",
        "priceUsd": "999.0",
        "liquidity": {"usd": 1_000_000.0},
        "volume": {"h24": 500_000.0},
        "txns": {"m5": {"buys": 400, "sells": 400}},
    }


class _TransportFailureClient:
    """An `httpx.AsyncClient` stand-in that reproduces each way the REAL
    DexScreener adapter loses a request. It never raises out of the adapter
    (`dexscreener.py` explicitly disclaims that) — it degrades to [], which is
    exactly why the empty list cannot be read as a provider answer.

    `mode="ok"` returns a healthy pair so the same harness can drive a
    recovered provider on a later pass."""

    mode = "429"
    requested: list[str] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        import httpx

        type(self).requested.append(url)
        request = httpx.Request("GET", url)
        if self.mode == "429":
            return httpx.Response(429, request=request)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if self.mode == "server_error":
            return httpx.Response(503, request=request)
        if self.mode == "bad_json":
            return httpx.Response(200, content=b"<html>nope", request=request)
        # B1 (SHAPE). An HTTP 200 that DECODES cleanly but is not the shape the
        # endpoint contracts for. `bad_json` above is caught by `response.json()`
        # raising; these are not — they used to be coerced away by the caller's
        # `if not isinstance(payload, list): return []`, so the ledger recorded
        # a SUCCESS and the row was written as a provider ANSWER.
        if self.mode == "200_dict_error":
            # what a JSON error object / an upstream schema change looks like
            return httpx.Response(
                200, request=request,
                json={"error": "rate limited", "code": 429},
            )
        if self.mode == "200_html_string":
            # a WAF/Cloudflare interstitial served as a JSON string body
            return httpx.Response(
                200, request=request, json="<html>Attention Required</html>",
            )
        # Probe 15 (CRYPTO-COVERAGE-REPAIR-002 round 2): `expect=list` validates
        # the CONTAINER, not the endpoint's CONTRACT. These are all genuine JSON
        # ARRAYS — `isinstance(payload, list)` is true for every one of them —
        # whose elements do not parse into a usable pair for this chain. The
        # trigger is an upstream field rename inside the array, arguably likelier
        # than the container-type change B1 already fixed.
        if self.mode == "200_list_of_strings":
            return httpx.Response(200, request=request, json=["nope", "still nope"])
        if self.mode == "200_list_of_ints":
            return httpx.Response(200, request=request, json=[1, 2, 3])
        if self.mode == "200_list_of_dicts_no_keys":
            return httpx.Response(
                200, request=request,
                json=[{"someRenamedField": "x"}, {"anotherOne": 2}],
            )
        if self.mode == "200_list_of_dicts_wrong_chain":
            return httpx.Response(200, request=request, json=[{
                "chainId": "ethereum",
                "pairAddress": "PairWrongChain",
                "baseToken": {"address": "0xabc", "symbol": "T"},
            }])
        if self.mode == "200_list_of_nulls":
            return httpx.Response(200, request=request, json=[None, None])
        if self.mode == "200_nested_error_envelope_list":
            # an error object, shaped like the array the endpoint is supposed
            # to return, so it still passes `isinstance(payload, list)`
            return httpx.Response(
                200, request=request,
                json=[{"error": "rate limited", "code": 429}],
            )
        # CONTROL: a genuinely empty array is an honest provider answer and
        # must stay terminal — it must NOT be swept up by the Probe 15 check.
        if self.mode == "200_empty_list":
            return httpx.Response(200, request=request, json=[])
        if self.mode == "ok":
            token = url.rsplit("/", 1)[-1]
            return httpx.Response(200, request=request, json=[{
                "chainId": CHAIN,
                "pairAddress": "PairReal",
                "baseToken": {"address": token, "symbol": "T"},
                "quoteToken": {
                    "address": "So11111111111111111111111111111111111111112",
                },
                "dexId": "raydium",
                "priceUsd": "0.001",
                "liquidity": {"usd": 12345.0},
                "volume": {"h24": 100.0},
                "txns": {"m5": {"buys": 2, "sells": 1}},
            }])
        # GATE 1 (CRYPTO-COVERAGE-REPAIR-002). Every mode below is a HEALTHY
        # provider response: HTTP 200, a genuine JSON array, right chain, every
        # required field present and well-formed. B1 does not fire, Probe 15
        # does not fire (except where noted), the pair parses, scores and is
        # selectable. The ONLY thing wrong is WHICH TOKEN it is about.
        if self.mode == "identity_wrong_token":
            return httpx.Response(200, request=request, json=[
                _foreign_pair_json("PairForeign"),
            ])
        if self.mode == "identity_mixture":
            token = url.rsplit("/", 1)[-1]
            # The foreign pair is deliberately built to WIN selection if the
            # gate is removed: `active_pair_quality_score` caps the liquidity
            # term at ~100 and pays only +25 for an exact base-token match, so
            # 1,000,000 of foreign liquidity against 1,000 of ours beats the
            # bonus by ~75 points. Reverting the gate therefore does not merely
            # admit the foreign pair, it SELECTS it — and its price is 999.0,
            # six orders of magnitude from the truth.
            return httpx.Response(200, request=request, json=[
                _foreign_pair_json("PairForeign"),
                {
                    "chainId": CHAIN,
                    "pairAddress": "PairMine",
                    "baseToken": {"address": token, "symbol": "T"},
                    "quoteToken": {"address": WSOL},
                    "dexId": "raydium",
                    "priceUsd": "0.001",
                    "liquidity": {"usd": 1_000.0},
                    "volume": {"h24": 10.0},
                    "txns": {"m5": {"buys": 2, "sells": 1}},
                },
            ])
        if self.mode == "identity_quote_only":
            # our token appears — as the QUOTE side. `priceUsd` is the FOREIGN
            # base asset's price, not ours.
            token = url.rsplit("/", 1)[-1]
            entry = _foreign_pair_json("PairQuoteSide")
            entry["quoteToken"] = {"address": token, "symbol": "T"}
            return httpx.Response(200, request=request, json=[entry])
        # UPSTREAM FIELD DRIFT: the identity field itself is gone, renamed or
        # null. These are NOT Gate 1's to catch — `_parse_pair` drops a pair
        # without `baseToken.address`, and Probe 15's outcome check then turns
        # "non-empty payload, zero usable pairs" into a failed REQUEST. Pinned
        # here so the boundary between the two mechanisms is asserted, not
        # assumed, and so drift can never silently start being read as identity.
        if self.mode in (
            "identity_field_absent", "identity_field_renamed",
            "identity_field_null",
        ):
            token = url.rsplit("/", 1)[-1]
            base = {
                "identity_field_absent": {"symbol": "T"},
                "identity_field_renamed": {"tokenAddress": token, "symbol": "T"},
                "identity_field_null": {"address": None, "symbol": "T"},
            }[self.mode]
            return httpx.Response(200, request=request, json=[{
                "chainId": CHAIN,
                "pairAddress": "PairDrift",
                "baseToken": base,
                "quoteToken": {"address": WSOL},
                "dexId": "raydium",
                "priceUsd": "0.001",
                "liquidity": {"usd": 12345.0},
                "volume": {"h24": 100.0},
                "txns": {"m5": {"buys": 2, "sells": 1}},
            }])
        raise AssertionError(self.mode)


async def run_real_pass(session, *, mode, now=NOW, cfg=None):
    """One pass driven through the REAL `DexScreenerAdapter` with httpx
    intercepted, so transport failures take the production code path."""
    import httpx

    from app.adapters.dexscreener import DexScreenerAdapter

    _TransportFailureClient.mode = mode
    _TransportFailureClient.requested = []
    original = httpx.AsyncClient
    httpx.AsyncClient = _TransportFailureClient
    try:
        s = settings()
        service = CryptoHorizonService(
            adapter=DexScreenerAdapter(settings=s), settings=s,
        )
        return await sparse.run_scheduled_sparse_observation(
            session, settings=s, service=service, config=cfg or config(), now=now,
            sleeper=lambda _x: None,
        )
    finally:
        httpx.AsyncClient = original


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


@pytest.mark.asyncio
async def test_a_birth_with_no_first_evidence_at_is_never_enrolled(session):
    """Eligibility used to anchor on `coalesce(first_evidence_at, observed_at)`
    while `CryptoLifecycleTapeRecorder.compute_survival` anchors STRICTLY on
    `first_evidence_at` and sets `provider_gap=True` the moment it is NULL.

    Measured before the fix: a birth with NULL `first_evidence_at` was enrolled
    and observed, then scored `survived_6h=None, provider_gap=True` — provider
    spend with a provably zero denominator gain, one field over from the rule
    that exists to prevent exactly that. The fallback was wrong on its own
    terms too: `observed_at` is the TAPE RUN time, not a birth time."""
    good = add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    anchorless = add_birth(
        session, 2, anchor=NOW - timedelta(hours=6), first_evidence=False,
    )
    session.commit()

    assert sparse.enrolment_rejection_reason(good, NOW) is None
    assert sparse.enrolment_rejection_reason(anchorless, NOW) == (
        sparse.REJECT_NO_ANCHOR_TIMESTAMP
    )
    adapter = FakeAdapter({token_id(n): [pair(token_id(n))] for n in (1, 2)})
    # the operator-facing dry run counts the exclusion rather than hiding it
    preview = await run_pass(session, adapter=FakeAdapter(), now=NOW, dry_run=True)
    assert preview["enrolment_rejections"] == {sparse.REJECT_NO_ANCHOR_TIMESTAMP: 1}
    assert preview["would_enrol"] == 1

    r = await run_pass(session, adapter=adapter, now=NOW)
    assert r["enrolled"] == 1
    assert adapter.fetched == [token_id(1)], "spend on a zero-denominator birth"
    members = session.query(CryptoHorizonCohortMember).all()
    assert [m.token_address for m in members] == [token_id(1)]


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
async def test_a_failed_request_is_re_attempted_while_the_band_is_still_open(session):
    """B2. A provider ANSWER is terminal; a failed REQUEST is not an answer.

    Measured before the fix: 6h band open, first fetch fails, the next pass 30
    minutes later made ZERO adapter calls against a healthy provider with 30
    minutes of band left — and it was CORRELATED, so one DexScreener
    rate-limit window burned every token in the pass permanently."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()

    first = await run_real_pass(session, mode="429", now=NOW)
    assert first["outcome_counts"] == {OBS_REQUEST_FAILED: 1}
    assert first["retryable_request_failures"] == 0  # nothing to retry yet

    # 30 minutes later the band is still open (it closes at anchor + 6h + 60m)
    second = await run_real_pass(
        session, mode="ok", now=NOW + timedelta(minutes=30),
    )
    assert second["retryable_request_failures"] == 1
    assert second["request_failures_reattempted"] == 1
    assert second["outcome_counts"] == {OBS_OBSERVED: 1}
    obs = session.query(CryptoHorizonObservation).one()
    assert obs.status == OBS_OBSERVED
    assert obs.horizon == "6h"
    assert session.query(CryptoPriceTick).count() == 1


@pytest.mark.asyncio
async def test_a_re_attempt_is_hard_capped_at_two_per_token_horizon(session):
    """The retry must be bounded, not a window. Two attempts per (token,
    horizon) — 4 provider requests per birth, ever — and the third pass inside
    the same open band makes no call at all."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()

    await run_real_pass(session, mode="429", now=NOW)
    second = await run_real_pass(session, mode="timeout", now=NOW + timedelta(minutes=20))
    assert second["request_failures_reattempted"] == 1
    assert second["external_calls"] == 1
    obs = session.query(CryptoHorizonObservation).one()
    assert obs.status == OBS_REQUEST_FAILED
    assert obs.raw_payload[sparse.ATTEMPTS_KEY] == sparse.SPARSE_MAX_ATTEMPTS

    # third pass, band STILL open, provider healthy — the cap holds
    third = await run_real_pass(session, mode="ok", now=NOW + timedelta(minutes=40))
    assert third["external_calls"] == 0
    assert third["due_observations"] == 0
    assert session.query(CryptoHorizonObservation).one().status == OBS_REQUEST_FAILED


@pytest.mark.asyncio
async def test_a_failed_request_is_not_re_attempted_after_the_band_closes(session):
    """Retryability never extends the band. Once it has closed the miss is
    permanent, exactly like every other miss in this lane."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_real_pass(session, mode="429", now=NOW)
    # the 6h band closes at anchor + 6h + 60m == NOW + 60m
    after = await run_real_pass(session, mode="ok", now=NOW + timedelta(minutes=61))
    assert after["external_calls"] == 0
    assert after["due_observations"] == 0
    assert session.query(CryptoHorizonObservation).one().status == OBS_REQUEST_FAILED


@pytest.mark.asyncio
async def test_a_provider_answer_stays_terminal_even_with_the_band_open(session):
    """The other side of the cause split: `provider_no_pair` is an ANSWER and
    must stay one-shot, or a token the provider genuinely has nothing for gets
    re-fetched at every pass for the whole band."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    import httpx

    class EmptyAnswer(_TransportFailureClient):
        async def get(self, url):
            type(self).requested.append(url)
            return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    from app.adapters.dexscreener import DexScreenerAdapter

    original = httpx.AsyncClient
    httpx.AsyncClient = EmptyAnswer
    try:
        s = settings()
        service = CryptoHorizonService(
            adapter=DexScreenerAdapter(settings=s), settings=s,
        )
        first = await sparse.run_scheduled_sparse_observation(
            session, settings=s, service=service, config=config(), now=NOW,
            sleeper=lambda _x: None,
        )
    finally:
        httpx.AsyncClient = original
    assert first["outcome_counts"] == {OBS_PROVIDER_NO_PAIR: 1}

    later = await run_real_pass(session, mode="ok", now=NOW + timedelta(minutes=30))
    assert later["external_calls"] == 0
    assert later["due_observations"] == 0
    assert session.query(CryptoHorizonObservation).one().status == OBS_PROVIDER_NO_PAIR


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
    """STAGGERED anchors, deliberately: the original fixture used five births
    at IDENTICAL anchors, so the selection ORDER was unobservable and the test
    could not exercise its own case."""
    for n in range(1, 6):
        add_birth(session, n, anchor=NOW - timedelta(hours=6, minutes=10 * n))
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
async def test_the_observe_limit_serves_the_soonest_deadline_first(session):
    """B5. `observe_limit` used to sort by the ABSOLUTE distance to the horizon
    target, which conflates "59 minutes of runway left" with "closes in 1
    minute" and therefore served the LEAST urgent member first. Measured: three
    births, `observe_limit=1` — the one with 1 minute of band left was deferred
    and permanently lost.

    Ordering must be earliest-deadline-first: the band that closes soonest is
    the one a bounded pass must not defer."""
    # anchors chosen so all three 6h bands are open at NOW but close 1, 31 and
    # 59 minutes from now respectively
    for n, minutes_left in ((1, 59), (2, 31), (3, 1)):
        add_birth(
            session, n,
            anchor=NOW - timedelta(hours=6, minutes=60 - minutes_left),
        )
    session.commit()
    pairs = {token_id(n): [pair(token_id(n))] for n in (1, 2, 3)}
    adapter = FakeAdapter(pairs)
    r = await run_pass(session, adapter=adapter, now=NOW, cfg=config(observe_limit=1))
    assert r["observations_recorded"] == 1
    assert adapter.fetched == [token_id(3)], (
        "the member with 1 minute of band left was deferred in favour of one "
        "with an hour of runway"
    )
    obs = session.query(CryptoHorizonObservation).one()
    assert obs.token_address == token_id(3)


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
        plan, _retryable = sparse._sparse_plan(
            session, config(), cohort, NOW + timedelta(hours=1),
        )
    finally:
        sa_event.remove(CryptoHorizonCohortMember, "load", _seen)

    assert len(loaded) == 1, f"loaded {len(loaded)} members; the query is unbounded"
    # the aged-out tail contributes nothing to the plan either
    assert {e.token_address for e in plan} == {token_id(1)}


def _explain(engine, session, run) -> list[tuple[str, list[str]]]:
    """Every SELECT `run()` issues, paired with its EXPLAIN QUERY PLAN.

    The plan is what matters, not the row count: a 101-row fixture with no
    `sqlite_stat1` produces a DIFFERENT plan than a realistic table with one,
    so a bound-test that pins ORM materialisation pins nothing about scan cost.
    """
    from sqlalchemy import event as sa_event

    captured: list[tuple[str, object]] = []

    @sa_event.listens_for(engine, "before_cursor_execute")
    def _cap(conn, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            captured.append((statement, params))

    try:
        run()
    finally:
        sa_event.remove(engine, "before_cursor_execute", _cap)

    out = []
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        for statement, params in captured:
            cursor.execute("EXPLAIN QUERY PLAN " + statement, params)
            out.append((statement, [row[3] for row in cursor.fetchall()]))
    finally:
        raw.close()
    return out


@pytest.fixture
def big_cohort():
    """A realistically sized rolling cohort WITH `sqlite_stat1` populated —
    without ANALYZE, SQLite plans from defaults and the assertions below are
    meaningless."""
    from sqlalchemy import text

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    cohort = CryptoHorizonCohort(
        chain=CHAIN, member_limit=0, window_hours=25,
        note=sparse.COHORT_NOTE,
        provenance={"membership": MEMBERSHIP_ROLLING},
        created_at=NOW,
    )
    s.add(cohort)
    s.flush()
    s.bulk_insert_mappings(CryptoHorizonCohortMember, [
        {
            "cohort_id": cohort.id, "chain": CHAIN,
            "token_address": f"M{i:012d}", "symbol": "x",
            "birth_event_id": None,
            "birth_observed_at": NOW - timedelta(days=60) + timedelta(minutes=i % 86000),
            "first_evidence_at": NOW - timedelta(days=60) + timedelta(minutes=i % 86000),
            "added_at": NOW - timedelta(days=60) + timedelta(minutes=i % 86000),
        }
        for i in range(20_000)
    ])
    s.bulk_insert_mappings(CryptoTokenBirthEvent, [
        {
            "chain": CHAIN, "token_address": f"B{i:012d}", "symbol": "x",
            "observed_at": NOW - timedelta(minutes=i % 1400),
            "first_evidence_at": NOW - timedelta(minutes=i % 1400),
            "launch_source": "dexscreener:profile",
            "first_pair_address": "p", "first_dex_id": "raydium",
            "initial_price_usd": 0.001, "initial_liquidity_usd": 5_000.0,
            "created_at": NOW - timedelta(minutes=i % 1400),
        }
        for i in range(4_000)
    ])
    s.commit()
    s.execute(text("ANALYZE"))
    s.commit()
    try:
        yield engine, s, cohort
    finally:
        s.close()


def test_the_pass_queries_never_scan_the_member_table(big_cohort):
    """B7. The pre-fetch phase was the real contention source: phase-attributed
    against a real competing writer at 193k members, the prelude was 11.8s of a
    14.3s pass with a 2,023ms competing-writer max and 2 hard lock failures,
    while the fetch phase's max was 28ms and the write phase's 45ms.

    From EXPLAIN QUERY PLAN at scale: `_enrolment_candidates`' `coalesce(...)`
    anchor was non-sargable so the birth index was unusable, then LIST SUBQUERY
    1 materialised ALL members for the `NOT IN`, then a temp B-tree sort;
    `_sparse_plan` was a bare `SCAN crypto_horizon_cohort_members`. The
    docstring's "this never walks the whole birth table" was false.

    This asserts the PLAN, not a row count."""
    engine, s, cohort = big_cohort
    cfg = sparse.SparseObservationConfig(chain=CHAIN)
    plans = _explain(
        engine, s,
        lambda: (
            sparse._enrolment_candidates(s, cfg, cohort, NOW),
            sparse._sparse_plan(s, cfg, cohort, NOW),
        ),
    )
    assert plans
    for statement, plan in plans:
        joined = " | ".join(plan)
        assert "SCAN crypto_horizon_cohort_members" not in joined, (
            f"{joined}\nfor:\n{statement}"
        )
        assert "SCAN crypto_token_birth_events" not in joined, (
            f"{joined}\nfor:\n{statement}"
        )
        assert "LIST SUBQUERY" not in joined, (
            "the member table is being materialised into a temporary table:\n"
            f"{joined}\nfor:\n{statement}"
        )
        assert "TEMP B-TREE" not in joined, f"{joined}\nfor:\n{statement}"

    flat = [line for _st, plan in plans for line in plan]
    assert any(
        "ix_horizon_member_cohort_added_at" in line for line in flat
    ), flat
    assert any(
        "ix_crypto_token_birth_events_first_evidence_at" in line for line in flat
    ), flat


@pytest.mark.asyncio
async def test_the_lane_never_files_its_own_band_edge_as_out_of_band(
    session, monkeypatch,
):
    """LOW (band edge). The plan is fixed at pass START; the tick is stamped
    after the FETCH. So a token planned with seconds of band left can be
    answered HONESTLY up to `max_duration_seconds` after its band closed —
    measured overshoot 1.21s — and the row was written with an `observed_at`
    outside its own band. The pass then reported `observed: 1` while the report
    reported `out_of_band: 1` for the same row.

    The interpretive cost is the point: `out_of_band_rate` is the governance
    signal for MANUAL-LANE CONTAMINATION, and a signal that also fires benignly
    from this lane's own clock cannot carry that meaning.

    Driven deterministically by advancing the logical clock past `window_end`
    between planning and staging, which is exactly what a slow fetch does."""
    # window_end = anchor + 6h + band; put it one second after `now`
    anchor = (
        NOW
        - timedelta(hours=6)
        - timedelta(minutes=sparse.SPARSE_BAND_MINUTES)
        + timedelta(seconds=1)
    )
    add_birth(session, 1, anchor=anchor)
    session.commit()

    real = sparse._logical_clock
    monkeypatch.setattr(
        sparse, "_logical_clock",
        lambda now, started: (lambda: now + timedelta(seconds=2)),
    )
    r = await run_pass(
        session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}), now=NOW,
    )
    monkeypatch.setattr(sparse, "_logical_clock", real)

    assert r["status"] in sparse.HEALTHY_STATUSES, r.get("error")
    assert r["due_observations"] == 1, "the 6h band was not planned as due"
    assert r["band_closed_during_pass"] == 1, (
        "the band closed between plan and fetch and the skip was not counted"
    )
    assert r["observations_recorded"] == 0
    assert session.query(CryptoHorizonObservation).count() == 0, (
        "an observation was written outside its own band by this lane's clock"
    )

    # and the report agrees with the pass rather than contradicting it: no
    # `out_of_band` row exists to dilute the contamination signal
    rep = sparse.build_observation_coverage_report(
        session, settings=settings(), now=NOW + timedelta(hours=48),
    )
    assert rep["by_horizon"]["6h"]["out_of_band"] == 0
    assert rep["by_horizon"]["6h"]["observed"] == 0


@pytest.mark.asyncio
async def test_the_pass_reports_whether_the_working_set_index_exists(session):
    """B5. The plan-assertion test above CANNOT catch a missing migration 0029:
    it runs against a `create_all` schema, which always has the index. And
    `crypto-sparse-observe` deliberately does not call `ensure_schema_current`
    (the `crypto-tape-reconcile` precedent), so it is the one command that runs
    happily against an un-migrated DB, on the slow path — measured 416 ms cold
    against 25 ms. Nothing said so. This project's own history is the argument:
    a missing `ANALYZE` stayed invisible for six sessions and cost 9.9x.

    The receipt is a receipt, not a refusal: the pass still works without the
    index, just slowly, so the value must appear and the pass must still pass."""
    from sqlalchemy import text

    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()

    r = await run_pass(
        session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}), now=NOW,
    )
    assert r["status"] in sparse.HEALTHY_STATUSES, r.get("error")
    assert r["working_set_index_present"] is True

    # and it is a real probe of THIS database, not a constant: drop the index
    # migration 0029 creates and the same pass must say so
    session.execute(text(f"DROP INDEX {sparse.WORKING_SET_INDEX}"))
    session.commit()
    add_birth(session, 2, anchor=NOW - timedelta(hours=6))
    session.commit()

    r2 = await run_pass(
        session, adapter=FakeAdapter({token_id(2): [pair(token_id(2))]}), now=NOW,
    )
    assert r2["working_set_index_present"] is False, (
        "a missing migration 0029 was reported as present"
    )
    assert r2["status"] in sparse.HEALTHY_STATUSES, (
        "the index is a receipt, not a gate — the pass must still work"
    )
    assert r2["observations_recorded"] == 1

    # the dry-run path reports it too — that is the path an operator runs FIRST
    dry = await run_pass(
        session, adapter=FakeAdapter(), now=NOW, dry_run=True,
    )
    assert dry["working_set_index_present"] is False


@pytest.mark.asyncio
async def test_a_member_whose_bands_have_all_closed_is_never_replanned(session):
    """Convergence: once both bands are behind it, a member drops out of the
    working set permanently rather than being re-walked at every pass."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}), now=NOW)
    cohort = session.query(CryptoHorizonCohort).one()
    long_after = NOW + timedelta(days=3)
    assert sparse._sparse_plan(session, config(), cohort, long_after) == ([], {})
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
async def test_a_lock_on_the_first_commit_restages_the_batch_instead_of_losing_it(
    session,
):
    """B4 (DATA LOSS). `session.rollback()` expunges every staged object, so a
    retry ladder that simply re-calls `session.commit()` commits an EMPTY
    transaction and returns successfully having written nothing — while the
    caller has already counted the rows. Measured before the fix: 3 staged
    observations, first commit raises `database is locked`, the pass returns
    `status=ok, observations_recorded=3` with ZERO rows in the database, after
    spending 3 real provider requests.

    The batch must be RE-STAGED after the rollback, and nothing may be counted
    until the commit has returned."""
    import sqlite3

    from sqlalchemy.exc import OperationalError

    for n in (1, 2, 3):
        add_birth(session, n, anchor=NOW - timedelta(hours=6))
    session.commit()
    pairs = {token_id(n): [pair(token_id(n))] for n in (1, 2, 3)}

    real_commit = session.commit
    failures = {"n": 0}

    def flaky_commit():
        # fail only the first WRITE-phase commit; enrolment commits (which
        # stage no observation) go through untouched
        staging_observations = any(
            isinstance(obj, CryptoHorizonObservation) for obj in session.new
        )
        if staging_observations and failures["n"] == 0:
            failures["n"] += 1
            raise OperationalError(
                "INSERT INTO crypto_horizon_observations", {},
                sqlite3.OperationalError("database is locked"),
            )
        return real_commit()

    session.commit = flaky_commit
    try:
        r = await run_pass(
            session, adapter=FakeAdapter(pairs), now=NOW, cfg=config(write_batch_size=25),
        )
    finally:
        del session.commit

    assert failures["n"] == 1, "the lock was never injected"
    assert r["status"] == sparse.STATUS_OK, r.get("error")
    persisted = session.query(CryptoHorizonObservation).count()
    assert persisted == 3, "the rolled-back batch was never re-staged"
    assert r["observations_recorded"] == persisted
    assert r["ticks_written"] == session.query(CryptoPriceTick).count() == 3
    assert r["batches_committed"] == 1


@pytest.mark.asyncio
async def test_nothing_is_counted_until_the_batch_commit_returns(session):
    """B4, the reporting half. A permanently locked database must produce
    `status=db_locked` with the counters reflecting what is actually durable —
    never a report of rows that were staged and discarded."""
    import sqlite3

    from sqlalchemy.exc import OperationalError

    for n in (1, 2, 3):
        add_birth(session, n, anchor=NOW - timedelta(hours=6))
    session.commit()
    pairs = {token_id(n): [pair(token_id(n))] for n in (1, 2, 3)}

    real_commit = session.commit

    def always_locked():
        if any(isinstance(obj, CryptoHorizonObservation) for obj in session.new):
            raise OperationalError(
                "INSERT INTO crypto_horizon_observations", {},
                sqlite3.OperationalError("database is locked"),
            )
        return real_commit()

    session.commit = always_locked
    try:
        r = await run_pass(
            session, adapter=FakeAdapter(pairs), now=NOW, cfg=config(write_batch_size=25),
        )
    finally:
        del session.commit

    assert r["status"] == sparse.STATUS_DB_LOCKED
    assert r["observations_recorded"] == 0
    assert r["ticks_written"] == 0
    assert r["batches_committed"] == 0
    assert session.query(CryptoHorizonObservation).count() == 0


def test_the_write_lock_property_rests_on_pysqlites_deferred_begin(tmp_path):
    """The transaction-shape test is a SMOKE CHECK, not the guarantee —
    injecting five `session.execute(select(...))` calls into the fetch phase
    was measured to leave all four of its assertions passing. The real
    guarantee is structural (`_fetch_phase` takes no session; the service holds
    none) PLUS pysqlite's DEFERRED implicit BEGIN: a read must not open a write
    transaction. Nothing in this repo owned that assumption.

    Asserted here at the `app/db.py` boundary, against a second connection that
    must still be able to write while the first holds an open read."""
    import inspect as _inspect
    import sqlite3

    from sqlalchemy import create_engine, text

    from app import db as app_db

    # 1. the structural half, stated as a fact about the signature
    params = _inspect.signature(sparse._fetch_phase).parameters
    assert "session" not in params, params
    service_attrs = vars(CryptoHorizonService(settings=settings()))
    assert not any("session" in name for name in service_attrs), service_attrs

    # 2. the pysqlite half, at the db.py boundary
    path = tmp_path / "begin.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args=app_db.connect_args_for(url))
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
            conn.execute(text("INSERT INTO t (v) VALUES ('a')"))

        reader = engine.connect()
        try:
            reader.execute(text("SELECT * FROM t")).all()
            # the read must NOT have taken a write lock: a second, independent
            # connection can still write
            writer = sqlite3.connect(str(path), timeout=0.2)
            try:
                writer.execute("INSERT INTO t (v) VALUES ('b')")
                writer.commit()
            finally:
                writer.close()
        finally:
            reader.close()
    finally:
        engine.dispose()

    assert app_db.connect_args_for(url).get("timeout"), (
        "SQLite must get a busy timeout; without it a competing writer fails "
        "immediately instead of waiting"
    )


@pytest.mark.asyncio
async def test_the_fetch_deadline_is_anchored_at_fetch_start(session):
    """`.env.example`, `config.py` and `docs/FEATURE_FLAGS.md` all describe
    `max_duration_seconds` as a budget on the FETCH phase, but it was anchored
    at PASS start — so the real fetch budget shrank by however long the prelude
    took, silently more as the cohort grew."""
    import inspect as _inspect

    for n in range(1, 4):
        add_birth(session, n, anchor=NOW - timedelta(hours=6, minutes=n))
    session.commit()

    source = _inspect.getsource(sparse._fetch_phase)
    assert "_now() + timedelta(seconds=max_duration_seconds)" in source
    assert "started" not in _inspect.signature(sparse._fetch_phase).parameters

    # a prelude that eats real wall clock must not eat the fetch budget
    pairs = {token_id(n): [pair(token_id(n))] for n in range(1, 4)}
    slow_prelude = sparse._enrolment_candidates

    def _slow(*args, **kwargs):
        time.sleep(0.35)
        return slow_prelude(*args, **kwargs)

    sparse._enrolment_candidates = _slow
    try:
        r = await run_pass(
            session, adapter=FakeAdapter(pairs), now=NOW,
            cfg=config(max_duration_seconds=0.3),
        )
    finally:
        sparse._enrolment_candidates = slow_prelude

    assert r["stop_reason"] == sparse.STOP_COMPLETE, (
        "the prelude consumed the fetch budget"
    )
    assert r["observations_recorded"] == 3


@pytest.mark.asyncio
async def test_a_page_full_of_ineligible_births_is_reported_not_hidden(session):
    """The enrolment page reads `enrol_limit * 2` rows and filters in Python,
    so a page dominated by ineligible births can starve eligible ones behind
    them — and the rejects are re-read at the head of every pass until they age
    out. That must be visible, not look like "nothing to enrol"."""
    for n in range(1, 9):
        add_birth(session, n, anchor=NOW - timedelta(hours=6), complete=False)
    add_birth(session, 9, anchor=NOW - timedelta(hours=6))
    session.commit()
    r = await run_pass(
        session, adapter=FakeAdapter({token_id(9): [pair(token_id(9))]}),
        now=NOW, cfg=config(enrol_limit=4),
    )
    # the eligible birth is BEYOND the page (8 ineligible ones fill it), so it
    # is genuinely starved this pass — which is precisely the condition the
    # marker exists to make visible instead of silent
    assert r["enrolment_rejections"][sparse.REJECT_PAGE_EXHAUSTED] == 8
    assert r["enrolled"] == 0
    assert r["births_considered"] == 8

    # a larger page reaches it, and then the marker is gone
    r2 = await run_pass(
        session, adapter=FakeAdapter({token_id(9): [pair(token_id(9))]}),
        now=NOW, cfg=config(enrol_limit=200),
    )
    assert sparse.REJECT_PAGE_EXHAUSTED not in r2["enrolment_rejections"]
    assert r2["enrolled"] == 1


@pytest.mark.asyncio
async def test_the_pass_reports_write_lock_instrumentation(session):
    """The reconciler persists lock-wait/write-hold and its timer is disarmed
    precisely because those are uncalibrated on EVO. This lane proposed an
    hourly unattended timer against the same file with `duration_ms` and
    nothing else."""
    for n in range(1, 5):
        add_birth(session, n, anchor=NOW - timedelta(hours=6, minutes=n))
    session.commit()
    pairs = {token_id(n): [pair(token_id(n))] for n in range(1, 5)}
    r = await run_pass(
        session, adapter=FakeAdapter(pairs), now=NOW, cfg=config(write_batch_size=2),
    )
    lock = r["write_lock"]
    assert lock["batches"] == 2
    assert lock["lock_failures"] == 0
    assert lock["retry_attempts"] == 0
    assert lock["write_hold_ms_max"] >= 0.0
    assert lock["commit_ms_max"] >= 0.0
    # GATE7-SPARSE-TELEMETRY-001: these are now persisted per pass to the
    # non-SQLite 001A JSONL sink, so the gate text this payload used to carry
    # ("NOT persisted to a run table — install no timer until it is") is no
    # longer true and no longer here. `persisted` is the ACTUAL outcome of this
    # pass's append, not a claim — see
    # test_a_failed_append_reports_persisted_false_rather_than_claiming_success.
    assert lock["persisted"] is True
    assert "run table" in lock["note"]
    assert "no timer is installed" in lock["note"]


@pytest.mark.asyncio
async def test_the_audit_payload_stores_no_per_candidate_blob(session):
    """RAW-PAYLOAD-STORAGE-001 made and REVERSED this exact decision six days
    before this lane was written. Keeping 3 per-candidate diagnostics was
    measured at 424 MB/year of a ~750 MB/year total — 71% of this lane's growth
    — on a 4.55 GB database already past a 3,072 MB gate, for a blob nothing
    reads. What IS read stays: why the pair was chosen, and how many there
    were."""
    import json

    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    many = [pair(token_id(1), address=f"Pair{i}", liq=1000.0 + i) for i in range(20)]
    await run_pass(session, adapter=FakeAdapter({token_id(1): many}), now=NOW)
    obs = session.query(CryptoHorizonObservation).one()
    assert sparse.AUDIT_CANDIDATE_LIMIT == 0
    assert obs.raw_payload["candidates"] == []
    assert obs.raw_payload["candidate_count"] == 20
    assert obs.raw_payload["selected_pair_basis"]
    # a fixture-derived size bound, not a host measurement: it exists to catch
    # an unbounded payload creeping back in, not to predict production bytes
    assert len(json.dumps(obs.raw_payload)) < 512


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


@pytest.mark.asyncio
async def test_a_refusal_reports_the_work_it_had_already_done(session):
    """B8. `_refused` rebuilt the result from `_base_result`, destroying the
    evidence of what the pass had already done. Measured: a
    `provider_policy_violation` after enrolment reported `enrolled: 0,
    persisted: False, cohort_id: None` while the database held 1 cohort and 5
    members."""
    from app.services.crypto_provider_policy import Provider, guard_provider_request

    class ViolatingOnFirst(FakeAdapter):
        async def fetch_pairs_for_token(self, token_address):
            await guard_provider_request(Provider.SOLANA_TRACKER)
            return []

    for n in range(1, 6):
        add_birth(session, n, anchor=NOW - timedelta(hours=6))
    session.commit()
    r = await run_pass(session, adapter=ViolatingOnFirst(), now=NOW)

    assert r["status"] == sparse.STATUS_PROVIDER_POLICY_VIOLATION
    assert session.query(CryptoHorizonCohort).count() == 1
    assert session.query(CryptoHorizonCohortMember).count() == 5
    assert r["enrolled"] == 5, "the refusal claimed no enrolment happened"
    assert r["cohort_id"] == session.query(CryptoHorizonCohort).one().id
    assert r["cohort_created"] is True
    assert r["persisted"] is True
    assert r["births_considered"] == 5


@pytest.mark.asyncio
async def test_a_policy_violation_reports_the_provider_spend_it_had_made(session):
    """The other half of B8: a violation on request 5 of 8 reported
    `external_calls: 0`, `solana_tracker_calls: 0` hardcoded and NO ledger,
    after 4 real fetches. The one path whose purpose is to prove what a paid
    provider did understated real spend as zero."""
    from app.services.crypto_provider_policy import Provider, guard_provider_request

    class ViolatingOnFifth(FakeAdapter):
        async def fetch_pairs_for_token(self, token_address):
            if self.calls >= 4:
                await guard_provider_request(Provider.SOLANA_TRACKER)
            return await super().fetch_pairs_for_token(token_address)

    for n in range(1, 9):
        add_birth(session, n, anchor=NOW - timedelta(hours=6, minutes=n))
    session.commit()
    pairs = {token_id(n): [pair(token_id(n))] for n in range(1, 9)}
    adapter = ViolatingOnFifth(pairs)
    r = await run_pass(session, adapter=adapter, now=NOW)

    assert r["status"] == sparse.STATUS_PROVIDER_POLICY_VIOLATION
    assert adapter.calls == 4, "the fake did not reach the fifth request"
    assert r["external_calls"] == 4, "real provider spend was reported as zero"
    assert r["provider_ledger"], "the ledger snapshot was lost with the run context"
    assert r["denied_provider_attempts"] == {"solana-tracker": 1}
    assert r["solana_tracker_calls"] == 0
    for key in ("authorized", "started", "succeeded", "failed"):
        assert r["provider_ledger"]["solana-tracker"][key] == 0, key


@pytest.mark.asyncio
async def test_a_lock_mid_write_reports_the_batches_that_are_durable(session):
    """B8, the partially-committed case: the result claimed 0 rows while
    committed observations and ticks were durable on disk."""
    import sqlite3

    from sqlalchemy.exc import OperationalError

    for n in range(1, 7):
        add_birth(session, n, anchor=NOW - timedelta(hours=6, minutes=n))
    session.commit()
    pairs = {token_id(n): [pair(token_id(n))] for n in range(1, 7)}

    real_commit = session.commit
    seen = {"obs_commits": 0}

    def locked_after_two_batches():
        if any(isinstance(o, CryptoHorizonObservation) for o in session.new):
            seen["obs_commits"] += 1
            if seen["obs_commits"] > 2:
                raise OperationalError(
                    "INSERT INTO crypto_horizon_observations", {},
                    sqlite3.OperationalError("database is locked"),
                )
        return real_commit()

    session.commit = locked_after_two_batches
    try:
        r = await run_pass(
            session, adapter=FakeAdapter(pairs), now=NOW,
            cfg=config(write_batch_size=2),
        )
    finally:
        del session.commit

    assert r["status"] == sparse.STATUS_DB_LOCKED
    durable_obs = session.query(CryptoHorizonObservation).count()
    durable_ticks = session.query(CryptoPriceTick).count()
    assert durable_obs == 4 and durable_ticks == 4
    assert r["observations_recorded"] == durable_obs
    assert r["ticks_written"] == durable_ticks
    assert r["batches_committed"] == 2
    assert r["outcome_counts"] == {OBS_OBSERVED: 4}
    assert r["persisted"] is True
    assert r["enrolled"] == 6


@pytest.mark.asyncio
async def test_a_lock_during_enrolment_reports_the_members_that_are_durable(session):
    """A lock partway through enrolment must report the committed members, and
    must NOT claim a cohort it rolled back into non-existence."""
    import sqlite3

    from sqlalchemy.exc import OperationalError

    for n in range(1, 7):
        add_birth(session, n, anchor=NOW - timedelta(hours=6, minutes=n))
    session.commit()

    real_commit = session.commit
    seen = {"member_commits": 0}

    def locked_after_two_batches():
        if any(isinstance(o, CryptoHorizonCohortMember) for o in session.new):
            seen["member_commits"] += 1
            if seen["member_commits"] > 2:
                raise OperationalError(
                    "INSERT INTO crypto_horizon_cohort_members", {},
                    sqlite3.OperationalError("database is locked"),
                )
        return real_commit()

    session.commit = locked_after_two_batches
    try:
        r = await run_pass(
            session, adapter=FakeAdapter({}), now=NOW, cfg=config(write_batch_size=2),
        )
    finally:
        del session.commit

    assert r["status"] == sparse.STATUS_DB_LOCKED
    durable = session.query(CryptoHorizonCohortMember).count()
    assert durable == 4
    assert r["enrolled"] == durable
    assert r["cohort_id"] == session.query(CryptoHorizonCohort).one().id
    assert r["persisted"] is True


@pytest.mark.asyncio
async def test_a_ledger_that_proves_a_paid_request_is_a_typed_refusal(session):
    """MUTANT KILLER 1. Deleting `_fetch_phase`'s ledger assertion left all 57
    original tests passing: nothing exercised it, and it raised a bare
    `RuntimeError` (an uncaught traceback, not a typed refusal). This drives
    the assertion directly by marking a SolanaTracker request as started and
    succeeded from inside the fetch phase."""
    from app.services.crypto_provider_policy import (
        Provider,
        mark_started,
        mark_succeeded,
    )

    class SpendingBehindTheGuard(FakeAdapter):
        async def fetch_pairs_for_token(self, token_address):
            # exactly what a bypassed deny set looks like in the ledger: a real
            # request accounted against a provider this lane never authorizes
            mark_started(Provider.SOLANA_TRACKER)
            mark_succeeded(Provider.SOLANA_TRACKER)
            return await super().fetch_pairs_for_token(token_address)

    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    r = await run_pass(
        session, adapter=SpendingBehindTheGuard({token_id(1): [pair(token_id(1))]}),
        now=NOW,
    )
    assert r["status"] == sparse.STATUS_PROVIDER_POLICY_VIOLATION
    assert r["status"] not in sparse.HEALTHY_STATUSES
    assert "solana-tracker" in r["error"]
    assert session.query(CryptoHorizonObservation).count() == 0

    # B2 + B3. This branch is the SEVERE one — a paid request actually went
    # out — and it used to be the one with the POORER record: `external_calls:
    # 0`, `solana_tracker_calls: 0`, `provider_ledger: None`, while the milder
    # DENIED path (nothing spent) reported all three. It must now carry the
    # full receipt, and every number must be DERIVED from the ledger.
    #
    # These assertions replace a retired source-text test that pinned the
    # literal `ledger.get("solana-tracker", {})`. That wording check killed one
    # mutant by accident: a semantically identical mutant that zeroes the same
    # receipt without writing the banned string survived the whole suite. Both
    # die here, because a ledger that accounts a paid request MOVES the number.
    assert r["provider_ledger"], "the ledger snapshot was lost with the run context"
    entry = r["provider_ledger"]["solana-tracker"]
    assert (entry["started"], entry["succeeded"]) == (1, 1)
    assert r["solana_tracker_calls"] == sum(
        entry.get(k, 0) for k in ("authorized", "started", "succeeded", "failed")
    ) == 2, "a proven paid request was still reported as zero paid requests"
    assert r["external_calls"] == 1, "the DexScreener spend was reported as zero"
    assert r["denied_provider_attempts"] == {}, (
        "nothing was denied here — the request got through, which is the point"
    )


def test_the_paid_provider_receipt_is_a_pure_function_of_the_ledger():
    """MUTANT KILLER 2, rewritten (B3).

    This test used to assert that `_run_locked`'s SOURCE TEXT contained the
    literal `ledger.get("solana-tracker", {})` and did not contain
    `result["solana_tracker_calls"] = 0`. That pins WORDING, not behaviour: a
    reviewer's mutant died only because it happened to write that exact string,
    and a semantically identical mutant that zeroed the receipt some other way
    survived all 97 tests. This project has named that defect class repeatedly.

    So: drive the single population helper every exit now shares, with a ledger
    the module never produced, and assert the OUTPUT. Any mutant that stops
    deriving the count from the ledger fails here regardless of how it is
    spelled — and the behavioural end-to-end proof on a real pass lives in
    `test_a_ledger_that_proves_a_paid_request_is_a_typed_refusal` above."""
    ledger = {
        "dexscreener": {"authorized": 3, "started": 3, "succeeded": 3, "failed": 0},
        "solana-tracker": {
            "authorized": 2, "started": 2, "succeeded": 1, "failed": 1,
            "blocked_policy": 4,
        },
    }
    result: dict = {}
    sparse._apply_provider_ledger(result, ledger, calls=3)

    assert result["solana_tracker_calls"] == 6
    assert result["external_calls"] == 3
    assert result["provider_ledger"] is ledger
    assert result["denied_provider_attempts"] == {"solana-tracker": 4}

    # and it is a FUNCTION of the ledger, not a constant: a different ledger
    # must produce a different number through the very same call
    other: dict = {}
    sparse._apply_provider_ledger(
        other,
        {"solana-tracker": {"authorized": 1, "started": 1, "succeeded": 1,
                            "failed": 0, "blocked_policy": 0}},
        calls=0,
    )
    assert other["solana_tracker_calls"] == 3
    assert other["denied_provider_attempts"] == {}


@pytest.mark.asyncio
async def test_the_policy_cap_is_a_real_second_ceiling(session):
    """MUTANT KILLER 3. Nulling the policy cap survived the original suite —
    nothing asserted the cap exists or binds. The loop's `observe_limit` stops
    first in normal operation, so this drives the CAP directly."""
    from app.services.crypto_provider_policy import Provider

    policy = sparse._dexscreener_only_policy("test-run", 7)
    assert policy.cap(Provider.DEXSCREENER) == 7, "the DexScreener cap is not set"
    for provider in Provider:
        if provider is not Provider.DEXSCREENER:
            assert policy.cap(provider) is None or policy.cap(provider) == 0

    # and it BINDS: a fetch phase asked for more tokens than the cap allows
    # (only reachable if the loop bound is bypassed) stops making requests
    from app.services.crypto_provider_policy import (
        ProviderCapExhausted,
        guard_provider_request,
        provider_run,
    )

    capped = sparse._dexscreener_only_policy("test-run", 2)
    with provider_run(capped):
        await guard_provider_request(Provider.DEXSCREENER)
        await guard_provider_request(Provider.DEXSCREENER)
        with pytest.raises(ProviderCapExhausted):
            await guard_provider_request(Provider.DEXSCREENER)


@pytest.mark.asyncio
async def test_a_lost_rolling_marker_never_creates_a_second_cohort(session):
    """The rolling marker is one unconstrained JSON key. Dropped from the
    existing cohort's provenance, the next pass silently created a SECOND
    rolling cohort and split the observation denominator in two."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter({}), now=NOW)
    cohort = session.query(CryptoHorizonCohort).one()

    provenance = dict(cohort.provenance)
    provenance.pop("membership")
    cohort.provenance = provenance
    session.commit()
    assert not is_rolling_cohort(cohort)

    add_birth(session, 2, anchor=NOW - timedelta(hours=6))
    session.commit()
    r = await run_pass(session, adapter=FakeAdapter({}), now=NOW + timedelta(hours=1))
    assert r["status"] == sparse.STATUS_AMBIGUOUS_COHORT
    assert r["status"] not in sparse.HEALTHY_STATUSES
    assert session.query(CryptoHorizonCohort).count() == 1


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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [
    "429", "timeout", "server_error", "bad_json",
    # B1 (SHAPE): an HTTP 200 whose body decodes but is not a pair array. These
    # two fabricated `token_inactive` — terminally, with the pass exiting `ok`
    # and `ledger_failed == 0` — until `_get` learned the shape each endpoint
    # contracts for. The trigger is narrow; the blast radius is total and
    # correlated, because a shape change writes off EVERY token in EVERY pass.
    "200_dict_error", "200_html_string",
])
@pytest.mark.parametrize("horizon,age_hours", [("6h", 6), ("24h", 24)])
async def test_a_transport_failure_is_never_persisted_as_a_token_state(
    session, mode, horizon, age_hours,
):
    """B1 (FABRICATION), driven through the REAL adapter.

    `DexScreenerAdapter` never raises — 429, timeout, 5xx and undecodable JSON
    all `return None`, so `fetch_pairs_for_token` returns []. The write phase
    then read that empty list as a provider ANSWER and persisted
    `provider_no_pair` — or, at 24h where `aged` is true by construction, the
    TERMINAL `token_inactive`: an affirmative claim that the token is dead,
    derived from a request that never returned an answer, and never re-examined.

    The old test for this path used a fake adapter raising `RuntimeError`, a
    contract the real adapter explicitly disclaims: the tested path could not
    occur and the production path was untested."""
    import httpx

    from app.adapters.dexscreener import DexScreenerAdapter

    add_birth(session, 1, anchor=NOW - timedelta(hours=age_hours))
    session.commit()

    _TransportFailureClient.mode = mode
    _TransportFailureClient.requested = []
    original = httpx.AsyncClient
    httpx.AsyncClient = _TransportFailureClient
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

    assert _TransportFailureClient.requested, "the real adapter issued no request"
    assert r["status"] in sparse.HEALTHY_STATUSES, r.get("error")
    obs = session.query(CryptoHorizonObservation).one()
    assert obs.horizon == horizon
    assert obs.status not in (OBS_PROVIDER_NO_PAIR, OBS_TOKEN_INACTIVE), (
        f"a {mode} was persisted as the token-state fact {obs.status!r}"
    )
    assert obs.status == OBS_REQUEST_FAILED
    assert obs.missing_cause == OBS_REQUEST_FAILED
    assert r["outcome_counts"] == {OBS_REQUEST_FAILED: 1}
    assert session.query(CryptoPriceTick).count() == 0


def test_a_deterministic_guard_skip_counts_as_a_lost_request():
    """The OTHER half of the B1 control, which was untested.

    `_transport_failures` reads three ledger buckets, not one. `failed` covers
    429 / timeout / connection error / 5xx / undecodable JSON (and now, since
    round 3, a 200 of the wrong shape). `skipped_cap` and `skipped_budget`
    cover the DETERMINISTIC guard skips: `_get` returns None before opening a
    client, `fetch_pairs_for_token` degrades that to [], and the write phase
    would read it as a provider ANSWER — the same fabrication B1 is about,
    reached without a socket ever opening.

    Dropping either bucket leaves every other test in this file passing, so it
    is asserted directly against the ledger the guard itself keeps."""
    from app.services.crypto_provider_policy import Provider, ProviderLedger

    class _Ctx:
        def __init__(self, ledger):
            self.ledger = ledger

    dex = Provider.DEXSCREENER

    assert sparse._transport_failures(_Ctx(ProviderLedger())) == 0

    only_failed = ProviderLedger()
    only_failed.failed[dex] = 2
    assert sparse._transport_failures(_Ctx(only_failed)) == 2

    only_cap = ProviderLedger()
    only_cap.skipped_cap[dex] = 3
    assert sparse._transport_failures(_Ctx(only_cap)) == 3, (
        "a per-run cap skip returned None and was not counted as a lost request"
    )

    only_budget = ProviderLedger()
    only_budget.skipped_budget[dex] = 4
    assert sparse._transport_failures(_Ctx(only_budget)) == 4, (
        "a budget skip returned None and was not counted as a lost request"
    )

    all_three = ProviderLedger()
    all_three.failed[dex] = 2
    all_three.skipped_cap[dex] = 3
    all_three.skipped_budget[dex] = 4
    assert sparse._transport_failures(_Ctx(all_three)) == 9

    # and it is DexScreener-scoped: another provider's losses are not this
    # lane's transport failures
    other = ProviderLedger()
    other.failed[Provider.SOLANA_TRACKER] = 5
    assert sparse._transport_failures(_Ctx(other)) == 0


@pytest.mark.asyncio
async def test_an_empty_provider_answer_is_still_a_provider_answer(session):
    """The other half of B1: the delta must not turn a genuine `[]` — HTTP 200
    with an empty pair list — into `request_failed`. That would make every
    honest `provider_no_pair` retryable and double this lane's spend."""
    import httpx

    from app.adapters.dexscreener import DexScreenerAdapter

    class EmptyClient(_TransportFailureClient):
        async def get(self, url):
            type(self).requested.append(url)
            return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    EmptyClient.requested = []
    original = httpx.AsyncClient
    httpx.AsyncClient = EmptyClient
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

    assert r["outcome_counts"] == {OBS_PROVIDER_NO_PAIR: 1}
    obs = session.query(CryptoHorizonObservation).one()
    assert obs.status == OBS_PROVIDER_NO_PAIR


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,age_hours,expected_status", [
    # Probe 15: a genuine JSON ARRAY (isinstance(payload, list) is True — B1's
    # `expect=list` check does not fire) whose elements do not parse into a
    # usable pair for this chain. Before this fix each of these six reached
    # `select_pair([])`, which writes `candidate_count: 0` — indistinguishable
    # from a token that genuinely has no pairs — and past 24h `aged` makes the
    # miss the terminal, affirmative `token_inactive`. Aged here so the
    # terminal path is exercised, same as B1's own parametrization.
    ("200_list_of_strings", 24, OBS_REQUEST_FAILED),
    ("200_list_of_ints", 24, OBS_REQUEST_FAILED),
    ("200_list_of_dicts_no_keys", 24, OBS_REQUEST_FAILED),
    ("200_list_of_dicts_wrong_chain", 24, OBS_REQUEST_FAILED),
    ("200_list_of_nulls", 24, OBS_REQUEST_FAILED),
    ("200_nested_error_envelope_list", 24, OBS_REQUEST_FAILED),
    # CONTROL: a genuinely empty array is an honest provider answer. Not aged,
    # so a false positive here would show up as the wrong status directly
    # (aged would otherwise mask `provider_no_pair` behind `token_inactive`,
    # which is also the CORRECT terminal answer for an honest empty list —
    # this control isolates the un-aged case to catch a false `mark_failed`).
    ("200_empty_list", 6, OBS_PROVIDER_NO_PAIR),
    # CONTROL: one real, chain-matching, fully-shaped pair must still produce
    # an honest observation — the fix must not become a false positive on the
    # happy path.
    ("ok", 6, OBS_OBSERVED),
])
async def test_a_non_empty_payload_that_parses_to_zero_pairs_is_a_failed_request(
    session, mode, age_hours, expected_status,
):
    """Probe 15, driven through the REAL adapter. `mark_failed` fires on
    OUTCOME (non-empty payload in, zero chain-matching pairs out), not on any
    enumerated shape — this is the class-closing property under test, not one
    more shape added to a list."""
    import httpx

    from app.adapters.dexscreener import DexScreenerAdapter

    add_birth(session, 1, anchor=NOW - timedelta(hours=age_hours))
    session.commit()

    _TransportFailureClient.mode = mode
    _TransportFailureClient.requested = []
    original = httpx.AsyncClient
    httpx.AsyncClient = _TransportFailureClient
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

    assert _TransportFailureClient.requested, "the real adapter issued no request"
    assert r["status"] in sparse.HEALTHY_STATUSES, r.get("error")
    obs = session.query(CryptoHorizonObservation).one()
    assert obs.status == expected_status, (
        f"mode={mode!r} produced {obs.status!r}, expected {expected_status!r}"
    )
    if expected_status == OBS_REQUEST_FAILED:
        assert obs.missing_cause == OBS_REQUEST_FAILED
        assert session.query(CryptoPriceTick).count() == 0
    elif expected_status == OBS_PROVIDER_NO_PAIR:
        assert session.query(CryptoPriceTick).count() == 0
    else:  # OBS_OBSERVED
        assert session.query(CryptoPriceTick).count() == 1


# --- 7b. GATE 1 — exact token identity ------------------------------------------
#
# The defect: `fetch_pairs_for_token` filters the provider's answer by CHAIN
# only, and `select_pair`'s quality policy pays an exact base-token match a +25
# BONUS instead of gating on it. So a chain-correct, well-formed, non-empty
# answer about a DIFFERENT token parses cleanly, scores, is selected, and is
# recorded as `OBS_OBSERVED` with a price tick filed under the requested token's
# address. Every earlier check in this milestone passes on that response.
#
# THE PROOF IS AT THE DATABASE, not at a return value. Note carefully that
# `token_address` on the tick is copied from the PLAN ENTRY, so it always equals
# the requested token by construction — asserting only that would prove nothing.
# What actually distinguishes a right tick from a wrong one is its CONTENT:
# `pair_address` / `price_usd` / `liquidity_usd` must come from a pair whose
# BASE token is that same address. `_assert_no_foreign_tick` asserts both.


def _assert_no_foreign_tick(session, token: str) -> None:
    """No persisted tick may be sourced from another token's pair.

    Read back from `crypto_price_ticks` itself — not from the pass result, not
    from the ORM identity map — because the guarantee this milestone owes is
    about what is DURABLE."""
    rows = session.execute(
        select(
            CryptoPriceTick.token_address,
            CryptoPriceTick.pair_address,
            CryptoPriceTick.price_usd,
        )
    ).all()
    for row in rows:
        assert row.token_address == token, (
            f"a tick was persisted under {row.token_address!r}, not {token!r}"
        )
        assert row.pair_address != "PairForeign", (
            "a tick was persisted from another token's pair "
            f"({row.pair_address}) under {token}"
        )
        assert row.pair_address != "PairQuoteSide", (
            "a tick was persisted from a pair that merely QUOTES this token "
            f"({row.pair_address}); its price is the base asset's, not ours"
        )
        assert row.price_usd != 999.0, (
            "a tick carries the FOREIGN token's price under this token's address"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected_status,expected_ticks", [
    # 1. WRONG TOKEN, SAME CHAIN. The whole answer is about someone else.
    ("identity_wrong_token", OBS_IDENTITY_MISMATCH, 0),
    # 2. MIXTURE. Ours is present but out-scored ~75 points by the foreign pair;
    #    ours must be the one selected and the foreign one must not leak.
    ("identity_mixture", OBS_OBSERVED, 1),
    # 3. QUOTE-SIDE ONLY. Our token is in the pair — as the quote. `priceUsd` is
    #    the BASE asset's price. Deliberately NOT identity: see
    #    `_identity_matched`. A quote-side match would write another asset's
    #    price under our address, which is the defect wearing a nicer hat.
    ("identity_quote_only", OBS_IDENTITY_MISMATCH, 0),
    # 4. UPSTREAM FIELD DRIFT. Not Gate 1's to catch and deliberately so:
    #    `_parse_pair` drops a pair with no `baseToken.address`, and Probe 15
    #    turns "non-empty payload, zero usable pairs" into a failed REQUEST
    #    before identity is ever consulted. Pinned so the boundary is asserted.
    ("identity_field_absent", OBS_REQUEST_FAILED, 0),
    ("identity_field_renamed", OBS_REQUEST_FAILED, 0),
    ("identity_field_null", OBS_REQUEST_FAILED, 0),
    # 5. HONEST EMPTY ANSWER. `[]` is a real provider answer about OUR token and
    #    must stay terminal — aged past 24h that is `token_inactive`, and the
    #    gate must not convert it into an identity result.
    ("200_empty_list", OBS_TOKEN_INACTIVE, 0),
    # 6. EXACT CORRECT BASE PAIR. The happy path must be untouched.
    ("ok", OBS_OBSERVED, 1),
])
async def test_gate_1_no_tick_is_ever_persisted_from_another_tokens_pair(
    session, mode, expected_status, expected_ticks,
):
    """Driven through the REAL `DexScreenerAdapter` so the gate is proven on the
    production code path, not on a fake that could not have had the defect.

    Aged past 24h on purpose: `aged` is what makes `_record_observation` read
    `candidate_count == 0` as the TERMINAL, affirmative claim `token_inactive`.
    That is the outcome an identity mismatch must never be able to reach, so the
    test is run where reaching it is easiest."""
    token = token_id(1)
    add_birth(session, 1, anchor=NOW - timedelta(hours=24))
    session.commit()

    r = await run_real_pass(session, mode=mode, now=NOW)

    assert _TransportFailureClient.requested, "the real adapter issued no request"
    assert r["status"] in sparse.HEALTHY_STATUSES, r.get("error")
    obs = session.query(CryptoHorizonObservation).one()
    assert obs.status == expected_status, (
        f"mode={mode!r} produced {obs.status!r}, expected {expected_status!r}"
    )
    assert obs.token_address == token
    # THE LOAD-BEARING ASSERTION, read back from the table.
    assert session.query(CryptoPriceTick).count() == expected_ticks
    _assert_no_foreign_tick(session, token)

    if expected_status == OBS_IDENTITY_MISMATCH:
        # a typed NON-observation: no price, no pair, no tick, and above all
        # neither `token_inactive` nor `observed`
        assert obs.missing_cause == OBS_IDENTITY_MISMATCH
        assert obs.tick_id is None
        assert obs.price_usd is None and obs.liquidity_usd is None
        assert obs.pair_address is None
        assert r["outcome_counts"] == {OBS_IDENTITY_MISMATCH: 1}
    if mode == "identity_mixture":
        # ours was selected, and every number on the row is ours
        tick = session.query(CryptoPriceTick).one()
        assert tick.pair_address == "PairMine"
        assert tick.price_usd == 0.001
        assert tick.liquidity_usd == 1_000.0
        assert obs.pair_address == "PairMine"
        assert obs.price_usd == 0.001


def test_gate_1_the_foreign_pair_would_win_selection_without_the_gate():
    """The mutations above are only load-bearing if the foreign pair is what the
    unguarded lane WOULD have chosen. Asserted against the real scorer rather
    than assumed, so a future re-tuning of `active_pair_quality_score` that
    quietly makes the fixture harmless fails here instead of silently turning
    six mutations into no-ops."""
    from app.services.crypto_horizon import active_pair_quality_score, select_pair

    token = token_id(1)
    mine = pair(token, price=0.001, liq=1_000.0, address="PairMine")
    foreign = pair(FOREIGN_TOKEN, price=999.0, liq=1_000_000.0, address="PairForeign")

    assert active_pair_quality_score(foreign, token) > active_pair_quality_score(
        mine, token
    ), "the +25 exact-match bonus already outweighs the fixture: mutation is inert"

    # the shared, ungated selector picks the foreign pair — this IS the defect
    chosen, _basis = select_pair([foreign, mine], token)
    assert chosen.pair_address == "PairForeign"
    assert chosen.base_token_address != token

    # the lane's gate is what removes it from consideration at all
    kept = [p for p in (foreign, mine) if sparse._identity_matched(p, token)]
    assert [p.pair_address for p in kept] == ["PairMine"]


def test_gate_1_does_not_change_the_shared_adapters_contract():
    """BLAST RADIUS. The gate lives in this lane, not in `DexScreenerAdapter`,
    which the scout, meme, discovery and frozen-horizon lanes share. The adapter
    must still return a chain-matching foreign pair unchanged; if this ever
    starts failing, the gate has leaked out of its lane."""
    import inspect

    from app.adapters import dexscreener

    source = inspect.getsource(dexscreener.DexScreenerAdapter.fetch_pairs_for_token)
    assert "base_token_address" not in source, (
        "the identity gate leaked into the SHARED adapter"
    )
    assert "token_address" in inspect.signature(
        dexscreener.DexScreenerAdapter.fetch_pairs_for_token
    ).parameters


@pytest.mark.asyncio
async def test_gate_1_an_identity_mismatch_is_re_plannable_while_the_band_is_open(
    session,
):
    """TERMINALITY. `identity_mismatch` sits on the RETRYABLE side of the
    cause-based split, for the reason `_is_retryable` gives: terminal means "the
    provider told us something ABOUT THIS TOKEN", and an answer naming only
    other tokens told us nothing about this one. It is also indistinguishable
    from upstream field drift, i.e. a contract violation, which is precisely the
    correlated whole-fleet failure the rule exists to keep re-plannable."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()

    first = await run_real_pass(session, mode="identity_wrong_token", now=NOW)
    assert first["outcome_counts"] == {OBS_IDENTITY_MISMATCH: 1}
    assert first["retryable_request_failures"] == 0  # nothing to retry yet

    # 30 minutes later the band is still open (it closes at anchor + 6h + 60m)
    second = await run_real_pass(session, mode="ok", now=NOW + timedelta(minutes=30))
    assert second["retryable_request_failures"] == 1
    assert second["request_failures_reattempted"] == 1
    assert second["outcome_counts"] == {OBS_OBSERVED: 1}

    obs = session.query(CryptoHorizonObservation).one()  # UPDATED, not inserted
    assert obs.status == OBS_OBSERVED
    assert session.query(CryptoPriceTick).count() == 1
    _assert_no_foreign_tick(session, token_id(1))


@pytest.mark.asyncio
async def test_gate_1_an_identity_mismatch_is_hard_capped_at_two_attempts(session):
    """Making identity retryable must not widen the spend bound. The cap is per
    (token, horizon) and counts ATTEMPTS regardless of cause, so a permanently
    mismatching token costs exactly one extra request, ever — the same
    `SPARSE_MAX_ATTEMPTS = 2` ceiling `request_failed` already carries."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()

    await run_real_pass(session, mode="identity_wrong_token", now=NOW)
    second = await run_real_pass(
        session, mode="identity_wrong_token", now=NOW + timedelta(minutes=20),
    )
    assert second["external_calls"] == 1
    obs = session.query(CryptoHorizonObservation).one()
    assert obs.status == OBS_IDENTITY_MISMATCH
    assert obs.raw_payload[sparse.ATTEMPTS_KEY] == sparse.SPARSE_MAX_ATTEMPTS

    # third pass, band STILL open, provider healthy — the cap holds and no
    # further request is bought
    third = await run_real_pass(session, mode="ok", now=NOW + timedelta(minutes=40))
    assert third["external_calls"] == 0
    assert third["due_observations"] == 0
    assert session.query(CryptoHorizonObservation).one().status == OBS_IDENTITY_MISMATCH
    assert session.query(CryptoPriceTick).count() == 0


@pytest.mark.asyncio
async def test_gate_1_an_identity_mismatch_is_terminal_once_the_band_closes(session):
    """Retryability never extends the band. The planner alone decides what is
    still due, and a closed band ends the horizon whatever the cause."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_real_pass(session, mode="identity_wrong_token", now=NOW)
    # the 6h band closes at anchor + 6h + 60m == NOW + 60m
    after = await run_real_pass(session, mode="ok", now=NOW + timedelta(minutes=61))
    assert after["external_calls"] == 0
    assert after["due_observations"] == 0
    assert session.query(CryptoHorizonObservation).one().status == OBS_IDENTITY_MISMATCH
    assert session.query(CryptoPriceTick).count() == 0


@pytest.mark.asyncio
async def test_gate_1_records_what_the_provider_actually_answered(session):
    """`AUDIT_CANDIDATE_LIMIT = 0` stores no per-candidate diagnostics, so
    without this receipt an identity mismatch would be a status with no
    evidence — indistinguishable, on inspection, from a bug in the gate itself.
    Bounded at 5 addresses so `raw_payload` is not re-inflated."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_real_pass(session, mode="identity_wrong_token", now=NOW)

    gate = session.query(CryptoHorizonObservation).one().raw_payload[
        "selected_pair_basis"
    ]["identity_gate"]
    assert gate["requested_token"] == token_id(1)
    assert gate["pairs_returned"] == 1
    assert gate["exact_base_matches"] == 0
    assert gate["rejected_base_tokens"] == [FOREIGN_TOKEN]
    assert len(gate["rejected_base_tokens"]) <= 5


@pytest.mark.asyncio
async def test_gate_1_never_fabricates_a_price_from_a_foreign_candidate(session):
    """The subtler leak. When nothing is eligible, `_record_observation` harvests
    `price_usd` from `candidates` for the early-liquidity diagnostic. The gate
    therefore has to filter `candidates` too, not just the selection — otherwise
    a foreign pair's price is written onto OUR observation row even though no
    tick is."""
    from app.services.crypto_horizon import OBS_NO_LIQUIDITY_STATE

    token = token_id(1)
    adapter = FakeAdapter({token: [
        # ours: priced but zero liquidity, so nothing is eligible
        pair(token, price=0.001, liq=0.0, address="PairMine"),
        # theirs: priced far higher, and the harvest takes the HIGHEST price
        pair(FOREIGN_TOKEN, price=999.0, liq=0.0, address="PairForeign"),
    ]})
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()

    await run_pass(session, adapter=adapter, now=NOW)

    obs = session.query(CryptoHorizonObservation).one()
    assert obs.status == OBS_NO_LIQUIDITY_STATE
    assert obs.price_usd == 0.001, "a FOREIGN pair's price was written to our row"
    assert obs.pair_address == "PairMine"
    assert session.query(CryptoPriceTick).count() == 0


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


def _sql_for(session, table: str, run) -> list[str]:
    """Every SELECT `run()` issues against `table`, as raw SQL."""
    from sqlalchemy import event as sa_event

    seen: list[str] = []

    @sa_event.listens_for(session.get_bind(), "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):
        if table in statement and statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)

    try:
        run()
    finally:
        sa_event.remove(session.get_bind(), "before_cursor_execute", _capture)
    return seen


@pytest.mark.asyncio
async def test_the_coverage_report_bounds_both_queries_by_the_same_window(session):
    """B6. `--hours` filtered the MEMBERS query only; the observations query
    was `WHERE cohort_id = :id` unconditionally, and it selected whole
    entities. Measured at one year of this lane's own output (193,450 members /
    386,900 observations, ANALYZE run): members 25.1s, observations 368.3s,
    peak RSS 692MB, SHARED held 14.7s, 3 hard `database is locked` failures in
    a competing writer — and `--hours 48` left the 368s query untouched. This
    is the operator's only verification surface."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}), now=NOW)

    statements = _sql_for(
        session, "crypto_horizon_observations",
        lambda: sparse.build_observation_coverage_report(
            session, settings=settings(), hours=48, now=NOW + timedelta(hours=2),
        ),
    )
    obs_selects = [s for s in statements if "FROM crypto_horizon_observations" in s]
    assert obs_selects, statements
    scan_all = [
        s for s in obs_selects
        if "added_at" not in s and "ORDER BY" not in s.upper()
    ]
    assert not scan_all, (
        "the observations query is not bounded by the report window:\n"
        + "\n\n".join(scan_all)
    )
    # and it reads columns, not the 22-field entity (raw_payload above all)
    for statement in obs_selects:
        assert "raw_payload" not in statement, statement


@pytest.mark.asyncio
async def test_the_coverage_report_selects_member_columns_not_entities(session):
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}), now=NOW)
    statements = _sql_for(
        session, "crypto_horizon_cohort_members",
        lambda: sparse.build_observation_coverage_report(
            session, settings=settings(), now=NOW + timedelta(hours=2),
        ),
    )
    assert statements
    for statement in statements:
        assert "crypto_horizon_cohort_members.symbol" not in statement, statement
        assert "crypto_horizon_cohort_members.birth_event_id" not in statement, statement


@pytest.mark.asyncio
async def test_a_window_shorter_than_a_closed_24h_band_is_refused(session):
    """`--hours 24` structurally nulls the 24h denominator: a closed 24h band
    needs `now > anchor + 25h` while `added_at >= now - hours` and `added_at >=
    anchor`, so no member can qualify. Measured: `hours=24` gave
    `bands_closed=0, look_completion_rate=None` — a silent empty answer that
    reads like "nothing to report"."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}), now=NOW)

    refused = sparse.build_observation_coverage_report(
        session, settings=settings(), hours=24, now=NOW + timedelta(hours=2),
    )
    assert refused["status"] == "window_too_short"
    assert refused["minimum_window_hours"] == sparse.MIN_REPORT_HOURS == 25
    assert "24h" in refused["error"]
    assert "by_horizon" not in refused

    ok = sparse.build_observation_coverage_report(
        session, settings=settings(), hours=25, now=NOW + timedelta(hours=2),
    )
    assert ok["status"] == "ok"


@pytest.mark.asyncio
async def test_the_report_reports_liveness_and_the_exact_timer_line(session):
    """`scheduling_miss_rate` detects a SLOW timer but not a STOPPED one — late
    births land in the excluded `enrolled_after_band_closed` bucket or vanish
    past the enrolment window entirely, and the rate lags a full band."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}), now=NOW)

    fresh = sparse.build_observation_coverage_report(
        session, settings=settings(), now=NOW + timedelta(minutes=30),
    )
    assert fresh["liveness"]["cadence_warning"] is False
    assert fresh["liveness"]["previous_write_age_minutes"] < 60
    assert fresh["liveness"]["expected_timer_oncalendar"] == sparse.timer_oncalendar()

    stopped = sparse.build_observation_coverage_report(
        session, settings=settings(), now=NOW + timedelta(hours=4),
    )
    assert stopped["liveness"]["cadence_warning"] is True
    assert stopped["liveness"]["previous_write_age_minutes"] > 1.5 * 60

    # LOW. The field is named for what it IS. It is derived from MAX(id) over
    # rows this lane WROTE, so a healthy pass with nothing to enrol and nothing
    # due does not advance it and false-warns. Calling it `latest_pass_at` sold
    # a pass heartbeat the mechanism does not have, and a real one needs a run
    # table this round did not decide on. The name and the stated meaning are
    # the fix; the weaker claim must be readable AS the weaker claim.
    assert "latest_pass_at" not in stopped["liveness"]
    assert "previous_pass_age_minutes" not in stopped["liveness"]
    assert stopped["liveness"]["latest_write_at"]
    assert "write" in stopped["liveness"]["cadence_warning_means"].lower()
    assert "not a pass heartbeat" in (
        stopped["liveness"]["cadence_warning_means"].lower()
    )


@pytest.mark.asyncio
async def test_enrolling_after_the_band_closed_shows_up_as_a_rate(session):
    """`enrolled_after_band_closed` is excluded from every rate. Without a
    compensating signal, 20 births all enrolled after their 6h band had closed
    make every rate read clean while the mechanism accomplishes nothing."""
    for n in range(1, 21):
        add_birth(session, n, anchor=NOW - timedelta(hours=7, minutes=30))
    session.commit()
    r = await run_pass(session, adapter=FakeAdapter({}), now=NOW)
    assert r["due_observations"] == 0

    report = sparse.build_observation_coverage_report(
        session, settings=settings(), now=NOW + timedelta(minutes=5),
    )
    six = report["by_horizon"]["6h"]
    assert six[sparse.OBS_STATE_ENROLLED_TOO_LATE] == 20
    assert six["look_completion_rate"] is None  # nothing in the denominator
    assert six["never_had_a_chance_rate"] == 1.0
    assert report["enrolment_lag_seconds"]["p50"] is not None


@pytest.mark.asyncio
async def test_a_late_enrolment_tail_is_visible_when_the_rate_dilutes(session):
    """LOW (dilution). `never_had_a_chance_rate` is a RATE: 20 members enrolled
    after their band closed among 200 healthy pending ones reads as 0.1, and
    `p90` misses a 10% tail entirely — every headline reads clean while every
    tenth member never had a chance. `max` was the only unambiguous signal and
    a single number cannot say how many. `p99` and `over_band_count` can."""
    # 180 fresh members (lag ~0) and 20 enrolled long after their band closed
    for n in range(1, 181):
        add_birth(session, n, anchor=NOW - timedelta(minutes=5))
    for n in range(181, 201):
        add_birth(session, n, anchor=NOW - timedelta(hours=8))
    session.commit()
    await run_pass(session, adapter=FakeAdapter({}), now=NOW)

    lag = sparse.build_observation_coverage_report(
        session, settings=settings(), now=NOW + timedelta(minutes=5),
    )["enrolment_lag_seconds"]

    half_width = lag["band_half_width_seconds"]
    assert lag["p90"] <= half_width, (
        "this fixture is only interesting if p90 stays clean"
    )
    assert lag["p99"] > half_width, "the 10% tail is invisible at p99 too"
    assert lag["over_band_count"] == 20, (
        "the number of members enrolled past the band half-width is not stated"
    )
    assert lag["max"] > half_width


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


@pytest.mark.asyncio
async def test_the_manual_lane_refuses_the_rolling_cohort(session):
    """B3. `is_rolling_cohort` was checked at exactly ONE choke point
    (`build_arm_plan`). `observe_once` and `build_plan` had none, they plan at
    the FRACTIONAL tape tolerance (+/-12h at 24h, not this lane's +/-60min) and
    take the retry-in-place branch — and every sparse pass prints `cohort_id=`
    on stdout, so the id is right there.

    Measured before the fix: a `request_failed` at 6h, then
    `observe_once(<that cohort id>)` two hours later produced
    `status=observed`, `observed_at` a full hour outside `window_end`, and the
    coverage report scored `look_completion_rate 1.0` while printing
    `target_distance max 7200.0` against `band_half_width 3600.0`."""
    from app.services.crypto_horizon import (
        STATUS_ROLLING_COHORT_REFUSED,
        RollingCohortRefused,
    )

    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_real_pass(session, mode="429", now=NOW)
    cohort = session.query(CryptoHorizonCohort).one()
    assert is_rolling_cohort(cohort)

    s = settings()
    service = CryptoHorizonService(
        adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}), settings=s,
    )
    two_hours_later = session  # (the manual lane reads the wall clock itself)
    r = await service.observe_once(two_hours_later, cohort_id=cohort.id)
    assert r["status"] == STATUS_ROLLING_COHORT_REFUSED
    assert r["external_calls"] == 0
    assert r["persisted"] is False
    # nothing was rewritten
    assert session.query(CryptoHorizonObservation).one().status == OBS_REQUEST_FAILED

    with pytest.raises(RollingCohortRefused):
        service.build_plan(session, cohort.id)

    # and the CLI surfaces it as a non-zero exit, not a silent success
    from app import cli

    rc = await cli.crypto_horizon_observe_once(
        cohort_id=cohort.id, session=session,
    )
    assert rc == -1


@pytest.mark.asyncio
async def test_a_frozen_cohort_is_still_manually_observable(session):
    """The refusal must be surgical: the manual lane it was written for keeps
    working exactly as before."""
    s = settings()
    service = CryptoHorizonService(
        adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}), settings=s,
    )
    add_birth(session, 1, anchor=datetime.now(timezone.utc) - timedelta(minutes=15))
    session.commit()
    built = service.create_cohort(session, hours=24, limit=10, confirm=True)
    cohort_id = built["cohort_id"]
    assert not is_rolling_cohort(session.get(CryptoHorizonCohort, cohort_id))
    assert isinstance(service.build_plan(session, cohort_id), list)
    r = await service.observe_once(session, cohort_id=cohort_id)
    assert r["status"] == "ok"


@pytest.mark.asyncio
async def test_an_out_of_band_observation_is_not_scored_as_a_look(session):
    """B3, reporting half. An `observed` row whose `observed_at` falls outside
    the member-horizon's band is a distinct `out_of_band` state, excluded from
    `observed` and from `target_distance_seconds` — the invariant being that
    `target_distance_seconds.max` can never exceed the band half-width."""
    add_birth(session, 1, anchor=NOW - timedelta(hours=6))
    session.commit()
    await run_pass(
        session, adapter=FakeAdapter({token_id(1): [pair(token_id(1))]}), now=NOW,
    )
    obs = session.query(CryptoHorizonObservation).one()
    assert obs.status == OBS_OBSERVED

    at = NOW + timedelta(minutes=90)
    clean = sparse.build_observation_coverage_report(
        session, settings=settings(), now=at,
    )
    assert clean["by_horizon"]["6h"]["observed"] == 1
    assert clean["by_horizon"]["6h"][sparse.OBS_STATE_OUT_OF_BAND] == 0
    assert clean["by_horizon"]["6h"]["look_completion_rate"] == 1.0

    # now move that observation an hour outside its band, exactly as a manual
    # `observe_once` pass at the fractional tolerance would have written it
    obs.observed_at = NOW + timedelta(minutes=120)
    session.commit()
    dirty = sparse.build_observation_coverage_report(
        session, settings=settings(), now=at + timedelta(minutes=60),
    )
    six = dirty["by_horizon"]["6h"]
    assert six[sparse.OBS_STATE_OUT_OF_BAND] == 1
    assert six["observed"] == 0
    assert six["look_completion_rate"] == 0.0
    assert six["out_of_band_rate"] == 1.0
    distance = dirty["target_distance_seconds"]
    assert distance["max"] is None or (
        distance["max"] <= distance["band_half_width_seconds"]
    )


@pytest.mark.asyncio
async def test_the_target_distance_never_exceeds_the_band_half_width(session):
    """The invariant, stated once and asserted over every horizon of a mixed
    population — the number that would have disproved the fabricated
    `look_completion_rate 1.0`."""
    for n in range(1, 5):
        add_birth(session, n, anchor=NOW - timedelta(hours=6, minutes=15 * n))
    session.commit()
    pairs = {token_id(n): [pair(token_id(n))] for n in range(1, 5)}
    await run_pass(session, adapter=FakeAdapter(pairs), now=NOW)
    report = sparse.build_observation_coverage_report(
        session, settings=settings(), now=NOW + timedelta(hours=2),
    )
    d = report["target_distance_seconds"]
    assert d["max"] is not None
    assert d["max"] <= d["band_half_width_seconds"], report["by_horizon"]


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


def test_the_only_schema_change_is_one_additive_index():
    """This lane reuses `crypto_horizon_cohorts` / `_cohort_members` /
    `_observations` unchanged — no new table, no new column, no data change.

    It does add ONE index (0029, B7): the standing rolling cohort makes
    `cohort_id` non-selective, so the per-pass working-set query planned as a
    bare `SCAN crypto_horizon_cohort_members`. The branch used to claim "no
    migration"; this test states what the migration actually is, so the claim
    cannot quietly grow."""
    import pathlib
    import re

    versions = sorted(
        p.name for p in pathlib.Path("alembic/versions").glob("0*.py")
    )
    new = [v for v in versions if v.startswith(("0029", "0030"))]
    assert new == ["0029_horizon_member_cohort_added_at.py"], versions

    source = pathlib.Path(
        "alembic/versions/0029_horizon_member_cohort_added_at.py"
    ).read_text()
    forbidden = re.compile(
        r"\b(create_table|drop_table|add_column|drop_column|alter_column|"
        r"execute|bulk_insert)\b"
    )
    assert not forbidden.search(source), "0029 is not additive-index-only"
    assert source.count("op.create_index") == 1
    assert source.count("op.drop_index") == 1  # and it is reversible


def test_the_index_migration_round_trips(tmp_path):
    """Up and down, per docs/TESTING_POLICY.md."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from app.db import PROJECT_ROOT, run_migrations

    url = f"sqlite:///{tmp_path}/idx.db"
    run_migrations(url)

    def index_names() -> set[str]:
        engine = create_engine(url)
        try:
            return {
                ix["name"]
                for ix in inspect(engine).get_indexes("crypto_horizon_cohort_members")
            }
        finally:
            engine.dispose()

    assert "ix_horizon_member_cohort_added_at" in index_names()

    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.downgrade(config, "0028")
    assert "ix_horizon_member_cohort_added_at" not in index_names()
    # the unique index the double-enrolment guarantee depends on is untouched
    assert "ix_horizon_member_cohort_token" in index_names()
    command.upgrade(config, "0029")
    assert "ix_horizon_member_cohort_added_at" in index_names()



def test_the_cli_commands_are_registered():
    from app import cli

    parser = cli.build_parser()
    names = set(parser._subparsers._group_actions[0].choices)
    assert "crypto-sparse-observe" in names
    assert "crypto-observation-coverage-report" in names


def test_the_coverage_report_window_is_bounded_by_default(monkeypatch):
    """B6. `--hours None` — what the operator got by typing the command's name
    — is one dense column scan of the whole observation table. Measured at year
    1 on a 754MB fixture it stalls a co-tenant writer 3.0-3.7s; EVO's database
    is 4.55GB with 1.01x-5.80x documented load overshoot, and three clean runs
    at a 2s busy timeout on the smaller file is sampling luck, not a result.
    `--hours 48` measures 0.6-1.2s with a 506ms stall.

    So the DEFAULT is bounded and full history is an explicit `--all`. This
    asserts the value that actually reaches the builder, not the help text."""
    from app import cli

    seen: list = []

    async def _fake(hours=None, top=5, session=None):
        seen.append(hours)
        return 0

    monkeypatch.setattr(cli, "crypto_observation_coverage_report", _fake)

    assert cli.main(["crypto-observation-coverage-report"]) == 0
    assert seen == [sparse.DEFAULT_REPORT_HOURS], (
        "the bare command still reaches the unbounded whole-table scan"
    )
    assert sparse.DEFAULT_REPORT_HOURS >= sparse.MIN_REPORT_HOURS, (
        "the default window structurally nulls the 24h denominator"
    )

    # full history is still reachable, and still means "no window"
    seen.clear()
    assert cli.main(["crypto-observation-coverage-report", "--all"]) == 0
    assert seen == [None]

    # an explicit window still wins
    seen.clear()
    assert cli.main(["crypto-observation-coverage-report", "--hours", "48"]) == 0
    assert seen == [48]

    # and the two forms cannot be combined into an ambiguous request
    seen.clear()
    assert cli.main(
        ["crypto-observation-coverage-report", "--hours", "48", "--all"]
    ) == 1
    assert seen == [], "a conflicting request still ran"
