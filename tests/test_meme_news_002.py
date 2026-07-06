"""MEME-NEWS-002 tests: scheduled runner wraps the scout, flag gates the
scheduled path (manual always allowed), graceful provider/error handling, the
windowed report, derived alerts, retention pruning, and systemd artifacts.
No live network; in-memory SQLite."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.config as config_module
import app.services.meme_news as meme_news_module
from app import cli
from app.adapters.dexscreener import PairData, TokenProfile
from app.config import Settings
from app.db import Base
from app.models import (
    DomainMarketInventorySnapshot,
    DomainScoutRun,
    MemeAttentionSnapshot,
    MemeCatalystEvent,
    MemeScoutRun,
)
from app.services.meme_news import (
    MemeNewsAlertService,
    MemeNewsConfig,
    MemeNewsReportService,
    MemeNewsScoutRunner,
)
from app.services.meme_scout import MemeScoutConfig, MemeScoutService
from app.services.retention import RetentionConfig, RetentionService

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def profile(addr, *, boost=None, links=None):
    return TokenProfile(chain="solana", token_address=addr, url="http://t",
                        description="d", boost_amount=boost, raw={"links": links or []})


def pair(addr, *, liq=1000.0):
    return PairData(chain="solana", pair_address=addr + "-p", base_token_address=addr,
                    base_token_symbol="TKN", base_token_name="Token", liquidity_usd=liq,
                    volume_1h_usd=500.0, volume_5m_usd=100.0, volume_24h_usd=5000.0,
                    price_usd=0.01, price_change_5m=1.0, price_change_1h=2.0)


class FakeDex:
    def __init__(self, profiles=None, boosts=None, pairs=None, fail=False):
        self._p, self._b, self._pairs, self._fail = profiles or [], boosts or [], pairs or {}, fail

    async def fetch_latest_token_profiles(self):
        return [] if self._fail else list(self._p)

    async def fetch_latest_boosted_tokens(self):
        return [] if self._fail else list(self._b)

    async def fetch_pairs_for_token(self, addr):
        return [] if self._fail else list(self._pairs.get(addr, []))


def runner_with(dex) -> MemeNewsScoutRunner:
    return MemeNewsScoutRunner(scout=MemeScoutService(adapter=dex, config=MemeScoutConfig(limit=30)))


class FakeRunner:
    def __init__(self):
        self.called = False

    async def run_cycle(self, session, limit=None):
        self.called = True
        r = MemeScoutRun(status="ok", started_at=NOW, created_at=NOW, tokens_scored=1)
        session.add(r)
        session.commit()
        return r


def use_settings(monkeypatch, **kw):
    s = Settings(_env_file=None, **kw)
    monkeypatch.setattr(config_module, "get_settings", lambda: s)
    monkeypatch.setattr(meme_news_module, "get_settings", lambda: s)
    return s


# --- runner -----------------------------------------------------------------

class TestRunner:
    def test_runner_calls_scout_service(self, session):
        runner = runner_with(FakeDex(profiles=[profile("A")], pairs={"A": [pair("A")]}))
        run = asyncio.run(runner.run_cycle(session))
        assert run.status == "ok" and run.tokens_scored == 1
        assert session.query(MemeAttentionSnapshot).count() == 1

    def test_partial_provider_failure_still_completes(self, session):
        # profiles present but NO pairs for the token -> best pair None, still scores
        runner = runner_with(FakeDex(profiles=[profile("A")], pairs={}))
        run = asyncio.run(runner.run_cycle(session))
        assert run.status == "ok" and run.tokens_scored == 1
        snap = session.query(MemeAttentionSnapshot).one()
        assert snap.liquidity_usd is None  # degraded, not crashed

    def test_full_provider_outage_degrades_to_zero(self, session):
        run = asyncio.run(runner_with(FakeDex(fail=True)).run_cycle(session))
        assert run.status == "ok" and run.tokens_scored == 0

    def test_run_cycle_never_raises(self, session):
        class Exploding:
            async def scan_once(self, session, limit=None):
                raise RuntimeError("boom")

        runner = MemeNewsScoutRunner(scout=Exploding())
        run = asyncio.run(runner.run_cycle(session))  # must not raise
        assert run is None  # no run persisted, handled gracefully


# --- flag gating (scheduled vs manual) --------------------------------------

class TestFlagGating:
    def test_scheduled_refuses_without_flag(self, session, monkeypatch, capsys):
        use_settings(monkeypatch, enable_meme_news_scout=False)
        fr = FakeRunner()
        rc = asyncio.run(cli.meme_news_run_once(scheduled=True, runner=fr, session=session))
        assert rc == 0 and fr.called is False
        assert "ENABLE_MEME_NEWS_SCOUT=false" in capsys.readouterr().out

    def test_scheduled_runs_with_flag(self, session, monkeypatch):
        use_settings(monkeypatch, enable_meme_news_scout=True)
        fr = FakeRunner()
        asyncio.run(cli.meme_news_run_once(scheduled=True, runner=fr, session=session))
        assert fr.called is True

    def test_manual_runs_even_with_flag_off(self, session, monkeypatch):
        use_settings(monkeypatch, enable_meme_news_scout=False)
        fr = FakeRunner()
        asyncio.run(cli.meme_news_run_once(scheduled=False, runner=fr, session=session))
        assert fr.called is True  # manual path ignores the loop flag


# --- report -----------------------------------------------------------------

def seed_snap(session, addr, *, attention, risk=None, conf=1.0, minutes_ago=10, boost=None):
    session.add(MemeAttentionSnapshot(
        chain="solana", token_address=addr, symbol=addr, attention_score=attention,
        risk_level=risk, risk_score=0.9 if risk in ("severe", "high") else 0.1,
        provider_confidence=conf, boost_amount=boost,
        observed_at=NOW - timedelta(minutes=minutes_ago),
        created_at=NOW - timedelta(minutes=minutes_ago),
    ))
    session.commit()


class TestReport:
    def test_report_top_attention_and_counts(self, session):
        seed_snap(session, "LOW", attention=0.2)
        seed_snap(session, "HIGH", attention=0.8)
        seed_snap(session, "MID", attention=0.5)
        r = MemeNewsReportService().build(session, hours=24)
        assert r.new_tokens == 3
        assert r.top_attention[0]["token"].startswith("HIGH")
        assert r.attention_max == 0.8
        assert r.row_counts["meme_attention_snapshots"] == 3

    def test_report_flags_high_risk_and_missing_coverage(self, session):
        seed_snap(session, "SEV", attention=0.4, risk="severe", conf=1.0)
        seed_snap(session, "NOPROV", attention=0.3, risk=None, conf=0.25)
        r = MemeNewsReportService().build(session, hours=24)
        assert any(t["token"].startswith("SEV") for t in r.high_risk_tokens)
        assert r.missing_holder_coverage == 1


# --- alerts -----------------------------------------------------------------

class TestAlerts:
    def test_high_attention_and_severe_risk(self, session, monkeypatch):
        use_settings(monkeypatch)  # defaults: threshold 0.6, severe alert on
        seed_snap(session, "HOT", attention=0.7)
        seed_snap(session, "BAD", attention=0.3, risk="severe")
        alerts = MemeNewsAlertService().evaluate(session, hours=6)
        kinds = {a.alert_type for a in alerts}
        assert "high_attention" in kinds and "severe_risk" in kinds
        assert any(a.severity == "warn" and a.alert_type == "severe_risk" for a in alerts)

    def test_boost_increase_and_attention_jump(self, session, monkeypatch):
        use_settings(monkeypatch)
        # attention jump for one token across two snapshots
        seed_snap(session, "JMP", attention=0.30, minutes_ago=20)
        seed_snap(session, "JMP", attention=0.50, minutes_ago=5)
        session.add(MemeCatalystEvent(
            source="dexscreener", subject_type="token", subject_ref="JMP",
            catalyst_type="boost_increase", magnitude=120.0,
            observed_at=NOW - timedelta(minutes=5), created_at=NOW - timedelta(minutes=5),
        ))
        session.commit()
        kinds = {a.alert_type for a in MemeNewsAlertService().evaluate(session, hours=6)}
        assert "attention_jump" in kinds and "boost_increase" in kinds

    def test_provider_degradation(self, session, monkeypatch):
        use_settings(monkeypatch)
        for i in range(4):
            seed_snap(session, f"NP{i}", attention=0.2, conf=0.25)  # all missing provider data
        alerts = MemeNewsAlertService().evaluate(session, hours=6)
        assert any(a.alert_type == "provider_degradation" and a.severity == "warn" for a in alerts)


# --- retention --------------------------------------------------------------

class TestRetention:
    def test_meme_tables_pruned_domain_protected(self, session):
        old = NOW - timedelta(days=20)
        new = NOW - timedelta(days=1)
        for ts, addr in ((old, "OLD"), (new, "NEW")):
            session.add(MemeAttentionSnapshot(chain="solana", token_address=addr,
                        attention_score=0.3, observed_at=ts, created_at=ts))
            session.add(MemeCatalystEvent(source="dexscreener", subject_type="token",
                        subject_ref=addr, catalyst_type="profile_seen", observed_at=ts, created_at=ts))
            session.add(MemeScoutRun(status="ok", started_at=ts, created_at=ts))
        # domain inventory is protected — an old row must survive
        session.add(DomainScoutRun(status="ok", started_at=old, created_at=old))
        session.add(DomainMarketInventorySnapshot(domain="weather", market_count=1,
                    observed_at=old, created_at=old))
        session.commit()

        svc = RetentionService(RetentionConfig(meme_days=14))
        counts = svc.prune(session, dry_run=True)
        assert counts["meme_attention_snapshots"] == 1
        assert counts["meme_catalyst_events"] == 1
        assert counts["meme_scout_runs"] == 1
        assert "domain_scout_runs" not in counts
        assert "domain_market_inventory_snapshots" not in counts

        svc.prune(session, dry_run=False)
        assert session.query(MemeAttentionSnapshot).count() == 1  # only NEW remains
        assert session.query(DomainScoutRun).count() == 1  # protected, untouched


# --- systemd artifacts + config ---------------------------------------------

def test_systemd_artifacts_exist():
    base = REPO / "infra" / "systemd" / "user"
    assert (base / "probability-arena-meme-news.service").is_file()
    timer = (base / "probability-arena-meme-news.timer").read_text()
    assert "OnUnitActiveSec" in timer
    svc = (base / "probability-arena-meme-news.service").read_text()
    assert "meme-news-run-once --scheduled" in svc  # timer refuses without flag


def test_config_defaults():
    s = Settings(_env_file=None)
    assert s.enable_meme_news_scout is False
    assert s.meme_news_retention_days == 14
    assert s.meme_news_attention_alert_threshold == 0.6
    assert MemeNewsConfig.from_settings(s).enabled is False


def test_main_wires_meme_news_commands(monkeypatch):
    captured = {}

    async def fake(scheduled=False, limit=None, runner=None, session=None):
        captured["scheduled"] = scheduled
        return 2

    monkeypatch.setattr(cli, "meme_news_run_once", fake)
    assert cli.main(["meme-news-run-once", "--scheduled"]) == 0
    assert captured["scheduled"] is True
