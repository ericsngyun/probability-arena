"""Edge precheck (MVP-005A) tests: gap math, every status, deterministic
precedence, persistence, CLI gating, API, and MarketOps double-gating.
Measurement only — and the tests assert that no advice language leaks."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import cli
from app.config import get_settings
from app.db import Base, get_db
from app.main import app
from app.models import (
    EdgePrecheckSnapshot,
    MarketForecastRecord,
    MarketPriceTick,
    MarketResolutionAssessment,
)
from app.services.edge_precheck import (
    STATUS_INVALID_LOW_CONFIDENCE,
    STATUS_INVALID_LOW_LIQUIDITY,
    STATUS_INVALID_NOT_SOURCE_BACKED,
    STATUS_INVALID_RESOLUTION,
    STATUS_INVALID_STALE_FORECAST,
    STATUS_INVALID_STALE_SNAPSHOT,
    STATUS_INVALID_WIDE_SPREAD,
    STATUS_NO_GAP,
    STATUS_PAPER_CANDIDATE_LATER,
    STATUS_WATCHLIST,
    EdgePrecheckConfig,
    EdgePrecheckReportService,
    EdgePrecheckService,
)

NOW = datetime.now(timezone.utc)
TICKER = "KXMLBTOTAL-26JUL021915STLATL-18"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def seed_forecast(
    session,
    ticker=TICKER,
    probability=0.62,
    confidence=0.65,
    evidence_depth="source_backed",
    forecaster="baseball_evidence",
    age_seconds=60,
) -> MarketForecastRecord:
    row = MarketForecastRecord(
        market_ticker=ticker,
        forecaster_name=forecaster,
        forecaster_version="v1",
        prompt_version="v1",
        estimated_probability=probability,
        confidence=confidence,
        evidence_depth=evidence_depth,
        forecast_risk="medium",
        created_at=NOW - timedelta(seconds=age_seconds),
    )
    session.add(row)
    session.commit()
    return row


def seed_tick(
    session,
    ticker=TICKER,
    midpoint=0.50,
    spread=4,
    liquidity=2_000,
    age_seconds=30,
) -> MarketPriceTick:
    observed = NOW - timedelta(seconds=age_seconds)
    bid = int(round((midpoint - spread / 200) * 100))
    row = MarketPriceTick(
        market_ticker=ticker,
        observed_at=observed,
        yes_bid=bid,
        yes_ask=bid + spread,
        midpoint=midpoint,
        spread=spread,
        volume_24h=100,
        liquidity_proxy=liquidity,
        created_at=observed,
    )
    session.add(row)
    session.commit()
    return row


def seed_resolution(session, ticker=TICKER, tradeability="researchable"):
    row = MarketResolutionAssessment(
        market_ticker=ticker,
        model_name="rule-based",
        prompt_version="v1",
        clarity_score=0.9,
        resolution_risk="low",
        tradeability=tradeability,
        created_at=NOW,
    )
    session.add(row)
    session.commit()
    return row


def seed_all(session, **forecast_overrides):
    seed_resolution(session)
    seed_tick(session)
    return seed_forecast(session, **forecast_overrides)


def service(**cfg) -> EdgePrecheckService:
    return EdgePrecheckService(EdgePrecheckConfig(**cfg))


class TestGapMath:
    def test_positive_gap_signed_and_abs(self, session):
        forecast = seed_all(session, probability=0.62)
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.probability_gap == pytest.approx(0.12)
        assert row.abs_probability_gap == pytest.approx(0.12)
        assert row.market_midpoint == 0.50
        assert row.status == STATUS_WATCHLIST

    def test_negative_gap_preserved(self, session):
        forecast = seed_all(session, probability=0.38)
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.probability_gap == pytest.approx(-0.12)
        assert row.abs_probability_gap == pytest.approx(0.12)
        assert row.status == STATUS_WATCHLIST

    def test_missing_market_snapshot_yields_null_gap(self, session):
        seed_resolution(session)
        forecast = seed_forecast(session)  # no tick at all
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.probability_gap is None
        assert row.market_midpoint is None
        assert row.status == STATUS_INVALID_STALE_SNAPSHOT
        # spread/liquidity checks also fail and are all collected
        assert STATUS_INVALID_WIDE_SPREAD in row.invalidation_reasons
        assert STATUS_INVALID_LOW_LIQUIDITY in row.invalidation_reasons


class TestStatuses:
    def test_not_source_backed(self, session):
        forecast = seed_all(session, evidence_depth="template_only")
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.status == STATUS_INVALID_NOT_SOURCE_BACKED

    def test_low_confidence(self, session):
        forecast = seed_all(session, confidence=0.55)
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.status == STATUS_INVALID_LOW_CONFIDENCE

    def test_wide_spread(self, session):
        seed_resolution(session)
        seed_tick(session, spread=15)
        forecast = seed_forecast(session)
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.status == STATUS_INVALID_WIDE_SPREAD

    def test_low_liquidity(self, session):
        seed_resolution(session)
        seed_tick(session, liquidity=100)
        forecast = seed_forecast(session)
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.status == STATUS_INVALID_LOW_LIQUIDITY

    def test_stale_forecast_sports_uses_tighter_threshold(self, session):
        forecast = seed_all(session, age_seconds=400)  # >300s sports limit, <900s general
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.status == STATUS_INVALID_STALE_FORECAST
        assert row.raw_context["max_forecast_age_applied"] == 300

    def test_fresh_sports_forecast_passes(self, session):
        forecast = seed_all(session, age_seconds=200)
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.status == STATUS_WATCHLIST

    def test_stale_market_snapshot(self, session):
        seed_resolution(session)
        seed_tick(session, age_seconds=300)  # > 120s
        forecast = seed_forecast(session)
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.status == STATUS_INVALID_STALE_SNAPSHOT

    def test_resolution_risk_for_avoid_and_missing(self, session):
        seed_resolution(session, tradeability="avoid")
        seed_tick(session)
        forecast = seed_forecast(session)
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.status == STATUS_INVALID_RESOLUTION

        other = seed_forecast(session, ticker="NO-RESOLUTION-MKT")
        seed_tick(session, ticker="NO-RESOLUTION-MKT")
        row = service().precheck_forecast(session, other, now=NOW)
        assert STATUS_INVALID_RESOLUTION in row.invalidation_reasons

    def test_no_gap_below_threshold(self, session):
        forecast = seed_all(session, probability=0.52)  # gap 0.02 < 0.05
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.status == STATUS_NO_GAP
        assert row.invalidation_reasons == []

    def test_precedence_is_deterministic(self, session):
        # avoid resolution + template depth + stale + low confidence together
        seed_resolution(session, tradeability="avoid")
        forecast = seed_forecast(
            session, evidence_depth="template_only", confidence=0.4, age_seconds=5000
        )  # no tick either
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.status == STATUS_INVALID_RESOLUTION  # first in precedence
        assert row.invalidation_reasons == [
            STATUS_INVALID_RESOLUTION,
            STATUS_INVALID_NOT_SOURCE_BACKED,
            STATUS_INVALID_STALE_FORECAST,
            STATUS_INVALID_STALE_SNAPSHOT,
            STATUS_INVALID_LOW_CONFIDENCE,
            STATUS_INVALID_WIDE_SPREAD,
            STATUS_INVALID_LOW_LIQUIDITY,
        ]


class TestPersistence:
    def _measure(self, session, svc):
        forecast = seed_forecast(session)
        return svc.precheck_forecast(session, forecast, now=NOW)

    def test_watchlist_until_persistence_threshold(self, session):
        seed_resolution(session)
        seed_tick(session)
        svc = service(required_persistence_snapshots=3)

        first = self._measure(session, svc)
        assert first.status == STATUS_WATCHLIST and first.persistence_count == 1
        second = self._measure(session, svc)
        assert second.status == STATUS_WATCHLIST and second.persistence_count == 2
        third = self._measure(session, svc)
        assert third.status == STATUS_PAPER_CANDIDATE_LATER
        assert third.persistence_count == 3

    def test_direction_flip_resets_streak(self, session):
        seed_resolution(session)
        seed_tick(session)
        svc = service(required_persistence_snapshots=3)
        svc.precheck_forecast(session, seed_forecast(session, probability=0.62), now=NOW)
        svc.precheck_forecast(session, seed_forecast(session, probability=0.62), now=NOW)
        flipped = svc.precheck_forecast(
            session, seed_forecast(session, probability=0.38), now=NOW
        )
        assert flipped.status == STATUS_WATCHLIST
        assert flipped.persistence_count == 1

    def test_invalid_snapshot_breaks_streak(self, session):
        seed_resolution(session)
        seed_tick(session)
        svc = service(required_persistence_snapshots=3)
        self._measure(session, svc)
        self._measure(session, svc)
        # an invalid measurement lands between (low confidence)
        svc.precheck_forecast(session, seed_forecast(session, confidence=0.3), now=NOW)
        fourth = self._measure(session, svc)
        assert fourth.status == STATUS_WATCHLIST
        assert fourth.persistence_count == 1

    def test_candidate_label_attaches_no_behavior(self, session):
        """paper_candidate_later must be a row label and nothing else: no
        signals, no alerts, no forecasts, no orders — the tables that could
        record behavior stay untouched."""
        from app.models import CryptoOpportunitySignal, MarketOpsAlert, OpportunitySignal

        seed_resolution(session)
        seed_tick(session)
        svc = service(required_persistence_snapshots=1)
        row = self._measure(session, svc)
        assert row.status == STATUS_PAPER_CANDIDATE_LATER
        assert session.execute(select(OpportunitySignal)).scalars().all() == []
        assert session.execute(select(MarketOpsAlert)).scalars().all() == []
        assert session.execute(select(CryptoOpportunitySignal)).scalars().all() == []


class TestBatchAndAudit:
    def test_batch_measures_latest_forecast_per_ticker(self, session):
        seed_resolution(session)
        seed_tick(session)
        seed_forecast(session, probability=0.55)  # older
        newest = seed_forecast(session, probability=0.62, age_seconds=10)
        seed_resolution(session, ticker="OTHER-MKT")
        seed_tick(session, ticker="OTHER-MKT")
        other = seed_forecast(session, ticker="OTHER-MKT")

        snapshots = service().run_batch(session, limit=10, now=NOW)
        assert len(snapshots) == 2
        by_ticker = {s.market_ticker: s for s in snapshots}
        assert by_ticker[TICKER].forecast_id == newest.id
        assert by_ticker["OTHER-MKT"].forecast_id == other.id

    def test_snapshot_is_fully_auditable(self, session):
        forecast = seed_all(session)
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.thresholds["min_abs_gap"] == 0.05
        assert "domain:sports_baseball" in row.tags
        assert "market_type:total" in row.tags
        assert row.raw_context["measurement_only"] is True
        assert row.forecast_age_seconds == 60
        assert row.market_snapshot_age_seconds == 30
        assert row.resolution_assessment_id is not None
        assert row.market_snapshot_id is not None

    def test_signal_link_recorded_when_present(self, session):
        from app.models import OpportunitySignal

        forecast = seed_all(session)
        signal = OpportunitySignal(
            market_ticker=TICKER,
            signal_type="price_move_threshold",
            signal_status="forecast_refreshed",
            observed_at=NOW,
            reason="seeded",
            refreshed_forecast_id=forecast.id,
            created_at=NOW,
        )
        session.add(signal)
        session.commit()
        row = service().precheck_forecast(session, forecast, now=NOW)
        assert row.signal_id == signal.id


class TestReport:
    def test_report_aggregates_measurement_only(self, session):
        seed_resolution(session)
        seed_tick(session)
        svc = service()
        svc.precheck_forecast(session, seed_forecast(session, probability=0.62), now=NOW)
        svc.precheck_forecast(
            session, seed_forecast(session, probability=0.38, confidence=0.3), now=NOW
        )
        report = EdgePrecheckReportService().build(session)
        assert report.total_snapshots == 2
        assert report.by_status[STATUS_WATCHLIST] == 1
        assert report.by_status[STATUS_INVALID_LOW_CONFIDENCE] == 1
        assert report.by_forecaster["baseball_evidence"] == 2
        assert report.by_domain["sports_baseball"] == 2
        assert report.by_market_type["total"] == 2
        assert report.mean_abs_gap == pytest.approx(0.12)
        assert "Measurement only" in report.note
        assert report.invalidation_reason_counts[STATUS_INVALID_LOW_CONFIDENCE] == 1
        assert report.recent_largest_gaps[0].abs_probability_gap == pytest.approx(0.12)

    def test_output_contains_no_advice_language(self, session):
        import re

        forecast = seed_all(session)
        service().precheck_forecast(session, forecast, now=NOW)
        session.commit()
        report = EdgePrecheckReportService().build(session)
        text = report.model_dump_json().lower()
        for banned in ("buy", "sell", "bet", "trade", "position", "order", "stake"):
            assert not re.search(rf"\b{banned}\b", text), banned


class TestCli:
    async def test_refuses_when_flag_false(self, session, capsys, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_edge_precheck", False)
        count = await cli.edge_precheck(session=session)
        assert count == 0
        output = capsys.readouterr().out
        assert "ENABLE_EDGE_PRECHECK=false" in output
        assert session.execute(select(EdgePrecheckSnapshot)).scalars().all() == []

    async def test_force_readonly_runs_and_stays_measurement_only(
        self, session, capsys, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "enable_edge_precheck", False)
        seed_all(session)
        count = await cli.edge_precheck(force_readonly=True, session=session)
        assert count == 1
        output = capsys.readouterr().out.lower()
        assert "measurement only" in output
        assert "gap=" in output
        for banned in ("buy", "sell", "bet ", "trade"):
            assert banned not in output

    async def test_runs_when_flag_true(self, session, capsys, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_edge_precheck", True)
        seed_all(session)
        count = await cli.edge_precheck(session=session)
        assert count == 1

    async def test_report_cli(self, session, capsys, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_edge_precheck", True)
        seed_all(session)
        await cli.edge_precheck(session=session)
        capsys.readouterr()
        total = await cli.edge_precheck_report(session=session)
        assert total == 1
        output = capsys.readouterr().out
        assert "edge precheck (measurement only)" in output
        assert "paper_candidate_later (review label, no behavior)" in output
        lowered = output.lower()
        for banned in ("buy", "sell", " bet", "trade"):
            assert banned not in lowered

    def test_main_wires_commands(self, monkeypatch):
        captured = []

        async def fake(*args, **kwargs):
            captured.append(kwargs)
            return 0

        monkeypatch.setattr(cli, "edge_precheck", fake)
        monkeypatch.setattr(cli, "edge_precheck_report", fake)
        assert cli.main(["edge-precheck", "--limit", "5", "--force-readonly"]) == 0
        assert cli.main(["edge-precheck-report"]) == 0
        assert captured[0] == {"limit": 5, "force_readonly": True}


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
    def test_run_refuses_without_flag_or_force(self, client, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_edge_precheck", False)
        test_client, _ = client
        assert test_client.post("/edge-precheck/run").status_code == 409

    def test_run_list_report_roundtrip(self, client, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_edge_precheck", False)
        test_client, session = client
        seed_all(session)

        created = test_client.post("/edge-precheck/run?force_readonly=true").json()
        assert len(created) == 1
        assert created[0]["status"] == STATUS_WATCHLIST
        assert "probability_gap" in created[0]
        # no advice fields exist in the serialized shape
        for banned_field in ("side", "size", "direction", "ev", "action"):
            assert banned_field not in created[0]

        snapshots = test_client.get("/edge-precheck/snapshots").json()
        assert len(snapshots) == 1
        filtered = test_client.get(
            f"/edge-precheck/snapshots?status={STATUS_WATCHLIST}"
        ).json()
        assert len(filtered) == 1
        assert test_client.get("/edge-precheck/snapshots?status=bogus").status_code == 422

        report = test_client.get("/edge-precheck/report").json()
        assert report["total_snapshots"] == 1
        assert "Measurement only" in report["note"]

    def test_run_allowed_when_flag_true(self, client, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_edge_precheck", True)
        test_client, session = client
        seed_all(session)
        assert test_client.post("/edge-precheck/run").status_code == 200


class TestMarketOpsIntegration:
    async def test_not_run_by_default(self, session, monkeypatch):
        from tests.test_marketops import autopilot

        monkeypatch.setattr(get_settings(), "enable_edge_precheck", True)
        run = await autopilot().run_once(session)  # include_edge_precheck defaults false
        assert "edge_precheck" not in run.summary["stages"]
        assert session.execute(select(EdgePrecheckSnapshot)).scalars().all() == []

    async def test_runs_only_when_both_flags_true(self, session, monkeypatch):
        from app.services.marketops import MarketOpsConfig
        from tests.test_marketops import autopilot

        seed_all(session)
        monkeypatch.setattr(get_settings(), "enable_edge_precheck", True)
        run = await autopilot(
            cfg=MarketOpsConfig(include_edge_precheck=True),
            edge_precheck_service=EdgePrecheckService(EdgePrecheckConfig()),
        ).run_once(session)
        assert run.summary["stages"]["edge_precheck"] == "ok"
        assert run.summary["edge_precheck"]["snapshots"] == 1
        assert len(session.execute(select(EdgePrecheckSnapshot)).scalars().all()) == 1

    async def test_engine_flag_false_skips_even_when_included(self, session, monkeypatch):
        from app.services.marketops import MarketOpsConfig
        from tests.test_marketops import autopilot

        monkeypatch.setattr(get_settings(), "enable_edge_precheck", False)
        run = await autopilot(
            cfg=MarketOpsConfig(include_edge_precheck=True)
        ).run_once(session)
        assert run.summary["stages"]["edge_precheck"] == "skipped"
        assert session.execute(select(EdgePrecheckSnapshot)).scalars().all() == []
