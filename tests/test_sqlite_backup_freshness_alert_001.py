"""SQLITE-BACKUP-FRESHNESS-ALERT-001 — local manifest-backed backup health.

Covers the freshness contract (36h, newest-committed-manifest semantics, the
no-silent-fallback rule), path/symlink confinement, the read-only report CLI,
the fail-isolated MarketOps hook, the deduplicated alert lifecycle and its
recovery path, boundedness, and the scope guarantees (no provider calls, no
backup execution, no pruning, no migration, no new timer/daemon, no trading or
capital surface).

Everything runs against disposable backup roots and disposable SQLite
databases. No production backup artifact is ever created, read, aged, renamed
or deleted by these tests.
"""

import ast
import asyncio
import gzip
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  — registers every table on Base.metadata
from app.config import Settings
from app.db import Base
from app.models import MarketOpsAlert
from app.services import backup as bk
from app.services import backup_freshness as bf
from app.services import marketops as mo

REPO = Path(__file__).resolve().parents[1]
UNIT_DIR = REPO / "infra/systemd/user"

STAMP = "%Y%m%dT%H%M%SZ"
NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# disposable fixtures
# --------------------------------------------------------------------------

def write_backup(
    root: Path,
    created_at: datetime,
    *,
    payload_overrides: dict | None = None,
    artifact: bool = True,
    artifact_bytes: int | None = None,
    manifest_text: str | None = None,
) -> tuple[Path, Path]:
    """Publish a disposable backup pair exactly as backup_database would:
    strict names, data file first, manifest last."""
    root.mkdir(parents=True, exist_ok=True)
    stamp = created_at.strftime(STAMP)
    artifact_path = root / f"{bk.BACKUP_PREFIX}{stamp}{bk.BACKUP_SUFFIX}"
    manifest_path = root / f"{bk.BACKUP_PREFIX}{stamp}{bk.MANIFEST_SUFFIX}"

    size = 0
    if artifact:
        with gzip.open(artifact_path, "wb") as fh:
            fh.write(b"disposable-backup-payload")
        size = artifact_path.stat().st_size

    if manifest_text is not None:
        manifest_path.write_text(manifest_text)
        return artifact_path, manifest_path

    payload = {
        "manifest_version": bk.MANIFEST_VERSION,
        "created_at": created_at.isoformat(),
        "backup_filename": artifact_path.name,
        "backup_bytes": artifact_bytes if artifact_bytes is not None else size,
        "sha256": "0" * 64,
        "alembic_revision": "0027",
        "integrity_check": "ok",
        "status": "verified",
        "source_database_path_redacted": ".../probability_arena.db",
    }
    payload.update(payload_overrides or {})
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return artifact_path, manifest_path


@pytest.fixture
def root(tmp_path) -> Path:
    directory = tmp_path / "backups"
    directory.mkdir(parents=True)
    return directory


# Captured at import time so a test that monkeypatches the module attribute can
# still reach the genuine evaluator without recursing into its own patch.
_REAL_EVALUATE = bf.evaluate_backup_freshness


def evaluate(root: Path, now: datetime = NOW):
    return _REAL_EVALUATE(backup_root=root, now=now)


def pin_evaluator(monkeypatch, root: Path, now: datetime = NOW) -> list:
    """Point production callers at a disposable backup root and a fixed clock,
    and record every call so 'exactly once per cycle' is provable."""
    calls: list = []

    def pinned(*_a, **_k):
        calls.append(1)
        return _REAL_EVALUATE(backup_root=root, now=now)

    monkeypatch.setattr(bf, "evaluate_backup_freshness", pinned)
    return calls


def stamp_alembic(settings, revision: str = "0027"):
    """Create the schema AND an alembic_version row, so a disposable database
    carries the same revision record the production database does (a manifest
    with no revision is legitimately not a healthy production backup)."""
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL)"
        )
        conn.exec_driver_sql("DELETE FROM alembic_version")
        conn.exec_driver_sql(
            "INSERT INTO alembic_version (version_num) VALUES (?)", (revision,)
        )
    engine.dispose()



class DisposableDb:
    """A disposable MarketOps database plus a real session FACTORY.

    Production gives the hook its own short-lived session per cycle, so tests
    must too — handing it the same session the assertions use would hide the
    `close()` and the independent `commit()` that are the point of the design.
    `expire_on_commit=False` matches app/db.py.
    """

    def __init__(self, tmp_path):
        self.engine = create_engine(f"sqlite:///{tmp_path / 'marketops.db'}")
        Base.metadata.create_all(self.engine)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.opened = 0
        self.closed = 0

    def session(self):
        self.opened += 1
        real = self._factory()
        original_close = real.close

        def counting_close():
            self.closed += 1
            original_close()

        real.close = counting_close
        return real

    def inspect(self):
        """A fresh session for assertions — never the one the hook used."""
        return self._factory()


@pytest.fixture
def db(tmp_path):
    return DisposableDb(tmp_path)


def make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'marketops.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


# --------------------------------------------------------------------------
# 1-3, 21-22: the freshness contract
# --------------------------------------------------------------------------

class TestFreshnessContract:
    def test_threshold_is_36_hours(self):
        assert bf.BACKUP_FRESHNESS_MAX_AGE_SECONDS == 36 * 60 * 60 == 129600

    def test_fresh_verified_backup_is_healthy(self, root):
        write_backup(root, NOW - timedelta(hours=2))
        result = evaluate(root)
        assert result.healthy is True
        assert result.status == bf.STATUS_HEALTHY
        assert result.reason == bf.REASON_HEALTHY
        assert result.external_calls == 0
        assert result.threshold_seconds == 129600
        assert result.size_matches_manifest is True
        assert result.artifact_is_regular_file is True
        assert result.manifest_alembic_revision == "0027"

    def test_exactly_36_hours_is_healthy(self, root):
        write_backup(root, NOW - timedelta(seconds=129600))
        result = evaluate(root)
        assert result.age_seconds == 129600
        assert result.healthy is True
        assert result.reason == bf.REASON_HEALTHY

    def test_one_second_past_36_hours_is_stale(self, root):
        write_backup(root, NOW - timedelta(seconds=129601))
        result = evaluate(root)
        assert result.healthy is False
        assert result.reason == bf.REASON_BACKUP_STALE
        assert result.status == bf.STATUS_UNHEALTHY
        assert result.age_seconds == 129601

    def test_stale_only_applies_to_a_structurally_valid_pair(self, root):
        """A stale AND broken backup reports the structural fault, never
        `backup_stale` — malformed states are not normalized into staleness."""
        artifact, _ = write_backup(root, NOW - timedelta(days=10))
        artifact.unlink()
        assert evaluate(root).reason == bf.REASON_ARTIFACT_MISSING

    def test_naive_created_at_is_normalized_to_utc(self, root):
        naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)
        write_backup(root, NOW - timedelta(hours=1),
                     payload_overrides={"created_at": naive.isoformat()})
        result = evaluate(root)
        assert result.healthy is True
        assert result.age_seconds == pytest.approx(3600, abs=1)

    def test_utc_offset_created_at_is_handled(self, root):
        offset = (NOW - timedelta(hours=3)).astimezone(timezone(timedelta(hours=-7)))
        write_backup(root, NOW - timedelta(hours=3),
                     payload_overrides={"created_at": offset.isoformat()})
        result = evaluate(root)
        assert result.healthy is True
        assert result.age_seconds == pytest.approx(10800, abs=1)

    def test_implausibly_future_manifest_cannot_mask_staleness(self, root):
        write_backup(root, NOW + timedelta(days=2))
        result = evaluate(root)
        assert result.healthy is False
        # unhealthy so it cannot mask staleness, but a clock problem is not
        # "backup protection is gone"
        assert result.reason == bf.REASON_MANIFEST_FUTURE_DATED
        assert result.severity == "warning"

    def test_small_backwards_clock_step_stays_healthy(self, root):
        """An NTP/chrony step must not page anyone about a perfect backup."""
        write_backup(root, NOW + timedelta(seconds=120))
        result = evaluate(root)
        assert result.healthy is True, result.reason

    def test_future_dated_names_the_offending_manifest(self, root):
        _artifact, manifest = write_backup(root, NOW + timedelta(days=2))
        result = evaluate(root)
        assert result.newest_manifest_filename == manifest.name

    def test_far_future_filename_stamp_is_not_a_permanent_critical(self, root):
        """A stray far-future-stamped manifest is never reaped by retention
        (which only prunes .db.gz names), so it must not latch a critical
        forever over an otherwise healthy backup."""
        write_backup(root, NOW - timedelta(hours=1))
        (root / "backup-20991231T000000Z.manifest.json").write_text("{}")
        result = evaluate(root)
        assert result.healthy is False
        assert result.reason == bf.REASON_MANIFEST_FUTURE_DATED
        assert result.severity == "warning"
        assert result.newest_manifest_filename == "backup-20991231T000000Z.manifest.json"

    def test_evaluated_at_is_utc_aware(self, root):
        write_backup(root, NOW - timedelta(hours=1))
        parsed = datetime.fromisoformat(evaluate(root).evaluated_at)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)


