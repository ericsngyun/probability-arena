"""MarketOps Autopilot (OPS-006) tests: cycle audit, deterministic
auto-promotion, stage delegation to existing services, alert lifecycle,
CLI, API, and the optional loop. Everything mocked — no live network."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import cli
from app.config import get_settings
from app.db import Base, get_db
from app.main import app
from app.models import Market, MarketOpsAlert, MarketOpsRun, OpportunitySignal, WatcherRun
from app.services import marketops as marketops_module
from app.services.marketops import (
    ALERT_CC_SAMPLE_UPDATE,
    ALERT_NO_RECENT_SIGNALS,
    ALERT_PROVIDER_ERROR,
    ALERT_SERVICE_HEALTH,
    ALERT_SOURCE_BACKED_FORECAST,
    ALERT_TOO_MANY_SIGNALS,
    MarketOpsAlertService,
    MarketOpsAutopilotService,
    MarketOpsConfig,
    MarketOpsReportService,
)
from app.services.signal_workflow import SignalPromotionService
from tests.test_signal_workflow import make_processor

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def seed_signal(
    session,
    ticker="GEN-MKT-1",
    signal_type="price_move_threshold",
    status="new",
    age_minutes=10.0,
):
    observed = NOW - timedelta(minutes=age_minutes)
    row = OpportunitySignal(
        market_ticker=ticker,
        signal_type=signal_type,
        signal_status=status,
        observed_at=observed,
        reason="seeded",
        created_at=observed,
    )
    session.add(row)
    session.commit()
    return row


def seed_market(session, ticker):
    row = Market(
        ticker=ticker,
        title=f"{ticker} market?",
        status="active",
        rules_primary="Resolves YES if the thing happens per the official source.",
    )
    session.add(row)
    session.commit()
    return row


class FakeCryptoService:
    def __init__(self, tokens=3, signals=2, explode=False):
        self.calls: list[int] = []
        self.tokens = tokens
        self.signals = signals
        self.explode = explode
        self._next_id = 100

    async def scan_once(self, session, limit=None):
        self.calls.append(limit)
        if self.explode:
            raise RuntimeError("crypto provider down")
        self._next_id += 1
        return SimpleNamespace(
            id=self._next_id,
            tokens_checked=self.tokens,
            signals_created=self.signals,
        )


class FakeOutcomeService:
    def __init__(self, synced=4):
        self.calls: list[int] = []
        self._synced = synced

    async def sync_known_markets(self, session, limit=None):
        self.calls.append(limit)
        return [object()] * self._synced


class FakeCalibrationService:
    def __init__(self, scored=7):
        self.calls: list[int] = []
        self._scored = scored

    def score_unscored(self, session, limit=None):
        self.calls.append(limit)
        return {"scored": self._scored, "pending_outcome": 1, "unscorable": 0, "skipped": 2}


class FakeCCService:
    def __init__(self, pair_count=0, label="insufficient_sample", delta=None):
        self.pair_count = pair_count
        self.label = label
        self.delta = delta

    def compare(self, session, **kwargs):
        paired = (
            SimpleNamespace(
                pair_count=self.pair_count,
                sample_label=self.label,
                mean_delta_brier=self.delta,
            )
            if self.pair_count
            else None
        )
        return SimpleNamespace(
            baseline_forecaster="template_baseline",
            challenger_forecaster="baseball_evidence_v1",
            paired=paired,
            sample_label=self.label,
        )


def autopilot(session=None, cfg=None, **overrides) -> MarketOpsAutopilotService:
    defaults = dict(
        config=cfg or MarketOpsConfig(),
        promotion_service=SignalPromotionService(),
        processing_service=make_processor(),
        crypto_service=FakeCryptoService(),
        outcome_service=FakeOutcomeService(),
        calibration_service=FakeCalibrationService(),
        champion_challenger_service=FakeCCService(),
        alert_service=MarketOpsAlertService(),
    )
    defaults.update(overrides)
    return MarketOpsAutopilotService(**defaults)


class TestRunOnce:
    async def test_records_run_with_counters(self, session):
        seed_market(session, "GEN-MKT-1")
        seed_signal(session, ticker="GEN-MKT-1")
        service = autopilot()
        run = await service.run_once(session)

        assert run.status == "ok"
        assert run.id is not None
        assert run.signals_seen == 1
        assert run.signals_promoted == 1
        assert run.signals_processed == 1
        assert run.crypto_tokens_seen == 3
        assert run.crypto_signals_created == 2
        assert run.outcomes_synced == 4
        assert run.forecasts_scored == 7
        assert run.duration_ms is not None and run.finished_at is not None
        assert run.config["promote_limit"] == 5
        assert run.summary["stages"]["promote_signals"] == "ok"
        assert run.summary["stages"]["crypto_scan"] == "ok"
        assert run.summary["processed_tickers"] == ["GEN-MKT-1"]
        # persisted
        assert session.get(MarketOpsRun, run.id) is not None

    async def test_stage_limits_flow_from_config(self, session):
        crypto = FakeCryptoService()
        outcomes = FakeOutcomeService()
        calibration = FakeCalibrationService()
        cfg = MarketOpsConfig(
            crypto_scan_limit=42, sync_outcome_limit=99, score_limit=123
        )
        await autopilot(
            cfg=cfg,
            crypto_service=crypto,
            outcome_service=outcomes,
            calibration_service=calibration,
        ).run_once(session)
        assert crypto.calls == [42]
        assert outcomes.calls == [99]
        assert calibration.calls == [123]

    async def test_crypto_skipped_when_excluded(self, session):
        crypto = FakeCryptoService()
        run = await autopilot(
            cfg=MarketOpsConfig(include_crypto=False), crypto_service=crypto
        ).run_once(session)
        assert crypto.calls == []
        assert run.summary["stages"]["crypto_scan"] == "skipped"
        assert run.crypto_tokens_seen == 0

    async def test_probability_markets_skipped_when_excluded(self, session):
        seed_signal(session)
        run = await autopilot(
            cfg=MarketOpsConfig(include_probability_markets=False)
        ).run_once(session)
        assert run.signals_seen == 0
        assert run.signals_promoted == 0
        assert run.summary["stages"]["probability_markets"] == "skipped"

    async def test_stage_failure_marks_partial_and_alerts(self, session):
        run = await autopilot(crypto_service=FakeCryptoService(explode=True)).run_once(session)
        assert run.status == "partial"
        assert "crypto_scan" in run.summary["stage_errors"]
        alerts = session.execute(select(MarketOpsAlert)).scalars().all()
        provider_alerts = [a for a in alerts if a.alert_type == ALERT_PROVIDER_ERROR]
        assert len(provider_alerts) == 1
        assert "crypto provider down" in provider_alerts[0].message
        # later stages still ran
        assert run.outcomes_synced == 4

    async def test_fail_fast_marks_run_error(self, session):
        run = await autopilot(
            cfg=MarketOpsConfig(fail_fast=True),
            crypto_service=FakeCryptoService(explode=True),
        ).run_once(session)
        assert run.status == "error"
        assert run.error_type == "RuntimeError"


class TestAutoPromotion:
    def test_prioritizes_source_backed_domains_then_type_priority(self, session):
        seed_signal(session, ticker="GEN-1", signal_type="price_move_threshold")
        seed_signal(session, ticker="KXMLBTOTAL-26JUL021915STLATL-18",
                    signal_type="spread_tightened")
        seed_signal(session, ticker="KXWCGAME-26JUN14USAWAL",
                    signal_type="liquidity_appeared")
        service = autopilot(cfg=MarketOpsConfig(promote_limit=2))
        selected, seen = service.select_signals_for_promotion(session, NOW)
        assert seen == 3
        # baseball + soccer beat the generic ticker despite its stronger type
        assert [s.market_ticker[:4] for s in selected] == ["KXML", "KXWC"]

    def test_one_signal_per_ticker_per_cycle(self, session):
        seed_signal(session, ticker="KXMLB-A", signal_type="price_move_threshold")
        seed_signal(session, ticker="KXMLB-A", signal_type="spread_tightened")
        seed_signal(session, ticker="KXMLB-B", signal_type="liquidity_appeared")
        selected, _ = autopilot().select_signals_for_promotion(session, NOW)
        assert len(selected) == 2
        assert {s.market_ticker for s in selected} == {"KXMLB-A", "KXMLB-B"}

    def test_promote_limit_cap(self, session):
        for i in range(8):
            seed_signal(session, ticker=f"KXMLB-{i}")
        selected, seen = autopilot(
            cfg=MarketOpsConfig(promote_limit=3)
        ).select_signals_for_promotion(session, NOW)
        assert seen == 8
        assert len(selected) == 3

    def test_age_window_excludes_too_young_and_too_old(self, session):
        seed_signal(session, ticker="YOUNG", age_minutes=0.1)  # < 30s old
        seed_signal(session, ticker="OLD", age_minutes=60 * 25)  # > 24h old
        seed_signal(session, ticker="RIGHT", age_minutes=10)
        selected, seen = autopilot().select_signals_for_promotion(session, NOW)
        assert seen == 1
        assert [s.market_ticker for s in selected] == ["RIGHT"]

    def test_skips_non_new_and_errored_signals(self, session):
        seed_signal(session, ticker="DISMISSED", status="dismissed")
        seed_signal(session, ticker="REVIEWED", status="reviewed")
        errored = seed_signal(session, ticker="ERRORED")
        errored.processing_error_type = "RuntimeError"
        session.commit()
        selected, seen = autopilot().select_signals_for_promotion(session, NOW)
        assert seen == 0 and selected == []

    def test_skips_recently_refreshed_and_pending_tickers(self, session):
        refreshed = seed_signal(session, ticker="FRESH", status="forecast_refreshed")
        refreshed.processed_at = NOW - timedelta(minutes=5)
        seed_signal(session, ticker="PENDING", status="promoted_to_research")
        session.commit()
        seed_signal(session, ticker="FRESH")   # same ticker, refreshed 5min ago
        seed_signal(session, ticker="PENDING")  # same ticker, already awaiting
        seed_signal(session, ticker="OK-1")
        selected, _ = autopilot().select_signals_for_promotion(session, NOW)
        assert [s.market_ticker for s in selected] == ["OK-1"]


class TestAlerts:
    async def test_too_many_signals_alert(self, session, monkeypatch):
        monkeypatch.setattr(marketops_module, "TOO_MANY_SIGNALS_PER_HOUR", 2)
        for i in range(4):
            seed_signal(session, ticker=f"T-{i}", age_minutes=1000)  # outside promo window
        for i in range(3):
            seed_signal(session, ticker=f"F-{i}", age_minutes=5)
        await autopilot(cfg=MarketOpsConfig(promote_limit=0)).run_once(session)
        alerts = [
            a for a in session.execute(select(MarketOpsAlert)).scalars().all()
            if a.alert_type == ALERT_TOO_MANY_SIGNALS
        ]
        assert len(alerts) == 1
        assert alerts[0].severity == "warning"

    async def test_no_recent_signals_alert_when_watcher_enabled(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_realtime_watcher", True)
        run = await autopilot().run_once(session)
        types = {
            a.alert_type for a in session.execute(select(MarketOpsAlert)).scalars().all()
        }
        assert ALERT_NO_RECENT_SIGNALS in types
        assert ALERT_SERVICE_HEALTH in types  # no watcher runs recorded either
        assert run.alerts_created >= 2

    async def test_no_signal_alerts_when_watcher_disabled(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_realtime_watcher", False)
        await autopilot().run_once(session)
        types = {
            a.alert_type for a in session.execute(select(MarketOpsAlert)).scalars().all()
        }
        assert ALERT_NO_RECENT_SIGNALS not in types
        assert ALERT_SERVICE_HEALTH not in types

    async def test_healthy_watcher_raises_no_health_alert(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_realtime_watcher", True)
        seed_signal(session, ticker="ANY", age_minutes=5)
        session.add(WatcherRun(status="ok", started_at=NOW - timedelta(minutes=2),
                               created_at=NOW - timedelta(minutes=2)))
        session.commit()
        await autopilot(cfg=MarketOpsConfig(promote_limit=0)).run_once(session)
        types = {
            a.alert_type for a in session.execute(select(MarketOpsAlert)).scalars().all()
        }
        assert ALERT_SERVICE_HEALTH not in types
        assert ALERT_NO_RECENT_SIGNALS not in types

    async def test_crypto_spike_alert(self, session, monkeypatch):
        monkeypatch.setattr(marketops_module, "CRYPTO_SIGNAL_SPIKE_PER_CYCLE", 2)
        await autopilot(crypto_service=FakeCryptoService(signals=5)).run_once(session)
        alerts = [
            a for a in session.execute(select(MarketOpsAlert)).scalars().all()
            if a.alert_type == "crypto_signal_spike"
        ]
        assert len(alerts) == 1

    async def test_db_growth_alert(self, session, monkeypatch):
        monkeypatch.setattr(marketops_module, "database_size_mb", lambda *a, **k: 600.0)
        await autopilot().run_once(session)
        alerts = [
            a for a in session.execute(select(MarketOpsAlert)).scalars().all()
            if a.alert_type == "db_growth_warning"
        ]
        assert len(alerts) == 1

    async def test_cc_sample_update_alert_on_change_only(self, session):
        service = autopilot(champion_challenger_service=FakeCCService(pair_count=3,
                                                                      label="insufficient_sample",
                                                                      delta=-0.01))
        await service.run_once(session)
        first = [
            a for a in session.execute(select(MarketOpsAlert)).scalars().all()
            if a.alert_type == ALERT_CC_SAMPLE_UPDATE
        ]
        assert len(first) == 1
        assert "0 -> 3" in first[0].title
        # same pair count next cycle -> no new alert
        await service.run_once(session)
        second = [
            a for a in session.execute(select(MarketOpsAlert)).scalars().all()
            if a.alert_type == ALERT_CC_SAMPLE_UPDATE
        ]
        assert len(second) == 1

    async def test_source_backed_alert_for_soccer_refresh(self, session, monkeypatch):
        from tests.test_soccer_canary import (
            WC_TICKER,
            MockSoccerFetcher,
            seed_soccer_market,
        )

        monkeypatch.setattr(get_settings(), "enable_soccer_external_research", True)
        seed_soccer_market(session)
        seed_signal(session, ticker=WC_TICKER)
        service = autopilot(
            processing_service=make_processor(
                collector=None, soccer_fetcher=MockSoccerFetcher()
            )
        )
        run = await service.run_once(session)
        assert run.signals_processed == 1
        alerts = [
            a for a in session.execute(select(MarketOpsAlert)).scalars().all()
            if a.alert_type == ALERT_SOURCE_BACKED_FORECAST
        ]
        assert len(alerts) == 1
        assert WC_TICKER in alerts[0].title
        assert alerts[0].evidence["collector"] == "soccer-external"

    async def test_open_alerts_are_not_duplicated_across_cycles(self, session):
        service = autopilot(crypto_service=FakeCryptoService(explode=True))
        await service.run_once(session)
        await service.run_once(session)
        provider_alerts = [
            a for a in session.execute(select(MarketOpsAlert)).scalars().all()
            if a.alert_type == ALERT_PROVIDER_ERROR
        ]
        assert len(provider_alerts) == 1  # deduped while open

    def test_resolve_alert(self, session):
        alert_service = MarketOpsAlertService()
        alert = alert_service.create(session, "provider_error", "warning", "t", "m")
        session.commit()
        resolved = alert_service.resolve(session, alert.id)
        assert resolved.status == "resolved"
        assert resolved.resolved_at is not None
        # idempotent
        again = alert_service.resolve(session, alert.id)
        assert again.resolved_at == resolved.resolved_at
        with pytest.raises(LookupError):
            alert_service.resolve(session, 9999)

    async def test_resolved_alert_can_reopen_via_new_alert(self, session):
        service = autopilot(crypto_service=FakeCryptoService(explode=True))
        await service.run_once(session)
        alert = session.execute(select(MarketOpsAlert)).scalars().first()
        MarketOpsAlertService().resolve(session, alert.id)
        await service.run_once(session)
        provider_alerts = [
            a for a in session.execute(select(MarketOpsAlert)).scalars().all()
            if a.alert_type == ALERT_PROVIDER_ERROR
        ]
        assert len(provider_alerts) == 2  # resolved one stays; fresh open one


class TestReport:
    async def test_report_aggregates_and_recommends(self, session):
        seed_market(session, "GEN-MKT-1")
        seed_signal(session, ticker="GEN-MKT-1")
        await autopilot(
            champion_challenger_service=FakeCCService(pair_count=5, delta=-0.02)
        ).run_once(session)
        report = MarketOpsReportService().build(session)
        assert report.runs_total == 1
        assert report.latest_run.status == "ok"
        assert report.champion_challenger["pair_count"] == 5
        assert report.crypto_totals == {"tokens": 0, "signals": 0}
        assert report.forecasts_by_forecaster.get("template_baseline") == 1
        # cc info alert is open -> but severity info; no warnings -> accumulate advice
        assert "keep accumulating" in report.recommended_action

    async def test_report_flags_open_warnings_first(self, session):
        await autopilot(crypto_service=FakeCryptoService(explode=True)).run_once(session)
        report = MarketOpsReportService().build(session)
        assert "Investigate" in report.recommended_action

    def test_report_before_any_run(self, session):
        report = MarketOpsReportService().build(session)
        assert report.runs_total == 0
        assert report.latest_run is None
        assert "marketops-run-once" in report.recommended_action


class TestCli:
    async def test_run_once_cli(self, session, capsys):
        seed_market(session, "GEN-MKT-1")
        seed_signal(session, ticker="GEN-MKT-1")
        exit_code = await cli.marketops_run_once(services=autopilot(), session=session)
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "marketops run #1: ok" in output
        assert "promoted=1 processed=1" in output

    async def test_run_once_cli_reports_stage_errors(self, session, capsys):
        exit_code = await cli.marketops_run_once(
            services=autopilot(crypto_service=FakeCryptoService(explode=True)),
            session=session,
        )
        assert exit_code == 0  # partial is still operational
        output = capsys.readouterr().out
        assert "partial" in output
        assert "stage crypto_scan: RuntimeError" in output

    async def test_report_cli(self, session, capsys):
        await autopilot().run_once(session)
        total = await cli.marketops_report(session=session)
        assert total == 1
        output = capsys.readouterr().out
        assert "last run: #1 ok" in output
        assert "recommended action:" in output

    async def test_alerts_and_resolve_cli(self, session, capsys):
        await autopilot(crypto_service=FakeCryptoService(explode=True)).run_once(session)
        count = await cli.marketops_alerts(limit=10, session=session)
        assert count == 1
        output = capsys.readouterr().out
        assert "provider_error" in output

        exit_code = await cli.marketops_resolve_alert(1, session=session)
        assert exit_code == 0
        assert "resolved" in capsys.readouterr().out
        assert await cli.marketops_resolve_alert(999, session=session) == 1

    async def test_loop_requires_flag(self, session, capsys, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_marketops_autopilot", False)
        iterations = await cli.marketops_loop(
            interval=1, services=autopilot(), session=session, max_iterations=3
        )
        assert iterations == 0
        assert "ENABLE_MARKETOPS_AUTOPILOT=false" in capsys.readouterr().out

    async def test_loop_runs_and_exits_cleanly(self, session, capsys, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_marketops_autopilot", True)
        iterations = await cli.marketops_loop(
            interval=0, services=autopilot(), session=session, max_iterations=2
        )
        assert iterations == 2
        output = capsys.readouterr().out
        assert "marketops loop stopped after 2 iteration(s)" in output
        assert session.execute(select(MarketOpsRun)).scalars().all()

    def test_main_wires_marketops_commands(self, monkeypatch):
        captured = []

        async def fake(*args, **kwargs):
            captured.append(kwargs)
            return 0

        for name in (
            "marketops_run_once",
            "marketops_report",
            "marketops_alerts",
            "marketops_resolve_alert",
            "marketops_loop",
        ):
            monkeypatch.setattr(cli, name, fake)
        assert cli.main(["marketops-run-once"]) == 0
        assert cli.main(["marketops-report"]) == 0
        assert cli.main(["marketops-alerts", "--limit", "5"]) == 0
        assert cli.main(["marketops-resolve-alert", "3"]) == 0
        assert cli.main(["marketops-loop", "--interval", "60"]) == 0
        assert len(captured) == 5


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
    async def test_runs_report_alerts_endpoints(self, client):
        test_client, session = client
        await autopilot(crypto_service=FakeCryptoService(explode=True)).run_once(session)

        runs = test_client.get("/marketops/runs").json()
        assert len(runs) == 1 and runs[0]["status"] == "partial"

        run = test_client.get(f"/marketops/runs/{runs[0]['id']}").json()
        assert run["summary"]["stage_errors"]["crypto_scan"].startswith("RuntimeError")
        assert test_client.get("/marketops/runs/999").status_code == 404

        report = test_client.get("/marketops/report").json()
        assert report["runs_total"] == 1
        assert "Investigate" in report["recommended_action"]

        alerts = test_client.get("/marketops/alerts").json()
        assert len(alerts) == 1 and alerts[0]["status"] == "open"
        assert test_client.get("/marketops/alerts?status=bogus").status_code == 422

        resolved = test_client.patch(f"/marketops/alerts/{alerts[0]['id']}/resolve").json()
        assert resolved["status"] == "resolved" and resolved["resolved_at"]
        assert test_client.patch("/marketops/alerts/999/resolve").status_code == 404
        assert test_client.get("/marketops/alerts?status=open").json() == []

    def test_endpoints_empty_before_any_run(self, client):
        test_client, _ = client
        assert test_client.get("/marketops/runs").json() == []
        assert test_client.get("/marketops/alerts").json() == []
        report = test_client.get("/marketops/report").json()
        assert report["latest_run"] is None
