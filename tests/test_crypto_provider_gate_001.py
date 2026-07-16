"""CRYPTO-DISCOVERY-PROVIDER-GATE-001: explicit, fail-closed provider policy.

Everything is mocked (respx / stubs) — NO live provider validation anywhere,
including the July 15 regression configuration.
"""

import httpx
import pytest
import respx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import cli
from app.config import get_settings
from app.db import Base
from app.models import CryptoTokenRiskAssessment
from app.services.crypto_provider_policy import (
    Authorization,
    MandatoryProviderDeniedError,
    MissingPolicyError,
    PaidProviderNotConfirmedError,
    Provider,
    ProviderCapExhausted,
    ProviderDeniedError,
    ProviderNotPlannedError,
    ProviderPolicy,
    ProviderPolicyError,
    ProviderSkip,
    UnknownProviderError,
    canonical,
    current_context,
    guard_provider_request,
    provider_run,
    resolve_cli_policy,
)
from app.services.crypto_risk import (
    GoPlusSolanaRiskAdapter,
    SolanaTrackerRiskAdapter,
)
from app.services.crypto_risk_engine import CryptoRiskEngine, CryptoRiskProviderRegistry
from app.services.crypto_scout import CryptoDiscoveryService
from tests.test_crypto_arena import TOKEN_A, FakeDexAdapter

GOPLUS_PAYLOAD = {"result": {TOKEN_A: {"rug_pull": True, "top_10_holder_rate": "0.5"}}}
ST_PAYLOAD = {"risk": {"score": 6, "risks": [{"name": "Mint Authority"}]}}


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _enable(monkeypatch, *, engine=True, goplus=True, st=True, birdeye=False):
    s = get_settings()
    monkeypatch.setattr(s, "enable_crypto_risk_provider", False)
    monkeypatch.setattr(s, "enable_crypto_risk_engine", engine)
    monkeypatch.setattr(s, "enable_goplus_risk", goplus)
    monkeypatch.setattr(s, "enable_solana_tracker_risk", st)
    monkeypatch.setattr(s, "enable_birdeye_risk", birdeye)
    return s


def _service(monkeypatch, **kw) -> CryptoDiscoveryService:
    _enable(monkeypatch, **kw)
    engine = CryptoRiskEngine(registry=CryptoRiskProviderRegistry())
    return CryptoDiscoveryService(
        adapter=FakeDexAdapter(), risk_provider=None, risk_engine=engine
    )


def _mock_providers(stack):
    st = stack.get(url__startswith=SolanaTrackerRiskAdapter.API_BASE).mock(
        return_value=httpx.Response(200, json=ST_PAYLOAD)
    )
    gp = stack.get(url__startswith=GoPlusSolanaRiskAdapter.API_BASE).mock(
        return_value=httpx.Response(200, json=GOPLUS_PAYLOAD)
    )
    return st, gp


# --- canonical identifiers + closed enum ------------------------------------


def test_unknown_provider_identifier_fails_closed():
    with pytest.raises(UnknownProviderError):
        canonical("solanatracker")  # spoofed / non-canonical
    assert canonical("solana-tracker") is Provider.SOLANA_TRACKER


# --- fail-closed entry / context integrity ----------------------------------


async def test_scan_once_without_policy_fails_before_any_adapter_request(monkeypatch):
    service = _service(monkeypatch)
    with respx.mock as stack:
        st, gp = _mock_providers(stack)
        with pytest.raises(MissingPolicyError):
            await service.scan_once(object())  # no policy, no context
        assert st.call_count == 0 and gp.call_count == 0


async def test_guard_missing_context_fails_closed():
    assert current_context() is None
    with pytest.raises(MissingPolicyError):
        await guard_provider_request(Provider.DEXSCREENER)


async def test_ambient_and_explicit_policy_run_id_must_match(monkeypatch, session):
    service = _service(monkeypatch)
    other = ProviderPolicy.allow_all_for_tests(run_id="a")
    with provider_run(ProviderPolicy.allow_all_for_tests(run_id="b")):
        with pytest.raises(MissingPolicyError):
            await service.scan_once(session, policy=other)


# --- enforcement outcomes ---------------------------------------------------


async def test_denied_provider_hard_fails_before_http():
    policy = ProviderPolicy(
        run_id="r",
        allowed=frozenset({Provider.SOLANA_TRACKER}),
        denied=frozenset({Provider.SOLANA_TRACKER}),
        caps={},
        paid_confirmed=frozenset({Provider.SOLANA_TRACKER}),
    )
    with respx.mock as stack:
        st, _ = _mock_providers(stack)
        with provider_run(policy):
            with pytest.raises(ProviderDeniedError):
                await SolanaTrackerRiskAdapter().assess(TOKEN_A)
        assert st.call_count == 0  # no HTTP client ever opened