# --------------------------------------------------------------------------
# 4-13: unhealthy states
# --------------------------------------------------------------------------

class TestUnhealthyStates:
    def test_no_manifest_is_unhealthy(self, root):
        result = evaluate(root)
        assert result.reason == bf.REASON_NO_COMMITTED_BACKUP
        assert result.healthy is False
        assert result.strict_manifest_count == 0

    def test_orphan_artifact_without_manifest_is_not_a_committed_backup(self, root):
        with gzip.open(root / "backup-20260803T000000Z.db.gz", "wb") as fh:
            fh.write(b"x")
        assert evaluate(root).reason == bf.REASON_NO_COMMITTED_BACKUP

    def test_backup_root_missing(self, tmp_path):
        result = evaluate(tmp_path / "nope")
        assert result.reason == bf.REASON_ROOT_MISSING
        assert result.backup_root_exists is False

    def test_backup_root_is_a_symlink(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        write_backup(real, NOW - timedelta(hours=1))
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        result = evaluate(link)
        assert result.reason == bf.REASON_ROOT_SYMLINK
        assert result.backup_root_is_symlink is True

    def test_backup_root_is_a_regular_file(self, tmp_path):
        target = tmp_path / "not-a-dir"
        target.write_text("x")
        assert evaluate(target).reason == bf.REASON_ROOT_INVALID

    def test_unconfigured_backup_root(self, tmp_path):
        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'a.db'}", backup_dir="   "
        )
        result = bf.evaluate_backup_freshness(settings, now=NOW)
        assert result.reason == bf.REASON_ROOT_UNCONFIGURED
        assert result.backup_root_configured is False

    def test_invalid_manifest_json(self, root):
        write_backup(root, NOW - timedelta(hours=1), manifest_text="{not json")
        result = evaluate(root)
        assert result.reason == bf.REASON_MANIFEST_INVALID
        assert result.invalid_manifest_count == 1

    def test_manifest_that_is_not_a_json_object(self, root):
        write_backup(root, NOW - timedelta(hours=1), manifest_text="[1, 2, 3]")
        assert evaluate(root).reason == bf.REASON_MANIFEST_INVALID

    def test_unsupported_manifest_version(self, root):
        write_backup(root, NOW - timedelta(hours=1),
                     payload_overrides={"manifest_version": 99})
        result = evaluate(root)
        assert result.reason == bf.REASON_MANIFEST_UNSUPPORTED
        assert result.manifest_version == 99

    def test_manifest_status_not_verified(self, root):
        write_backup(root, NOW - timedelta(hours=1),
                     payload_overrides={"status": "in_progress"})
        result = evaluate(root)
        assert result.reason == bf.REASON_MANIFEST_NOT_VERIFIED
        assert result.manifest_status == "in_progress"

    def test_manifest_integrity_check_not_ok(self, root):
        write_backup(root, NOW - timedelta(hours=1),
                     payload_overrides={"integrity_check": "corrupt"})
        assert evaluate(root).reason == bf.REASON_MANIFEST_NOT_VERIFIED

    def test_manifest_alembic_revision_missing(self, root):
        """backup.py treats alembic_revision as optional; this check does not.
        The divergence is deliberate (see the module comment) — so it gets its
        OWN reason, not `manifest_invalid`, which would send an operator hunting
        for corruption in an intact file."""
        write_backup(root, NOW - timedelta(hours=1),
                     payload_overrides={"alembic_revision": None})
        assert evaluate(root).reason == bf.REASON_MANIFEST_REVISION_INVALID

    def test_hyphenated_alembic_revision_is_accepted(self, root):
        """Alembic allows arbitrary rev-ids; `--rev-id ops-001` must not read as
        a corrupt manifest."""
        write_backup(root, NOW - timedelta(hours=1),
                     payload_overrides={"alembic_revision": "ops-001.a_2"})
        assert evaluate(root).healthy is True

    def test_alembic_revision_with_trailing_newline_is_rejected(self, root):
        write_backup(root, NOW - timedelta(hours=1),
                     payload_overrides={"alembic_revision": "0027\n"})
        assert evaluate(root).reason == bf.REASON_MANIFEST_REVISION_INVALID

    def test_manifest_strings_are_bounded_and_control_char_free(self, root):
        """Manifest strings are attacker-influenced and get echoed into a
        terminal, a JSON blob and an alert row."""
        write_backup(root, NOW - timedelta(hours=1), payload_overrides={
            "alembic_revision": "\x1b[2J" + "A" * 40000,
            "status": "verified",
        })
        result = evaluate(root)
        assert result.reason == bf.REASON_MANIFEST_REVISION_INVALID
        assert len(result.manifest_alembic_revision) <= bf.MAX_MANIFEST_FIELD_CHARS
        assert "\x1b" not in result.manifest_alembic_revision

    def test_artifact_that_is_not_a_gzip_stream(self, root):
        artifact, _ = write_backup(root, NOW - timedelta(hours=1))
        size = artifact.stat().st_size
        artifact.write_bytes(b"NOT A GZIP STREAM".ljust(size, b"\x00"))
        result = evaluate(root)
        assert result.size_matches_manifest is True
        assert result.reason == bf.REASON_ARTIFACT_INVALID

    def test_group_or_world_writable_backup_root(self, root):
        write_backup(root, NOW - timedelta(hours=1))
        os.chmod(root, 0o777)
        try:
            assert evaluate(root).reason == bf.REASON_ROOT_INSECURE
        finally:
            os.chmod(root, 0o755)

    def test_fifo_manifest_cannot_hang_the_cycle(self, root):
        """A planted FIFO would block a naive read forever; the evaluator has no
        timeout anywhere, so MarketOps would hang."""
        write_backup(root, NOW - timedelta(hours=2))
        os.mkfifo(root / "backup-20260803T110000Z.manifest.json")
        result = evaluate(root)  # must return, not block
        assert result.reason == bf.REASON_MANIFEST_INVALID

    def test_deeply_nested_json_cannot_degrade_the_whole_check(self, root):
        """json.loads raises RecursionError (a RuntimeError, not ValueError) on
        pathological nesting. One planted file must invalidate ONE candidate,
        never collapse the verdict into a warning-severity `evaluation_error` —
        which is exactly the outcome an attacker would want."""
        (root / "backup-20200101T000000Z.manifest.json").write_text(
            "[" * 20000 + "]" * 20000
        )
        result = evaluate(root)
        assert result.status == bf.STATUS_UNHEALTHY
        assert result.severity == "critical"
        # the poison file is the only (and therefore newest) candidate, so it
        # is correctly reported as an invalid manifest — NOT as a
        # warning-severity "the monitor broke", which is what a RecursionError
        # escaping _read_candidate would have produced
        assert result.reason == bf.REASON_MANIFEST_INVALID
        assert result.invalid_manifest_count == 1

    def test_manifest_backup_bytes_malformed(self, root):
        write_backup(root, NOW - timedelta(hours=1),
                     payload_overrides={"backup_bytes": "many"})
        assert evaluate(root).reason == bf.REASON_MANIFEST_INVALID

    def test_artifact_missing(self, root):
        artifact, _ = write_backup(root, NOW - timedelta(hours=1))
        artifact.unlink()
        result = evaluate(root)
        assert result.reason == bf.REASON_ARTIFACT_MISSING
        assert result.artifact_exists is False

    def test_artifact_is_a_symlink(self, tmp_path, root):
        outside = tmp_path / "elsewhere.db.gz"
        with gzip.open(outside, "wb") as fh:
            fh.write(b"disposable-backup-payload")
        artifact, _ = write_backup(root, NOW - timedelta(hours=1))
        artifact.unlink()
        artifact.symlink_to(outside)
        result = evaluate(root)
        assert result.reason == bf.REASON_ARTIFACT_SYMLINK
        assert result.artifact_is_symlink is True

    def test_artifact_is_not_a_regular_file(self, root):
        artifact, _ = write_backup(root, NOW - timedelta(hours=1))
        artifact.unlink()
        artifact.mkdir()
        assert evaluate(root).reason == bf.REASON_ARTIFACT_INVALID

    def test_artifact_size_mismatch(self, root):
        artifact, _ = write_backup(root, NOW - timedelta(hours=1))
        with open(artifact, "ab") as fh:
            fh.write(b"appended-after-publication")
        result = evaluate(root)
        assert result.reason == bf.REASON_ARTIFACT_SIZE_MISMATCH
        assert result.artifact_bytes != result.manifest_backup_bytes

    def test_artifact_truncated_to_zero(self, root):
        artifact, _ = write_backup(root, NOW - timedelta(hours=1))
        artifact.write_bytes(b"")
        assert evaluate(root).reason == bf.REASON_ARTIFACT_SIZE_MISMATCH

    def test_manifest_path_traversal_rejected(self, root, tmp_path):
        outside = tmp_path / "escaped.db.gz"
        with gzip.open(outside, "wb") as fh:
            fh.write(b"disposable-backup-payload")
        write_backup(
            root, NOW - timedelta(hours=1),
            payload_overrides={
                "backup_filename": f"../{outside.name}",
                "backup_bytes": outside.stat().st_size,
            },
        )
        assert evaluate(root).reason == bf.REASON_MANIFEST_INVALID

    def test_absolute_artifact_path_in_manifest_rejected(self, root, tmp_path):
        outside = tmp_path / "backup-20260803T000000Z.db.gz"
        with gzip.open(outside, "wb") as fh:
            fh.write(b"disposable-backup-payload")
        write_backup(
            root, NOW - timedelta(hours=1),
            payload_overrides={
                "backup_filename": str(outside),
                "backup_bytes": outside.stat().st_size,
            },
        )
        assert evaluate(root).reason == bf.REASON_MANIFEST_INVALID

    def test_artifact_path_traversal_via_symlinked_manifest_entry(self, tmp_path, root):
        """A strict-named manifest that is itself a symlink is never opened and
        never treated as a committed record — it surfaces as invalid."""
        real_manifest = tmp_path / "planted.json"
        real_manifest.write_text(json.dumps({"manifest_version": 1}))
        # newer than the healthy pair below, but NOT future-dated
        link = root / "backup-20260803T115900Z.manifest.json"
        link.symlink_to(real_manifest)
        write_backup(root, NOW - timedelta(hours=1))
        result = evaluate(root)
        assert result.reason == bf.REASON_MANIFEST_INVALID
        assert result.newest_manifest_filename == link.name


