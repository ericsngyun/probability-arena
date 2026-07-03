from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import (
    ForecastScoreRecord,
    Market,
    MarketForecastRecord,
    MarketOutcomeRecord,
    MarketPriceTick,
    OpportunitySignal,
    PipelineRun,
    PipelineStageRun,
    WatcherRun,
)
from app.services.retention import PROTECTED_TABLES, RetentionConfig, RetentionService

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def seed_tick(session, ticker="R-1", age_days=0.0) -> MarketPriceTick:
    row = MarketPriceTick(
        market_ticker=ticker,
        observed_at=days_ago(age_days),
        midpoint=0.5,
        volume_24h=0,
        liquidity_proxy=0,
        created_at=days_ago(age_days),
    )
    session.add(row)
    session.commit()
    return row


def seed_watcher_run(session, age_days=0.0) -> WatcherRun:
    row = WatcherRun(status="ok", started_at=days_ago(age_days), created_at=days_ago(age_days))
    session.add(row)
    session.commit()
    return row


def seed_pipeline_run(session, age_days=0.0, status="completed", stages=2) -> PipelineRun:
    run = PipelineRun(
        run_type="baseline",
        status=status,
        started_at=days_ago(age_days),
        created_at=days_ago(age_days),
    )
    session.add(run)
    session.commit()
    for i in range(stages):
        session.add(
            PipelineStageRun(
                pipeline_run_id=run.id,
                stage_name=f"stage_{i}",
                status="completed",
                started_at=days_ago(age_days),
                created_at=days_ago(age_days),
            )
        )
    session.commit()
    return run


def seed_signal(session, age_days=0.0, ticker="R-1") -> OpportunitySignal:
    row = OpportunitySignal(
        market_ticker=ticker,
        signal_type="price_move_threshold",
        signal_status="new",
        observed_at=days_ago(age_days),
        reason="seeded",
        created_at=days_ago(age_days),
    )
    session.add(row)
    session.commit()
    return row


