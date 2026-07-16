"""Crypto risk engine (CRYPTO-002) tests: provider adapters, heuristics,
composite scoring, registry fallback, discovery/signal integration, CLI,
API, and key redaction. Everything mocked — no live network."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import cli
from app.config import get_settings
from app.db import Base, get_db
from app.main import app
from app.models import (
    CryptoOpportunitySignal,
    CryptoPair,
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenRiskAssessment,
)
from app.services.crypto_risk import (
    GoPlusSolanaRiskAdapter,
    RiskAssessment,
    SolanaTrackerRiskAdapter,
)
from app.services.crypto_risk_engine import (
    CAT_BOOSTED,
    CAT_LIQUIDITY_REMOVED,
    CAT_LOW_LIQUIDITY,
    CAT_NEW_PAIR,
    CAT_PROVIDER_UNKNOWN,
    CAT_SUSPICIOUS_VOLUME,
    CryptoRiskEngine,
    CryptoRiskProviderRegistry,
    CryptoRiskReportService,
    HeuristicRiskEngine,
    RiskEngineConfig,
    composite_from,
    level_for,
)
from app.services.crypto_provider_policy import ProviderPolicy
from app.services.crypto_scout import CryptoScoutConfig, CryptoSignalService

_TEST_POLICY = ProviderPolicy.allow_all_for_tests()
from tests.test_crypto_arena import (
    PAIR_A,
    TOKEN_A,
    TOKEN_B,
    FakeDexAdapter,
    discovery,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _governed_provider_run():
    # GATE-001: these tests exercise real adapters/discovery in isolation; the
    # unconditional risk-adapter guard fails closed without a policy, so install
    # an explicit allow-all test context (never a service/adapter default).
    from app.services.crypto_provider_policy import provider_run

    with provider_run(_TEST_POLICY):
        yield


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def make_token(address=TOKEN_A, symbol="MEME", name="Meme Coin") -> CryptoToken:
    return CryptoToken(
        chain="solana",
        token_address=address,
        symbol=symbol,
        name=name,
        first_seen_at=NOW,
        last_seen_at=NOW,
        created_at=NOW,
    )


def make_pair(created_hours_ago=2.0, address=PAIR_A, token=TOKEN_A) -> CryptoPair:
    return CryptoPair(
        chain="solana",
        pair_address=address,
        base_token_address=token,
        pair_created_at=NOW - timedelta(hours=created_hours_ago),
        first_seen_at=NOW,
        created_at=NOW,
    )


def make_tick(
    liquidity=25_000.0,
    volume_5m=4_000.0,
    volume_24h=90_000.0,
    change_5m=3.0,
    boosts_active=None,
    token=TOKEN_A,
    pair=PAIR_A,
) -> CryptoPriceTick:
    return CryptoPriceTick(
        chain="solana",
        token_address=token,
        pair_address=pair,
        observed_at=NOW,
        price_usd=0.001,
        liquidity_usd=liquidity,
        volume_5m_usd=volume_5m,
        volume_24h_usd=volume_24h,
        price_change_5m=change_5m,
        raw_payload={"boosts_active": boosts_active},
        created_at=NOW,
    )


def heuristics(**cfg) -> HeuristicRiskEngine:
    return HeuristicRiskEngine(RiskEngineConfig(**cfg))


def evaluate(
    token=None,
    pair=None,
    tick=None,
    previous=None,
    provider_flags=None,
    pair_count=2,
    provider_backed=False,
    **cfg,
):
    return heuristics(**cfg).evaluate(
        token=token if token is not None else make_token(),
        pair=pair if pair is not None else make_pair(),
        tick=tick if tick is not None else make_tick(),
        previous=previous,
        provider_flags=provider_flags or {},
        pair_count=pair_count,
        provider_backed=provider_backed,
        now=NOW,
    )


GOPLUS_PAYLOAD = {
    "code": 1,
    "message": "OK",
    "result": {
        TOKEN_A: {
            "top_10_holder_rate": "0.45",
            "creator_percent": "0.05",
            "mintable": {"status": "1"},
            "freezable": {"status": "0"},
            "holder_count": "1,234",
        }
    },
}

SOLANA_TRACKER_PAYLOAD = {
    "token": {"name": "Meme Coin"},
    "risk": {
        "rugged": False,
        "score": 7,
        "risks": [{"name": "Freeze Authority"}, {"name": "Mint Authority"}],
        "snipers": {"percentage": 0.31},
        "top10": 0.62,
    },
}


class TestGoPlusAdapter:
    @respx.mock
    async def test_parses_solana_token_security_response(self):
        respx.get(url__startswith=GoPlusSolanaRiskAdapter.API_BASE).mock(
            return_value=httpx.Response(200, json=GOPLUS_PAYLOAD)
        )
        result = await GoPlusSolanaRiskAdapter(api_key="k").assess(TOKEN_A)
        assert result.provider == "goplus"
        assert result.flags["top10_holder_pct"] == 45.0
        assert result.flags["insider_pct"] == 5.0
        assert result.flags["mint_authority_enabled"] is True
        assert result.flags["freeze_authority_enabled"] is False
        assert result.flags["holder_count"] == 1234

    @respx.mock
    async def test_handles_429_http_error_and_drift(self):
        adapter = GoPlusSolanaRiskAdapter()
        route = respx.get(url__startswith=GoPlusSolanaRiskAdapter.API_BASE)
        route.mock(return_value=httpx.Response(429))
        assert await adapter.assess(TOKEN_A) is None
        route.mock(return_value=httpx.Response(500))
        assert await adapter.assess(TOKEN_A) is None
        route.mock(side_effect=httpx.ConnectError("no auth / no route"))
        assert await adapter.assess(TOKEN_A) is None
        route.mock(return_value=httpx.Response(200, json={"result": {TOKEN_A: {"???": 1}}}))
        assert await adapter.assess(TOKEN_A) is None  # nothing recognizable
        route.mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
        assert await adapter.assess(TOKEN_A) is None
        route.mock(return_value=httpx.Response(200, json=[]))
        assert await adapter.assess(TOKEN_A) is None


class TestSolanaTrackerAdapter:
    @respx.mock
    async def test_parses_risk_response(self):
        respx.get(url__startswith=SolanaTrackerRiskAdapter.API_BASE).mock(
            return_value=httpx.Response(200, json=SOLANA_TRACKER_PAYLOAD)
        )
        result = await SolanaTrackerRiskAdapter(api_key="k").assess(TOKEN_A)
        assert result.provider == "solana-tracker"
        assert result.risk_score == 0.7  # 7/10 normalized
        assert result.flags["mint_authority_enabled"] is True
        assert result.flags["freeze_authority_enabled"] is True
        assert result.flags["sniper_pct"] == 31.0
        assert result.flags["top10_holder_pct"] == 62.0

    @respx.mock
    async def test_handles_errors_and_drift(self):
        adapter = SolanaTrackerRiskAdapter()
        route = respx.get(url__startswith=SolanaTrackerRiskAdapter.API_BASE)
        route.mock(return_value=httpx.Response(429))
        assert await adapter.assess(TOKEN_A) is None
        route.mock(return_value=httpx.Response(200, json={"no": "risk key"}))
        assert await adapter.assess(TOKEN_A) is None


class TestHeuristics:
    def test_flags_low_liquidity(self):
        scores, reasons = evaluate(tick=make_tick(liquidity=900))
        assert CAT_LOW_LIQUIDITY in reasons
        assert scores["liquidity"] == 0.5

    def test_flags_liquidity_removed_as_severe_category(self):
        scores, reasons = evaluate(
            previous=make_tick(liquidity=40_000), tick=make_tick(liquidity=8_000)
        )
        assert CAT_LIQUIDITY_REMOVED in reasons
        composite, level = composite_from(scores, reasons)
        assert level == "severe"  # severe categories floor the composite

    def test_flags_young_pair(self):
        _, reasons = evaluate(pair=make_pair(created_hours_ago=0.01))  # 36s old
        assert CAT_NEW_PAIR in reasons
        _, reasons = evaluate(pair=make_pair(created_hours_ago=2))
        assert CAT_NEW_PAIR not in reasons

    def test_flags_suspicious_price_and_volume(self):
        _, reasons = evaluate(tick=make_tick(change_5m=80.0))
        assert "extreme_price_movement" in reasons
        _, reasons = evaluate(tick=make_tick(liquidity=10_000, volume_5m=25_000))
        assert CAT_SUSPICIOUS_VOLUME in reasons
        _, reasons = evaluate(tick=make_tick(liquidity=10_000, volume_24h=300_000))
        assert "fake_volume_suspected" in reasons

    def test_boost_is_context_not_automatic_severe(self):
        scores, reasons = evaluate(tick=make_tick(boosts_active=3))
        assert CAT_BOOSTED in reasons
        composite, level = composite_from(scores, reasons)
        assert level in ("low", "medium")  # context bump only

    def test_missing_metadata_flagged(self):
        _, reasons = evaluate(token=make_token(symbol=None, name=None))
        assert "missing_metadata" in reasons

    def test_holder_and_authority_scores_from_provider_flags(self):
        scores, reasons = evaluate(
            provider_flags={
                "top10_holder_pct": 45.0,
                "sniper_pct": 30.0,
                "insider_pct": 5.0,
                "mint_authority_enabled": True,
                "freeze_authority_enabled": False,
            },
            provider_backed=True,
        )
        assert "high_holder_concentration" in reasons
        assert "sniper_concentration" in reasons
        assert "insider_concentration" not in reasons  # 5 < 15 threshold
        assert "mint_authority_enabled" in reasons
        assert "freeze_authority_enabled" not in reasons
        assert scores["holder"] > 0.5
        assert scores["authority"] == 0.6

    def test_provider_unknown_when_heuristic_only(self):
        scores, reasons = evaluate(provider_backed=False)
        assert CAT_PROVIDER_UNKNOWN in reasons
        assert scores["provider"] is None

    def test_provider_rug_and_honeypot_flags(self):
        scores, reasons = evaluate(
            provider_flags={"rug_risk": True, "honeypot": True}, provider_backed=True
        )
        assert "provider_rug_flag" in reasons
        assert "provider_honeypot_flag" in reasons
        assert scores["provider"] == 1.0
        _, level = composite_from(scores, reasons)
        assert level == "severe"


class TestComposite:
    def test_level_bands(self):
        assert level_for(None) == "unknown"
        assert level_for(0.1) == "low"
        assert level_for(0.3) == "medium"
        assert level_for(0.6) == "high"
        assert level_for(0.8) == "severe"

    def test_weighted_mean_over_available_scores(self):
        composite, level = composite_from(
            {"liquidity": 0.5, "holder": None, "authority": None,
             "market_structure": 0.0, "manipulation": 0.0, "provider": None},
            [],
        )
        # (0.5*0.2) / (0.2+0.15+0.10) = 0.2222
        assert composite == pytest.approx(0.2222, abs=1e-4)
        assert level == "low"

    def test_nothing_measurable_is_unknown(self):
        composite, level = composite_from(
            {name: None for name in ("liquidity", "holder", "authority",
                                     "market_structure", "manipulation", "provider")},
            [],
        )
        assert composite is None and level == "unknown"


class ExplodingAdapter:
    name = "exploding"

    async def assess(self, token_address):
        raise RuntimeError("provider meltdown")


class StubAdapter:
    name = "stub"

    def __init__(self, flags=None, score=None):
        self.flags = flags or {}
        self.score = score

    async def assess(self, token_address):
        return RiskAssessment(
            provider=self.name,
            token_address=token_address,
            risk_score=self.score,
            flags=dict(self.flags),
        )


class TestRegistryAndEngine:
    def test_no_flags_means_no_adapters(self, monkeypatch):
        for flag in ("enable_goplus_risk", "enable_solana_tracker_risk"):
            monkeypatch.setattr(get_settings(), flag, False)
        registry = CryptoRiskProviderRegistry()
        assert registry.adapters == []
        assert registry.provider_backed is False

    def test_flags_select_adapters(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_goplus_risk", True)
        monkeypatch.setattr(get_settings(), "enable_solana_tracker_risk", True)
        registry = CryptoRiskProviderRegistry()
        names = {adapter.name for adapter in registry.adapters}
        assert names == {"goplus", "solana-tracker"}

    async def test_provider_failure_isolated(self):
        registry = CryptoRiskProviderRegistry(
            adapters=[ExplodingAdapter(), StubAdapter(flags={"rug_risk": True})]
        )
        results, errors = await registry.gather(TOKEN_A)
        assert len(results) == 1 and results[0].provider == "stub"
        assert "exploding" in errors

    async def test_engine_heuristic_only_persists_assessment(self, session):
        engine = CryptoRiskEngine(
            registry=CryptoRiskProviderRegistry(adapters=[]),
            config=RiskEngineConfig(),
            chain="solana",
        )
        evaluation = await engine.evaluate(
            session,
            token=make_token(),
            pair=make_pair(),
            tick=make_tick(liquidity=900),
            previous=None,
        )
        assert evaluation.composite_risk_level in ("low", "medium")
        assert CAT_LOW_LIQUIDITY in evaluation.reasons
        assert evaluation.provider_names == []
        row = session.execute(select(CryptoTokenRiskAssessment)).scalars().one()
        assert row.provider == "risk-engine"
        assert row.liquidity_risk_score == 0.5
        assert row.holder_risk_score is None
        assert row.composite_risk_level == evaluation.composite_risk_level
        assert CAT_PROVIDER_UNKNOWN in row.risk_reasons
        assert row.heuristic_version == "v1"

    async def test_engine_provider_failure_falls_back_to_heuristics(self, session):
        engine = CryptoRiskEngine(
            registry=CryptoRiskProviderRegistry(adapters=[ExplodingAdapter()]),
            config=RiskEngineConfig(),
            chain="solana",
        )
        evaluation = await engine.evaluate(
            session, token=make_token(), pair=make_pair(), tick=make_tick(), previous=None
        )
        assert evaluation.provider_errors["exploding"].startswith("RuntimeError")
        assert evaluation.composite_risk_level != "unknown"  # heuristics still ran
        row = session.execute(select(CryptoTokenRiskAssessment)).scalars().one()
        assert row.raw_payload["provider_errors"]["exploding"].startswith("RuntimeError")

    async def test_engine_merges_provider_facts_pessimistically(self, session):
        engine = CryptoRiskEngine(
            registry=CryptoRiskProviderRegistry(
                adapters=[
                    StubAdapter(flags={"top10_holder_pct": 30.0}),
                    StubAdapter(flags={"top10_holder_pct": 55.0, "honeypot": True}),
                ]
            ),
            config=RiskEngineConfig(),
            chain="solana",
        )
        evaluation = await engine.evaluate(
            session, token=make_token(), pair=make_pair(), tick=make_tick(), previous=None
        )
        assert evaluation.provider_flags["top10_holder_pct"] == 55.0
        assert evaluation.composite_risk_level == "severe"  # honeypot floors it


def engine_with(adapters) -> CryptoRiskEngine:
    return CryptoRiskEngine(
        registry=CryptoRiskProviderRegistry(adapters=adapters),
        config=RiskEngineConfig(),
        chain="solana",
    )


class TestDiscoveryIntegration:
    async def test_scan_with_engine_persists_engine_assessments(self, session):
        service = discovery(risk_provider=None)
        service.risk_provider = None
        service.risk_engine = engine_with([])
        run = await service.scan_once(session, policy=_TEST_POLICY)
        assert run.status == "ok"
        rows = session.execute(select(CryptoTokenRiskAssessment)).scalars().all()
        assert {row.token_address for row in rows} == {TOKEN_A, TOKEN_B}
        assert all(row.provider == "risk-engine" for row in rows)
        # healthy mock pairs -> no risk-type signals fired
        risk_types = {"rug_risk", "holder_risk", "suspicious_supply_control"}
        signals = session.execute(select(CryptoOpportunitySignal)).scalars().all()
        assert not [s for s in signals if s.signal_type in risk_types]

    async def test_dangerous_provider_facts_fire_all_risk_signals(self, session):
        service = discovery(risk_provider=None)
        service.risk_provider = None
        service.risk_engine = engine_with(
            [
                StubAdapter(
                    flags={
                        "rug_risk": True,
                        "top10_holder_pct": 80.0,
                        "mint_authority_enabled": True,
                    }
                )
            ]
        )
        await service.scan_once(session, policy=_TEST_POLICY)
        types = {
            s.signal_type
            for s in session.execute(select(CryptoOpportunitySignal)).scalars().all()
        }
        assert {"rug_risk", "holder_risk", "suspicious_supply_control"} <= types

    async def test_engine_disabled_preserves_crypto001_behavior(self, session):
        service = discovery()  # mock provider, no engine
        assert service.risk_engine is None  # flag off by default
        await service.scan_once(session, policy=_TEST_POLICY)
        rows = session.execute(select(CryptoTokenRiskAssessment)).scalars().all()
        assert all(row.provider == "mock" for row in rows)
        assert all(row.composite_risk_score is None for row in rows)

    async def test_no_risk_source_keeps_risk_signals_inactive(self, session):
        service = discovery(risk_provider=None)
        service.risk_provider = None
        service.risk_engine = None
        await service.scan_once(session, policy=_TEST_POLICY)
        risk_types = {"rug_risk", "holder_risk", "suspicious_supply_control"}
        signals = session.execute(select(CryptoOpportunitySignal)).scalars().all()
        assert not [s for s in signals if s.signal_type in risk_types]
        assert session.execute(select(CryptoTokenRiskAssessment)).scalars().all() == []


class TestCli:
    async def test_crypto_risk_assess_cli(self, session, capsys):
        seeder = discovery()
        seeder.risk_provider = None  # seed tokens/ticks only, no assessments
        await seeder.scan_once(session, policy=_TEST_POLICY)
        count = await cli.crypto_risk_assess(limit=10, engine=engine_with([]), session=session, yes=True)
        assert count == 2
        output = capsys.readouterr().out
        assert "assessed 2 token(s)" in output
        assert "level=" in output
        rows = session.execute(select(CryptoTokenRiskAssessment)).scalars().all()
        assert len(rows) == 2

    async def test_crypto_risk_assess_creates_risk_signals_for_danger(self, session, capsys):
        await discovery(risk_provider=None).scan_once(session, policy=_TEST_POLICY)
        engine = engine_with([StubAdapter(flags={"rug_risk": True})])
        await cli.crypto_risk_assess(limit=10, engine=engine, session=session, yes=True)
        output = capsys.readouterr().out
        assert "risk signal(s)" in output
        types = {
            s.signal_type
            for s in session.execute(select(CryptoOpportunitySignal)).scalars().all()
        }
        assert "rug_risk" in types

    async def test_crypto_risk_report_cli(self, session, capsys, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_crypto_risk_engine", True)
        await discovery(risk_provider=None).scan_once(session, policy=_TEST_POLICY)
        await cli.crypto_risk_assess(limit=10, engine=engine_with([]), session=session, yes=True)
        capsys.readouterr()
        total = await cli.crypto_risk_report(session=session)
        assert total == 2
        output = capsys.readouterr().out
        assert "crypto risk: engine=heuristic-only (heuristics v1)" in output
        assert "by level:" in output
        assert "common reasons:" in output
        assert "provider_unknown" in output

    async def test_crypto_report_mentions_engine_mode(self, session, capsys, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_crypto_risk_engine", False)
        await cli.crypto_report(session=session)
        assert "risk engine: disabled" in capsys.readouterr().out

    def test_main_wires_risk_commands(self, monkeypatch):
        captured = []

        async def fake(*args, **kwargs):
            captured.append(kwargs)
            return 0

        monkeypatch.setattr(cli, "crypto_risk_assess", fake)
        monkeypatch.setattr(cli, "crypto_risk_report", fake)
        assert cli.main(["crypto-risk-assess", "--limit", "5"]) == 0
        assert cli.main(["crypto-risk-report"]) == 0
        assert len(captured) == 2

    async def test_agent_context_redacts_api_keys(self, capsys, monkeypatch):
        monkeypatch.setattr(get_settings(), "goplus_api_key", "SECRET-GOPLUS-KEY-123")
        monkeypatch.setattr(
            get_settings(), "solana_tracker_api_key", "SECRET-TRACKER-KEY-456"
        )
        exit_code = await cli.agent_context()
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "SECRET-GOPLUS-KEY-123" not in output
        assert "SECRET-TRACKER-KEY-456" not in output


class TestRiskReportService:
    async def test_report_ranks_and_explains(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_crypto_risk_engine", True)
        monkeypatch.setattr(get_settings(), "enable_goplus_risk", False)
        monkeypatch.setattr(get_settings(), "enable_solana_tracker_risk", False)
        await discovery(risk_provider=None).scan_once(session, policy=_TEST_POLICY)
        # one clean heuristic pass + one dangerous provider-backed pass
        await cli.crypto_risk_assess(limit=1, engine=engine_with([]), session=session, yes=True)
        dangerous = engine_with(
            [StubAdapter(flags={"rug_risk": True, "top10_holder_pct": 90.0})]
        )
        await cli.crypto_risk_assess(limit=10, engine=dangerous, session=session, yes=True)

        report = CryptoRiskReportService().build(session)
        assert report.engine_mode == "heuristic-only"  # provider flags off
        assert report.tokens_assessed == 2
        assert report.by_level.get("severe", 0) >= 1
        assert report.top_risky_tokens
        top = report.top_risky_tokens[0]
        assert top.composite_risk_level == "severe"
        assert "provider_rug_flag" in (top.risk_reasons or [])
        assert "provider_rug_flag" in report.common_reasons
        assert report.risk_signals_created.get("rug_risk", 0) >= 1

    def test_empty_report(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_crypto_risk_engine", False)
        report = CryptoRiskReportService().build(session)
        assert report.engine_mode == "disabled"
        assert report.assessments_total == 0
        assert report.top_risky_tokens == []


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app), session
    app.dependency_overrides.clear()


class TestApi:
    async def test_risk_endpoints(self, client, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_crypto_risk_engine", True)
        test_client, session = client
        seeder = discovery()
        seeder.risk_provider = None
        seeder.risk_engine = None  # seed only; flag is monkeypatched on
        await seeder.scan_once(session, policy=_TEST_POLICY)
        await cli.crypto_risk_assess(
            limit=10,
            engine=engine_with([StubAdapter(flags={"rug_risk": True})]),
            session=session,
            yes=True,
        )

        assessments = test_client.get("/crypto/risk-assessments").json()
        assert len(assessments) == 2
        assert all("raw_payload" not in a for a in assessments)
        assert all(a["composite_risk_level"] == "severe" for a in assessments)

        severe_only = test_client.get("/crypto/risk-assessments?risk_level=severe").json()
        assert len(severe_only) == 2

        token_risk = test_client.get(f"/crypto/tokens/{TOKEN_A}/risk").json()
        assert token_risk["token_address"] == TOKEN_A
        assert "provider_rug_flag" in token_risk["risk_reasons"]
        assert test_client.get("/crypto/tokens/UNKNOWN/risk").status_code == 404

        report = test_client.get("/crypto/risk-report").json()
        assert report["engine_mode"] == "heuristic-only"
        assert report["by_level"]["severe"] == 2
        assert report["risk_signals_created"]["rug_risk"] >= 1

    def test_risk_endpoints_empty(self, client):
        test_client, _ = client
        assert test_client.get("/crypto/risk-assessments").json() == []
        assert test_client.get("/crypto/tokens/NOPE/risk").status_code == 404
        report = test_client.get("/crypto/risk-report").json()
        assert report["assessments_total"] == 0