# --------------------------------------------------------------------------
# 16-20: selection, strict matching, boundedness
# --------------------------------------------------------------------------

class TestSelectionAndScanning:
    def test_newest_committed_manifest_wins(self, root):
        write_backup(root, NOW - timedelta(days=5))
        write_backup(root, NOW - timedelta(days=3))
        newest, _ = write_backup(root, NOW - timedelta(hours=1))
        result = evaluate(root)
        assert result.newest_backup_filename == newest.name
        assert result.strict_manifest_count == 3
        assert result.healthy is True

    def test_selection_is_deterministic(self, root):
        for hours in (1, 5, 9, 13):
            write_backup(root, NOW - timedelta(hours=hours))
        picks = {evaluate(root).newest_manifest_filename for _ in range(8)}
        assert len(picks) == 1

    def test_created_at_must_agree_with_the_filename_stamp(self, root):
        """backup.py derives the filename stamp, created_at and backup_filename
        from ONE variable, so genuine output always agrees. Requiring that
        agreement stops the entire freshness verdict from resting on a single
        mutable JSON scalar while the immutable corroborating value sits unused
        in the filename right next to it."""
        write_backup(
            root, NOW - timedelta(days=10),
            payload_overrides={"created_at": (NOW - timedelta(hours=1)).isoformat()},
        )
        result = evaluate(root)
        assert result.healthy is False
        assert result.reason == bf.REASON_MANIFEST_INVALID

    def test_manifest_may_only_certify_its_own_artifact(self, root):
        """A fresh manifest must not be able to vouch for an artifact produced
        by an entirely different run."""
        old_artifact, _ = write_backup(root, NOW - timedelta(days=30))
        write_backup(
            root, NOW - timedelta(hours=1),
            payload_overrides={
                "backup_filename": old_artifact.name,
                "backup_bytes": old_artifact.stat().st_size,
            },
        )
        result = evaluate(root)
        assert result.healthy is False
        assert result.reason == bf.REASON_MANIFEST_INVALID

    def test_lenient_stamp_junk_cannot_win_newest(self, root):
        """strptime accepts 1-2 digit fields, so `...T13426Z` parses to 13:42:06
        — NEWER than the canonical 01:34:26 of the same day. Retention only
        prunes .db.gz names, so such a file would latch the alert forever."""
        write_backup(root, NOW - timedelta(hours=1))
        (root / "backup-20260803T13426Z.manifest.json").write_text("{}")
        result = evaluate(root)
        assert result.healthy is True
        assert result.strict_manifest_count == 1

    def test_broken_newest_manifest_does_not_fall_back_to_an_older_healthy_one(self, root):
        write_backup(root, NOW - timedelta(hours=6))          # healthy, older
        write_backup(root, NOW - timedelta(hours=1),          # broken, newest
                     manifest_text="{corrupted")
        result = evaluate(root)
        assert result.healthy is False
        assert result.reason == bf.REASON_MANIFEST_INVALID
        assert result.strict_manifest_count == 2
        assert result.invalid_manifest_count == 1

    def test_newest_manifest_with_missing_artifact_does_not_fall_back(self, root):
        write_backup(root, NOW - timedelta(hours=6))
        artifact, _ = write_backup(root, NOW - timedelta(hours=1))
        artifact.unlink()
        assert evaluate(root).reason == bf.REASON_ARTIFACT_MISSING

    def test_failed_backup_publishing_no_manifest_leaves_prior_backup_newest(self, root):
        """The pipeline publishes the manifest LAST, so a failed run leaves no
        manifest and the prior verified backup legitimately stays newest — it
        goes stale naturally if verified backups stop appearing."""
        write_backup(root, NOW - timedelta(hours=20))
        (root / "backup-20260803T110000Z.db.gz.part").write_bytes(b"partial")
        assert evaluate(root).healthy is True
        later = bf.evaluate_backup_freshness(
            backup_root=root, now=NOW + timedelta(hours=20)
        )
        assert later.reason == bf.REASON_BACKUP_STALE

    def test_strict_filename_matching(self, root):
        write_backup(root, NOW - timedelta(hours=1))
        for name in (
            "backup-notatimestamp.manifest.json",
            "backup-20260803T999999Z.manifest.json",
            "manifest.json",
            "backup-20260803T120000Z.manifest.json.bak",
            "BACKUP-20260803T120000Z.manifest.json",
            "prefix-backup-20260803T120000Z.manifest.json",
        ):
            (root / name).write_text("{}")
        result = evaluate(root)
        assert result.strict_manifest_count == 1
        assert result.healthy is True

    def test_unknown_files_and_subdirectories_ignored_safely(self, root):
        write_backup(root, NOW - timedelta(hours=1))
        (root / bk.LOCK_FILENAME).write_bytes(b"")
        (root / "README").write_text("hello")
        (root / "tmp").mkdir()
        (root / "tmp" / "backup-20260101T000000Z.manifest.json").write_text("{}")
        result = evaluate(root)
        assert result.strict_manifest_count == 1
        assert result.healthy is True

    def test_scan_is_not_recursive(self, root, monkeypatch):
        write_backup(root, NOW - timedelta(hours=1))
        monkeypatch.setattr(
            bf.os, "walk", lambda *a, **k: pytest.fail("recursive traversal")
        )
        assert evaluate(root).healthy is True

    def test_large_inventory_reports_honestly_without_a_false_critical(self, root):
        """Reading in arbitrary scandir order and bailing at the cap would page
        "backup protection is gone" for a large-but-HEALTHY inventory. Sorting
        first makes the read cap unable to change the verdict, while the true
        total is still reported."""
        write_backup(root, NOW - timedelta(hours=1))          # newest, healthy
        for minutes in range(bf.MANIFEST_INSPECTION_CAP + 20):
            write_backup(root, NOW - timedelta(days=2, minutes=minutes + 1),
                         artifact=False)
        result = evaluate(root)
        assert result.healthy is True, result.reason
        assert result.strict_manifest_count == bf.MANIFEST_INSPECTION_CAP + 21
        assert result.newest_verified_at is not None

    def test_directory_entry_cap_fails_visibly(self, root, monkeypatch):
        monkeypatch.setattr(bf, "DIRECTORY_ENTRY_CAP", 3)
        write_backup(root, NOW - timedelta(hours=1))
        for i in range(6):
            (root / f"noise-{i}").write_text("x")
        assert evaluate(root).reason == bf.REASON_INSPECTION_CAP

    def test_oversized_manifest_is_not_read_into_memory(self, root):
        write_backup(root, NOW - timedelta(hours=1),
                     manifest_text="x" * (bf.MAX_MANIFEST_BYTES + 1))
        assert evaluate(root).reason == bf.REASON_MANIFEST_INVALID


