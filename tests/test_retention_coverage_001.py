"""RETENTION-COVERAGE-001 — read-only retention coverage analysis.

This milestone produces a decision package, not a deletion. The tests below pin
that: no delete path exists, no --confirm flag exists, growth windows are never
extrapolated past the history the database actually holds, and the logical-vs-
physical distinction is stated rather than implied.
"""

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.config import Settings
from app.db import Base
from app.models import MarketPriceTick, MarketOpsRun
from app.services import retention_coverage as rc
from app.services.retention import PROTECTED_TABLES

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 4, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cov.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def settings_for(tmp_path):
    return Settings(database_url=f"sqlite:///{tmp_path / 'cov.db'}")


def seed_ticks(session, count, *, oldest_days=5.0):
    for i in range(count):
        age = oldest_days * (1 - i / max(count - 1, 1))
        session.add(MarketPriceTick(
            market_ticker=f"T-{i % 7}",
            created_at=NOW - timedelta(days=age),
            observed_at=NOW - timedelta(days=age),
        ))
    session.commit()


class TestReadOnly:
    def test_no_confirm_flag_exists(self):
        from app.cli import build_parser

        args = build_parser().parse_args(["retention-coverage-report"])
        assert args.command == "retention-coverage-report"
        assert not hasattr(args, "confirm")
        for flag in ("--confirm", "--apply", "--delete", "--prune"):
            with pytest.raises(SystemExit):
                build_parser().parse_args(["retention-coverage-report", flag])

    def test_module_contains_no_delete_path(self):
        """AST audit: the analysis module cannot delete or mutate anything."""
        tree = ast.parse((REPO / "app/services/retention_coverage.py").read_text())
        banned = {"delete", "remove", "drop", "truncate", "add", "merge",
                  "commit", "flush", "execute_many", "unlink", "rmtree",
                  "write_text", "write_bytes", "mkdir"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_node = node.func
                name = (
                    func_node.attr if isinstance(func_node, ast.Attribute)
                    else func_node.id if isinstance(func_node, ast.Name) else None
                )
                assert name not in banned, f"mutating call {name!r}"
        # Only the SQL this module actually EXECUTES — the docstring
        # necessarily names VACUUM in order to say it is not authorized here.
        executed = [
            n.args[0].value
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == "text"
            and n.args and isinstance(n.args[0], ast.Constant)
        ]
        assert executed, "expected some raw SQL to audit"
        for statement in executed:
            upper = statement.upper()
            for banned_sql in ("DELETE", "UPDATE", "INSERT", "VACUUM", "DROP",
                               "ALTER", "CREATE", "REPLACE"):
                assert banned_sql not in upper, f"{statement!r}: {banned_sql}"

    def test_no_provider_or_network_surface(self):
        source = (REPO / "app/services/retention_coverage.py").read_text().lower()
        for banned in ("httpx", "requests.", "aiohttp", "urllib", "socket",
                       "dexscreener", "goplus", "kalshi", "expected_value",
                       "kelly", "position_siz", "place_order", "wallet"):
            assert banned not in source, banned

    def test_report_declares_its_own_read_only_posture(self, db, tmp_path):
        seed_ticks(db, 20)
        report = rc.build_retention_coverage(
            db, settings_for(tmp_path), now=NOW, include_dbstat=False
        )
        assert report.external_calls == 0
        assert report.persisted is False
        assert report.deletes_performed == 0

    def test_running_the_report_changes_no_row(self, db, tmp_path):
        seed_ticks(db, 40)
        before = db.execute(select(func.count()).select_from(MarketPriceTick)).scalar()
        rc.build_retention_coverage(db, settings_for(tmp_path), now=NOW)
        rc.build_retention_coverage(db, settings_for(tmp_path), now=NOW)
        after = db.execute(select(func.count()).select_from(MarketPriceTick)).scalar()
        assert before == after == 40

    def test_cli_writes_nothing_to_disk(self, db, tmp_path, capsys, monkeypatch):
        from app import cli

        seed_ticks(db, 10)
        monkeypatch.setattr(
            rc, "get_settings", lambda *a, **k: settings_for(tmp_path)
        )
        before = sorted((p.name, p.stat().st_size) for p in tmp_path.iterdir())
        cli.retention_coverage_report(fmt="json", session=db)
        capsys.readouterr()
        after = sorted((p.name, p.stat().st_size) for p in tmp_path.iterdir())
        assert before == after


class TestMeasurementHonesty:
    def test_growth_windows_are_not_extrapolated(self, db, tmp_path):
        """A 30d number over 5d of history is a lie, not an estimate."""
        seed_ticks(db, 50, oldest_days=5.0)
        report = rc.build_retention_coverage(
            db, settings_for(tmp_path), now=NOW, include_dbstat=False
        )
        ticks = next(t for t in report.tables if t["table"] == "market_price_ticks")
        assert ticks["history_days"] is not None and ticks["history_days"] <= 5.1
        assert "1d" in ticks["rows_added"]
        assert "30d" not in ticks["rows_added"], "extrapolated past real history"
        assert "14d" not in ticks["rows_added"]

    def test_windows_appear_once_history_covers_them(self, db, tmp_path):
        seed_ticks(db, 60, oldest_days=20.0)
        report = rc.build_retention_coverage(
            db, settings_for(tmp_path), now=NOW, include_dbstat=False
        )
        ticks = next(t for t in report.tables if t["table"] == "market_price_ticks")
        assert set(ticks["rows_added"]) >= {"1d", "7d", "14d"}
        assert "30d" not in ticks["rows_added"]

    def test_eligible_counts_are_measured_against_the_current_window(
        self, db, tmp_path
    ):
        seed_ticks(db, 100, oldest_days=10.0)
        report = rc.build_retention_coverage(
            db, settings_for(tmp_path), now=NOW, include_dbstat=False
        )
        ticks = next(t for t in report.tables if t["table"] == "market_price_ticks")
        assert ticks["retention_days"] == 7  # RetentionConfig default
        expected = db.execute(
            select(func.count()).select_from(MarketPriceTick).where(
                MarketPriceTick.created_at < NOW - timedelta(days=7)
            )
        ).scalar()
        assert ticks["eligible_now"] == expected
        assert 0 < expected < 100

    def test_freelist_is_reported_separately_from_file_size(self, db, tmp_path):
        seed_ticks(db, 30)
        report = rc.build_retention_coverage(
            db, settings_for(tmp_path), now=NOW, include_dbstat=False
        )
        assert report.page_count is not None
        assert report.page_size == 4096
        assert report.freelist_pages is not None
        assert report.freelist_percent is not None

    def test_missing_dbstat_is_reported_not_faked(self, db, tmp_path, monkeypatch):
        seed_ticks(db, 10)
        monkeypatch.setattr(rc, "_dbstat", lambda *_a, **_k: None)
        report = rc.build_retention_coverage(db, settings_for(tmp_path), now=NOW)
        assert report.dbstat_available is False
        for row in report.tables:
            assert row["total_bytes"] is None
            assert row["percent_of_database"] is None

    def test_tables_without_a_timestamp_column_report_no_windows(self, db, tmp_path):
        seed_ticks(db, 5)
        report = rc.build_retention_coverage(
            db, settings_for(tmp_path), now=NOW, include_dbstat=False
        )
        for row in report.tables:
            if row["timestamp_column"] is None:
                assert row["rows_added"] == {}
                assert row["eligible_now"] is None


class TestClassificationAndFloors:
    def test_every_table_is_classified(self, db, tmp_path):
        seed_ticks(db, 5)
        db.add(MarketOpsRun(status="ok", created_at=NOW, started_at=NOW))
        db.commit()
        report = rc.build_retention_coverage(
            db, settings_for(tmp_path), now=NOW, include_dbstat=False
        )
        valid = {
            rc.RETAIN_INDEFINITELY, rc.RETAIN_LONG_WINDOW,
            rc.RETAIN_AGGREGATED_ONLY, rc.SAFE_FOR_BOUNDED_PRUNING,
            rc.BLOCKED_PENDING_DEPENDENCY_REVIEW,
        }
        assert report.tables
        for row in report.tables:
            assert row["classification"] in valid

    def test_protected_and_unbounded_tables_are_blocked_not_prunable(self):
        """A table that is both protected and has no window is exactly the case
        that needs a human decision — never a default."""
        for table in ("market_snapshots", "crypto_token_discovery_events",
                      "crypto_token_risk_assessments"):
            assert table in PROTECTED_TABLES
            assert rc._classify(table, None, True) == \
                rc.BLOCKED_PENDING_DEPENDENCY_REVIEW

    def test_raw_ticks_are_retain_aggregated_only(self):
        assert rc._classify("market_price_ticks", 2, False) == rc.RETAIN_AGGREGATED_ONLY

    def test_calibration_evidence_is_retain_indefinitely(self):
        for table in ("market_outcomes", "forecast_scores", "market_forecasts",
                      "crypto_horizon_cohorts", "crypto_horizon_observations"):
            assert rc._classify(table, None, table in PROTECTED_TABLES) == \
                rc.RETAIN_INDEFINITELY

    def test_preservation_floors_carry_a_reason(self):
        for table, (days, reason) in rc.PRESERVATION_FLOORS.items():
            assert reason and len(reason) > 20, table
            assert days is None or days > 0

    def test_floors_are_evidence_backed_not_size_driven(self):
        """The floors must justify themselves on research grounds — none may
        cite the size gate as its rationale."""
        for table, (_days, reason) in rc.PRESERVATION_FLOORS.items():
            lowered = reason.lower()
            for banned in ("3072", "size gate", "under the gate", "shrink"):
                assert banned not in lowered, f"{table}: {banned}"

    def test_blocked_tables_list_their_readers(self, db, tmp_path):
        seed_ticks(db, 5)
        report = rc.build_retention_coverage(
            db, settings_for(tmp_path), now=NOW, include_dbstat=False
        )
        for row in report.tables:
            if row["classification"] == rc.BLOCKED_PENDING_DEPENDENCY_REVIEW:
                assert row["table"] in rc.DEPENDENCIES or row["readers"] == []

    def test_retention_windows_match_the_deployed_config(self, db, tmp_path):
        from app.services.retention import RetentionConfig

        cfg = RetentionConfig.from_settings(settings_for(tmp_path))
        assert rc._retention_days_for("market_price_ticks", cfg) == cfg.tick_days
        assert rc._retention_days_for("market_price_tick_buckets", cfg) == \
            cfg.tick_bucket_days
        assert rc._retention_days_for("crypto_price_ticks", cfg) == cfg.crypto_days
        # signal_days == 0 means "keep forever", not "prune everything"
        assert rc._retention_days_for("opportunity_signals", cfg) is None
        assert rc._retention_days_for("market_snapshots", cfg) is None


class TestOutput:
    def test_text_and_json_parity(self, db, tmp_path, capsys, monkeypatch):
        from app import cli

        seed_ticks(db, 25)
        monkeypatch.setattr(
            rc, "get_settings", lambda *a, **k: settings_for(tmp_path)
        )
        text = cli.retention_coverage_report(fmt="text", session=db)
        capsys.readouterr()
        data = cli.retention_coverage_report(fmt="json", session=db)
        out = capsys.readouterr().out
        volatile = {"generated_at"}
        assert {k: v for k, v in text.items() if k not in volatile} == \
               {k: v for k, v in data.items() if k not in volatile}
        assert json.loads(out)["tables"] == data["tables"]

    def test_text_output_states_the_logical_vs_physical_boundary(
        self, db, tmp_path, capsys, monkeypatch
    ):
        from app import cli

        seed_ticks(db, 10)
        monkeypatch.setattr(
            rc, "get_settings", lambda *a, **k: settings_for(tmp_path)
        )
        cli.retention_coverage_report(fmt="text", session=db)
        out = capsys.readouterr().out
        assert "high-water-mark ratchet" in out
        assert "does NOT shrink" in out
        assert "VACUUM" in out
        assert "authorizes neither" in out
        assert "READ ONLY" in out

    def test_output_is_secret_free(self, db, tmp_path, capsys, monkeypatch):
        from app import cli

        seed_ticks(db, 10)
        monkeypatch.setattr(
            rc, "get_settings", lambda *a, **k: settings_for(tmp_path)
        )
        cli.retention_coverage_report(fmt="json", session=db)
        out = capsys.readouterr().out.lower()
        for banned in ("password", "secret", "api_key", "apikey", "authorization",
                       "bearer", "postgresql://", "sk-", "token="):
            assert banned not in out

    def test_no_migration_added(self):
        for path in (REPO / "alembic/versions").glob("*.py"):
            assert "retention_coverage" not in path.read_text().lower()

    def test_documentation_exists_and_states_the_verdict(self):
        doc = REPO / "docs/RETENTION_COVERAGE_2026_08.md"
        text = doc.read_text()
        lowered = text.lower()
        for required in ("preservation floors", "conservative", "balanced",
                         "aggressive", "vacuum", "high-water", "raw_payload",
                         "activation gate"):
            assert required in lowered, required
        verdicts = (
            "READY FOR BOUNDED RETENTION ACTIVATION",
            "MORE DEPENDENCY EVIDENCE REQUIRED",
            "RETENTION IS NOT THE PRIMARY GROWTH LEVER",
        )
        assert sum(v in text for v in verdicts) >= 1

    def test_no_pruning_is_activated_by_this_milestone(self):
        """The retention service's own defaults must be untouched."""
        from app.services.retention import RetentionConfig

        defaults = RetentionConfig()
        assert defaults.tick_days == 7
        assert defaults.signal_days == 0
        assert defaults.tick_bucket_days == 90
        assert defaults.crypto_days == 7


class TestOrdering:
    def test_ranks_by_rows_when_byte_attribution_is_unavailable(
        self, db, tmp_path, monkeypatch
    ):
        """--no-dbstat has no byte counts; an alphabetical list presented as a
        ranking would mislead the exact decision this report exists to support."""
        seed_ticks(db, 40)
        for i in range(5):
            db.add(MarketOpsRun(status="ok", created_at=NOW, started_at=NOW))
        db.commit()
        report = rc.build_retention_coverage(
            db, settings_for(tmp_path), now=NOW, include_dbstat=False
        )
        rows = [t["rows"] for t in report.tables]
        assert rows == sorted(rows, reverse=True)
        assert report.tables[0]["table"] == "market_price_ticks"
