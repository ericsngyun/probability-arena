"""OPS-013 tests: production-safe tick aggregation.

Per-sub-window commits (seconds of SQLite lock hold, not one long transaction),
bounded retry on a locked database, loud (never silent) failed/oversized
windows, the audit spine (tick_aggregation_runs, migration 0024), the
flag-gated scheduled path (ENABLE_TICK_AGGREGATION_TIMER=false default), timer
artifacts that exist but are never auto-installed, and the raw-retention
READINESS report (evidence only — enacts nothing; raw retention unchanged).
Storage/durability only — buckets are telemetry summaries, never trading
signals. No live network; in-memory SQLite.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import cli
from app.config import Settings
from app.db import Base
from app.models import MarketPriceTick, MarketPriceTickBucket, TickAggregationRun
from app.services.retention import RetentionConfig, RetentionService
from app.services.tick_aggregation import (
    READINESS_CLEAN_CYCLES,
    READINESS_COVERAGE_RATE,
    TickAggregationReportService,
    TickAggregationService,
    bucket_start_for,
)

REPO = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def tick(session, ticker="KXMLBGAME-1", *, at, bid=40, ask=44, liq=1000):
    session.add(MarketPriceTick(
        market_ticker=ticker, observed_at=at, yes_bid=bid, yes_ask=ask,
        midpoint=round((bid + ask) / 200, 4), spread=ask - bid,
        volume_24h=100, liquidity_proxy=liq, created_at=at,
    ))


def seed_hours(session, n_hours, per_hour=3, ticker="KXMLBGAME-1"):
    """`per_hour` ticks spaced WITHIN each of the last n_hours full hours."""
    step = 60 // per_hour
    for h in range(1, n_hours + 1):
        start = bucket_start_for(NOW - timedelta(hours=h), 3600)
        for i in range(per_hour):
            tick(session, ticker, at=start + timedelta(minutes=step * i, seconds=30))
    session.commit()


def svc(**kw) -> TickAggregationService:
    return TickAggregationService(settings=Settings(_env_file=None, **kw))


# --- per-sub-window commits -----------------------------------------------------


class TestPerSubwindowCommits:
    def test_commits_once_per_subwindow_not_once_per_pass(self, session, monkeypatch):
        """The OPS-012 failure mode was ONE long commit for the whole pass. Now
        each 1h sub-window commits separately (plus 2 tiny audit-row commits)."""
        seed_hours(session, 4)
        commits = []
        real_commit = session.commit
        monkeypatch.setattr(session, "commit", lambda: (commits.append(1), real_commit())[1])

        stats = svc().aggregate(session, hours=4)

        data_windows = [s for s in stats.subwindows if s.buckets_written]
        assert len(data_windows) == 4
        # 4 data commits + open/close audit-row commits
        assert len(commits) == len(data_windows) + 2
        assert stats.max_commit_ms >= 0
        assert all(s.status == "ok" for s in stats.subwindows)

    def test_subwindow_stats_reported_per_window(self, session):
        seed_hours(session, 3)
        stats = svc().aggregate(session, hours=3)

        assert len(stats.subwindows) >= 3
        for s in stats.subwindows:
            assert s.end == s.start + timedelta(hours=1)
            assert s.duration_ms >= 0 and s.commit_ms >= 0

    def test_configurable_subwindow_hours(self, session, monkeypatch):
        seed_hours(session, 4)
        commits = []
        real_commit = session.commit
        monkeypatch.setattr(session, "commit", lambda: (commits.append(1), real_commit())[1])

        stats = svc().aggregate(session, hours=4, subwindow_hours=2)

        data_windows = [s for s in stats.subwindows if s.buckets_written]
        assert len(data_windows) == 2          # two 2h windows
        assert len(commits) == 2 + 2           # + audit open/close
        assert stats.subwindow_hours == 2

    def test_invalid_subwindow_hours_rejected(self, session):
        with pytest.raises(ValueError):
            svc().aggregate(session, subwindow_hours=0)

    def test_idempotent_across_subwindows(self, session):
        seed_hours(session, 3)
        s1 = svc().aggregate(session, hours=3)
        s2 = svc().aggregate(session, hours=3)

        assert s1.buckets_inserted > 0 and s1.buckets_updated == 0
        assert s2.buckets_inserted == 0 and s2.buckets_updated == s1.buckets_inserted
        # no duplicates
        keys = [(b.market_ticker, b.bucket_start) for b in
                session.query(MarketPriceTickBucket).all()]
        assert len(keys) == len(set(keys))

    def test_dry_run_writes_nothing_including_audit_row(self, session):
        seed_hours(session, 2)
        stats = svc().aggregate(session, hours=2, dry_run=True)

        assert stats.buckets_written > 0        # reported
        assert stats.run_id is None
        assert session.query(MarketPriceTickBucket).count() == 0
        assert session.query(TickAggregationRun).count() == 0


# --- bounded retry / loud failure ------------------------------------------------


class FlakyCommitSession:
    """Wraps a Session; commit raises OperationalError the first `fail_n` times
    it is called through the wrapper (synthetic locked-database)."""

    def __init__(self, session, fail_n):
        self._session = session
        self._fails_left = fail_n
        self.commit_attempts = 0

    def commit(self):
        self.commit_attempts += 1
        if self._fails_left > 0:
            self._fails_left -= 1
            raise OperationalError("database is locked", None, Exception("locked"))
        return self._session.commit()

    def __getattr__(self, name):
        return getattr(self._session, name)


class TestBoundedRetry:
    def test_retry_recovers_from_transient_lock(self, session, monkeypatch):
        monkeypatch.setattr("app.services.tick_aggregation.time.sleep", lambda s: None)
        seed_hours(session, 1)
        flaky = FlakyCommitSession(session, fail_n=2)  # audit-open commit fails twice

        stats = svc(tick_aggregation_busy_retries=3).aggregate(flaky, hours=1)

        assert stats.failed_windows == []
        assert stats.buckets_written > 0
        run = session.query(TickAggregationRun).one()
        assert run.status == "ok"

    def test_exhausted_retries_record_failed_window_loudly_and_continue(
        self, session, monkeypatch
    ):
        """A window whose commit never succeeds is rolled back, recorded in
        failed_windows AND on the audit row — never silently skipped — and the
        pass continues to later windows."""
        monkeypatch.setattr("app.services.tick_aggregation.time.sleep", lambda s: None)
        seed_hours(session, 3)

        service = svc(tick_aggregation_busy_retries=1)
        # fail only the FIRST data sub-window's commit (attempt #2 overall:
        # attempt 1 = audit-row open; attempts 2+3 = its retry) — later windows fine
        class FirstWindowFails(FlakyCommitSession):
            def commit(self):
                self.commit_attempts += 1
                if self.commit_attempts in (2, 3):
                    raise OperationalError("database is locked", None, Exception("locked"))
                return self._session.commit()

        flaky = FirstWindowFails(session, fail_n=0)
        stats = service.aggregate(flaky, hours=3)

        assert len(stats.failed_windows) == 1
        ok_windows = [s for s in stats.subwindows if s.status == "ok" and s.buckets_written]
        assert len(ok_windows) == 2  # later windows still processed
        run = session.query(TickAggregationRun).one()
        assert run.status == "error"
        assert run.error_type == "SubwindowCommitFailed"
        assert run.failed_windows  # loud on the audit spine too

    def test_failed_window_repaired_by_rerun(self, session, monkeypatch):
        monkeypatch.setattr("app.services.tick_aggregation.time.sleep", lambda s: None)
        seed_hours(session, 2)

        class FirstWindowFails(FlakyCommitSession):
            def commit(self):
                self.commit_attempts += 1
                if self.commit_attempts in (2, 3):
                    raise OperationalError("database is locked", None, Exception("locked"))
                return self._session.commit()

        svc(tick_aggregation_busy_retries=1).aggregate(FirstWindowFails(session, 0), hours=2)
        stats2 = svc().aggregate(session, hours=2)  # clean rerun

        assert stats2.failed_windows == []
        hours_covered = {b.bucket_start.replace(minute=0, second=0)
                         for b in session.query(MarketPriceTickBucket).all()}
        assert len(hours_covered) >= 2

    def test_oversized_subwindow_skipped_loudly(self, session):
        seed_hours(session, 2, per_hour=5)
        stats = svc(tick_aggregation_max_rows_per_subwindow=3).aggregate(session, hours=2)

        assert len(stats.oversized_windows) == 2
        assert all(s.status == "oversized_skipped" for s in stats.subwindows
                   if s.start.isoformat() in stats.oversized_windows)
        run = session.query(TickAggregationRun).one()
        assert run.oversized_windows  # recorded on the audit spine


# --- scheduled gate ----------------------------------------------------------------


class TestScheduledGate:
    def test_scheduled_noop_when_flag_false(self, session, monkeypatch, capsys):
        import app.config as config_module

        monkeypatch.setattr(config_module, "get_settings",
                            lambda: Settings(_env_file=None, enable_tick_aggregation_timer=False))
        monkeypatch.setattr(cli, "get_settings",
                            lambda: Settings(_env_file=None, enable_tick_aggregation_timer=False),
                            raising=False)
        seed_hours(session, 1)

        n = asyncio.run(cli.aggregate_market_ticks(scheduled=True, session=session))
        out = capsys.readouterr().out
        assert n == 0
        assert "ENABLE_TICK_AGGREGATION_TIMER=false" in out
        assert session.query(TickAggregationRun).count() == 0
        assert session.query(MarketPriceTickBucket).count() == 0

    def test_scheduled_proceeds_when_flag_true(self, session, monkeypatch):
        import app.config as config_module

        s = Settings(_env_file=None, enable_tick_aggregation_timer=True)
        monkeypatch.setattr(config_module, "get_settings", lambda: s)

        seed_hours(session, 1)
        n = asyncio.run(cli.aggregate_market_ticks(scheduled=True, hours=1, session=session))

        assert n > 0
        run = session.query(TickAggregationRun).one()
        assert run.scheduled is True

    def test_manual_always_allowed_when_flag_false(self, session, monkeypatch):
        import app.config as config_module

        monkeypatch.setattr(config_module, "get_settings",
                            lambda: Settings(_env_file=None, enable_tick_aggregation_timer=False))
        seed_hours(session, 1)

        n = asyncio.run(cli.aggregate_market_ticks(hours=1, session=session))
        assert n > 0

    def test_flag_default_is_false(self):
        assert Settings(_env_file=None).enable_tick_aggregation_timer is False


# --- timer artifacts ----------------------------------------------------------------


class TestTimerArtifacts:
    def test_unit_files_exist_with_scheduled_gate(self):
        timer = REPO / "infra/systemd/user/probability-arena-tick-aggregation.timer"
        service = REPO / "infra/systemd/user/probability-arena-tick-aggregation.service"
        assert timer.exists() and service.exists()
        assert "NOT auto-installed" in timer.read_text()
        body = service.read_text()
        assert "--scheduled" in body
        assert "ENABLE_TICK_AGGREGATION_TIMER" in body
        assert "never" in body.lower()  # boundary language present

    def test_unit_files_have_no_forbidden_vocabulary(self):
        for name in ("probability-arena-tick-aggregation.timer",
                     "probability-arena-tick-aggregation.service"):
            body = (REPO / "infra/systemd/user" / name).read_text().lower()
            for bad in ("arbitrage", "opportunity", "paper trad", "position siz"):
                assert bad not in body


# --- readiness report -----------------------------------------------------------------


def report(session):
    return TickAggregationReportService().build(session, settings=Settings(_env_file=None))


def clean_scheduled_run(session, *, hours_ago=1):
    at = NOW - timedelta(hours=hours_ago)
    session.add(TickAggregationRun(
        status="ok", scheduled=True, started_at=at, finished_at=at,
        window_hours=12, rows_read=100, buckets_written=10, created_at=at,
    ))


class TestReadinessReport:
    def test_not_ready_with_no_runs(self, session):
        seed_hours(session, 2)
        r = report(session)
        assert r.readiness == "not_ready"
        assert any("clean_scheduled_cycles" in x for x in r.readiness_reasons)

    def test_ready_when_all_gates_pass(self, session):
        # full coverage: aggregate everything, fresh raw feed, clean cycles
        seed_hours(session, 5)
        tick(session, at=NOW - timedelta(minutes=2))  # fresh raw feed
        session.commit()
        svc().aggregate(session, hours=6)
        for i in range(READINESS_CLEAN_CYCLES):
            clean_scheduled_run(session, hours_ago=i + 1)
        session.commit()

        r = report(session)
        # the aggregate() call above wrote a clean (manual) run; scheduled count
        # comes from the seeded rows
        assert r.clean_scheduled_cycles >= READINESS_CLEAN_CYCLES
        assert r.coverage_rate_last_72h is not None
        assert r.coverage_rate_last_72h >= READINESS_COVERAGE_RATE
        assert r.raw_feed_fresh is True
        assert r.readiness == "ready_to_stage"
        assert r.readiness_reasons == []

    def test_recent_error_run_blocks_readiness(self, session):
        seed_hours(session, 3)
        tick(session, at=NOW - timedelta(minutes=2))
        session.commit()
        svc().aggregate(session, hours=4)
        for i in range(READINESS_CLEAN_CYCLES):
            clean_scheduled_run(session, hours_ago=i + 1)
        session.add(TickAggregationRun(
            status="error", scheduled=True, started_at=NOW, error_type="SubwindowCommitFailed",
            created_at=NOW,
        ))
        session.commit()

        r = report(session)
        assert r.readiness == "not_ready"
        assert any("recent_runs_with_errors" in x for x in r.readiness_reasons)

    def test_stale_raw_feed_blocks_readiness(self, session):
        seed_hours(session, 3)  # newest raw tick is ~1h old -> not fresh
        svc().aggregate(session, hours=4)
        for i in range(READINESS_CLEAN_CYCLES):
            clean_scheduled_run(session, hours_ago=i + 1)
        session.commit()

        r = report(session)
        assert r.raw_feed_fresh is False
        assert r.readiness == "not_ready"

    def test_readiness_never_changes_retention(self, session):
        """The readiness verdict is evidence only: raw retention is byte-for-byte
        the settings value before and after building the report."""
        s = Settings(_env_file=None)
        before = RetentionConfig.from_settings(s).tick_days
        report(session)
        assert RetentionConfig.from_settings(s).tick_days == before == s.tick_retention_days

    def test_report_cli_prints_readiness(self, session, capsys):
        seed_hours(session, 1)
        session.commit()
        asyncio.run(cli.tick_aggregation_report(session=session))
        out = capsys.readouterr().out
        assert "raw retention reduction READINESS" in out
        assert "enacts nothing" in out


# --- retention of the audit spine ---------------------------------------------------


class TestRunRetention:
    def test_old_finished_runs_pruned_running_kept(self, session):
        old = NOW - timedelta(days=60)
        session.add(TickAggregationRun(status="ok", started_at=old, created_at=old))
        session.add(TickAggregationRun(status="running", started_at=old, created_at=old))
        session.add(TickAggregationRun(status="ok", started_at=NOW, created_at=NOW))
        session.commit()

        counts = RetentionService(RetentionConfig(watcher_run_days=30)).prune(session)
        assert counts["tick_aggregation_runs"] == 1
        remaining = {r.status for r in session.query(TickAggregationRun).all()}
        assert remaining == {"running", "ok"}

    def test_prune_report_includes_runs_table(self, session):
        session.commit()
        rows = RetentionService(RetentionConfig()).prune_report(session)
        assert any(r.table == "tick_aggregation_runs" for r in rows)

    def test_raw_tick_window_still_unchanged(self):
        assert RetentionConfig().tick_days == 7
        s = Settings(_env_file=None)
        assert s.tick_retention_days == 7


# --- CLI options ----------------------------------------------------------------------


class TestCLI:
    def test_new_options_parse(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "aggregate_market_ticks", fake)
        rc = cli.main(["aggregate-market-ticks", "--hours", "12",
                       "--subwindow-hours", "2", "--scheduled"])
        assert rc == 0
        assert captured["subwindow_hours"] == 2
        assert captured["scheduled"] is True

    def test_cli_prints_per_subwindow_summary(self, session, capsys):
        seed_hours(session, 2)
        asyncio.run(cli.aggregate_market_ticks(hours=2, session=session))
        out = capsys.readouterr().out
        assert "sub-windows committed=" in out
        assert "max_commit_ms=" in out

    def test_cli_exit_nonzero_on_failed_window(self, session, monkeypatch, capsys):
        monkeypatch.setattr("app.services.tick_aggregation.time.sleep", lambda s: None)
        monkeypatch.setattr(
            "app.services.tick_aggregation.get_settings",
            lambda: Settings(_env_file=None, tick_aggregation_busy_retries=0),
        )
        seed_hours(session, 1)

        class DataWindowFails(FlakyCommitSession):
            def commit(self):
                self.commit_attempts += 1
                if self.commit_attempts == 2:  # attempt 1 = audit open; 2 = the data window
                    raise OperationalError("database is locked", None, Exception("locked"))
                return self._session.commit()

        flaky = DataWindowFails(session, 0)
        n = asyncio.run(cli.aggregate_market_ticks(hours=1, session=flaky))
        out = capsys.readouterr().out
        assert n == -1
        assert "COMMIT FAILED" in out


# --- safety ---------------------------------------------------------------------------


def test_migration_0024_round_trips(tmp_path):
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path}/t.db"
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0024")
    assert "tick_aggregation_runs" in inspect(create_engine(url)).get_table_names()
    command.downgrade(cfg, "0023")
    assert "tick_aggregation_runs" not in inspect(create_engine(url)).get_table_names()
    command.upgrade(cfg, "0024")


class TestSafety:
    def test_run_model_has_no_forbidden_columns(self):
        cols = set(TickAggregationRun.__table__.columns.keys())
        for bad in ("side", "size", "ev", "expected_value", "profit", "wallet",
                    "order", "arbitrage", "arb", "recommendation", "signal"):
            assert bad not in cols

    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "tick_aggregation.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("arbitrage", "opportunity", "expected_value", "paper_trad",
                    "place_order", "wallet", "private_key", "kelly",
                    "position_siz", "swap", "jupiter"):
            assert bad not in code

    def test_no_live_network(self):
        src = (REPO / "app" / "services" / "tick_aggregation.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src