# --------------------------------------------------------------------------
# 23-26, 46: the read-only report CLI + performance
# --------------------------------------------------------------------------

class TestReportCLI:
    def _run(self, monkeypatch, root, fmt):
        from app import cli

        pin_evaluator(monkeypatch, root)
        return cli.sqlite_backup_freshness_report(fmt=fmt)

    def test_text_and_json_parity(self, root, monkeypatch, capsys):
        write_backup(root, NOW - timedelta(hours=2))
        text = self._run(monkeypatch, root, "text")
        capsys.readouterr()
        data = self._run(monkeypatch, root, "json")
        assert text == data
        out = capsys.readouterr().out
        assert json.loads(out) == data

    def test_text_output_discloses_threshold_age_and_reason(
        self, root, monkeypatch, capsys
    ):
        write_backup(root, NOW - timedelta(hours=2))
        self._run(monkeypatch, root, "text")
        out = capsys.readouterr().out
        assert "129600" in out and "36h" in out
        assert "reason=healthy" in out
        assert "age_seconds=" in out
        assert "verify-db-backup" in out

    def test_cli_writes_nothing(self, root, monkeypatch, capsys):
        write_backup(root, NOW - timedelta(hours=2))
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                  for p in root.iterdir()}
        self._run(monkeypatch, root, "json")
        capsys.readouterr()
        after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                 for p in root.iterdir()}
        assert before == after

    def test_cli_makes_zero_provider_calls(self, root, monkeypatch, capsys):
        import httpx

        write_backup(root, NOW - timedelta(hours=2))
        for attr in ("get", "post", "request"):
            monkeypatch.setattr(
                httpx.AsyncClient, attr,
                lambda *a, **k: pytest.fail("provider call from a read-only report"),
            )
            monkeypatch.setattr(
                httpx.Client, attr,
                lambda *a, **k: pytest.fail("provider call from a read-only report"),
            )
        data = self._run(monkeypatch, root, "json")
        capsys.readouterr()
        assert data["external_calls"] == 0

    def test_cli_does_not_execute_backup_or_prune(self, root, monkeypatch, capsys):
        write_backup(root, NOW - timedelta(hours=2))
        for name in ("backup_database", "apply_retention", "prune_old_backups",
                     "verify_backup", "_sha256"):
            monkeypatch.setattr(
                bk, name, lambda *a, **k: pytest.fail(f"{name} called from report")
            )
        self._run(monkeypatch, root, "json")
        capsys.readouterr()

    def test_cli_registered_with_format_choices(self):
        from app.cli import build_parser

        args = build_parser().parse_args(
            ["sqlite-backup-freshness-report", "--format", "json"]
        )
        assert args.command == "sqlite-backup-freshness-report"
        assert args.fmt == "json"
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["sqlite-backup-freshness-report", "--format", "yaml"]
            )

    def test_cli_exposes_no_arbitrary_path_or_clock_override(self):
        from app.cli import build_parser

        for flag in ("--backup-root", "--now"):
            with pytest.raises(SystemExit):
                build_parser().parse_args(
                    ["sqlite-backup-freshness-report", flag, "/etc"]
                )

    def test_output_is_secret_free(self, root, monkeypatch, capsys):
        write_backup(root, NOW - timedelta(hours=2))
        self._run(monkeypatch, root, "json")
        raw = capsys.readouterr().out
        # the backup root is deliberately disclosed (it is operational config,
        # not a secret); pytest's tmp path embeds the test's own name, so scan
        # everything EXCEPT that one value
        out = raw.replace(str(root), "<backup-root>").lower()
        for banned in ("password", "secret", "api_key", "apikey", "authorization",
                       "bearer", "postgresql://", "sk-", "token="):
            assert banned not in out

    def test_evaluator_is_bounded(self, root):
        for days in range(1, 11):
            write_backup(root, NOW - timedelta(days=days))
        write_backup(root, NOW - timedelta(hours=1))
        evaluate(root)  # warm the filesystem cache
        started = time.perf_counter()
        for _ in range(5):
            result = evaluate(root)
        elapsed_ms = (time.perf_counter() - started) * 1000 / 5
        assert result.healthy is True
        # Generous vs. the ~25 ms design budget so CI noise cannot flake it,
        # while still failing loudly if the check ever starts hashing/opening.
        assert elapsed_ms < 250, f"evaluator took {elapsed_ms:.1f} ms per call"




# --------------------------------------------------------------------------
# 27-33: MarketOps hook isolation
# --------------------------------------------------------------------------

def make_autopilot(root, db=None, **cfg_kwargs):
    cfg = mo.MarketOpsConfig(
        include_probability_markets=False,
        include_crypto=False,
        **cfg_kwargs,
    )
    return mo.MarketOpsAutopilotService(
        config=cfg,
        backup_freshness_session_factory=(db.session if db is not None else None),
    )


def open_freshness_alerts(session):
    return (
        session.query(MarketOpsAlert)
        .filter(
            MarketOpsAlert.alert_type == mo.ALERT_BACKUP_FRESHNESS,
            MarketOpsAlert.status == mo.ALERT_STATUS_OPEN,
        )
        .order_by(MarketOpsAlert.id.asc())
        .all()
    )


def run_hook(db, root, monkeypatch=None, now=NOW):
    """Drive one hook invocation exactly as step 7b does."""
    service = make_autopilot(root, db=db, include_backup_freshness_alert=True)
    previous = bf.evaluate_backup_freshness
    bf.evaluate_backup_freshness = lambda *a, **k: _REAL_EVALUATE(
        backup_root=root, now=now
    )
    try:
        summary: dict = {}
        created = service._evaluate_backup_freshness(summary)
        return created, summary
    finally:
        bf.evaluate_backup_freshness = previous


