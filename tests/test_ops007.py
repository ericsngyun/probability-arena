"""OPS-007 tests: MarketOps overlap guard, SQLite busy timeout, and DB
backup/verify/retention utilities. No live network."""

import gzip
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import cli
from app.config import Settings, get_settings
from app.db import Base, connect_args_for
from app.models import MarketOpsRun
from app.services.backup import (
    BackupResult,
    backup_database,
    backup_dir_stats,
    list_backups,
    prune_old_backups,
    verify_backup,
)
from tests.test_marketops import FakeCryptoService, autopilot

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def seed_running_run(session, minutes_ago=1.0) -> MarketOpsRun:
    started = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    run = MarketOpsRun(status="running", started_at=started, created_at=started)
    session.add(run)
    session.commit()
    return run


class TestOverlapGuard:
    async def test_concurrent_run_is_skipped_gracefully(self, session):
        active = seed_running_run(session, minutes_ago=1)
        crypto = FakeCryptoService()
        run = await autopilot(crypto_service=crypto).run_once(session)

        assert run.status == "skipped"
        assert run.summary == {"reason": "already_running", "active_run_id": active.id}
        assert run.duration_ms == 0
        assert crypto.calls == []  # no stage executed
        # active run untouched
        assert session.get(MarketOpsRun, active.id).status == "running"

    async def test_stale_running_run_does_not_wedge(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "marketops_lock_stale_after_minutes", 30)
        seed_running_run(session, minutes_ago=45)  # stale: crashed long ago
        run = await autopilot().run_once(session)
        assert run.status == "ok"  # proceeded despite the stale lock row

    async def test_fresh_run_within_stale_window_blocks(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "marketops_lock_stale_after_minutes", 30)
        seed_running_run(session, minutes_ago=29)
        run = await autopilot().run_once(session)
        assert run.status == "skipped"

    async def test_cli_reports_skip_and_exits_zero(self, session, capsys):
        active = seed_running_run(session)
        exit_code = await cli.marketops_run_once(services=autopilot(), session=session)
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "skipped (already_running" in output
        assert f"#{active.id}" in output

    async def test_normal_cycle_unaffected_when_no_active_run(self, session):
        run = await autopilot().run_once(session)
        assert run.status == "ok"

    async def test_skipped_run_does_not_confuse_cc_alert_baseline(self, session):
        from tests.test_marketops import FakeCCService

        service = autopilot(champion_challenger_service=FakeCCService(pair_count=3))
        first = await service.run_once(session)
        assert first.summary["champion_challenger"]["pair_count"] == 3
        # a skipped run lands between two normal runs
        seed_running_run(session, minutes_ago=1)
        skipped = await service.run_once(session)
        assert skipped.status == "skipped"
        session.execute(
            select(MarketOpsRun).where(MarketOpsRun.status == "running")
        ).scalar_one().status = "ok"  # release the seeded lock
        session.commit()
        second = await service.run_once(session)
        assert second.status == "ok"
        # pair count unchanged -> no duplicate cc alert despite skipped run between
        from app.models import MarketOpsAlert

        cc_alerts = [
            a for a in session.execute(select(MarketOpsAlert)).scalars().all()
            if a.alert_type == "champion_challenger_sample_update"
        ]
        assert len(cc_alerts) == 1


class TestSqliteBusyTimeout:
    def test_sqlite_urls_get_timeout_from_settings(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "sqlite_busy_timeout_ms", 12_000)
        args = connect_args_for("sqlite:///data/probability_arena.db")
        assert args == {"timeout": 12.0}

    def test_non_sqlite_urls_get_no_extra_args(self):
        assert connect_args_for("postgresql+psycopg2://arena:x@localhost/arena") == {}


def make_settings(tmp_path, retention_days=30) -> Settings:
    db_path = tmp_path / "data" / "arena.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        backup_dir=str(tmp_path / "backups"),
        backup_retention_days=retention_days,
    )
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return settings


