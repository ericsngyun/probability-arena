"""MEME-RISK-003 tests: Birdeye holder/creator provider adapter (success,
missing key, error), creator-concentration heuristic, provider registry wiring,
provider health report (explicit coverage gaps), and meme-risk coverage.
Read-only risk intelligence — no live network, no trade semantics."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.services.crypto_provider_health as cph
from app import cli
from app.config import Settings
from app.db import Base
from app.models import CryptoTokenRiskAssessment, MemeAttentionSnapshot
from app.services.crypto_provider_health import (
    CryptoProviderHealthReportService,
    MemeRiskCoverageReportService,
)
from app.services.crypto_risk import BirdeyeRiskAdapter
from app.services.crypto_risk_engine import (
    CAT_BUNDLER,
    CAT_CREATOR_CONCENTRATION,
    CAT_SNIPER,
    CryptoRiskProviderRegistry,
    HeuristicRiskEngine,
)

NOW = datetime.now(timezone.utc)
TOKEN = "So11111111111111111111111111111111111111112"

BIRDEYE_PAYLOAD = {
    "data": {
        "top10HolderPercent": 0.55,
        "creatorPercentage": 0.28,
        "holderCount": "2,015",
        "mutableMetadata": True,
        "freezeable": False,
    }
}


@pytest.fixture(autouse=True)
def _governed_provider_run():
    # GATE-001: real Birdeye adapter guard fails closed without a policy.
    from app.services.crypto_provider_policy import ProviderPolicy, provider_run

    with provider_run(ProviderPolicy.allow_all_for_tests()):
        yield


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def settings(**kw):
    return Settings(_env_file=None, **kw)


# --- Birdeye adapter --------------------------------------------------------

class TestBirdeyeAdapter:
    def test_parse_success_holder_and_creator(self):
        a = BirdeyeRiskAdapter(api_key="k")
        r = a.parse(TOKEN, BIRDEYE_PAYLOAD)
        assert r.provider == "birdeye"
        assert r.flags["top10_holder_pct"] == 55.0
        assert r.flags["creator_pct"] == 28.0
        assert r.flags["holder_count"] == 2015
        assert r.flags["mint_authority_enabled"] is True
        assert r.flags["freeze_authority_enabled"] is False

    def test_schema_drift_returns_none(self):
        assert BirdeyeRiskAdapter().parse(TOKEN, {"data": {"unrelated": 1}}) is None
        assert BirdeyeRiskAdapter().parse(TOKEN, {"nope": 1}) is None

    @respx.mock
    async def test_assess_missing_key_still_works(self):
        # no key -> unauthenticated GET (no X-API-KEY header); success parses
        route = respx.get(url__startswith=BirdeyeRiskAdapter.API_BASE).mock(
            return_value=httpx.Response(200, json=BIRDEYE_PAYLOAD)
        )
        r = await BirdeyeRiskAdapter(api_key="").assess(TOKEN)
        assert r is not None and r.flags["creator_pct"] == 28.0
        assert "X-API-KEY" not in route.calls[0].request.headers

    @respx.mock
    async def test_assess_http_error_degrades_to_none(self):
        respx.get(url__startswith=BirdeyeRiskAdapter.API_BASE).mock(
            return_value=httpx.Response(500)
        )
        assert await BirdeyeRiskAdapter(api_key="k").assess(TOKEN) is None

    @respx.mock
    async def test_assess_rate_limit_degrades_to_none(self):
        respx.get(url__startswith=BirdeyeRiskAdapter.API_BASE).mock(
            return_value=httpx.Response(429)
        )
        assert await BirdeyeRiskAdapter().assess(TOKEN) is None


# --- heuristic: creator + sniper/bundler ------------------------------------

class TestHeuristic:
    def test_creator_concentration_fires(self):
        scores, reasons = HeuristicRiskEngine().evaluate(
            token=None, pair=None, tick=None, previous=None,
            provider_flags={"creator_pct": 40.0}, provider_backed=True,
        )
        assert CAT_CREATOR_CONCENTRATION in reasons
        assert scores["holder"] and scores["holder"] > 0

    def test_creator_below_threshold_does_not_fire(self):
        _, reasons = HeuristicRiskEngine().evaluate(
            token=None, pair=None, tick=None, previous=None,
            provider_flags={"creator_pct": 5.0}, provider_backed=True,
        )
        assert CAT_CREATOR_CONCENTRATION not in reasons

    def test_sniper_bundler_creator_together(self):
        _, reasons = HeuristicRiskEngine().evaluate(
            token=None, pair=None, tick=None, previous=None,
            provider_flags={"sniper_pct": 30.0, "bundler_pct": 40.0, "creator_pct": 25.0},
            provider_backed=True,
        )
        assert {CAT_SNIPER, CAT_BUNDLER, CAT_CREATOR_CONCENTRATION} <= set(reasons)


# --- registry wiring --------------------------------------------------------

class TestRegistry:
    def test_birdeye_added_when_enabled(self):
        reg = CryptoRiskProviderRegistry(settings=settings(enable_birdeye_risk=True))
        assert any(a.name == "birdeye" for a in reg.adapters)

    def test_birdeye_absent_when_disabled(self):
        reg = CryptoRiskProviderRegistry(settings=settings(enable_goplus_risk=True))
        assert not any(a.name == "birdeye" for a in reg.adapters)
        assert any(a.name == "goplus" for a in reg.adapters)


# --- provider health report (explicit gaps) ---------------------------------

def seed_assessment(session, token, *, flags, providers, provider_errors=None):
    session.add(CryptoTokenRiskAssessment(
        chain="solana", token_address=token, provider="risk-engine",
        composite_risk_level="low", composite_risk_score=0.1,
        flags=flags, provider_names=providers,
        raw_payload={"provider_errors": provider_errors or {}},
        created_at=NOW,
    ))
    session.commit()


class TestProviderHealth:
    def test_goplus_only_shows_sniper_bundler_creator_gaps(self, session, monkeypatch):
        monkeypatch.setattr(cph, "get_settings",
                            lambda: settings(enable_crypto_risk_engine=True, enable_goplus_risk=True))
        r = CryptoProviderHealthReportService().build(session)
        assert r.engine_mode == "provider-backed"
        # goplus covers top10_holder + insider; sniper/bundler/creator are gaps
        assert set(r.coverage_gaps) == {"sniper", "bundler", "creator"}
        by_name = {p["name"]: p for p in r.providers}
        assert by_name["goplus"]["status"] == "active"
        assert by_name["solana-tracker"]["status"] == "disabled"
        assert by_name["helius"]["status"] == "reserved"

    def test_enabling_birdeye_and_solanatracker_closes_gaps(self, session, monkeypatch):
        monkeypatch.setattr(cph, "get_settings", lambda: settings(
            enable_crypto_risk_engine=True, enable_goplus_risk=True,
            enable_solana_tracker_risk=True, enable_birdeye_risk=True,
        ))
        r = CryptoProviderHealthReportService().build(session)
        assert r.coverage_gaps == []  # all holder dimensions now covered
        assert "creator" in r.covered_dimensions and "birdeye" in r.covered_dimensions["creator"]
        assert "sniper" in r.covered_dimensions

    def test_observed_coverage_counts_real_flag_presence(self, session, monkeypatch):
        monkeypatch.setattr(cph, "get_settings",
                            lambda: settings(enable_crypto_risk_engine=True, enable_goplus_risk=True))
        seed_assessment(session, "T1", flags={"top10_holder_pct": 40.0}, providers=["goplus"])
        seed_assessment(session, "T2", flags={"top10_holder_pct": 30.0, "creator_pct": 20.0},
                        providers=["goplus", "birdeye"])
        r = CryptoProviderHealthReportService().build(session)
        assert r.observed_coverage["top10_holder"]["covered"] == 2
        assert r.observed_coverage["creator"]["covered"] == 1
        assert r.observed_coverage["sniper"]["covered"] == 0
        assert r.provider_use.get("birdeye") == 1


# --- meme-risk coverage -----------------------------------------------------

class TestMemeRiskCoverage:
    def test_coverage_over_meme_lane(self, session):
        # token A has full provider data; token B has none (gap)
        seed_assessment(session, "A", flags={"top10_holder_pct": 50.0, "sniper_pct": 30.0,
                        "creator_pct": 22.0}, providers=["solana-tracker", "birdeye"])
        for tk, conf in (("A", 1.0), ("B", 0.25)):
            session.add(MemeAttentionSnapshot(
                chain="solana", token_address=tk, attention_score=0.4, provider_confidence=conf,
                observed_at=NOW - timedelta(minutes=10), created_at=NOW - timedelta(minutes=10),
            ))
        session.commit()
        r = MemeRiskCoverageReportService().build(session, hours=24)
        assert r.tokens == 2
        assert r.with_provider_data == 1 and r.missing_provider_data == 1
        assert r.by_dimension["sniper"]["covered"] == 1
        assert r.by_dimension["bundler"]["covered"] == 0
        assert "bundler" in r.coverage_gaps and "creator" not in r.coverage_gaps


# --- CLI wiring -------------------------------------------------------------

def test_main_wires_new_reports(monkeypatch):
    captured = {}

    async def fake_health(session=None):
        captured["health"] = True
        return 0

    async def fake_cov(hours=24, session=None):
        captured["hours"] = hours
        return 3

    monkeypatch.setattr(cli, "crypto_provider_health_report", fake_health)
    monkeypatch.setattr(cli, "meme_risk_coverage_report", fake_cov)
    assert cli.main(["crypto-provider-health-report"]) == 0
    assert captured["health"] is True
    assert cli.main(["meme-risk-coverage-report", "--hours", "12"]) == 0
    assert captured["hours"] == 12


def test_cli_health_report_prints_gaps(session, capsys, monkeypatch):
    monkeypatch.setattr(cph, "get_settings",
                        lambda: settings(enable_crypto_risk_engine=True, enable_goplus_risk=True))
    asyncio.run(cli.crypto_provider_health_report(session=session))
    out = capsys.readouterr().out
    assert "COVERAGE GAPS" in out and "sniper" in out
    assert "crypto provider health" in out
