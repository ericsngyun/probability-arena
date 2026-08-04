import math

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import ForecastScoreRecord, Market, MarketForecastRecord, MarketOutcomeRecord
from app.services.calibration import (
    LOG_LOSS_EPSILON,
    CalibrationService,
    absolute_error,
    brier_score,
    log_loss,
)
from tests.conftest import make_market
from tests.test_outcomes import FakeOutcomeAdapter, seed_market


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


async def seed_forecast(
    session,
    ticker: str,
    probability: float = 0.7,
    forecaster=None,
) -> MarketForecastRecord:
    """Market + template packet + forecast, with an overridable probability."""
    from app.services.forecasting import ForecastingService, TemplateBaselineForecaster
    from app.services.research import TemplateResearchCollector, create_research_packet

    market = seed_market(session, ticker)
    await create_research_packet(
        session, market, collector=TemplateResearchCollector(name="template", version="v1")
    )
    service = ForecastingService(forecaster=forecaster or TemplateBaselineForecaster())
    row = await service.forecast_market(session, market)
    row.estimated_probability = probability  # pin for exact-math assertions
    session.commit()
    return row


async def seed_outcome(session, ticker: str, detail: dict) -> MarketOutcomeRecord:
    from app.services.outcomes import OutcomeService

    return await OutcomeService(adapter=FakeOutcomeAdapter({ticker: detail})).sync_ticker(
        session, ticker
    )


class TestMetrics:
    def test_brier(self):
        assert brier_score(0.7, 1.0) == pytest.approx(0.09)
        assert brier_score(0.7, 0.0) == pytest.approx(0.49)
        assert brier_score(1.0, 1.0) == 0.0

    def test_log_loss(self):
        assert log_loss(0.7, 1.0) == pytest.approx(-math.log(0.7), abs=1e-6)
        assert log_loss(0.7, 0.0) == pytest.approx(-math.log(0.3), abs=1e-6)

    def test_log_loss_epsilon_clamp_keeps_extremes_finite(self):
        at_one = log_loss(1.0, 0.0)
        at_zero = log_loss(0.0, 1.0)
        expected = -math.log(LOG_LOSS_EPSILON)
        assert at_one == pytest.approx(expected, rel=1e-3)
        assert at_zero == pytest.approx(expected, rel=1e-3)
        assert math.isfinite(at_one) and math.isfinite(at_zero)

    def test_absolute_error(self):
        assert absolute_error(0.7, 1.0) == pytest.approx(0.3)
        assert absolute_error(0.7, 0.0) == pytest.approx(0.7)


class TestScoreForecast:
    async def test_scores_resolved_yes_outcome(self, session):
        forecast = await seed_forecast(session, "T-CAL-1", probability=0.7)
        outcome = await seed_outcome(session, "T-CAL-1", {"status": "settled", "result": "yes"})

        score = CalibrationService().score_forecast(session, forecast, outcome)
        assert score.score_status == "scored"
        assert score.was_resolved is True
        assert score.outcome_id == outcome.id
        assert score.brier_score == pytest.approx(0.09)
        assert score.log_loss == pytest.approx(-math.log(0.7), abs=1e-6)
        assert score.absolute_error == pytest.approx(0.3)
        assert f"forecaster:template_baseline" in score.score_tags
        assert any(tag.startswith("depth:") for tag in score.score_tags)
        assert any(tag.startswith("domain:") for tag in score.score_tags)

    async def test_unresolved_outcome_is_pending(self, session):
        forecast = await seed_forecast(session, "T-CAL-2")
        outcome = await seed_outcome(session, "T-CAL-2", {"status": "active"})

        score = CalibrationService().score_forecast(session, forecast, outcome)
        assert score.score_status == "pending_outcome"
        assert score.was_resolved is False
        assert score.brier_score is None and score.log_loss is None

    async def test_no_outcome_at_all_is_pending(self, session):
        forecast = await seed_forecast(session, "T-CAL-3")
        score = CalibrationService().score_forecast(session, forecast, None)
        assert score.score_status == "pending_outcome"
        assert score.outcome_id is None

    async def test_canceled_outcome_is_unscorable(self, session):
        forecast = await seed_forecast(session, "T-CAL-4")
        outcome = await seed_outcome(session, "T-CAL-4", {"status": "canceled"})
        score = CalibrationService().score_forecast(session, forecast, outcome)
        assert score.score_status == "unscorable"
        assert score.brier_score is None

    async def test_settled_unknown_side_is_unscorable(self, session):
        forecast = await seed_forecast(session, "T-CAL-5")
        outcome = await seed_outcome(session, "T-CAL-5", {"status": "settled", "result": ""})
        score = CalibrationService().score_forecast(session, forecast, outcome)
        assert score.score_status == "unscorable"


