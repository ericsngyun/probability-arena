from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import (
    MarketDetailEnrichment,
    MarketForecastRecord,
    MarketOutcomeRecord,
    MarketResearchPacket,
    MarketResolutionAssessment,
    PipelineRun,
    PipelineStageRun,
    ScannerRun,
)
from app.services.forecasting import TemplateBaselineForecaster
from app.services.pipeline import BASELINE_STAGES, BaselineConfig, PipelineRunner
from app.services.research import TemplateResearchCollector
from app.services.resolution import RuleBasedResolutionJudge
from tests.conftest import make_market
from tests.test_cli import FakeAdapter
from tests.test_enrichment import FakeDetailAdapter
from tests.test_outcomes import FakeOutcomeAdapter


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def make_runner(**overrides) -> PipelineRunner:
    markets = overrides.pop(
        "markets", [make_market(ticker="KXMLB-P1"), make_market(ticker="KXATP-P2")]
    )
    defaults = dict(
        scan_adapter=FakeAdapter(markets),
        enrichment_adapter=FakeDetailAdapter(),
        outcome_adapter=FakeOutcomeAdapter(
            {m.ticker: {"status": "settled", "result": "yes"} for m in markets}
        ),
        collector=TemplateResearchCollector(name="template", version="v1"),
        forecaster=TemplateBaselineForecaster(),
        judge=RuleBasedResolutionJudge(min_clarity_score=0.70, prompt_version="v1"),
    )
    defaults.update(overrides)
    return PipelineRunner(**defaults)


CFG = BaselineConfig(
    scan_limit=10, candidate_limit=10, sync_outcome_limit=10, score_limit=100, fail_fast=False
)


class TestBaselineRun:
    async def test_successful_run_records_parent_and_all_stages(self, session):
        run = await make_runner().run_baseline_pipeline(session, CFG)

        assert run.status == "completed"
        assert run.finished_at is not None and run.duration_ms is not None
        assert run.config["scan_limit"] == 10
        assert run.summary["stages"] == {name: "completed" for name in BASELINE_STAGES}

        stages = session.execute(
            select(PipelineStageRun).order_by(PipelineStageRun.id)
        ).scalars().all()
        assert [s.stage_name for s in stages] == list(BASELINE_STAGES)
        assert all(s.status == "completed" for s in stages)
        assert all(s.pipeline_run_id == run.id for s in stages)
        assert all(s.duration_ms is not None for s in stages)

        # Downstream rows actually created by the loop
        assert session.execute(select(ScannerRun)).scalars().first() is not None
        assert len(session.execute(select(MarketDetailEnrichment)).scalars().all()) == 2
        assert len(session.execute(select(MarketResolutionAssessment)).scalars().all()) == 2
        assert len(session.execute(select(MarketResearchPacket)).scalars().all()) == 2
        assert len(session.execute(select(MarketForecastRecord)).scalars().all()) == 2
        assert len(session.execute(select(MarketOutcomeRecord)).scalars().all()) == 2

        # Outcomes settled yes -> forecasts scored
        score_stage = next(s for s in stages if s.stage_name == "score_forecasts")
        assert score_stage.summary["scored"] == 2
        report_stage = stages[-1]
        assert report_stage.summary["resolved"] == 2

    async def test_failed_stage_records_error_and_continues_when_not_fail_fast(self, session):
        class ExplodingDetailAdapter(FakeDetailAdapter):
            async def get_market_detail(self, ticker):
                raise RuntimeError("kalshi detail exploded")

        runner = make_runner(enrichment_adapter=ExplodingDetailAdapter())
        run = await runner.run_baseline_pipeline(session, CFG)

        assert run.status == "completed_with_errors"
        stages = {s.stage_name: s for s in run.stages}
        assert stages["enrich_details"].status == "failed"
        assert stages["enrich_details"].error_type == "RuntimeError"
        assert "kalshi detail exploded" in stages["enrich_details"].error_message
        # later stages still ran
        assert stages["assess_resolution"].status == "completed"
        assert stages["forecast"].status == "completed"
        assert stages["calibration_report"].status == "completed"

    async def test_fail_fast_stops_after_failure(self, session):
        class ExplodingDetailAdapter(FakeDetailAdapter):
            async def get_market_detail(self, ticker):
                raise RuntimeError("boom")

        runner = make_runner(enrichment_adapter=ExplodingDetailAdapter())
        cfg = BaselineConfig(
            scan_limit=10, candidate_limit=10, sync_outcome_limit=10, score_limit=100,
            fail_fast=True,
        )
        run = await runner.run_baseline_pipeline(session, cfg)

        assert run.status == "failed"
        stage_names = [s.stage_name for s in run.stages]
        assert stage_names == ["scan", "enrich_details"]  # stopped at the failure
        assert run.summary["stages"]["enrich_details"] == "failed"

    async def test_lock_prevents_overlapping_runs(self, session):
        now = datetime.now(timezone.utc)
        active = PipelineRun(
            run_type="baseline", status="running", started_at=now, created_at=now
        )
        session.add(active)
        session.commit()

        run = await make_runner().run_baseline_pipeline(session, CFG)
        assert run.status == "skipped"
        assert run.summary == {"reason": "already_running", "active_run_id": active.id}
        assert run.stages == []
        # No pipeline work happened
        assert session.execute(select(ScannerRun)).scalars().first() is None

    async def test_dry_run_creates_audit_rows_only(self, session):
        cfg = BaselineConfig(scan_limit=10, candidate_limit=10, dry_run=True)
        run = await make_runner().run_baseline_pipeline(session, cfg)

        assert run.status == "dry_run"
        assert run.summary == {"dry_run": True}
        assert [s.stage_name for s in run.stages] == list(BASELINE_STAGES)
        assert all(s.status == "skipped" for s in run.stages)
        # No downstream rows of any kind
        for model in (
            ScannerRun,
            MarketDetailEnrichment,
            MarketResolutionAssessment,
            MarketResearchPacket,
            MarketForecastRecord,
            MarketOutcomeRecord,
        ):
            assert session.execute(select(model)).scalars().first() is None


