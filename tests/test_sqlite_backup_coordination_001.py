"""SQLITE-BACKUP-COORDINATION-001 — verified, coordinated, bounded SQLite backups.

Covers the backup correctness contract (capacity gate -> exclusive lock ->
online-backup snapshot -> verify -> manifest -> atomic publication -> bounded
tiered retention), the retention safety rules, filesystem permissions and path
confinement, and the shipped systemd units. No live network, no production
database, no provider call.
"""

import configparser
import gzip
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine

import app.models  # noqa: F401  — registers every table on Base.metadata
from app.config import Settings
from app.db import Base
from app.services import backup as bk

REPO = Path(__file__).resolve().parents[1]
UNIT_DIR = REPO / "infra/systemd/user"
SERVICE_UNIT = UNIT_DIR / "probability-arena-backup.service"
TIMER_UNIT = UNIT_DIR / "probability-arena-backup.timer"


def make_settings(tmp_path, backup_dir=None) -> Settings:
    db_path = tmp_path / "data" / "arena.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        backup_dir=str(backup_dir or (tmp_path / "backups")),
    )
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return settings


def touch_backup(directory: Path, stamp: str) -> Path:
    """A byte-valid gzip placeholder with a managed name (retention only ever
    inspects names/paths, never contents)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{bk.BACKUP_PREFIX}{stamp}{bk.BACKUP_SUFFIX}"
    with gzip.open(path, "wb") as fh:
        fh.write(b"placeholder")
    return path


class TestBackupContract:
    def test_dry_run_creates_nothing(self, tmp_path):
        settings = make_settings(tmp_path)
        result = bk.backup_database(settings, dry_run=True)
        assert result.status == "dry_run"
        assert result.path == ""
        assert bk.list_backups(settings) == []

    def test_confirmed_backup_publishes_artifact_and_manifest(self, tmp_path):
        settings = make_settings(tmp_path)
        result = bk.backup_database(settings)
        assert result.status == "success"
        artifact = Path(result.path)
        manifest = Path(result.manifest_path)
        assert artifact.is_file() and manifest.is_file()
        assert artifact.name.startswith(bk.BACKUP_PREFIX)
        assert artifact.name.endswith(bk.BACKUP_SUFFIX)
        assert manifest.name.endswith(bk.MANIFEST_SUFFIX)

    def test_manifest_matches_artifact_hash_and_size(self, tmp_path):
        settings = make_settings(tmp_path)
        result = bk.backup_database(settings)
        data = json.loads(Path(result.manifest_path).read_text())
        artifact = Path(result.path)
        assert data["sha256"] == bk._sha256(artifact) == result.sha256
        assert data["backup_bytes"] == artifact.stat().st_size
        assert data["backup_filename"] == artifact.name
        assert data["integrity_check"] == "ok"
        assert data["status"] == "verified"
        assert data["manifest_version"] == bk.MANIFEST_VERSION
        assert sorted(bk.EXPECTED_TABLES) == data["required_tables_present"]
        assert isinstance(data["selected_table_counts"], dict)

    def test_manifest_contains_no_secrets_or_credentials(self, tmp_path):
        settings = make_settings(tmp_path)
        result = bk.backup_database(settings)
        raw = Path(result.manifest_path).read_text().lower()
        for banned in ("password", "secret", "api_key", "apikey", "token=",
                       "authorization", "bearer", "postgresql://", "sk-"):
            assert banned not in raw
        data = json.loads(Path(result.manifest_path).read_text())
        # source path is redacted to a bare filename
        assert data["source_database_path_redacted"].startswith(".../")
        assert str(tmp_path) not in data["source_database_path_redacted"]

    def test_verification_reports_integrity_tables_and_revision(self, tmp_path):
        settings = make_settings(tmp_path)
        result = bk.backup_database(settings)
        verdict = bk.verify_backup(result.path)
        assert verdict.ok, verdict.detail
        assert verdict.tables >= 5
        assert verdict.sha256 == result.sha256
        # create_all test DBs legitimately lack alembic_version
        assert verdict.alembic_revision is None or isinstance(
            verdict.alembic_revision, str)

    def test_source_unchanged_and_inode_differs(self, tmp_path):
        settings = make_settings(tmp_path)
        db_path = bk._sqlite_path(settings)
        before = (db_path.stat().st_size, bk._sha256(db_path))
        result = bk.backup_database(settings)
        after = (db_path.stat().st_size, bk._sha256(db_path))
        assert before == after
        assert db_path.stat().st_ino != Path(result.path).stat().st_ino

    def test_backup_opens_read_only_and_outside_live_db(self, tmp_path):
        settings = make_settings(tmp_path)
        result = bk.backup_database(settings)
        db_path = bk._sqlite_path(settings)
        assert Path(result.path).resolve() != db_path.resolve()
        raw = tmp_path / "restored.db"
        with gzip.open(result.path, "rb") as src, open(raw, "wb") as dst:
            shutil.copyfileobj(src, dst)
        conn = sqlite3.connect(f"file:{raw}?mode=ro", uri=True)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE readonly_probe (id INTEGER)")
        finally:
            conn.close()

    def test_uses_online_backup_api_not_raw_copy(self):
        source = (REPO / "app/services/backup.py").read_text()
        assert "source.backup(" in source
        assert "shutil.copy2(" not in source

    def test_no_provider_or_network_surface(self):
        source = (REPO / "app/services/backup.py").read_text().lower()
        for banned in ("httpx", "requests.", "aiohttp", "urllib.request",
                       "solana", "birdeye", "goplus", "dexscreener"):
            assert banned not in source


class TestPublicationSafety:
    def test_verification_failure_prevents_publication(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path)
        monkeypatch.setattr(
            bk, "verify_backup",
            lambda path, **_kw: bk.VerifyResult(ok=False, detail="forced failure"),
        )
        result = bk.backup_database(settings)
        assert result.status == "failed_verification"
        assert result.path == ""
        assert bk.list_backups(settings) == []
        directory = bk._backup_dir(settings)
        assert not any(p.name.endswith(bk.PARTIAL_SUFFIX) for p in directory.iterdir())

    def test_interrupted_backup_leaves_no_partial_or_published_file(
        self, tmp_path, monkeypatch
    ):
        settings = make_settings(tmp_path)

        def boom(*_args, **_kwargs):
            raise OSError("simulated interruption")

        monkeypatch.setattr(bk.shutil, "copyfileobj", boom)
        with pytest.raises(OSError):
            bk.backup_database(settings)
        directory = bk._backup_dir(settings)
        leftovers = [p.name for p in directory.iterdir()
                     if p.name != bk.LOCK_FILENAME]
        assert leftovers == []
        assert bk.list_backups(settings) == []

    def test_capacity_shortfall_skips_without_writing(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path)
        touch_backup(bk._backup_dir(settings), "20260101T000000Z")
        monkeypatch.setattr(bk, "free_bytes", lambda _d: 1)
        result = bk.backup_database(settings)
        assert result.status == "skipped_capacity"
        assert result.path == "" and result.pruned == []
        # the pre-existing backup and the live DB are untouched
        assert len(bk.list_backups(settings)) == 1
        assert bk._sqlite_path(settings).exists()

    def test_capacity_requirement_includes_every_concurrent_copy(self):
        # raw snapshot + gzip + verification temp all coexist on the same volume
        assert bk.capacity_required(1000) == (
            bk.CAPACITY_MULTIPLIER * 1000 + bk.CAPACITY_MARGIN_BYTES)


class TestConcurrency:
    def test_lock_prevents_overlapping_backup(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        with bk._backup_lock(directory) as held:
            assert held is True
            result = bk.backup_database(settings)
        assert result.status == "skipped_overlap"
        assert result.path == ""
        assert bk.list_backups(settings) == []

    def test_overlap_skip_is_not_a_successful_backup(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        with bk._backup_lock(directory):
            result = bk.backup_database(settings)
        assert result.status != "success"

    def test_lock_released_after_use_so_next_run_succeeds(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        with bk._backup_lock(directory) as held:
            assert held
        assert bk.backup_database(settings).status == "success"

    def test_lock_acquisition_is_non_blocking(self, tmp_path):
        """No unbounded wait: a contended lock returns immediately."""
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        with bk._backup_lock(directory):
            started = time.monotonic()
            result = bk.backup_database(settings)
            assert time.monotonic() - started < 5.0
        assert result.status == "skipped_overlap"

    def test_lock_file_is_not_the_database(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        with bk._backup_lock(directory):
            pass
        lock = directory / bk.LOCK_FILENAME
        assert lock.exists()
        assert lock.resolve() != bk._sqlite_path(settings).resolve()


class TestPermissionsAndConfinement:
    def test_directory_and_file_modes(self, tmp_path):
        settings = make_settings(tmp_path)
        result = bk.backup_database(settings)
        directory = bk._backup_dir(settings)
        assert os.stat(directory).st_mode & 0o777 == bk.DIR_MODE
        assert os.stat(result.path).st_mode & 0o777 == bk.FILE_MODE
        assert os.stat(result.manifest_path).st_mode & 0o777 == bk.FILE_MODE

    def test_symlinked_backup_dir_is_rejected(self, tmp_path):
        real = tmp_path / "real_backups"
        real.mkdir()
        link = tmp_path / "linked_backups"
        link.symlink_to(real, target_is_directory=True)
        settings = make_settings(tmp_path, backup_dir=link)
        with pytest.raises(ValueError):
            bk.backup_database(settings)

    def test_verify_rejects_symlinked_artifact(self, tmp_path):
        settings = make_settings(tmp_path)
        result = bk.backup_database(settings)
        link = tmp_path / "link.db.gz"
        link.symlink_to(result.path)
        assert bk.verify_backup(str(link)).ok is False

    def test_confinement_rejects_symlink_and_traversal(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside.db.gz"
        outside.write_bytes(b"x")
        link = root / "backup-20260101T000000Z.db.gz"
        link.symlink_to(outside)
        assert bk._confined(link, root) is False
        assert bk._confined(root / ".." / "outside.db.gz", root) is False
        real = touch_backup(root, "20260102T000000Z")
        assert bk._confined(real, root) is True

    def test_symlinked_backup_is_skipped_never_deleted(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        outside = tmp_path / "precious.db.gz"
        outside.write_bytes(b"precious")
        for stamp in ("20260401T000000Z", "20260402T000000Z"):
            touch_backup(directory, stamp)
        link = directory / "backup-20250101T000000Z.db.gz"
        link.symlink_to(outside)
        plan = bk.apply_retention(settings, dry_run=False)
        assert link.name in plan.skipped
        assert link.name not in plan.deleted
        assert outside.exists() and outside.read_bytes() == b"precious"


class TestRetention:
    def test_dry_run_deletes_nothing(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        for day in range(1, 15):
            touch_backup(directory, f"202604{day:02d}T000000Z")
        before = {p.name for p in bk.list_backups(settings)}
        plan = bk.apply_retention(settings, dry_run=True)
        assert plan.dry_run is True
        assert plan.deleted  # there is something it *would* delete
        assert {p.name for p in bk.list_backups(settings)} == before

    def test_newest_backup_is_never_deleted(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        newest = touch_backup(directory, "20260601T120000Z")
        for day in range(1, 20):
            touch_backup(directory, f"202601{day:02d}T000000Z")
        plan = bk.apply_retention(settings, dry_run=False)
        assert newest.name in plan.retained
        assert newest.name not in plan.deleted
        assert newest.exists()

    def test_single_old_backup_is_retained(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        only = touch_backup(directory, "20200101T000000Z")
        plan = bk.apply_retention(settings, dry_run=False)
        assert plan.deleted == []
        assert only.exists()

    def test_tiered_classification_keeps_daily_weekly_monthly(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        base = datetime(2026, 6, 30, tzinfo=timezone.utc)
        for offset in range(0, 200, 3):  # ~67 backups spanning ~6 months
            stamp = (base - timedelta(days=offset)).strftime(bk.STAMP_FORMAT)
            touch_backup(directory, stamp)
        plan = bk.apply_retention(settings, dry_run=True)
        assert len(plan.retained) <= bk.RETAIN_DAILY + bk.RETAIN_WEEKLY + bk.RETAIN_MONTHLY
        assert plan.deleted, "old backups beyond the tiers should be pruned"
        # every retained/deleted name is a managed backup name
        for name in plan.retained + plan.deleted:
            assert bk._is_backup_name(name)

    def test_same_day_duplicates_kept_within_recency_floor(self, tmp_path):
        """An operator's manual backup taken just before a risky change must
        survive that night's scheduled run: the newest RETAIN_DAILY backups
        are always kept, regardless of which day they fall on."""
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        a = touch_backup(directory, "20260501T010000Z")
        b = touch_backup(directory, "20260501T020000Z")
        plan = bk.apply_retention(settings, dry_run=True)
        assert a.name in plan.retained and b.name in plan.retained
        assert plan.deleted == []

    def test_same_day_duplicates_collapse_beyond_recency_floor(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        # two backups on one old day, plus enough newer ones to exhaust the floor
        old_a = touch_backup(directory, "20260401T010000Z")
        old_b = touch_backup(directory, "20260401T020000Z")
        for day in range(10, 10 + bk.RETAIN_DAILY):
            touch_backup(directory, f"202605{day:02d}T000000Z")
        plan = bk.apply_retention(settings, dry_run=True)
        # beyond the floor only the newest of that day survives, via the daily tier
        assert old_b.name in plan.retained
        assert old_a.name in plan.deleted

    def test_unrelated_files_are_never_deleted(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        for day in range(1, 15):
            touch_backup(directory, f"202604{day:02d}T000000Z")
        strangers = []
        for name in ("notes.txt", "backup-not-a-stamp.db.gz",
                     "backup-20260101T000000Z.db", "important.sqlite3"):
            path = directory / name
            path.write_bytes(b"keep me")
            strangers.append(path)
        bk.apply_retention(settings, dry_run=False)
        for path in strangers:
            assert path.exists(), f"{path.name} must never be deleted"

    def test_retention_is_confined_to_backup_root(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        sibling = tmp_path / "backup-20260101T000000Z.db.gz"
        sibling.write_bytes(b"outside the root")
        for day in range(1, 15):
            touch_backup(directory, f"202605{day:02d}T000000Z")
        bk.apply_retention(settings, dry_run=False)
        assert sibling.exists()

    def test_manifest_removed_with_its_backup(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        older = touch_backup(directory, "20260401T010000Z")
        touch_backup(directory, "20260401T020000Z")  # same day, newer
        for day in range(10, 10 + bk.RETAIN_DAILY):  # exhaust the recency floor
            touch_backup(directory, f"202605{day:02d}T000000Z")
        manifest = directory / (
            older.name[:-len(bk.BACKUP_SUFFIX)] + bk.MANIFEST_SUFFIX)
        manifest.write_text("{}")
        bk.apply_retention(settings, dry_run=False)
        assert not older.exists() and not manifest.exists()

    def test_retention_runs_only_after_verified_publication(
        self, tmp_path, monkeypatch
    ):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        for stamp in ("20260501T010000Z", "20260501T020000Z"):
            touch_backup(directory, stamp)
        before = {p.name for p in bk.list_backups(settings)}
        monkeypatch.setattr(
            bk, "verify_backup",
            lambda path, **_kw: bk.VerifyResult(ok=False, detail="forced"),
        )
        assert bk.backup_database(settings).status == "failed_verification"
        assert {p.name for p in bk.list_backups(settings)} == before


class TestSystemdUnits:
    def test_units_exist(self):
        assert SERVICE_UNIT.is_file() and TIMER_UNIT.is_file()

    def _parse(self, path: Path) -> configparser.ConfigParser:
        # interpolation=None: systemd specifiers such as %h are literal, not
        # configparser interpolation syntax.
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        parser.read_string(path.read_text())
        return parser

    def test_service_is_user_level_oneshot_without_loop(self):
        service = self._parse(SERVICE_UNIT)
        assert service["Service"]["Type"] == "oneshot"
        text = SERVICE_UNIT.read_text()
        assert "Restart=always" not in text
        assert "User=" not in text and "Group=" not in text  # user-level only
        assert "sudo" not in text
        # recurrence belongs to the timer, never a loop inside the service
        for banned in ("while true", "loop", "--loop", "sleep "):
            assert banned not in text

    def test_service_uses_fixed_venv_python_and_paths(self):
        service = self._parse(SERVICE_UNIT)
        exec_start = service["Service"]["ExecStart"]
        assert ".venv/bin/python" in exec_start
        assert "-m app.cli backup-db" in exec_start
        assert service["Service"]["WorkingDirectory"].endswith(
            "projects/probability-arena")
        assert service["Service"]["EnvironmentFile"].endswith(".env")
        assert service["Service"]["NoNewPrivileges"] == "true"

    def test_timer_is_daily_persistent_and_owns_recurrence(self):
        timer = self._parse(TIMER_UNIT)
        assert timer["Timer"]["Persistent"] == "true"
        assert timer["Timer"]["Unit"] == "probability-arena-backup.service"
        assert timer["Install"]["WantedBy"] == "timers.target"
        on_calendar = timer["Timer"]["OnCalendar"]
        assert on_calendar.startswith("*-*-*")  # daily
        # avoid the crowded retention/baseline window at 00:00-00:06
        hour, minute, _ = on_calendar.split()[-1].split(":")
        assert not (hour == "00" and int(minute) <= 6)

    def test_timer_randomized_delay_is_bounded(self):
        timer = self._parse(TIMER_UNIT)
        delay = timer["Timer"].get("RandomizedDelaySec", "0")
        assert int(delay) <= 900  # bounded jitter only

    @pytest.mark.skipif(
        shutil.which("systemd-analyze") is None,
        reason="systemd-analyze unavailable (non-Linux dev host)",
    )
    def test_rendered_units_verify(self, tmp_path):
        for unit in (SERVICE_UNIT, TIMER_UNIT):
            shutil.copy2(unit, tmp_path / unit.name)
        result = subprocess.run(
            ["systemd-analyze", "verify",
             str(tmp_path / SERVICE_UNIT.name), str(tmp_path / TIMER_UNIT.name)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr


class TestIsolation:
    def test_no_pragma_migration_or_transaction_change(self):
        source = (REPO / "app/services/backup.py").read_text()
        # only query-form pragmas against the source database
        assert 'PRAGMA journal_mode"' in source or "PRAGMA journal_mode" in source
        for banned in ("journal_mode=WAL", "journal_mode = WAL", "PRAGMA synchronous =",
                       "PRAGMA busy_timeout =", "alembic upgrade", "op.execute"):
            assert banned not in source

    def test_backup_failure_is_isolated_from_callers(self, tmp_path, monkeypatch):
        """A skipped backup returns a bounded result rather than raising, so a
        oneshot caller (systemd) never sees an unbounded failure."""
        settings = make_settings(tmp_path)
        monkeypatch.setattr(bk, "free_bytes", lambda _d: 0)
        result = bk.backup_database(settings)
        assert result.status == "skipped_capacity"

    def test_telemetry_is_not_written_into_the_application_database(self, tmp_path):
        settings = make_settings(tmp_path)
        bk.backup_database(settings)
        conn = sqlite3.connect(bk._sqlite_path(settings))
        try:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()
        assert not any("telemetry" in name.lower() for name in tables)


class TestPartialFileHardening:
    """Security-review required conditions: in-flight working files must be
    0600 from birth and must never be written through a planted symlink."""

    def test_planted_symlink_at_partial_is_refused(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        victim = tmp_path / "victim.txt"
        victim.write_text("do not clobber me")
        # _create_private must refuse any pre-existing path, symlink or not
        planted = directory / "planted.part"
        planted.symlink_to(victim)
        with pytest.raises(OSError):
            bk._create_private(planted)
        assert victim.read_text() == "do not clobber me"

    def test_create_private_is_exclusive_and_0600(self, tmp_path):
        directory = tmp_path / "d"
        directory.mkdir()
        target = directory / "artifact.part"
        fd = bk._create_private(target)
        os.close(fd)
        assert os.stat(target).st_mode & 0o777 == bk.FILE_MODE
        with pytest.raises(FileExistsError):
            bk._create_private(target)  # O_EXCL: never silently overwrites

    def test_in_flight_raw_snapshot_is_never_group_readable(self, tmp_path):
        """The raw snapshot holds a full plaintext copy of the database while it
        is being written; it must be 0600 for that entire window, not only after
        a trailing chmod."""
        settings = make_settings(tmp_path)
        seen = {}
        real_connect = bk.sqlite3.connect

        def spy(target, *args, **kwargs):
            path = Path(str(target))
            if path.name.endswith(f".raw{bk.PARTIAL_SUFFIX}") and path.exists():
                seen["mode"] = os.stat(path).st_mode & 0o777
            return real_connect(target, *args, **kwargs)

        with pytest.MonkeyPatch.context() as m:
            m.setattr(bk.sqlite3, "connect", spy)
            assert bk.backup_database(settings).status == "success"
        assert seen.get("mode") == bk.FILE_MODE

    def test_retention_rejects_symlinked_root_on_destructive_path(self, tmp_path):
        real = tmp_path / "real_backups"
        real.mkdir()
        link = tmp_path / "linked_backups"
        link.symlink_to(real, target_is_directory=True)
        settings = make_settings(tmp_path, backup_dir=link)
        with pytest.raises(ValueError):
            bk.apply_retention(settings, dry_run=False)

    def test_stale_partials_are_reaped_but_published_backups_are_not(self, tmp_path):
        settings = make_settings(tmp_path)
        directory = bk._ensure_backup_dir(bk._backup_dir(settings))
        keeper = touch_backup(directory, "20260601T000000Z")
        stale_gz = directory / "backup-20260101T000000Z.db.gz.part"
        stale_raw = directory / ".backup-20260101T000000Z.raw.part"
        for path in (stale_gz, stale_raw):
            path.write_bytes(b"leftover")
            old = time.time() - 12 * 3600
            os.utime(path, (old, old))
        fresh = directory / "backup-20260602T000000Z.db.gz.part"
        fresh.write_bytes(b"in flight")
        reaped = bk._reap_stale_partials(directory)
        assert stale_gz.name in reaped and stale_raw.name in reaped
        assert not stale_gz.exists() and not stale_raw.exists()
        assert fresh.exists(), "recent working files must be left alone"
        assert keeper.exists(), "published backups are never reaped"


class TestWriterCoordination:
    def test_online_copy_is_chunked_not_single_step(self):
        """journal_mode=delete + a 30s busy timeout means a single-step copy of a
        multi-GiB database would block every writer's COMMIT past the busy
        handler. The copy must be bounded per step."""
        source = (REPO / "app/services/backup.py").read_text()
        assert "pages=BACKUP_PAGES_PER_STEP" in source
        assert "sleep=BACKUP_STEP_SLEEP_SECONDS" in source
        assert bk.BACKUP_PAGES_PER_STEP > 0

    def test_contention_is_bounded_and_returns_skip_not_raise(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path)

        real_connect = bk.sqlite3.connect

        class _Contended:
            def __init__(self, conn):
                self._conn = conn

            def backup(self, *_a, **_kw):
                raise bk.BackupContentionError("simulated restart storm")

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def fake_connect(target, *a, **kw):
            conn = real_connect(target, *a, **kw)
            path = Path(str(target))
            return _Contended(conn) if path.name.endswith(".db") else conn

        monkeypatch.setattr(bk.sqlite3, "connect", fake_connect)
        result = bk.backup_database(settings)
        assert result.status == "skipped_contention"
        assert result.path == ""
        assert bk.list_backups(settings) == []
        directory = bk._backup_dir(settings)
        assert not any(p.name.endswith(bk.PARTIAL_SUFFIX)
                       for p in directory.iterdir())

    def test_capacity_covers_verification_temp_copy(self):
        """Verification decompresses a third full copy, so the gate is 3x."""
        assert bk.CAPACITY_MULTIPLIER >= 3
        assert bk.capacity_required(1000) == 3000 + bk.CAPACITY_MARGIN_BYTES

    def test_verification_temp_is_pinned_to_backup_filesystem(self):
        source = (REPO / "app/services/backup.py").read_text()
        assert "verify_backup(str(partial), tmp_dir=directory)" in source


class TestUnitHardening:
    def _text(self):
        return SERVICE_UNIT.read_text()

    def test_requires_mount_and_pins_tmpdir(self):
        text = self._text()
        assert "RequiresMountsFor=/mnt/data" in text
        assert "Environment=TMPDIR=" in text and "/mnt/data" in text

    def test_timeout_has_headroom_over_internal_deadline(self):
        text = self._text()
        assert "TimeoutStartSec=45min" in text
        # systemd must never be what kills the Python cleanup path
        assert bk.BACKUP_DEADLINE_SECONDS < 45 * 60

    def test_resource_limits_present(self):
        text = self._text()
        assert "Nice=" in text and "CPUWeight=" in text