class TestMarketOpsHook:
    def test_flag_off_is_a_complete_no_op(self, db, root, monkeypatch):
        write_backup(root, NOW - timedelta(days=30))  # would alert if evaluated
        service = make_autopilot(root, db=db, include_backup_freshness_alert=False)
        summary: dict = {}
        called = pin_evaluator(monkeypatch, root)
        # exactly the guard the cycle applies at step 7b
        if service.config.include_backup_freshness_alert:  # pragma: no cover
            service._evaluate_backup_freshness(summary)
        assert called == []
        assert summary == {}
        assert db.opened == 0
        assert db.inspect().query(MarketOpsAlert).count() == 0

    def test_flag_on_evaluates_exactly_once(self, db, root, monkeypatch):
        write_backup(root, NOW - timedelta(hours=1))
        calls = pin_evaluator(monkeypatch, root)
        service = make_autopilot(root, db=db, include_backup_freshness_alert=True)
        summary: dict = {}
        service._evaluate_backup_freshness(summary)
        assert len(calls) == 1
        assert summary["backup_freshness"]["healthy"] is True

    def test_hook_uses_an_isolated_session_and_closes_it(self, db, root):
        """The alert write must never touch the shared cycle session."""
        run_hook(db, root)
        assert db.opened == 1
        assert db.closed == 1

    def test_hook_runs_no_scan_and_makes_zero_provider_calls(
        self, db, root, monkeypatch
    ):
        import httpx

        write_backup(root, NOW - timedelta(hours=1))
        for client in (httpx.AsyncClient, httpx.Client):
            for attr in ("get", "post", "request"):
                monkeypatch.setattr(
                    client, attr,
                    lambda *a, **k: pytest.fail("provider call from the hook"),
                )
        from app.services import crypto_scout

        monkeypatch.setattr(
            crypto_scout.CryptoDiscoveryService, "scan_once",
            lambda *a, **k: pytest.fail("hook started a scan"), raising=False,
        )
        _created, summary = run_hook(db, root)
        assert summary["backup_freshness"]["external_calls"] == 0

    def test_hook_does_not_mutate_backup_files(self, db, root):
        write_backup(root, NOW - timedelta(hours=1))
        before = {p.name: (p.stat().st_size, p.read_bytes()) for p in root.iterdir()}
        run_hook(db, root)
        after = {p.name: (p.stat().st_size, p.read_bytes()) for p in root.iterdir()}
        assert before == after

    def test_hook_never_executes_a_backup_or_retention(self, db, root, monkeypatch):
        write_backup(root, NOW - timedelta(hours=1))
        for name in ("backup_database", "apply_retention", "prune_old_backups"):
            monkeypatch.setattr(
                bk, name, lambda *a, **k: pytest.fail(f"hook called {name}")
            )
        run_hook(db, root)

    def test_hook_failure_cannot_fail_marketops(self, db, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("evaluator exploded")

        monkeypatch.setattr(bf, "evaluate_backup_freshness", boom)
        summary: dict = {}
        created = make_autopilot(
            None, db=db, include_backup_freshness_alert=True, fail_fast=True
        )._evaluate_backup_freshness(summary)
        assert created == 0
        assert "backup_freshness" not in summary
        assert summary["backup_freshness_error"].startswith("RuntimeError:")

    def test_hook_error_message_is_bounded(self, db, monkeypatch):
        monkeypatch.setattr(
            bf, "evaluate_backup_freshness",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x" * 5000)),
        )
        summary: dict = {}
        make_autopilot(None, db=db)._evaluate_backup_freshness(summary)
        assert len(summary["backup_freshness_error"]) <= 320

    def test_evaluator_swallows_unexpected_errors_as_evaluation_error(
        self, root, monkeypatch
    ):
        monkeypatch.setattr(
            bf, "_scan_manifests",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("io failure")),
        )
        result = evaluate(root)
        assert result.status == bf.STATUS_ERROR
        assert result.reason == bf.REASON_EVALUATION_ERROR
        assert result.severity == "warning"
        # no path is disclosed on the error path
        assert result.backup_root == ""

    def test_summary_is_bounded_and_path_free(self, db, root):
        write_backup(root, NOW - timedelta(hours=1))
        _created, summary = run_hook(db, root)
        payload = summary["backup_freshness"]
        assert set(payload) == set(bf.SUMMARY_FIELDS) | {"alert_action"}
        serialized = json.dumps(payload)
        assert str(root) not in serialized
        assert "sha256" not in serialized
        assert "source_database" not in serialized
        assert len(serialized) < 1024


# --------------------------------------------------------------------------
# 34-41: the alert lifecycle
# --------------------------------------------------------------------------

class TestAlertLifecycle:
    def test_healthy_state_creates_no_alert(self, db, root):
        write_backup(root, NOW - timedelta(hours=1))
        created, summary = run_hook(db, root)
        assert created == 0
        assert summary["backup_freshness"]["alert_action"] == "none"
        assert db.inspect().query(MarketOpsAlert).count() == 0

    def test_missing_backup_creates_exactly_one_alert(self, db, root):
        created, summary = run_hook(db, root)
        assert created == 1
        assert summary["backup_freshness"]["alert_action"] == "created"
        alerts = open_freshness_alerts(db.inspect())
        assert len(alerts) == 1
        assert alerts[0].severity == "critical"
        assert alerts[0].title == mo.ALERT_BACKUP_FRESHNESS_TITLE
        assert alerts[0].evidence["reason"] == bf.REASON_NO_COMMITTED_BACKUP

    def test_stale_backup_creates_exactly_one_alert(self, db, root):
        write_backup(root, NOW - timedelta(days=3))
        created, _ = run_hook(db, root)
        assert created == 1
        alerts = open_freshness_alerts(db.inspect())
        assert len(alerts) == 1
        assert alerts[0].evidence["reason"] == bf.REASON_BACKUP_STALE
        assert alerts[0].evidence["threshold_seconds"] == 129600

    def test_repeated_unhealthy_cycles_do_not_duplicate(self, db, root):
        write_backup(root, NOW - timedelta(days=3))
        actions = []
        for _ in range(12):
            created, summary = run_hook(db, root)
            actions.append((created, summary["backup_freshness"]["alert_action"]))
        assert actions[0] == (1, "created")
        assert all(a == (0, "updated") for a in actions[1:])
        assert len(open_freshness_alerts(db.inspect())) == 1

    def test_reason_change_updates_the_same_alert(self, db, root):
        write_backup(root, NOW - timedelta(days=3))
        run_hook(db, root)
        first = open_freshness_alerts(db.inspect())[0]
        alert_id = first.id
        assert first.evidence["reason"] == bf.REASON_BACKUP_STALE

        for entry in list(root.iterdir()):
            entry.unlink()
        created, summary = run_hook(db, root)
        assert created == 0
        assert summary["backup_freshness"]["alert_action"] == "updated"
        alerts = open_freshness_alerts(db.inspect())
        assert len(alerts) == 1
        assert alerts[0].id == alert_id
        assert alerts[0].evidence["reason"] == bf.REASON_NO_COMMITTED_BACKUP

    def test_duplicate_open_rows_converge_to_one(self, db, root):
        """Two open rows are reachable (the cycle overlap guard is a
        read-then-write check, not a lock). The hook must converge, not just
        refresh the oldest and leave the rest stale forever."""
        seed = db.inspect()
        for _ in range(3):
            seed.add(mo.MarketOpsAlert(
                alert_type=mo.ALERT_BACKUP_FRESHNESS, severity="critical",
                status=mo.ALERT_STATUS_OPEN,
                title=mo.ALERT_BACKUP_FRESHNESS_TITLE, message="stale",
                evidence={"reason": "old"}, created_at=NOW,
            ))
        seed.commit()
        assert len(open_freshness_alerts(seed)) == 3

        created, _ = run_hook(db, root)
        assert created == 0
        alerts = open_freshness_alerts(db.inspect())
        assert len(alerts) == 1
        assert alerts[0].evidence["reason"] == bf.REASON_NO_COMMITTED_BACKUP

    def test_recovery_resolves_the_active_alert(self, db, root):
        write_backup(root, NOW - timedelta(days=3))
        run_hook(db, root)
        assert len(open_freshness_alerts(db.inspect())) == 1

        write_backup(root, NOW - timedelta(hours=1))
        created, summary = run_hook(db, root)
        assert created == 0
        assert summary["backup_freshness"]["alert_action"] == "resolved"
        inspect = db.inspect()
        assert open_freshness_alerts(inspect) == []
        resolved = (
            inspect.query(MarketOpsAlert)
            .filter(MarketOpsAlert.alert_type == mo.ALERT_BACKUP_FRESHNESS)
            .all()
        )
        assert len(resolved) == 1
        assert resolved[0].status == mo.ALERT_STATUS_RESOLVED
        assert resolved[0].resolved_at is not None

    def test_repeated_healthy_cycles_stay_quiet(self, db, root):
        write_backup(root, NOW - timedelta(hours=1))
        actions = [
            run_hook(db, root)[1]["backup_freshness"]["alert_action"]
            for _ in range(10)
        ]
        assert actions == ["none"] * 10
        assert db.inspect().query(MarketOpsAlert).count() == 0

    def test_recovery_then_regression_reopens_one_alert(self, db, root):
        run_hook(db, root)                                   # unhealthy
        write_backup(root, NOW - timedelta(hours=1))         # healthy
        run_hook(db, root)
        for entry in list(root.iterdir()):                   # unhealthy again
            entry.unlink()
        created, summary = run_hook(db, root)
        assert (created, summary["backup_freshness"]["alert_action"]) == (1, "created")
        assert len(open_freshness_alerts(db.inspect())) == 1

    def test_alert_payload_is_secret_and_path_free(self, db, root):
        write_backup(root, NOW - timedelta(days=3))
        run_hook(db, root)
        alert = open_freshness_alerts(db.inspect())[0]
        blob = json.dumps(
            {"t": alert.title, "m": alert.message, "e": alert.evidence}
        ).lower()
        for banned in ("password", "secret", "api_key", "apikey", "authorization",
                       "bearer", "postgresql://", "sk-", "token=", "traceback"):
            assert banned not in blob
        assert str(root).lower() not in blob
        assert set(alert.evidence) == set(bf.SUMMARY_FIELDS)

    def test_alert_identifies_the_offending_manifest(self, db, root):
        """Every manifest-side fault leaves newest_backup_filename None, so the
        evidence must name the manifest or the alert names no file at all."""
        write_backup(root, NOW - timedelta(hours=1), manifest_text="{broken")
        run_hook(db, root)
        evidence = open_freshness_alerts(db.inspect())[0].evidence
        assert evidence["reason"] == bf.REASON_MANIFEST_INVALID
        assert evidence["newest_backup_filename"] is None
        assert evidence["newest_manifest_filename"].endswith(bk.MANIFEST_SUFFIX)

    def test_evaluation_error_alerts_at_warning_severity(self, db, root, monkeypatch):
        monkeypatch.setattr(
            bf, "_scan_manifests",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("io failure")),
        )
        created, summary = run_hook(db, root)
        assert created == 1
        assert summary["backup_freshness"]["reason"] == bf.REASON_EVALUATION_ERROR
        assert open_freshness_alerts(db.inspect())[0].severity == "warning"

    def test_evaluation_error_never_downgrades_an_open_critical(
        self, db, root, monkeypatch
    ):
        """A monitor hiccup must not overwrite an outstanding critical outage on
        the one row that carries it."""
        run_hook(db, root)  # critical: no committed backup
        assert open_freshness_alerts(db.inspect())[0].severity == "critical"

        monkeypatch.setattr(
            bf, "_scan_manifests",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("io failure")),
        )
        created, summary = run_hook(db, root)
        assert created == 0
        assert summary["backup_freshness"]["alert_action"] == "preserved"
        alert = open_freshness_alerts(db.inspect())[0]
        assert alert.severity == "critical"
        assert alert.evidence["reason"] == bf.REASON_NO_COMMITTED_BACKUP

    def test_severity_map_is_exhaustive_and_deliberate(self):
        for reason in bf.ALL_REASONS:
            result = bf.BackupFreshnessResult(
                status=bf.STATUS_UNHEALTHY, healthy=reason == bf.REASON_HEALTHY,
                reason=reason, evaluated_at=NOW.isoformat(),
            )
            if reason == bf.REASON_HEALTHY:
                assert result.severity == "info"
            elif reason in bf.WARNING_REASONS:
                assert result.severity == "warning"
            else:
                assert result.severity == "critical"
        # a broken check and a clock step are the ONLY non-critical faults
        assert bf.WARNING_REASONS == {
            bf.REASON_EVALUATION_ERROR, bf.REASON_MANIFEST_FUTURE_DATED
        }

    def test_alert_path_never_triggers_remediation(self, db, root, monkeypatch):
        for name in ("backup_database", "apply_retention", "prune_old_backups"):
            monkeypatch.setattr(
                bk, name, lambda *a, **k: pytest.fail(f"alert path called {name}")
            )
        run_hook(db, root)
        assert len(open_freshness_alerts(db.inspect())) == 1

    def test_alert_row_fits_its_columns(self, db, root):
        write_backup(root, NOW - timedelta(days=3))
        run_hook(db, root)
        alert = open_freshness_alerts(db.inspect())[0]
        assert len(alert.alert_type) <= 48
        assert len(alert.title) <= 256
        assert len(alert.severity) <= 16
        assert len(json.dumps(alert.evidence)) < 2048