class TestBackups:
    async def test_backup_creates_compressed_verified_file(self, tmp_path):
        settings = make_settings(tmp_path)
        result = backup_database(settings)
        assert isinstance(result, BackupResult)
        assert result.path.endswith(".db.gz")
        assert result.size_bytes > 0
        with gzip.open(result.path, "rb") as fh:
            assert fh.read(16).startswith(b"SQLite format 3")

        verdict = verify_backup(result.path)
        assert verdict.ok, verdict.detail
        assert verdict.tables >= 5

    def test_list_backups_newest_first(self, tmp_path):
        settings = make_settings(tmp_path)
        backup_database(settings)
        time.sleep(1.1)  # distinct timestamp in filename
        backup_database(settings)
        backups = list_backups(settings)
        assert len(backups) == 2
        assert backups[0].name > backups[1].name

    def test_retention_prunes_old_backups(self, tmp_path):
        settings = make_settings(tmp_path, retention_days=7)
        backup_database(settings)
        old = list_backups(settings)[0]
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old, (ten_days_ago, ten_days_ago))
        pruned = prune_old_backups(settings)
        assert pruned == [old]
        assert list_backups(settings) == []

    def test_backup_run_prunes_inline(self, tmp_path):
        settings = make_settings(tmp_path, retention_days=7)
        backup_database(settings)
        old = list_backups(settings)[0]
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old, (ten_days_ago, ten_days_ago))
        time.sleep(1.1)
        result = backup_database(settings)
        assert old.name in result.pruned
        assert len(list_backups(settings)) == 1

    def test_verify_rejects_garbage_and_incomplete(self, tmp_path):
        garbage = tmp_path / "backup-garbage.db.gz"
        garbage.write_bytes(b"not gzip at all")
        assert not verify_backup(str(garbage)).ok

        # valid gzip of a sqlite db missing expected tables
        raw = tmp_path / "tiny.db"
        conn = sqlite3.connect(raw)
        conn.execute("CREATE TABLE placeholder (id INTEGER)")
        conn.commit()
        conn.close()
        incomplete = tmp_path / "backup-incomplete.db.gz"
        with open(raw, "rb") as src, gzip.open(incomplete, "wb") as dst:
            dst.write(src.read())
        verdict = verify_backup(str(incomplete))
        assert not verdict.ok
        assert "missing expected tables" in verdict.detail

        assert not verify_backup(str(tmp_path / "nope.db.gz")).ok

    def test_non_sqlite_returns_guidance_without_action(self, tmp_path):
        settings = Settings(
            database_url="postgresql+psycopg2://arena:secret@localhost/arena",
            backup_dir=str(tmp_path / "backups"),
        )
        result = backup_database(settings)
        assert isinstance(result, str)
        assert "pg_dump" in result
        assert "secret" not in result  # no credentials leak
        assert not (tmp_path / "backups").exists()

    def test_backup_dir_stats(self, tmp_path):
        settings = make_settings(tmp_path)
        assert backup_dir_stats(settings) is None
        backup_database(settings)
        count, total_mb = backup_dir_stats(settings)
        assert count == 1 and total_mb > 0


class TestCli:
    async def test_backup_cli_roundtrip(self, tmp_path, capsys, monkeypatch):
        settings = make_settings(tmp_path)
        for attr in ("database_url", "backup_dir", "backup_retention_days"):
            monkeypatch.setattr(get_settings(), attr, getattr(settings, attr))

        assert await cli.backup_db() == 0
        output = capsys.readouterr().out
        assert "backup written:" in output

        assert await cli.list_db_backups() == 1
        assert "backup-" in capsys.readouterr().out

        path = str(list_backups(get_settings())[0])
        assert await cli.verify_db_backup(path) == 0
        assert "OK:" in capsys.readouterr().out
        assert await cli.verify_db_backup(str(tmp_path / "missing.gz")) == 1

    def test_main_wires_backup_commands(self, monkeypatch):
        captured = []

        async def fake(*args, **kwargs):
            captured.append(kwargs)
            return 0

        for name in ("backup_db", "list_db_backups", "verify_db_backup"):
            monkeypatch.setattr(cli, name, fake)
        assert cli.main(["backup-db"]) == 0
        assert cli.main(["list-db-backups"]) == 0
        assert cli.main(["verify-db-backup", "some/path.db.gz"]) == 0
        assert len(captured) == 3
