"""MEME-NEWS-001 tests: read-only meme attention scout (token/boost detection,
velocity + risk-penalty scoring, catalyst persistence, graceful provider
degradation) and the domain-expansion scout (grouping + canary priority).
No live network; in-memory SQLite; mocked adapter."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.adapters.dexscreener import PairData, TokenProfile
from app.config import Settings
from app.db import Base
from app.models import (
    CryptoTokenRiskAssessment,
    Market,
    MarketResolutionAssessment,
    MarketSnapshot,
    MemeAttentionSnapshot,
    MemeCatalystEvent,
    MemeScoutRun,
)
from app.services.domain_scout import DomainScoutService
from app.services.meme_scout import (
    CatalystReportService,
    MemeScoutConfig,
    MemeScoutReportService,
    MemeScoutService,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


# --- fakes ------------------------------------------------------------------

def profile(addr, *, url="http://token", desc="a token", boost=None, links=None):
    return TokenProfile(
        chain="solana", token_address=addr, url=url, description=desc,
        boost_amount=boost, raw={"links": links or []},
    )


def pair(addr, *, liq=1000.0, v1h=500.0, sym="TKN", name="Token"):
    return PairData(
        chain="solana", pair_address=addr + "-p", base_token_address=addr,
        base_token_symbol=sym, base_token_name=name, liquidity_usd=liq,
        volume_5m_usd=100.0, volume_1h_usd=v1h, volume_24h_usd=5000.0,
        price_usd=0.01, price_change_5m=1.0, price_change_1h=2.0,
    )


class FakeDex:
    source_name = "dexscreener"

    def __init__(self, profiles=None, boosts=None, pairs=None, fail=False):
        self._profiles = profiles or []
        self._boosts = boosts or []
        self._pairs = pairs or {}
        self._fail = fail  # simulate provider outage: empty lists (adapter never raises)

    async def fetch_latest_token_profiles(self):
        return [] if self._fail else list(self._profiles)

    async def fetch_latest_boosted_tokens(self):
        return [] if self._fail else list(self._boosts)

    async def fetch_pairs_for_token(self, addr):
        return [] if self._fail else list(self._pairs.get(addr, []))


def scout(**kw) -> MemeScoutService:
    return MemeScoutService(adapter=FakeDex(**kw), config=MemeScoutConfig(limit=30))


# --- Part A: meme attention scout -------------------------------------------

class TestMemeScout:
    def test_newest_token_detection_and_persist(self, session):
        svc = scout(
            profiles=[profile("AAA"), profile("BBB")],
            pairs={"AAA": [pair("AAA")], "BBB": [pair("BBB")]},
        )
        run = asyncio.run(svc.scan_once(session))
        assert run.status == "ok"
        assert run.profiles_seen == 2 and run.tokens_scored == 2
        snaps = session.query(MemeAttentionSnapshot).all()
        assert {s.token_address for s in snaps} == {"AAA", "BBB"}
        assert all(0.0 <= s.attention_score <= 1.0 for s in snaps)

    def test_boost_detection_records_catalyst(self, session):
        svc = scout(
            boosts=[profile("BOOST", boost=500.0)],
            pairs={"BOOST": [pair("BOOST")]},
        )
        asyncio.run(svc.scan_once(session))
        snap = session.query(MemeAttentionSnapshot).filter_by(token_address="BOOST").one()
        assert snap.boost_amount == 500.0
        boost_cats = session.query(MemeCatalystEvent).filter_by(catalyst_type="boost").all()
        assert boost_cats and boost_cats[0].magnitude == 500.0
        types = {c.catalyst_type for c in session.query(MemeCatalystEvent).all()}
        assert "boost" in types and "profile_seen" in types

    def test_velocity_scoring_from_previous_snapshot(self, session):
        # prior snapshot 2h ago: boost 100, liquidity 1000, vol1h 200
        session.add(
            MemeAttentionSnapshot(
                chain="solana", token_address="VEL", boost_amount=100.0,
                liquidity_usd=1000.0, volume_1h_usd=200.0,
                observed_at=NOW - timedelta(hours=2), created_at=NOW - timedelta(hours=2),
                has_social=False,
            )
        )
        session.commit()
        svc = scout(
            boosts=[profile("VEL", boost=300.0)],
            pairs={"VEL": [pair("VEL", liq=2000.0, v1h=400.0)]},
        )
        asyncio.run(svc.scan_once(session))
        snap = (
            session.query(MemeAttentionSnapshot)
            .filter_by(token_address="VEL").order_by(MemeAttentionSnapshot.id.desc()).first()
        )
        assert snap.liquidity_growth == pytest.approx(1.0)   # 1000 -> 2000
        assert snap.volume_growth == pytest.approx(1.0)      # 200 -> 400
        assert snap.boost_velocity == pytest.approx(100.0, rel=1e-2)   # (300-100)/2h
        assert any(
            c.catalyst_type == "boost_increase"
            for c in session.query(MemeCatalystEvent).all()
        )

    def test_risk_penalty_lowers_attention(self, session):
        # identical tokens; SAFE has low risk, DANGER severe — same provider conf
        for addr, level in (("SAFE", "low"), ("DANGER", "severe")):
            session.add(
                CryptoTokenRiskAssessment(
                    chain="solana", token_address=addr, provider="goplus",
                    composite_risk_level=level, composite_risk_score=0.1 if level == "low" else 0.9,
                    provider_names=["goplus"], created_at=NOW,
                )
            )
        session.commit()
        svc = scout(
            profiles=[profile("SAFE"), profile("DANGER")],
            pairs={"SAFE": [pair("SAFE")], "DANGER": [pair("DANGER")]},
        )
        asyncio.run(svc.scan_once(session))
        safe = session.query(MemeAttentionSnapshot).filter_by(token_address="SAFE").one()
        danger = session.query(MemeAttentionSnapshot).filter_by(token_address="DANGER").one()
        assert danger.risk_level == "severe"
        assert danger.attention_score < safe.attention_score

    def test_social_presence_scored_and_catalyst(self, session):
        svc = scout(
            profiles=[profile("SOC", links=[{"type": "twitter", "url": "x"}, {"type": "tg", "url": "y"}])],
            pairs={"SOC": [pair("SOC")]},
        )
        asyncio.run(svc.scan_once(session))
        snap = session.query(MemeAttentionSnapshot).filter_by(token_address="SOC").one()
        assert snap.has_social and snap.social_links_count == 2
        assert any(c.catalyst_type == "social_present" for c in session.query(MemeCatalystEvent).all())

    def test_provider_failure_degrades_gracefully(self, session):
        run = asyncio.run(scout(fail=True).scan_once(session))
        assert run.status == "ok"
        assert run.tokens_scored == 0
        assert session.query(MemeAttentionSnapshot).count() == 0

    def test_limit_caps_tokens_scored(self, session):
        profs = [profile(f"T{i}") for i in range(5)]
        pairs = {f"T{i}": [pair(f"T{i}")] for i in range(5)}
        svc = MemeScoutService(adapter=FakeDex(profiles=profs, pairs=pairs),
                               config=MemeScoutConfig(limit=2))
        run = asyncio.run(svc.scan_once(session))
        assert run.tokens_scored == 2

    def test_reports_build(self, session):
        asyncio.run(scout(
            boosts=[profile("R", boost=200.0, links=[{"url": "x"}])],
            pairs={"R": [pair("R")]},
        ).scan_once(session))
        mr = MemeScoutReportService().build(session)
        assert mr.total_snapshots == 1 and mr.top_attention
        cr = CatalystReportService().build(session)
        assert cr.total >= 2 and "profile_seen" in cr.by_type


# --- Part C: domain-expansion scout -----------------------------------------

def seed_market(session, ticker, *, title="", category=None, status="active",
                yes_bid=40, yes_ask=45, volume_24h=1000, liquidity=5000, clarity=None):
    m = Market(ticker=ticker, title=title, category=category, status=status)
    session.add(m)
    session.commit()
    session.add(
        MarketSnapshot(
            market_id=m.id, yes_bid=yes_bid, yes_ask=yes_ask, no_bid=100 - (yes_ask or 0),
            no_ask=100 - (yes_bid or 0), volume_24h=volume_24h, liquidity=liquidity,
        )
    )
    if clarity is not None:
        session.add(
            MarketResolutionAssessment(
                market_ticker=ticker, model_name="rule", clarity_score=clarity,
                resolution_risk="low", tradeability="researchable",
            )
        )
    session.commit()
    return m


class TestDomainScout:
    def test_domain_grouping(self, session):
        seed_market(session, "KXMLBGAME-A", title="MLB game")
        seed_market(session, "KXMLBGAME-B", title="MLB game")
        seed_market(session, "KXHIGHNYC-A", title="High temp NYC")
        seed_market(session, "KXNBA-A", title="NBA game")  # general -> basketball hint
        report = DomainScoutService().build(session, persist=True)
        by = {d["domain"]: d for d in report.domains}
        assert by["sports_baseball"]["market_count"] == 2
        assert "weather" in by
        assert "basketball" in by  # surfaced from series-prefix hint
        assert report.markets_scanned == 4

    def test_priority_scoring_and_forecaster_gap(self, session):
        # baseball (has forecaster) vs weather (no forecaster) with real supply
        for i in range(4):
            seed_market(session, f"KXMLBGAME-{i}", title="MLB", clarity=0.9)
        for i in range(4):
            seed_market(session, f"KXHIGH-{i}", title="temperature", clarity=0.95)
        report = DomainScoutService().build(session, persist=True)
        by = {d["domain"]: d for d in report.domains}
        assert by["sports_baseball"]["has_evidence_forecaster"] is True
        assert by["sports_baseball"]["priority_components"]["forecaster_gap"] == 0.0
        assert by["weather"]["has_evidence_forecaster"] is False
        assert by["weather"]["priority_components"]["forecaster_gap"] == 1.0
        # domains are returned ranked by priority
        priorities = [d["canary_priority"] for d in report.domains]
        assert priorities == sorted(priorities, reverse=True)

    def test_persists_run_and_inventory(self, session):
        seed_market(session, "KXMLBGAME-A", title="MLB")
        report = DomainScoutService().build(session, persist=True)
        from app.models import DomainMarketInventorySnapshot, DomainScoutRun
        assert session.query(DomainScoutRun).count() == 1
        assert session.query(DomainMarketInventorySnapshot).count() == len(report.domains)


# --- flags + CLI wiring -----------------------------------------------------

def test_flags_default_off_but_manual_scan_allowed(session):
    s = Settings(_env_file=None)
    assert s.enable_meme_scout is False
    assert s.enable_domain_scout is False
    # manual scan runs regardless of the (off) flag, mirroring the crypto lane
    run = asyncio.run(scout(profiles=[profile("Z")], pairs={"Z": [pair("Z")]}).scan_once(session))
    assert run.status == "ok"


def test_main_wires_meme_and_domain_commands(monkeypatch):
    captured = {}

    async def fake_scan(limit=None, service=None, session=None):
        captured["limit"] = limit
        return 3

    async def fake_domain(session=None):
        captured["domain"] = True
        return 5

    monkeypatch.setattr(cli, "meme_scan_once", fake_scan)
    monkeypatch.setattr(cli, "domain_scout_report", fake_domain)
    assert cli.main(["meme-scan-once", "--limit", "7"]) == 0
    assert captured["limit"] == 7
    assert cli.main(["domain-scout-report"]) == 0
    assert captured["domain"] is True


def test_cli_meme_and_catalyst_reports_print(session, capsys):
    asyncio.run(scout(boosts=[profile("P", boost=100.0)], pairs={"P": [pair("P")]}).scan_once(session))
    asyncio.run(cli.meme_scout_report(session=session))
    asyncio.run(cli.catalyst_report(session=session))
    out = capsys.readouterr().out
    assert "meme scout" in out and "catalyst events" in out