def count(session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar()


CFG = RetentionConfig(tick_days=7, watcher_run_days=30, pipeline_run_days=90, signal_days=0, batch_size=5000)


class TestRetentionService:
    def test_dry_run_counts_without_deleting(self, session):
        seed_tick(session, age_days=30)
        seed_tick(session, age_days=1)
        seed_watcher_run(session, age_days=60)

        counts = RetentionService(CFG).prune(session, dry_run=True)
        assert counts["market_price_ticks"] == 1
        assert counts["watcher_runs"] == 1
        assert count(session, MarketPriceTick) == 2  # nothing deleted
        assert count(session, WatcherRun) == 1

    def test_old_ticks_pruned_recent_preserved(self, session):
        old = seed_tick(session, ticker="OLD", age_days=8)
        recent = seed_tick(session, ticker="RECENT", age_days=6)

        counts = RetentionService(CFG).prune(session)
        assert counts["market_price_ticks"] == 1
        remaining = session.execute(select(MarketPriceTick)).scalars().all()
        assert [t.market_ticker for t in remaining] == ["RECENT"]

    def test_old_watcher_runs_pruned(self, session):
        seed_watcher_run(session, age_days=31)
        seed_watcher_run(session, age_days=29)
        counts = RetentionService(CFG).prune(session)
        assert counts["watcher_runs"] == 1
        assert count(session, WatcherRun) == 1

    def test_old_pipeline_runs_and_stages_pruned_safely(self, session):
        old = seed_pipeline_run(session, age_days=91, stages=3)
        recent = seed_pipeline_run(session, age_days=10, stages=2)
        stale_running = seed_pipeline_run(session, age_days=200, status="running", stages=1)

        counts = RetentionService(CFG).prune(session)
        assert counts["pipeline_runs"] == 1
        assert counts["pipeline_stage_runs"] == 3

        remaining_runs = session.execute(select(PipelineRun)).scalars().all()
        assert {r.id for r in remaining_runs} == {recent.id, stale_running.id}
        # recent run's stages intact; running row untouched regardless of age
        assert count(session, PipelineStageRun) == 3

    def test_signals_kept_forever_at_zero_retention(self, session):
        seed_signal(session, age_days=400)
        counts = RetentionService(CFG).prune(session)
        assert counts["opportunity_signals"] == 0
        assert count(session, OpportunitySignal) == 1

    def test_old_signals_pruned_when_retention_positive(self, session):
        seed_signal(session, age_days=40)
        seed_signal(session, age_days=5)
        cfg = RetentionConfig(signal_days=30)
        counts = RetentionService(cfg).prune(session)
        assert counts["opportunity_signals"] == 1
        assert count(session, OpportunitySignal) == 1

    def test_small_batch_size_deletes_everything(self, session):
        for i in range(7):
            seed_tick(session, ticker=f"B-{i}", age_days=10)
        cfg = RetentionConfig(tick_days=7, batch_size=2)
        counts = RetentionService(cfg).prune(session)
        assert counts["market_price_ticks"] == 7
        assert count(session, MarketPriceTick) == 0

    async def test_protected_intelligence_tables_never_pruned(self, session):
        # Seed old rows in protected tables (far older than every window)
        from tests.test_calibration import seed_forecast, seed_outcome

        forecast = await seed_forecast(session, "R-PROT", probability=0.6)
        await seed_outcome(session, "R-PROT", {"status": "settled", "result": "yes"})
        from app.services.calibration import CalibrationService

        CalibrationService().score_unscored(session)
        ancient = days_ago(1000)
        for model in (MarketOutcomeRecord, MarketForecastRecord, ForecastScoreRecord, Market):
            for row in session.execute(select(model)).scalars():
                row.created_at = ancient if hasattr(row, "created_at") else None
        session.commit()

        before = {
            model: count(session, model)
            for model in (MarketOutcomeRecord, MarketForecastRecord, ForecastScoreRecord, Market)
        }
        aggressive = RetentionConfig(
            tick_days=1, watcher_run_days=1, pipeline_run_days=1, signal_days=1, batch_size=10
        )
        RetentionService(aggressive).prune(session)
        for model, n in before.items():
            assert count(session, model) == n, f"{model.__tablename__} was pruned!"

    def test_protected_tables_constant_covers_intelligence_tables(self):
        for table in (
            "market_outcomes",
            "market_forecasts",
            "forecast_scores",
            "market_research_packets",
            "market_detail_enrichments",
            "market_resolution_assessments",
        ):
            assert table in PROTECTED_TABLES


class TestPipelineRetentionStage:
    async def test_retention_stage_appended_when_enabled(self, session):
        from app.services.pipeline import BASELINE_STAGES, BaselineConfig
        from tests.test_pipeline import make_runner

        seed_tick(session, age_days=30)  # prunable
        cfg = BaselineConfig(
            scan_limit=10, candidate_limit=10, sync_outcome_limit=10, score_limit=100,
            run_retention=True,
        )
        run = await make_runner().run_baseline_pipeline(session, cfg)

        assert run.status == "completed"
        stage_names = [s.stage_name for s in run.stages]
        assert stage_names == list(BASELINE_STAGES) + ["retention"]
        retention_stage = run.stages[-1]
        assert retention_stage.status == "completed"
        assert retention_stage.summary["market_price_ticks"] == 1
        # the pipeline creates no ticks itself; the seeded old tick is gone
        assert count(session, MarketPriceTick) == 0

    async def test_default_config_has_no_retention_stage(self, session):
        from app.services.pipeline import BASELINE_STAGES, BaselineConfig
        from tests.test_pipeline import make_runner

        cfg = BaselineConfig(scan_limit=10, candidate_limit=10)
        run = await make_runner().run_baseline_pipeline(session, cfg)
        assert [s.stage_name for s in run.stages] == list(BASELINE_STAGES)


class TestWatcherRetentionHook:
    async def test_prunes_at_most_once_per_day(self, session):
        from app.services.watcher import RealtimeWatcher, WatcherConfig
        from tests.test_watcher import FrameAdapter, market

        seed_tick(session, ticker="ANCIENT-1", age_days=30)
        seed_tick(session, ticker="ANCIENT-2", age_days=30)

        cfg = WatcherConfig(enable_retention=True, signal_cooldown_seconds=900)
        watcher = RealtimeWatcher(
            adapter=FrameAdapter([[market()], [market()]]), config=cfg
        )
        await watcher.watch_once(session, limit=10)
        # first pass pruned the two ancient ticks
        assert (
            session.execute(
                select(func.count()).select_from(MarketPriceTick).where(
                    MarketPriceTick.market_ticker.like("ANCIENT%")
                )
            ).scalar()
            == 0
        )
        first_prune_at = watcher._last_prune_at
        assert first_prune_at is not None

        seed_tick(session, ticker="ANCIENT-3", age_days=30)
        await watcher.watch_once(session, limit=10)
        # second pass within a day: no prune ran
        assert watcher._last_prune_at == first_prune_at
        assert (
            session.execute(
                select(func.count()).select_from(MarketPriceTick).where(
                    MarketPriceTick.market_ticker == "ANCIENT-3"
                )
            ).scalar()
            == 1
        )

    async def test_disabled_by_default(self, session):
        from app.services.watcher import RealtimeWatcher, WatcherConfig
        from tests.test_watcher import FrameAdapter, market

        seed_tick(session, ticker="ANCIENT-1", age_days=30)
        watcher = RealtimeWatcher(adapter=FrameAdapter([[market()]]), config=WatcherConfig())
        await watcher.watch_once(session, limit=10)
        assert watcher._last_prune_at is None
        assert (
            session.execute(
                select(func.count()).select_from(MarketPriceTick).where(
                    MarketPriceTick.market_ticker == "ANCIENT-1"
                )
            ).scalar()
            == 1
        )


class TestCli:
    async def test_prune_retention_cli_with_overrides(self, session, capsys):
        seed_tick(session, age_days=3)
        deleted = await cli.prune_retention(tick_days=2, session=session)
        assert deleted == 1
        output = capsys.readouterr().out
        assert "retention (deleted):" in output
        assert "market_price_ticks" in output
        assert "(retention disabled)" in output  # signals line annotated

    async def test_prune_retention_cli_dry_run(self, session, capsys):
        seed_tick(session, age_days=30)
        deleted = await cli.prune_retention(dry_run=True, session=session)
        assert deleted == 1
        assert "DRY RUN — would delete" in capsys.readouterr().out
        assert count(session, MarketPriceTick) == 1

    async def test_db_stats_cli(self, session, capsys):
        seed_tick(session)
        seed_watcher_run(session)
        seed_pipeline_run(session, stages=1)
        seed_signal(session)

        total = await cli.db_stats(session=session)
        assert total >= 4
        output = capsys.readouterr().out
        assert "database:" in output
        assert "row counts:" in output
        assert "market_price_ticks" in output
        assert "latest watcher run:" in output
        assert "latest pipeline run:" in output
        assert "signals by status: new=1" in output
        assert "signals by type: price_move_threshold=1" in output

    def test_main_wires_prune_and_stats(self, monkeypatch):
        captured = {}

        async def fake_prune(dry_run=False, tick_days=None, watcher_run_days=None,
                             pipeline_run_days=None, signal_days=None, batch_size=None,
                             session=None):
            captured.update(dry_run=dry_run, tick_days=tick_days, batch_size=batch_size)
            return 0

        async def fake_stats(session=None):
            captured["stats"] = True
            return 0

        monkeypatch.setattr(cli, "prune_retention", fake_prune)
        monkeypatch.setattr(cli, "db_stats", fake_stats)
        assert cli.main(["prune-retention", "--dry-run", "--tick-days", "3", "--batch-size", "10"]) == 0
        assert cli.main(["db-stats"]) == 0
        assert captured == {"dry_run": True, "tick_days": 3, "batch_size": 10, "stats": True}


def test_systemd_retention_artifacts_exist():
    base = Path(__file__).resolve().parents[1] / "infra" / "systemd" / "user"
    service = base / "probability-arena-retention.service"
    timer = base / "probability-arena-retention.timer"
    assert service.is_file() and timer.is_file()
    service_text = service.read_text()
    assert "prune-retention" in service_text
    assert "never pruned" in service_text
    timer_text = timer.read_text()
    assert "OnCalendar=daily" in timer_text
    assert "NOT auto-installed" in timer_text