class TestCliBaseline:
    async def test_run_baseline_prints_stage_summary(self, session, capsys):
        run = await cli.run_baseline(runner=make_runner(), session=session)
        assert run.status == "completed"
        output = capsys.readouterr().out
        assert f"pipeline run={run.id} status=completed" in output
        for name in BASELINE_STAGES:
            assert name in output

    async def test_run_baseline_dry_run(self, session, capsys):
        run = await cli.run_baseline(runner=make_runner(), session=session, dry_run=True)
        assert run.status == "dry_run"
        assert "status=dry_run" in capsys.readouterr().out

    async def test_run_baseline_option_overrides_reach_config(self, session):
        run = await cli.run_baseline(
            runner=make_runner(),
            session=session,
            scan_limit=7,
            candidate_limit=3,
            sync_outcome_limit=5,
            score_limit=50,
            fail_fast=True,
        )
        assert run.config["scan_limit"] == 7
        assert run.config["candidate_limit"] == 3
        assert run.config["sync_outcome_limit"] == 5
        assert run.config["score_limit"] == 50
        assert run.config["fail_fast"] is True

    async def test_pipeline_status_lists_runs_and_stages(self, session, capsys):
        await cli.run_baseline(runner=make_runner(), session=session)
        capsys.readouterr()

        count = await cli.pipeline_status(limit=5, session=session)
        assert count == 1
        output = capsys.readouterr().out
        assert "status=completed" in output
        assert "stages of run" in output
        assert "calibration_report" in output

    async def test_pipeline_status_empty(self, session, capsys):
        assert await cli.pipeline_status(session=session) == 0
        assert "no pipeline runs recorded" in capsys.readouterr().out

    def test_main_wires_run_baseline_and_status(self, monkeypatch):
        captured = {}

        class FakeRun:
            status = "completed"

        async def fake_baseline(**kwargs):
            captured.update(kwargs)
            return FakeRun()

        async def fake_status(limit=5, session=None):
            captured["status_limit"] = limit
            return 1

        monkeypatch.setattr(cli, "run_baseline", fake_baseline)
        monkeypatch.setattr(cli, "pipeline_status", fake_status)
        assert cli.main(["run-baseline", "--scan-limit", "100", "--dry-run"]) == 0
        assert captured["scan_limit"] == 100
        assert captured["dry_run"] is True
        assert cli.main(["pipeline-status", "--limit", "3"]) == 0
        assert captured["status_limit"] == 3