class TestScopeAndSafety:
    def test_no_new_migration(self):
        versions = REPO / "alembic/versions"
        for path in versions.glob("*.py"):
            text = path.read_text().lower()
            assert "backup_freshness" not in text
            assert "freshness" not in text

    def test_reuses_existing_alert_table(self):
        source = (REPO / "app/services/marketops.py").read_text()
        assert "MarketOpsAlert(" in source
        assert "backup_freshness_warning" in source
        module = (REPO / "app/services/backup_freshness.py").read_text()
        # the evaluator itself is pure filesystem — no ORM, no session, no SQL
        for banned in ("Session", "session", "sqlalchemy", "select(", "INSERT",
                       "sqlite3", "Base."):
            assert banned not in module

    def test_no_new_timer_or_daemon(self):
        for path in UNIT_DIR.iterdir():
            assert "freshness" not in path.name
            assert "freshness" not in path.read_text().lower()
        source = (REPO / "app/services/backup_freshness.py").read_text().lower()
        for banned in ("systemd", "systemctl", "while true", "asyncio.sleep",
                       "time.sleep", "threading", "schedule", "subprocess"):
            assert banned not in source

    def test_no_provider_network_or_capital_surface(self):
        source = (REPO / "app/services/backup_freshness.py").read_text().lower()
        for banned in ("httpx", "requests.", "aiohttp", "urllib", "socket",
                       "solana", "birdeye", "goplus", "dexscreener", "kalshi",
                       "expected_value", "kelly", "position_siz", "paper_trad",
                       "place_order", "submit_order", "create_order", "wallet",
                       "recommended_side", "trade_recommend", "execute_trade"):
            assert banned not in source

    def test_evaluator_never_hashes_decompresses_or_opens_the_backup(self):
        source = (REPO / "app/services/backup_freshness.py").read_text()
        # API usage, not prose — the module docstring necessarily discusses
        # hashing and decompression in order to explain why it does neither.
        code = "\n".join(
            line for line in source.split("\n") if not line.lstrip().startswith("#")
        )
        for banned in ("hashlib", "sha256(", "verify_backup(", "sqlite3",
                       "shutil.", "gzip.open", "import gzip", ".decompress("):
            assert banned not in code, banned
        # the only thing read from the artifact is its 3-byte magic
        assert "GZIP_MAGIC = b" in source
        assert "os.read(fd, len(GZIP_MAGIC))" in source

    def test_evaluator_performs_no_filesystem_writes(self):
        """AST audit: no write/create/delete call reachable in the module."""
        tree = ast.parse((REPO / "app/services/backup_freshness.py").read_text())
        banned_attrs = {
            "write_text", "write_bytes", "mkdir", "rmdir", "touch", "unlink",
            "chmod", "rename", "remove", "removedirs", "symlink_to", "makedirs",
            "fdopen", "truncate", "rmtree", "copy", "copy2", "copyfileobj",
            "hardlink_to", "link", "mknod", "utime", "mkfifo",
        }
        # `replace` and `open` are ambiguous (str.replace / datetime.replace are
        # pure; os.replace and builtins.open are not), so they are matched on the
        # full dotted target instead of the bare attribute name.
        banned_dotted = {"os.replace", "shutil.move"}

        def dotted(func):
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                return f"{func.value.id}.{func.attr}"
            return None

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else None
            )
            assert name not in banned_attrs, f"write-capable call {name!r}"
            assert dotted(func) not in banned_dotted, f"write-capable call {name!r}"
            # builtins.open would be a bare Name call
            assert not (isinstance(func, ast.Name) and func.id == "open"), \
                "evaluator must not open files for writing"
        # os.open IS used (single-descriptor confined reads), so assert it can
        # only ever be opened read-only: no write/create/append/truncate flag
        # appears anywhere in the module.
        source = (REPO / "app/services/backup_freshness.py").read_text()
        assert "os.O_RDONLY" in source
        for write_flag in ("os.O_WRONLY", "os.O_RDWR", "os.O_CREAT",
                           "os.O_APPEND", "os.O_TRUNC"):
            assert write_flag not in source, write_flag

    def test_threshold_constant_does_not_drift(self):
        """The 36h policy is DEFINED once and READ everywhere else, so the
        report CLI, the MarketOps evaluator, the alert path, the tests and the
        docs can never disagree about what "stale" means."""
        definitions = [
            relative for relative in (
                "app/services/backup_freshness.py", "app/cli.py",
                "app/services/marketops.py", "app/config.py",
            )
            if "BACKUP_FRESHNESS_MAX_AGE_SECONDS = " in (REPO / relative).read_text()
        ]
        assert definitions == ["app/services/backup_freshness.py"]
        # the CLI reads the constant rather than restating the number
        cli_source = (REPO / "app/cli.py").read_text()
        assert "BACKUP_FRESHNESS_MAX_AGE_SECONDS" in cli_source
        assert "129600" not in cli_source
        # the alert path carries the threshold from the evaluator result
        mo_source = (REPO / "app/services/marketops.py").read_text()
        assert "129600" not in mo_source and "36 * 60 * 60" not in mo_source
        assert "threshold_seconds" in mo_source
        doc = (REPO / "docs/SQLITE_BACKUP_FRESHNESS_ALERT_001.md").read_text()
        assert "129600" in doc and "36" in doc

    def test_flag_defaults_off_everywhere(self):
        assert Settings(
            database_url="sqlite:///x.db"
        ).marketops_include_backup_freshness_alert is False
        assert mo.MarketOpsConfig().include_backup_freshness_alert is False
        env_example = (REPO / ".env.example").read_text()
        assert "MARKETOPS_INCLUDE_BACKUP_FRESHNESS_ALERT=false" in env_example

    def test_flag_reads_from_settings(self, monkeypatch):
        monkeypatch.setenv("MARKETOPS_INCLUDE_BACKUP_FRESHNESS_ALERT", "true")
        settings = Settings(database_url="sqlite:///x.db")
        assert settings.marketops_include_backup_freshness_alert is True
        assert mo.MarketOpsConfig.from_settings(
            settings
        ).include_backup_freshness_alert is True

    def test_all_reasons_are_unique_and_mutually_exclusive(self):
        assert len(bf.ALL_REASONS) == len(set(bf.ALL_REASONS))
        assert bf.REASON_HEALTHY in bf.ALL_REASONS
        assert bf.CRITICAL_REASONS.isdisjoint(
            {bf.REASON_HEALTHY, bf.REASON_EVALUATION_ERROR}
        )

    def test_hook_is_gated_and_placed_after_health_alerts(self):
        source = (REPO / "app/services/marketops.py").read_text()
        health_at = source.index("alerts_created += self._health_alerts(session, now)")
        hook_at = source.index("if cfg.include_backup_freshness_alert:")
        assert health_at < hook_at
        assert "self._evaluate_backup_freshness(summary)" in source

    def test_alert_write_never_touches_the_shared_cycle_session(self):
        """The hook takes no session argument at all, so the shared cycle
        session cannot be written, flushed, or poisoned by it — including via
        begin_nested(), which autoflushes before emitting its SAVEPOINT."""
        import inspect as _inspect

        signature = _inspect.signature(
            mo.MarketOpsAutopilotService._evaluate_backup_freshness
        )
        assert list(signature.parameters) == ["self", "summary"]
        body = _inspect.getsource(
            mo.MarketOpsAutopilotService._evaluate_backup_freshness
        )
        code = "\n".join(
            line for line in body.split("\n")
            if not line.lstrip().startswith("#")
        ).split('"""')[-1]  # drop the docstring, which explains why not
        assert "begin_nested" not in code
        assert "alert_session" in code and "alert_session.close()" in code

    def test_documentation_exists(self):
        doc = REPO / "docs/SQLITE_BACKUP_FRESHNESS_ALERT_001.md"
        text = doc.read_text()
        for required in ("36", "129600", "backup_freshness_warning",
                         "MARKETOPS_INCLUDE_BACKUP_FRESHNESS_ALERT",
                         "verify-db-backup", "Rollback"):
            assert required in text