class TestScoreUnscored:
    async def test_no_duplicates_unless_outcome_changes(self, session):
        forecast = await seed_forecast(session, "T-DUP", probability=0.6)
        await seed_outcome(session, "T-DUP", {"status": "active"})
        service = CalibrationService()

        first = service.score_unscored(session)
        assert first["pending_outcome"] == 1

        second = service.score_unscored(session)
        assert second["skipped"] == 1
        assert second["pending_outcome"] == 0
        assert len(session.execute(select(ForecastScoreRecord)).scalars().all()) == 1

        # Outcome resolves -> a new score row is created (append-only audit)
        await seed_outcome(session, "T-DUP", {"status": "settled", "result": "no"})
        third = service.score_unscored(session)
        assert third["scored"] == 1
        rows = session.execute(
            select(ForecastScoreRecord).order_by(ForecastScoreRecord.id)
        ).scalars().all()
        assert len(rows) == 2
        assert rows[-1].brier_score == pytest.approx(0.36)

        fourth = service.score_unscored(session)
        assert fourth["skipped"] == 1


class TestSummary:
    async def test_groups_by_cohorts(self, session):
        good = await seed_forecast(session, "KXMLB-GOOD", probability=0.8)
        bad = await seed_forecast(session, "KXATP-BAD", probability=0.8)
        pending = await seed_forecast(session, "KXMLB-PEND", probability=0.5)
        await seed_outcome(session, "KXMLB-GOOD", {"status": "settled", "result": "yes"})
        await seed_outcome(session, "KXATP-BAD", {"status": "settled", "result": "no"})
        await seed_outcome(session, "KXMLB-PEND", {"status": "active"})

        service = CalibrationService()
        service.score_unscored(session)
        summary = service.summary(session)

        assert summary.total_scores == 3
        assert summary.resolved == 2
        assert summary.pending_outcome == 1
        assert summary.unscorable == 0
        # overall mean brier: (0.04 + 0.64) / 2
        assert summary.overall.count == 2
        assert summary.overall.mean_brier == pytest.approx(0.34)
        assert summary.overall.mean_absolute_error == pytest.approx(0.5)

        assert summary.by_evidence_depth["template_only"].count == 2
        assert summary.by_forecaster["template_baseline"].count == 2
        assert set(summary.by_forecast_risk) <= {"low", "medium", "high"}
        assert summary.by_domain["sports_baseball"].count == 1
        assert summary.by_domain["sports_tennis"].count == 1
        assert summary.by_tag  # calibration tags propagated

    async def test_summary_uses_latest_score_per_forecast(self, session):
        await seed_forecast(session, "T-LATEST", probability=0.9)
        await seed_outcome(session, "T-LATEST", {"status": "active"})
        service = CalibrationService()
        service.score_unscored(session)
        await seed_outcome(session, "T-LATEST", {"status": "settled", "result": "yes"})
        service.score_unscored(session)

        summary = service.summary(session)
        assert summary.total_scores == 1  # two rows exist, latest wins
        assert summary.resolved == 1
        assert summary.overall.mean_brier == pytest.approx(0.01)


class TestCliCalibration:
    async def test_score_forecasts_cli(self, session, capsys):
        await seed_forecast(session, "T-CLI-1", probability=0.7)
        await seed_outcome(session, "T-CLI-1", {"status": "settled", "result": "yes"})

        created = await cli.score_forecasts(limit=100, session=session)
        assert created == 1
        assert "scored=1" in capsys.readouterr().out

    async def test_calibration_report_cli(self, session, capsys):
        await seed_forecast(session, "T-CLI-2", probability=0.7)
        await seed_outcome(session, "T-CLI-2", {"status": "settled", "result": "yes"})
        await cli.score_forecasts(limit=100, session=session)
        capsys.readouterr()

        total = await cli.calibration_report(session=session)
        assert total == 1
        output = capsys.readouterr().out
        assert "calibration: total=1 resolved=1" in output
        assert "overall: brier=0.09" in output
        assert "evidence=template_only" in output
        assert "forecaster=template_baseline" in output

    def test_main_wires_score_and_report(self, monkeypatch):
        captured = {}

        async def fake_score(limit=500, session=None):
            captured["limit"] = limit
            return 1

        async def fake_report(session=None):
            captured["report"] = True
            return 1

        monkeypatch.setattr(cli, "score_forecasts", fake_score)
        monkeypatch.setattr(cli, "calibration_report", fake_report)
        assert cli.main(["score-forecasts", "--limit", "500"]) == 0
        assert cli.main(["calibration-report"]) == 0
        assert captured == {"limit": 500, "report": True}