async def test_not_planned_and_unconfirmed_paid_are_hard():
    with provider_run(ProviderPolicy(
        run_id="r", allowed=frozenset({Provider.GOPLUS}), denied=frozenset(),
        caps={}, paid_confirmed=frozenset(),
    )):
        with pytest.raises(ProviderNotPlannedError):
            await guard_provider_request(Provider.BIRDEYE)
    with provider_run(ProviderPolicy(
        run_id="r", allowed=frozenset({Provider.SOLANA_TRACKER}), denied=frozenset(),
        caps={}, paid_confirmed=frozenset(),
    )):
        with pytest.raises(PaidProviderNotConfirmedError):
            await guard_provider_request(Provider.SOLANA_TRACKER)


async def test_cap_exhaustion_is_soft_skip_not_error_not_request():
    policy = ProviderPolicy(
        run_id="r", allowed=frozenset({Provider.GOPLUS}), denied=frozenset(),
        caps={Provider.GOPLUS: 1}, paid_confirmed=frozenset(),
    )
    with provider_run(policy) as ctx:
        await guard_provider_request(Provider.GOPLUS)  # 1st reserves
        with pytest.raises(ProviderCapExhausted) as exc:
            await guard_provider_request(Provider.GOPLUS)  # 2nd exhausts
        assert not isinstance(exc.value, ProviderPolicyError)  # soft, not hard
        assert ctx.ledger.skipped_cap[Provider.GOPLUS] == 1
        assert ctx.ledger.authorized[Provider.GOPLUS] == 1


async def test_mandatory_dexscreener_denial_rejects_before_scan(monkeypatch, session):
    service = _service(monkeypatch)
    policy = ProviderPolicy(
        run_id="r", allowed=frozenset({Provider.GOPLUS}),
        denied=frozenset({Provider.DEXSCREENER}), caps={},
        paid_confirmed=frozenset(),
    )
    with respx.mock as stack:
        st, gp = _mock_providers(stack)
        with pytest.raises(MandatoryProviderDeniedError):
            await service.scan_once(session, policy=policy)
        assert st.call_count == 0 and gp.call_count == 0


# --- July 15 regression -----------------------------------------------------


async def test_july15_regression_deny_solana_tracker_zero_dispatches(
    monkeypatch, session
):
    # provider flag False, engine flag True, SolanaTracker adapter reachable
    service = _service(monkeypatch, engine=True, goplus=True, st=True)
    assert any(
        a.name == "solana-tracker" for a in service.risk_engine.registry.adapters
    )
    policy = ProviderPolicy(
        run_id="r",
        allowed=frozenset({Provider.DEXSCREENER, Provider.GOPLUS, Provider.SOLANA_TRACKER}),
        denied=frozenset({Provider.SOLANA_TRACKER}),  # explicit denial
        caps={},
        paid_confirmed=frozenset({Provider.SOLANA_TRACKER}),
    )
    with respx.mock as stack:
        st, gp = _mock_providers(stack)
        with provider_run(policy) as ctx:
            run = await service.scan_once(session, policy=policy)
        assert run.status == "ok"
        assert st.call_count == 0                                   # zero HTTP starts
        assert ctx.ledger.started.get(Provider.SOLANA_TRACKER, 0) == 0
        assert ctx.ledger.authorized.get(Provider.SOLANA_TRACKER, 0) == 0
        assert ctx.ledger.blocked_policy.get(Provider.SOLANA_TRACKER, 0) >= 1
        assert gp.call_count >= 1  # GoPlus fallback still runs


async def test_optional_denial_no_silent_fallback_authorization(monkeypatch, session):
    # ST denied -> not dispatched; GoPlus still dispatched; no ST authorization
    service = _service(monkeypatch)
    policy = ProviderPolicy(
        run_id="r",
        allowed=frozenset({Provider.DEXSCREENER, Provider.GOPLUS}),
        denied=frozenset({Provider.SOLANA_TRACKER}),
        caps={}, paid_confirmed=frozenset(),
    )
    with respx.mock as stack:
        st, gp = _mock_providers(stack)
        with provider_run(policy) as ctx:
            await service.scan_once(session, policy=policy)
        assert st.call_count == 0
        assert Provider.SOLANA_TRACKER not in ctx.ledger.authorized


async def test_budget_exhaustion_records_skipped_budget(monkeypatch, session):
    # daily STOP threshold must be set BEFORE the engine captures its budget cfg
    monkeypatch.setattr(get_settings(), "solana_tracker_stop_daily_requests", 0)
    service = _service(monkeypatch)
    policy = ProviderPolicy.allow_all_for_tests(run_id="r")
    with respx.mock as stack:
        st, gp = _mock_providers(stack)
        with provider_run(policy) as ctx:
            await service.scan_once(session, policy=policy)
        assert st.call_count == 0  # daily STOP -> skipped, no request
        assert ctx.ledger.skipped_budget.get(Provider.SOLANA_TRACKER, 0) >= 1


# --- ledger accounting ------------------------------------------------------