# --------------------------------------------------------------------------
# integration: a real published backup evaluates healthy
# --------------------------------------------------------------------------

class TestAgainstRealBackupPipeline:
    def test_real_published_backup_is_healthy(self, tmp_path):
        """End-to-end against backup_database itself, so the evaluator's view of
        the manifest contract cannot drift from what the pipeline publishes."""
        db_path = tmp_path / "data" / "arena.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            database_url=f"sqlite:///{db_path}",
            backup_dir=str(tmp_path / "backups"),
        )
        stamp_alembic(settings)

        result = bk.backup_database(settings)
        assert result.status == "success"

        health = bf.evaluate_backup_freshness(settings)
        assert health.healthy is True, health.reason
        assert health.newest_backup_filename == Path(result.path).name
        assert health.artifact_bytes == Path(result.path).stat().st_size
        assert health.manifest_backup_bytes == health.artifact_bytes
        assert health.size_matches_manifest is True
        assert health.manifest_status == "verified"
        assert health.manifest_integrity_check == "ok"
        assert health.external_calls == 0

    def test_manifest_field_names_match_the_publisher(self, tmp_path):
        db_path = tmp_path / "data" / "arena.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            database_url=f"sqlite:///{db_path}",
            backup_dir=str(tmp_path / "backups"),
        )
        stamp_alembic(settings)
        result = bk.backup_database(settings)
        published = json.loads(Path(result.manifest_path).read_text())
        for field in ("manifest_version", "created_at", "backup_filename",
                      "backup_bytes", "status", "integrity_check"):
            assert field in published

    def test_asyncio_is_not_required_by_the_evaluator(self, root):
        """The hook runs inside an async cycle but the evaluator is sync and
        must never block on an event loop."""
        write_backup(root, NOW - timedelta(hours=1))

        async def call():
            return evaluate(root)

        assert asyncio.run(call()).healthy is True

    def test_evaluator_tolerates_a_backup_root_it_cannot_read(self, tmp_path):
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o000)
        try:
            result = bf.evaluate_backup_freshness(backup_root=locked, now=NOW)
            assert result.healthy is False
            assert result.reason in (
                bf.REASON_EVALUATION_ERROR, bf.REASON_NO_COMMITTED_BACKUP
            )
        finally:
            os.chmod(locked, 0o700)



# --------------------------------------------------------------------------
# 28-32: full-cycle integration through MarketOpsAutopilotService.run_once
# --------------------------------------------------------------------------

class TestFullCycle:
    """Drives the REAL cycle entrypoint, so flag gating, ordering, one-evaluation
    -per-cycle and failure isolation are proved where they actually run — not
    just on the helper in isolation."""

    def _service(self, db, monkeypatch, root, *, flag, fail_fast=False):
        from tests.test_marketops import autopilot

        cfg = mo.MarketOpsConfig(
            include_probability_markets=False,
            include_crypto=False,
            include_backup_freshness_alert=flag,
            fail_fast=fail_fast,
        )
        calls = pin_evaluator(monkeypatch, root) if root is not None else []
        service = autopilot(cfg=cfg, backup_freshness_session_factory=db.session)
        return service, calls

    def test_flag_off_leaves_no_trace_in_a_real_cycle(self, db, root, monkeypatch):
        write_backup(root, NOW - timedelta(days=30))  # would alert if evaluated
        service, calls = self._service(db, monkeypatch, root, flag=False)
        run = asyncio.run(service.run_once(db.inspect()))
        assert run.status == "ok"
        assert calls == []
        assert db.opened == 0
        assert "backup_freshness" not in run.summary
        assert "backup_freshness_error" not in run.summary
        assert open_freshness_alerts(db.inspect()) == []

    def test_flag_on_evaluates_once_per_cycle(self, db, root, monkeypatch):
        write_backup(root, NOW - timedelta(hours=1))
        service, calls = self._service(db, monkeypatch, root, flag=True)
        run = asyncio.run(service.run_once(db.inspect()))
        assert run.status == "ok"
        assert run.summary["stage_errors"] == {}
        assert len(calls) == 1
        assert run.summary["backup_freshness"]["healthy"] is True
        assert run.summary["backup_freshness"]["alert_action"] == "none"
        assert run.summary["backup_freshness"]["external_calls"] == 0

    def test_two_cycles_evaluate_twice_and_never_duplicate(
        self, db, root, monkeypatch
    ):
        write_backup(root, NOW - timedelta(days=5))
        service, calls = self._service(db, monkeypatch, root, flag=True)
        session = db.inspect()
        first = asyncio.run(service.run_once(session))
        second = asyncio.run(service.run_once(session))
        assert len(calls) == 2
        assert first.summary["backup_freshness"]["alert_action"] == "created"
        assert second.summary["backup_freshness"]["alert_action"] == "updated"
        assert len(open_freshness_alerts(db.inspect())) == 1
        assert first.alerts_created == 1
        assert second.alerts_created == 0

    def test_evaluator_explosion_cannot_fail_the_cycle_under_fail_fast(
        self, db, monkeypatch
    ):
        monkeypatch.setattr(
            bf, "evaluate_backup_freshness",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("evaluator exploded")),
        )
        service, _ = self._service(db, monkeypatch, None, flag=True, fail_fast=True)
        run = asyncio.run(service.run_once(db.inspect()))
        assert run.status == "ok"
        assert run.summary["stage_errors"] == {}
        assert run.summary["backup_freshness_error"].startswith("RuntimeError:")
        assert "backup_freshness" not in run.summary

    def test_alert_write_failure_cannot_fail_or_poison_the_cycle(
        self, db, root, monkeypatch
    ):
        """The riskiest path: the alert write raises mid-cycle."""
        monkeypatch.setattr(
            mo.MarketOpsAlertService, "upsert_open",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("alert write failed")),
        )
        service, _ = self._service(db, monkeypatch, root, flag=True, fail_fast=True)
        run = asyncio.run(service.run_once(db.inspect()))
        assert run.status == "ok"
        assert run.summary["backup_freshness_error"].startswith("RuntimeError:")
        assert run.id is not None and run.finished_at is not None

    def test_real_driver_lock_error_cannot_fail_or_poison_the_cycle(
        self, db, root, tmp_path, monkeypatch
    ):
        """The production hazard, at the DRIVER level rather than mocked: a
        SECOND connection holds an exclusive write lock, so the alert INSERT
        genuinely fails with sqlite3 'database is locked'. The cycle must still
        commit its own run row and report ok."""
        import sqlite3

        from tests.test_marketops import autopilot

        blocker = sqlite3.connect(str(tmp_path / "marketops.db"), timeout=0.1)
        blocker.execute("PRAGMA busy_timeout = 100")

        def locking_factory():
            """Hold a real EXCLUSIVE write lock for EXACTLY the alert write, so
            the INSERT fails at the driver and nothing else in the cycle is
            collateral damage."""
            blocker.execute("BEGIN EXCLUSIVE")
            alert_session = db.session()
            original_close = alert_session.close

            def close_and_release():
                try:
                    blocker.rollback()
                finally:
                    original_close()

            alert_session.close = close_and_release
            return alert_session

        pin_evaluator(monkeypatch, root)
        service = autopilot(
            cfg=mo.MarketOpsConfig(
                include_probability_markets=False, include_crypto=False,
                include_backup_freshness_alert=True, fail_fast=True,
            ),
            backup_freshness_session_factory=locking_factory,
        )
        session = db.inspect()
        try:
            run = asyncio.run(service.run_once(session))
        finally:
            blocker.close()

        assert run.status == "ok"
        # it really was a driver-level lock failure, not some other error
        assert "database is locked" in run.summary["backup_freshness_error"]
        assert run.summary["backup_freshness_error"].startswith("OperationalError:")
        # the shared cycle session survived: the run row is committed and readable
        fresh = db.inspect()
        assert fresh.get(type(run), run.id).status == "ok"
        assert open_freshness_alerts(fresh) == []

    def test_alert_survives_a_later_cycle_failure(self, db, root, monkeypatch):
        """The alert commits on its own isolated session, so it is durable
        independently of whatever the cycle does afterwards — the right
        semantics for a monitoring signal."""
        service, _ = self._service(db, monkeypatch, root, flag=True)
        session = db.inspect()
        asyncio.run(service.run_once(session))
        session.rollback()
        assert len(open_freshness_alerts(db.inspect())) == 1

    def test_cycle_makes_no_backup_or_retention_call(self, db, root, monkeypatch):
        write_backup(root, NOW - timedelta(hours=1))
        for name in ("backup_database", "apply_retention", "prune_old_backups",
                     "verify_backup"):
            monkeypatch.setattr(
                bk, name, lambda *a, **k: pytest.fail(f"cycle called {name}")
            )
        service, _ = self._service(db, monkeypatch, root, flag=True)
        run = asyncio.run(service.run_once(db.inspect()))
        assert run.status == "ok"

    def test_cycle_does_not_mutate_the_backup_root(self, db, root, monkeypatch):
        write_backup(root, NOW - timedelta(hours=1))
        before = {p.name: (p.stat().st_size, p.read_bytes()) for p in root.iterdir()}
        service, _ = self._service(db, monkeypatch, root, flag=True)
        asyncio.run(service.run_once(db.inspect()))
        after = {p.name: (p.stat().st_size, p.read_bytes()) for p in root.iterdir()}
        assert before == after

    def test_publication_window_never_produces_a_spurious_alert(
        self, db, root, monkeypatch
    ):
        """The real interleaving: backup.py publishes the DATA file first and
        the manifest last, so there is an instant where the newest artifact has
        no manifest. The prior verified pair must remain authoritative."""
        write_backup(root, NOW - timedelta(hours=20))          # yesterday's pair
        stamp = (NOW - timedelta(seconds=5)).strftime(STAMP)
        with gzip.open(root / f"{bk.BACKUP_PREFIX}{stamp}{bk.BACKUP_SUFFIX}", "wb") as fh:
            fh.write(b"just-published-data-file")              # manifest not yet renamed
        service, _ = self._service(db, monkeypatch, root, flag=True)
        run = asyncio.run(service.run_once(db.inspect()))
        assert run.summary["backup_freshness"]["healthy"] is True
        assert open_freshness_alerts(db.inspect()) == []

    def test_retention_pass_never_disturbs_a_healthy_verdict(self, db, root):
        """Retention and freshness rank "newest" by different keys (filename
        stamp vs committed created_at) and retention deletes the artifact before
        its manifest. The newest backup is always retained, so the verdict must
        hold across a real retention pass."""
        settings = Settings(
            database_url="sqlite:///unused.db", backup_dir=str(root)
        )
        for days in range(1, 40):
            write_backup(root, NOW - timedelta(days=days))
        write_backup(root, NOW - timedelta(hours=1))
        assert evaluate(root).healthy is True
        bk.apply_retention(settings, dry_run=False)
        assert evaluate(root).healthy is True

    def test_unhealthy_state_is_discoverable_in_marketops_report(
        self, db, root, monkeypatch
    ):
        """The pre-existing db_growth_warning flood mints a new open row every
        ~13 minutes, and marketops-report shows only the 10 newest open alerts.
        A backup alert surfaced ONLY through open_alerts would vanish within
        hours, so it is carried on the run summary instead."""
        service, _ = self._service(db, monkeypatch, root, flag=True)
        session = db.inspect()
        asyncio.run(service.run_once(session))

        for i in range(40):  # bury it, exactly as production does
            session.add(mo.MarketOpsAlert(
                alert_type=mo.ALERT_DB_GROWTH, severity="critical",
                status=mo.ALERT_STATUS_OPEN, title=f"Database at {4000 + i} MiB",
                message="noise", evidence={}, created_at=NOW,
            ))
        session.commit()

        report = mo.MarketOpsReportService().build(session)
        assert all(
            a.alert_type != mo.ALERT_BACKUP_FRESHNESS for a in report.open_alerts
        ), "precondition: the alert really is buried in open_alerts"
        assert report.backup_freshness is not None
        assert report.backup_freshness["healthy"] is False
        assert "Backup protection is unhealthy" in report.recommended_action
        assert bf.REASON_NO_COMMITTED_BACKUP in report.recommended_action