async def test_ledger_distinguishes_authorized_started_succeeded(monkeypatch, session):
    service = _service(monkeypatch, st=False)  # GoPlus only, keep it simple
    policy = ProviderPolicy.allow_all_for_tests(run_id="r")
    with respx.mock as stack:
        _mock_providers(stack)
        with provider_run(policy) as ctx:
            await service.scan_once(session, policy=policy)
        gp = ctx.ledger.snapshot().get("goplus", {})
        assert gp["authorized"] == gp["started"] == gp["succeeded"] >= 1
        assert gp["failed"] == 0
        assert gp["blocked_policy"] == 0


# --- descriptors: single source; DexScreener max derivation -----------------


def test_dexscreener_max_requests_and_descriptor_identity(monkeypatch):
    service = _service(monkeypatch)
    descriptors = service.describe_providers(limit=40)
    dex = next(d for d in descriptors if d.provider is Provider.DEXSCREENER)
    assert dex.max_requests == 2 + 40  # profiles + boosts + per-token pairs
    assert dex.mandatory and not dex.paid
    # the SAME descriptors drive the CLI policy caps (no duplicated list)
    resolution = resolve_cli_policy(descriptors, confirm_paid=["solana-tracker"])
    assert resolution.policy.caps[Provider.DEXSCREENER] == dex.cap
    st = next(d for d in descriptors if d.provider is Provider.SOLANA_TRACKER)
    assert st.paid and resolution.policy.caps[Provider.SOLANA_TRACKER] == st.cap


# --- CLI contract -----------------------------------------------------------


async def test_bare_cli_prints_plan_and_makes_zero_calls(monkeypatch, session, capsys):
    service = _service(monkeypatch)
    with respx.mock as stack:
        st, gp = _mock_providers(stack)
        rc = await cli.crypto_scan_once(services=service, session=session)  # no --yes
        out = capsys.readouterr().out
        assert "provider plan" in out and "no provider was contacted" in out
        assert st.call_count == 0 and gp.call_count == 0
        assert "crypto scan #" not in out  # did NOT execute
        # ST enabled+selected but unconfirmed -> blocked plan -> rc=1
        assert rc == 1


async def test_provider_plan_flag_zero_calls_zero_writes(monkeypatch, session, capsys):
    service = _service(monkeypatch)
    before = session.execute(
        select(func.count()).select_from(CryptoTokenRiskAssessment)
    ).scalar()
    with respx.mock as stack:
        st, gp = _mock_providers(stack)
        await cli.crypto_scan_once(
            services=service, session=session, provider_plan=True
        )
        assert st.call_count == 0 and gp.call_count == 0
    after = session.execute(
        select(func.count()).select_from(CryptoTokenRiskAssessment)
    ).scalar()
    assert before == after  # zero writes


async def test_generic_yes_never_authorizes_paid(monkeypatch, session, capsys):
    service = _service(monkeypatch)  # ST enabled
    with respx.mock as stack:
        st, gp = _mock_providers(stack)
        rc = await cli.crypto_scan_once(services=service, session=session, yes=True)
        out = capsys.readouterr().out
        assert "not confirmed" in out and "crypto scan #" not in out
        assert st.call_count == 0  # paid provider never called under bare --yes
        assert rc == 1


async def test_deny_paid_allows_free_execution(monkeypatch, session, capsys):
    service = _service(monkeypatch)
    with respx.mock as stack:
        st, gp = _mock_providers(stack)
        rc = await cli.crypto_scan_once(
            services=service, session=session, yes=True,
            deny_provider=["solana-tracker"],
        )
        out = capsys.readouterr().out
        assert "crypto scan #" in out and rc == 0
        assert st.call_count == 0 and gp.call_count >= 1


async def test_confirm_paid_authorizes_solana_tracker(monkeypatch, session, capsys):
    service = _service(monkeypatch)
    with respx.mock as stack:
        st, gp = _mock_providers(stack)
        rc = await cli.crypto_scan_once(
            services=service, session=session, yes=True,
            confirm_paid_provider=["solana-tracker"],
        )
        assert rc == 0
        assert st.call_count >= 1  # now authorized


async def test_dexscreener_only_mode_is_honest(monkeypatch, session, capsys):
    service = _service(monkeypatch)
    with respx.mock as stack:
        st, gp = _mock_providers(stack)
        rc = await cli.crypto_scan_once(
            services=service, session=session, yes=True,
            allow_provider=["dexscreener"],
        )
        assert rc == 0
        assert st.call_count == 0 and gp.call_count == 0  # risk providers not planned


async def test_risk_assess_receives_identical_paid_enforcement(
    monkeypatch, session, capsys
):
    _enable(monkeypatch)
    engine = CryptoRiskEngine(registry=CryptoRiskProviderRegistry())
    with respx.mock as stack:
        st, gp = _mock_providers(stack)
        count = await cli.crypto_risk_assess(
            limit=5, engine=engine, session=session, yes=True
        )  # ST enabled, unconfirmed -> blocked, no execution
        out = capsys.readouterr().out
        assert "not confirmed" in out
        assert st.call_count == 0 and count == 0
