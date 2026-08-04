"""DB-GROWTH-ALERT-IDENTITY-001 — stable database-growth alert identity.

The pre-repair alert embedded the rounded database size in its TITLE, and
MarketOpsAlertService dedupes on (alert_type, title) — so every 1 MiB of growth
minted a new open row. Production accumulated 933 open rows with 933 distinct
titles between 2026-07-05 and 2026-08-03, which pinned marketops-report's
recommended action and buried every other alert type.

These tests pin the repaired contract: one stable identity, refreshed in place;
a resolution path; strict legacy matching; and a bounded, atomic, non-deleting
reconciliation for the existing backlog. Thresholds, the size calculation and
the severity bands are asserted UNCHANGED.

Everything runs against disposable in-memory/temp SQLite databases. No
production alert row is read or written by any test.
"""

import ast
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  — registers every table on Base.metadata
from app.config import Settings
from app.db import Base
from app.models import MarketOpsAlert
from app.services import marketops as mo

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 3, 21, 0, 0, tzinfo=timezone.utc)

WARN_MB = 1536.0
CRIT_MB = 3072.0


# --------------------------------------------------------------------------
# disposable fixtures
# --------------------------------------------------------------------------

class DisposableDb:
    """Disposable MarketOps database plus a real isolated session factory —
    production gives the alert write its own session, so tests must too.

    Tracks every session it hands out and disposes the engine on teardown: an
    undisposed file-backed SQLite engine per test leaks connections and slows
    (and destabilises) the rest of the suite.
    """

    def __init__(self, tmp_path):
        self.engine = create_engine(f"sqlite:///{tmp_path / 'marketops.db'}")
        Base.metadata.create_all(self.engine)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._issued = []

    def session(self):
        s = self._factory()
        self._issued.append(s)
        return s

    def inspect(self):
        return self.session()

    def close(self):
        for s in self._issued:
            try:
                s.close()
            except Exception:  # pragma: no cover - teardown must not fail a test
                pass
        self._issued.clear()
        self.engine.dispose()


@pytest.fixture
def db(tmp_path):
    disposable = DisposableDb(tmp_path)
    try:
        yield disposable
    finally:
        disposable.close()


def settings_for(tmp_path, warn=WARN_MB, crit=CRIT_MB) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'unused.db'}",
        db_growth_warning_mb=warn,
        db_growth_critical_mb=crit,
    )


def autopilot(db, size_mb, monkeypatch, tmp_path, *, fail_fast=False, warn=WARN_MB,
              crit=CRIT_MB):
    """A minimal cycle wired to a fixed reported database size."""
    monkeypatch.setattr(
        mo, "database_size_mb", lambda *a, **k: size_mb, raising=True
    )
    monkeypatch.setattr(
        mo, "get_settings", lambda *a, **k: settings_for(tmp_path, warn, crit)
    )
    cfg = mo.MarketOpsConfig(
        include_probability_markets=False,
        include_crypto=False,
        include_backup_freshness_alert=False,
        fail_fast=fail_fast,
    )
    return mo.MarketOpsAutopilotService(config=cfg, alert_session_factory=db.session)


def run_hook(db, size_mb, monkeypatch, tmp_path, **kw):
    service = autopilot(db, size_mb, monkeypatch, tmp_path, **kw)
    summary: dict = {}
    created = service._evaluate_db_growth(summary)
    return created, summary


def growth_alerts(session, status=None):
    q = select(MarketOpsAlert).where(
        MarketOpsAlert.alert_type == mo.ALERT_DB_GROWTH
    ).order_by(MarketOpsAlert.id.asc())
    if status:
        q = q.where(MarketOpsAlert.status == status)
    return list(session.execute(q).scalars().all())


def seed_legacy(session, count, *, severity="critical", start_mb=4000.0, at=None):
    """Reproduce the production backlog: one open row per distinct integer-MiB
    title, exactly as the pre-repair code minted them."""
    at = at or (NOW - timedelta(days=10))
    for i in range(count):
        size = start_mb + i
        suffix = " (critical)" if severity == "critical" else ""
        session.add(MarketOpsAlert(
            alert_type=mo.ALERT_DB_GROWTH,
            severity=severity,
            status=mo.ALERT_STATUS_OPEN,
            title=f"Database at {size:.0f} MiB{suffix}",
            message="legacy",
            evidence={"size_mb": round(size, 2), "threshold_mb": CRIT_MB},
            created_at=at + timedelta(minutes=6 * i),
        ))
    session.commit()


# --------------------------------------------------------------------------
# 1-2: the stable identity, and unchanged threshold/severity semantics
# --------------------------------------------------------------------------

class TestStableIdentity:
    def test_title_is_stable_and_carries_no_measurement(self):
        title = mo.ALERT_DB_GROWTH_TITLE
        assert not re.search(r"\d", title), "a digit in the title recreates the bug"
        for token in ("MiB", "MB", "size", "{"):
            assert token not in title

    def test_alert_type_is_unchanged_so_history_is_preserved(self):
        assert mo.ALERT_DB_GROWTH == "db_growth_warning"

    @pytest.mark.parametrize(
        "size_mb,expected_severity,expected_status",
        [
            (100.0, None, "ok"),
            (1535.9, None, "ok"),
            (1536.0, "warning", "warning"),      # >= warning
            (3071.9, "warning", "warning"),
            (3072.0, "critical", "critical"),    # >= critical
            (4261.71, "critical", "critical"),   # the real production size
        ],
    )
    def test_threshold_and_severity_semantics_unchanged(
        self, db, monkeypatch, tmp_path, size_mb, expected_severity, expected_status
    ):
        """Exactly the pre-repair bands: >= critical -> critical,
        >= warning -> warning, otherwise nothing."""
        _created, summary = run_hook(db, size_mb, monkeypatch, tmp_path)
        assert summary["db_growth"]["severity"] == expected_severity
        assert summary["db_growth"]["status"] == expected_status
        assert summary["db_growth"]["warning_mb"] == WARN_MB
        assert summary["db_growth"]["critical_mb"] == CRIT_MB

    def test_thresholds_are_read_from_settings_not_hardcoded(
        self, db, monkeypatch, tmp_path
    ):
        _c, summary = run_hook(
            db, 700.0, monkeypatch, tmp_path, warn=512.0, crit=1024.0
        )
        assert summary["db_growth"]["severity"] == "warning"
        assert summary["db_growth"]["warning_mb"] == 512.0

    def test_size_calculation_is_untouched(self):
        """database_size_mb is the pre-existing function; this milestone did not
        redefine how the database is measured."""
        import inspect

        source = inspect.getsource(mo.database_size_mb)
        assert "os.path.getsize" in source
        assert "1024 * 1024" in source


# --------------------------------------------------------------------------
# 3-8: the repaired lifecycle
# --------------------------------------------------------------------------

class TestLifecycle:
    def test_first_unhealthy_cycle_creates_exactly_one(self, db, monkeypatch, tmp_path):
        created, summary = run_hook(db, 4000.0, monkeypatch, tmp_path)
        assert created == 1
        assert summary["db_growth"]["alert_action"] == "created"
        alerts = growth_alerts(db.inspect(), "open")
        assert len(alerts) == 1
        assert alerts[0].title == mo.ALERT_DB_GROWTH_TITLE
        assert alerts[0].severity == "critical"

    def test_repeated_unhealthy_cycles_keep_exactly_one(self, db, monkeypatch, tmp_path):
        """The regression that produced 933 rows: 40 cycles at 40 different
        sizes must yield ONE row, not 40."""
        actions = []
        for i in range(40):
            created, summary = run_hook(db, 4000.0 + i, monkeypatch, tmp_path)
            actions.append((created, summary["db_growth"]["alert_action"]))
        assert actions[0] == (1, "created")
        assert all(a == (0, "updated") for a in actions[1:])
        alerts = growth_alerts(db.inspect(), "open")
        assert len(alerts) == 1
        assert len({a.title for a in growth_alerts(db.inspect())}) == 1

    def test_details_refresh_with_the_new_size(self, db, monkeypatch, tmp_path):
        run_hook(db, 4000.0, monkeypatch, tmp_path)
        first = growth_alerts(db.inspect(), "open")[0]
        assert first.evidence["size_mb"] == 4000.0

        run_hook(db, 4500.0, monkeypatch, tmp_path)
        alerts = growth_alerts(db.inspect(), "open")
        assert len(alerts) == 1
        assert alerts[0].id == first.id
        assert alerts[0].evidence["size_mb"] == 4500.0
        assert "4500" in alerts[0].message

    def test_severity_escalates_in_place(self, db, monkeypatch, tmp_path):
        run_hook(db, 2000.0, monkeypatch, tmp_path)          # warning band
        alert = growth_alerts(db.inspect(), "open")[0]
        assert alert.severity == "warning"

        run_hook(db, 4000.0, monkeypatch, tmp_path)          # critical band
        alerts = growth_alerts(db.inspect(), "open")
        assert len(alerts) == 1
        assert alerts[0].id == alert.id
        assert alerts[0].severity == "critical"

    def test_healthy_cycle_resolves_the_canonical_alert(self, db, monkeypatch, tmp_path):
        run_hook(db, 4000.0, monkeypatch, tmp_path)
        assert len(growth_alerts(db.inspect(), "open")) == 1

        created, summary = run_hook(db, 100.0, monkeypatch, tmp_path)
        assert created == 0
        assert summary["db_growth"]["alert_action"] == "resolved"
        assert growth_alerts(db.inspect(), "open") == []
        resolved = growth_alerts(db.inspect(), "resolved")
        assert len(resolved) == 1
        assert resolved[0].resolved_at is not None

    def test_healthy_cycle_also_resolves_legacy_duplicates(
        self, db, monkeypatch, tmp_path
    ):
        seed_legacy(db.inspect(), 25)
        created, summary = run_hook(db, 100.0, monkeypatch, tmp_path)
        assert created == 0
        assert summary["db_growth"]["alert_action"] == "resolved"
        assert growth_alerts(db.inspect(), "open") == []
        assert len(growth_alerts(db.inspect(), "resolved")) == 25

    def test_repeated_healthy_cycles_stay_quiet(self, db, monkeypatch, tmp_path):
        actions = [
            run_hook(db, 100.0, monkeypatch, tmp_path)[1]["db_growth"]["alert_action"]
            for _ in range(10)
        ]
        assert actions == ["none"] * 10
        assert growth_alerts(db.inspect()) == []

    def test_recovery_then_regression_reopens_exactly_one(
        self, db, monkeypatch, tmp_path
    ):
        run_hook(db, 4000.0, monkeypatch, tmp_path)
        run_hook(db, 100.0, monkeypatch, tmp_path)
        created, _ = run_hook(db, 4000.0, monkeypatch, tmp_path)
        assert created == 1
        assert len(growth_alerts(db.inspect(), "open")) == 1

    def test_unmeasurable_size_changes_nothing_and_never_resolves(
        self, db, monkeypatch, tmp_path
    ):
        """An evaluation that could not measure the database has no business
        closing a critical alert."""
        run_hook(db, 4000.0, monkeypatch, tmp_path)
        open_before = growth_alerts(db.inspect(), "open")
        assert len(open_before) == 1

        created, summary = run_hook(db, None, monkeypatch, tmp_path)
        assert created == 0
        assert summary["db_growth"]["status"] == "unavailable"
        assert summary["db_growth"]["alert_action"] == "none"
        still_open = growth_alerts(db.inspect(), "open")
        assert len(still_open) == 1
        assert still_open[0].severity == "critical"

    def test_evaluation_error_does_not_resolve_a_critical_alert(
        self, db, monkeypatch, tmp_path
    ):
        run_hook(db, 4000.0, monkeypatch, tmp_path)
        assert len(growth_alerts(db.inspect(), "open")) == 1

        def boom(*_a, **_k):
            raise RuntimeError("size probe exploded")

        service = autopilot(db, 4000.0, monkeypatch, tmp_path)
        monkeypatch.setattr(mo, "database_size_mb", boom)
        summary: dict = {}
        created = service._evaluate_db_growth(summary)
        assert created == 0
        assert summary["db_growth_error"].startswith("RuntimeError:")
        assert "db_growth" not in summary
        still_open = growth_alerts(db.inspect(), "open")
        assert len(still_open) == 1
        assert still_open[0].severity == "critical"

    def test_hook_error_is_bounded(self, db, monkeypatch, tmp_path):
        service = autopilot(db, 4000.0, monkeypatch, tmp_path)
        monkeypatch.setattr(
            mo, "database_size_mb",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x" * 5000)),
        )
        summary: dict = {}
        service._evaluate_db_growth(summary)
        assert len(summary["db_growth_error"]) <= 320

    def test_summary_is_bounded_and_secret_free(self, db, monkeypatch, tmp_path):
        _c, summary = run_hook(db, 4000.0, monkeypatch, tmp_path)
        blob = json.dumps(summary["db_growth"]).lower()
        for banned in ("password", "secret", "api_key", "authorization", "bearer",
                       "postgresql://", "sk-", "token=", "/users/", "/home/"):
            assert banned not in blob
        assert len(blob) < 512
        assert summary["db_growth"]["external_calls"] == 0


# --------------------------------------------------------------------------
# 9-11: the strict legacy matcher
# --------------------------------------------------------------------------

class TestLegacyMatcher:
    @pytest.mark.parametrize("title", [
        "Database at 513 MiB",
        "Database at 4262 MiB (critical)",
        "Database at 1 MiB",
        "Database at 999999 MiB",
    ])
    def test_matches_the_real_legacy_titles(self, title):
        assert mo.is_legacy_db_growth_title(title)

    @pytest.mark.parametrize("title", [
        "",
        "Backup protection unhealthy",
        "Database growth above threshold",          # the canonical title
        "Signal flood: 900 signals in the last hour",
        "Watcher looks stale or errored",
        "Database at MiB",
        "Database at 4262 GiB",
        "Database at 4262 MiB (warning)",
        "prefix Database at 4262 MiB",              # anchored: no substring match
        "Database at 4262 MiB suffix",
        "Database at 4262 MiB (critical) extra",
        "database at 4262 mib",                     # case sensitive
    ])
    def test_rejects_everything_else(self, title):
        assert not mo.is_legacy_db_growth_title(title)

    def test_pattern_is_anchored_not_a_substring_search(self):
        source = REPO / "app/services/marketops.py"
        text = source.read_text()
        assert "LEGACY_DB_GROWTH_TITLE_RE.fullmatch" in text
        assert "LEGACY_DB_GROWTH_TITLE_RE.search" not in text
        assert "like(" not in text.split("LEGACY_DB_GROWTH_TITLE_RE")[1][:2000]

    def test_matcher_is_always_paired_with_the_alert_type(self, db, tmp_path):
        """Even a same-titled alert of another type must be untouchable."""
        session = db.inspect()
        session.add(MarketOpsAlert(
            alert_type="some_other_type", severity="critical",
            status=mo.ALERT_STATUS_OPEN, title="Database at 4262 MiB (critical)",
            message="impostor", evidence={}, created_at=NOW,
        ))
        seed_legacy(session, 3)
        report = mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        assert report["matched_legacy"] == 3
        impostor = session.execute(
            select(MarketOpsAlert).where(MarketOpsAlert.alert_type == "some_other_type")
        ).scalars().one()
        assert impostor.status == mo.ALERT_STATUS_OPEN


# --------------------------------------------------------------------------
# 12-17: the reconciliation CLI
# --------------------------------------------------------------------------

class TestReconciliation:
    @pytest.fixture(autouse=True)
    def _measurable_size(self, monkeypatch):
        """Production always has a measurable size here; the unmeasurable case
        is covered explicitly below."""
        monkeypatch.setattr(mo, "database_size_mb", lambda *a, **k: 4000.0)

    def test_unmeasurable_size_converges_but_never_resolves_the_canonical(
        self, db, tmp_path, monkeypatch
    ):
        """Unavailable is not healthy: a failed measurement must not close a
        critical alert."""
        session = db.inspect()
        seed_legacy(session, 20)
        monkeypatch.setattr(mo, "database_size_mb", lambda *a, **k: None)
        report = mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        assert report["measurable"] is False
        assert report["severity"] is None
        assert report["status"] == "unavailable"
        assert report["canonical_refreshed"] is False
        assert report["remaining_open_after"] == 1
        assert report["resolved"] == 19
        open_rows = growth_alerts(db.inspect(), "open")
        assert len(open_rows) == 1
        assert open_rows[0].title == mo.ALERT_DB_GROWTH_TITLE
        assert open_rows[0].severity == "critical"      # untouched
        assert open_rows[0].evidence["size_mb"] == 4000.0  # untouched

    def test_dry_run_writes_nothing(self, db, tmp_path):
        session = db.inspect()
        seed_legacy(session, 933)
        report = mo.reconcile_db_growth_alerts(
            session, confirm=False, settings=settings_for(tmp_path)
        )
        assert report["persisted"] is False
        assert report["confirmed"] is False
        assert report["matched_legacy"] == 933
        assert report["would_resolve"] == 932
        assert report["remaining_open_after"] == 1
        assert report["external_calls"] == 0
        # nothing changed on disk
        fresh = db.inspect()
        assert len(growth_alerts(fresh, "open")) == 933
        assert growth_alerts(fresh, "resolved") == []

    def test_dry_run_reports_full_scope(self, db, tmp_path):
        session = db.inspect()
        seed_legacy(session, 10)
        report = mo.reconcile_db_growth_alerts(
            session, confirm=False, settings=settings_for(tmp_path)
        )
        for key in ("open_total", "matched_total", "matched_canonical",
                    "matched_legacy", "already_resolved", "canonical_id",
                    "canonical_source", "would_resolve", "would_resolve_id_range",
                    "excluded_unmatched", "size_mb", "severity", "hard_deletes"):
            assert key in report, key
        assert report["hard_deletes"] == 0
        assert report["canonical_source"] == "promoted_legacy"

    def test_confirmed_reconciliation_converges_to_one(self, db, tmp_path):
        session = db.inspect()
        seed_legacy(session, 933)
        report = mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        assert report["persisted"] is True
        assert report["resolved"] == 932
        fresh = db.inspect()
        open_rows = growth_alerts(fresh, "open")
        assert len(open_rows) == 1
        assert open_rows[0].title == mo.ALERT_DB_GROWTH_TITLE
        assert open_rows[0].id == report["canonical_id"]
        assert len(growth_alerts(fresh, "resolved")) == 932

    def test_no_hard_deletion_history_is_preserved(self, db, tmp_path):
        session = db.inspect()
        seed_legacy(session, 50)
        before = len(growth_alerts(session))
        mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        assert len(growth_alerts(db.inspect())) == before == 50

    def test_canonical_preserves_the_earliest_observation(self, db, tmp_path):
        session = db.inspect()
        oldest = NOW - timedelta(days=29)
        seed_legacy(session, 20, at=oldest)
        report = mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        canonical = db.inspect().get(MarketOpsAlert, report["canonical_id"])
        assert canonical.created_at.replace(tzinfo=timezone.utc) == oldest
        assert canonical.evidence["condition_first_observed_at"].startswith(
            oldest.strftime("%Y-%m-%d")
        )

    def test_existing_canonical_row_is_preferred_over_promotion(self, db, tmp_path):
        session = db.inspect()
        seed_legacy(session, 5)
        session.add(MarketOpsAlert(
            alert_type=mo.ALERT_DB_GROWTH, severity="critical",
            status=mo.ALERT_STATUS_OPEN, title=mo.ALERT_DB_GROWTH_TITLE,
            message="canonical", evidence={}, created_at=NOW,
        ))
        session.commit()
        report = mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        assert report["canonical_source"] == "existing_canonical"
        assert report["resolved"] == 5
        assert len(growth_alerts(db.inspect(), "open")) == 1

    def test_reconciliation_is_idempotent(self, db, tmp_path):
        session = db.inspect()
        seed_legacy(session, 100)
        first = mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        state_1 = [(a.id, a.status, a.title) for a in growth_alerts(db.inspect())]

        second = mo.reconcile_db_growth_alerts(
            db.inspect(), confirm=True, settings=settings_for(tmp_path)
        )
        state_2 = [(a.id, a.status, a.title) for a in growth_alerts(db.inspect())]

        assert state_1 == state_2
        assert second["resolved"] == 0
        assert second["canonical_id"] == first["canonical_id"]
        assert second["matched_legacy"] == 0
        assert len(growth_alerts(db.inspect(), "open")) == 1

    def test_healthy_database_resolves_everything_and_creates_nothing(
        self, db, tmp_path
    ):
        session = db.inspect()
        seed_legacy(session, 40)
        report = mo.reconcile_db_growth_alerts(
            session, confirm=True,
            settings=settings_for(tmp_path, warn=99000.0, crit=99999.0),
        )
        assert report["severity"] is None
        assert report["remaining_open_after"] == 0
        assert report["resolved"] == 40
        assert growth_alerts(db.inspect(), "open") == []

    def test_transaction_failure_rolls_back(self, db, tmp_path, monkeypatch):
        session = db.inspect()
        seed_legacy(session, 30)

        real_commit = Session.commit

        def exploding_commit(self, *a, **k):
            raise RuntimeError("commit failed")

        monkeypatch.setattr(Session, "commit", exploding_commit)
        with pytest.raises(RuntimeError):
            mo.reconcile_db_growth_alerts(
                session, confirm=True, settings=settings_for(tmp_path)
            )
        monkeypatch.setattr(Session, "commit", real_commit)

        fresh = db.inspect()
        assert len(growth_alerts(fresh, "open")) == 30
        assert growth_alerts(fresh, "resolved") == []

    def test_backup_freshness_alert_is_never_touched(self, db, tmp_path):
        session = db.inspect()
        session.add(MarketOpsAlert(
            alert_type=mo.ALERT_BACKUP_FRESHNESS, severity="critical",
            status=mo.ALERT_STATUS_OPEN, title=mo.ALERT_BACKUP_FRESHNESS_TITLE,
            message="backup gone", evidence={"reason": "no_committed_backup"},
            created_at=NOW,
        ))
        seed_legacy(session, 12)
        mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        freshness = session.execute(
            select(MarketOpsAlert).where(
                MarketOpsAlert.alert_type == mo.ALERT_BACKUP_FRESHNESS
            )
        ).scalars().one()
        assert freshness.status == mo.ALERT_STATUS_OPEN
        assert freshness.severity == "critical"
        assert freshness.evidence == {"reason": "no_committed_backup"}

    def test_unrelated_alert_types_are_untouched(self, db, tmp_path):
        session = db.inspect()
        for alert_type in (mo.ALERT_TOO_MANY_SIGNALS, mo.ALERT_SERVICE_HEALTH,
                           mo.ALERT_SOURCE_BACKED_FORECAST, mo.ALERT_CRYPTO_SPIKE,
                           mo.ALERT_CC_SAMPLE_UPDATE, mo.ALERT_PROVIDER_ERROR,
                           mo.ALERT_NO_RECENT_SIGNALS):
            session.add(MarketOpsAlert(
                alert_type=alert_type, severity="warning",
                status=mo.ALERT_STATUS_OPEN, title=f"{alert_type} title",
                message="x", evidence={}, created_at=NOW,
            ))
        seed_legacy(session, 8)
        mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        others = session.execute(
            select(MarketOpsAlert).where(
                MarketOpsAlert.alert_type != mo.ALERT_DB_GROWTH
            )
        ).scalars().all()
        assert len(others) == 7
        assert all(a.status == mo.ALERT_STATUS_OPEN for a in others)

    def test_unmatched_db_growth_titles_are_reported_and_never_written(
        self, db, tmp_path
    ):
        session = db.inspect()
        session.add(MarketOpsAlert(
            alert_type=mo.ALERT_DB_GROWTH, severity="warning",
            status=mo.ALERT_STATUS_OPEN, title="Some hand-written growth note",
            message="manual", evidence={}, created_at=NOW,
        ))
        seed_legacy(session, 4)
        report = mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        assert report["excluded_unmatched"] == 1
        assert report["excluded_unmatched_titles"] == ["Some hand-written growth note"]
        untouched = session.execute(
            select(MarketOpsAlert).where(
                MarketOpsAlert.title == "Some hand-written growth note"
            )
        ).scalars().one()
        assert untouched.status == mo.ALERT_STATUS_OPEN

    def test_already_resolved_rows_are_left_alone(self, db, tmp_path):
        session = db.inspect()
        seed_legacy(session, 6)
        rows = growth_alerts(session, "open")
        rows[0].status = mo.ALERT_STATUS_RESOLVED
        rows[0].resolved_at = NOW - timedelta(days=1)
        session.commit()
        stamp = rows[0].resolved_at

        report = mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        assert report["already_resolved"] == 1
        reloaded = db.inspect().get(MarketOpsAlert, rows[0].id)
        assert reloaded.status == mo.ALERT_STATUS_RESOLVED
        assert reloaded.resolved_at.replace(tzinfo=None) == stamp.replace(tzinfo=None)


# --------------------------------------------------------------------------
# 5, 18-19: reconciliation + natural cycle interaction
# --------------------------------------------------------------------------

class TestReconciliationAndCycle:
    @pytest.fixture(autouse=True)
    def _measurable_size(self, monkeypatch):
        monkeypatch.setattr(mo, "database_size_mb", lambda *a, **k: 4000.0)

    def test_natural_cycle_after_reconciliation_refreshes_not_duplicates(
        self, db, monkeypatch, tmp_path
    ):
        """The production sequence: reconcile the backlog, then let MarketOps
        run. It must refresh the canonical row, not mint a 934th."""
        session = db.inspect()
        seed_legacy(session, 933)
        report = mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        assert len(growth_alerts(db.inspect(), "open")) == 1

        for size in (4262.0, 4263.0, 4264.0):
            run_hook(db, size, monkeypatch, tmp_path)

        open_rows = growth_alerts(db.inspect(), "open")
        assert len(open_rows) == 1
        assert open_rows[0].id == report["canonical_id"]
        assert open_rows[0].evidence["size_mb"] == 4264.0
        assert len(growth_alerts(db.inspect())) == 933

    def test_cycle_then_reconcile_also_converges(self, db, monkeypatch, tmp_path):
        """The other order: a cycle mints the canonical row alongside the
        legacy backlog, then reconciliation resolves the legacy rows."""
        seed_legacy(db.inspect(), 50)
        run_hook(db, 4262.0, monkeypatch, tmp_path)
        assert len(growth_alerts(db.inspect(), "open")) == 51

        report = mo.reconcile_db_growth_alerts(
            db.inspect(), confirm=True, settings=settings_for(tmp_path)
        )
        assert report["canonical_source"] == "existing_canonical"
        assert report["resolved"] == 50
        assert len(growth_alerts(db.inspect(), "open")) == 1

    def test_the_933_row_pattern_cannot_recur(self, db, monkeypatch, tmp_path):
        """60 cycles at 60 distinct sizes — the production workload that produced
        the backlog. The invariant is proven long before this many, but the run
        is cheap and the regression it guards was expensive."""
        for i in range(60):
            run_hook(db, 4000.0 + i * 1.7, monkeypatch, tmp_path)
        assert len(growth_alerts(db.inspect(), "open")) == 1
        assert len(growth_alerts(db.inspect())) == 1


# --------------------------------------------------------------------------
# 18-22: full-cycle integration and isolation
# --------------------------------------------------------------------------

class TestFullCycle:
    def _service(self, db, monkeypatch, tmp_path, size_mb, **kw):
        from tests.test_marketops import autopilot as base_autopilot

        monkeypatch.setattr(mo, "database_size_mb", lambda *a, **k: size_mb)
        monkeypatch.setattr(
            mo, "get_settings", lambda *a, **k: settings_for(tmp_path)
        )
        cfg = mo.MarketOpsConfig(
            include_probability_markets=False, include_crypto=False,
            include_backup_freshness_alert=False, **kw,
        )
        return base_autopilot(cfg=cfg, alert_session_factory=db.session)

    def test_evaluates_once_per_natural_cycle(self, db, monkeypatch, tmp_path):
        calls = []
        real = mo.database_size_mb

        service = self._service(db, monkeypatch, tmp_path, 4000.0)
        monkeypatch.setattr(
            mo, "database_size_mb",
            lambda *a, **k: (calls.append(1), 4000.0)[1],
        )
        run = asyncio.run(service.run_once(db.inspect()))
        assert run.status == "ok"
        assert run.summary["stage_errors"] == {}
        assert len(calls) == 1
        assert run.summary["db_growth"]["alert_action"] == "created"
        assert run.alerts_created == 1
        assert real is not None

    def test_two_cycles_never_duplicate(self, db, monkeypatch, tmp_path):
        service = self._service(db, monkeypatch, tmp_path, 4000.0)
        session = db.inspect()
        first = asyncio.run(service.run_once(session))
        second = asyncio.run(service.run_once(session))
        assert first.summary["db_growth"]["alert_action"] == "created"
        assert second.summary["db_growth"]["alert_action"] == "updated"
        assert first.alerts_created == 1
        assert second.alerts_created == 0
        assert len(growth_alerts(db.inspect(), "open")) == 1

    def test_hook_explosion_cannot_fail_the_cycle_under_fail_fast(
        self, db, monkeypatch, tmp_path
    ):
        service = self._service(db, monkeypatch, tmp_path, 4000.0, fail_fast=True)
        monkeypatch.setattr(
            mo, "database_size_mb",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("probe exploded")),
        )
        run = asyncio.run(service.run_once(db.inspect()))
        assert run.status == "ok"
        assert run.summary["stage_errors"] == {}
        assert run.summary["db_growth_error"].startswith("RuntimeError:")
        assert "db_growth" not in run.summary

    def test_alert_write_failure_cannot_poison_the_cycle(
        self, db, monkeypatch, tmp_path
    ):
        service = self._service(db, monkeypatch, tmp_path, 4000.0, fail_fast=True)
        monkeypatch.setattr(
            mo.MarketOpsAlertService, "upsert_open",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("alert write failed")),
        )
        run = asyncio.run(service.run_once(db.inspect()))
        assert run.status == "ok"
        assert run.summary["db_growth_error"].startswith("RuntimeError:")
        assert run.id is not None and run.finished_at is not None

    def test_cycle_makes_no_provider_call_and_no_second_scan(
        self, db, monkeypatch, tmp_path
    ):
        import httpx

        for client in (httpx.AsyncClient, httpx.Client):
            for attr in ("get", "post", "request"):
                monkeypatch.setattr(
                    client, attr,
                    lambda *a, **k: pytest.fail("provider call from the db-growth hook"),
                )
        from app.services import crypto_scout

        monkeypatch.setattr(
            crypto_scout.CryptoDiscoveryService, "scan_once",
            lambda *a, **k: pytest.fail("hook started a scan"), raising=False,
        )
        service = self._service(db, monkeypatch, tmp_path, 4000.0)
        run = asyncio.run(service.run_once(db.inspect()))
        assert run.status == "ok"
        assert run.summary["db_growth"]["external_calls"] == 0

    def test_cycle_runs_no_backup_retention_or_cohort_action(
        self, db, monkeypatch, tmp_path
    ):
        from app.services import backup as bk
        from app.services import retention as rt

        for name in ("backup_database", "apply_retention", "prune_old_backups"):
            monkeypatch.setattr(
                bk, name, lambda *a, **k: pytest.fail(f"cycle called backup.{name}")
            )
        for name in dir(rt):
            if name.startswith("prune"):
                monkeypatch.setattr(
                    rt, name,
                    lambda *a, **k: pytest.fail(f"cycle called retention.{name}"),
                    raising=False,
                )
        service = self._service(db, monkeypatch, tmp_path, 4000.0)
        assert asyncio.run(service.run_once(db.inspect())).status == "ok"


# --------------------------------------------------------------------------
# 20-25: CLI, scope and safety
# --------------------------------------------------------------------------

class TestCliAndSafety:
    def test_cli_registered_with_expected_flags(self):
        from app.cli import build_parser

        args = build_parser().parse_args(
            ["marketops-reconcile-db-growth-alerts", "--dry-run", "--format", "json"]
        )
        assert args.command == "marketops-reconcile-db-growth-alerts"
        assert args.dry_run is True and args.confirm is False and args.fmt == "json"
        confirmed = build_parser().parse_args(
            ["marketops-reconcile-db-growth-alerts", "--confirm"]
        )
        assert confirmed.confirm is True and confirmed.dry_run is False

    def test_cli_defaults_to_not_confirming(self):
        from app.cli import build_parser

        args = build_parser().parse_args(["marketops-reconcile-db-growth-alerts"])
        assert args.confirm is False

    def test_dry_run_flag_overrides_confirm(self, db, tmp_path, monkeypatch, capsys):
        """--dry-run --confirm together must be safe, not destructive."""
        from app import cli

        session = db.inspect()
        seed_legacy(session, 5)
        monkeypatch.setattr(
            mo, "get_settings", lambda *a, **k: settings_for(tmp_path)
        )
        monkeypatch.setattr(mo, "database_size_mb", lambda *a, **k: 4000.0)
        args = cli.build_parser().parse_args(
            ["marketops-reconcile-db-growth-alerts", "--dry-run", "--confirm"]
        )
        report = cli.marketops_reconcile_db_growth_alerts(
            confirm=args.confirm and not args.dry_run, fmt="json", session=session
        )
        capsys.readouterr()
        assert report["persisted"] is False
        assert len(growth_alerts(db.inspect(), "open")) == 5

    def test_text_and_json_parity(self, db, tmp_path, monkeypatch, capsys):
        from app import cli

        session = db.inspect()
        seed_legacy(session, 7)
        monkeypatch.setattr(
            mo, "get_settings", lambda *a, **k: settings_for(tmp_path)
        )
        monkeypatch.setattr(mo, "database_size_mb", lambda *a, **k: 4000.0)
        text = cli.marketops_reconcile_db_growth_alerts(
            confirm=False, fmt="text", session=session
        )
        capsys.readouterr()
        data = cli.marketops_reconcile_db_growth_alerts(
            confirm=False, fmt="json", session=session
        )
        out = capsys.readouterr().out
        assert text == data
        assert json.loads(out) == data

    def test_cli_output_is_secret_free(self, db, tmp_path, monkeypatch, capsys):
        from app import cli

        session = db.inspect()
        seed_legacy(session, 3)
        monkeypatch.setattr(
            mo, "get_settings", lambda *a, **k: settings_for(tmp_path)
        )
        monkeypatch.setattr(mo, "database_size_mb", lambda *a, **k: 4000.0)
        cli.marketops_reconcile_db_growth_alerts(
            confirm=False, fmt="json", session=session
        )
        out = capsys.readouterr().out.lower()
        for banned in ("password", "secret", "api_key", "apikey", "authorization",
                       "bearer", "postgresql://", "sk-", "token="):
            assert banned not in out

    def test_no_migration_added(self):
        for path in (REPO / "alembic/versions").glob("*.py"):
            text = path.read_text().lower()
            assert "db_growth" not in text
            assert "alert_identity" not in text

    def test_no_schema_change(self):
        """The repair reuses the deployed marketops_alerts table as-is."""
        columns = {c.name for c in MarketOpsAlert.__table__.columns}
        assert columns == {
            "id", "alert_type", "severity", "status", "title", "message",
            "evidence", "created_at", "resolved_at",
        }

    def test_no_thresholds_changed(self):
        defaults = Settings(database_url="sqlite:///x.db")
        assert defaults.db_growth_warning_mb == 1536.0
        assert defaults.db_growth_critical_mb == 3072.0

    def test_reconciliation_never_deletes(self):
        """AST audit: the reconciliation path contains no delete/DELETE call."""
        source = (REPO / "app/services/marketops.py").read_text()
        tree = ast.parse(source)
        target = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "reconcile_db_growth_alerts"
        )
        for node in ast.walk(target):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else None
                )
                assert name not in {"delete", "remove", "drop", "truncate", "execute_many"}
        body = ast.get_source_segment(source, target)
        for banned in ("session.delete", "DELETE FROM", "drop(", ".delete()"):
            assert banned not in body, banned

    def test_reconciliation_touches_no_application_data(self):
        """It may only ever write MarketOpsAlert rows."""
        source = (REPO / "app/services/marketops.py").read_text()
        tree = ast.parse(source)
        target = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "reconcile_db_growth_alerts"
        )
        body = ast.get_source_segment(source, target)
        identifiers = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body))
        allowed_models = {"MarketOpsAlert"}
        model_like = {
            i for i in identifiers
            if i[:1].isupper() and i.endswith(
                ("Tick", "Signal", "Forecast", "Token", "Market", "Run", "Snapshot",
                 "Packet", "Outcome", "Cohort", "Observation")
            )
        }
        assert model_like <= allowed_models, model_like - allowed_models
        assert "MarketOpsAlert" in body

    def test_no_provider_or_capital_surface(self):
        source = (REPO / "app/services/marketops.py").read_text()
        tree = ast.parse(source)
        for name in ("reconcile_db_growth_alerts", "_db_growth_evidence",
                     "is_legacy_db_growth_title"):
            target = next(
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name
            )
            body = ast.get_source_segment(source, target).lower()
            # code only — the docstring necessarily says what it does NOT do
            code = body.split('"""')[-1]
            for banned in ("httpx", "requests.", "aiohttp", "urllib", "socket",
                           "expected_value", "kelly", "position_siz", "paper_trad",
                           "place_order", "wallet", "trade_recommend", "execute_trade"):
                assert banned not in code, f"{name}: {banned}"

    def test_marketops_report_surfaces_the_growth_status(
        self, db, monkeypatch, tmp_path
    ):
        service = mo.MarketOpsAutopilotService(
            config=mo.MarketOpsConfig(
                include_probability_markets=False, include_crypto=False,
                include_backup_freshness_alert=False,
            ),
            alert_session_factory=db.session,
        )
        monkeypatch.setattr(mo, "database_size_mb", lambda *a, **k: 4000.0)
        monkeypatch.setattr(
            mo, "get_settings", lambda *a, **k: settings_for(tmp_path)
        )
        session = db.inspect()
        asyncio.run(service.run_once(session))
        latest = session.execute(
            select(mo.MarketOpsRun).order_by(mo.MarketOpsRun.id.desc())
        ).scalars().first()
        assert latest.summary["db_growth"]["status"] == "critical"
        assert latest.summary["db_growth"]["size_mb"] == 4000.0


# --------------------------------------------------------------------------
# Review findings: discoverability, evidence durability, honest post-state,
# reversibility, and scoped resolution.
# --------------------------------------------------------------------------

class TestReviewFindings:
    @pytest.fixture(autouse=True)
    def _measurable_size(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mo, "database_size_mb", lambda *a, **k: 4000.0)
        monkeypatch.setattr(
            mo, "get_settings", lambda *a, **k: settings_for(tmp_path)
        )

    def _bury(self, session, n=40):
        """Reproduce the production swamp: thousands of newer info alerts that
        push everything else past `ORDER BY id DESC LIMIT 10`."""
        for i in range(n):
            session.add(MarketOpsAlert(
                alert_type=mo.ALERT_SOURCE_BACKED_FORECAST, severity="info",
                status=mo.ALERT_STATUS_OPEN, title=f"Source-backed refresh: T-{i}",
                message="noise", evidence={}, created_at=NOW,
            ))
        session.commit()

    def test_growth_stays_visible_after_reconciliation(self, db, tmp_path):
        """The failure this repair could otherwise CAUSE: the canonical row is
        the OLDEST id, so it can never re-enter the newest-10 open_alerts view.
        Without a summary-backed surface, reconciliation would make a 4 GB
        critical condition invisible and recommend 'No action needed'."""
        session = db.inspect()
        seed_legacy(session, 60)
        mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        self._bury(session)

        service = mo.MarketOpsAutopilotService(
            config=mo.MarketOpsConfig(
                include_probability_markets=False, include_crypto=False,
                include_backup_freshness_alert=False,
            ),
            alert_session_factory=db.session,
        )
        asyncio.run(service.run_once(session))

        report = mo.MarketOpsReportService().build(session)
        assert all(
            a.alert_type != mo.ALERT_DB_GROWTH for a in report.open_alerts
        ), "precondition: the canonical row really is buried in open_alerts"
        assert report.db_growth is not None
        assert report.db_growth["status"] == "critical"
        assert report.db_growth["size_mb"] == 4000.0
        assert "above the critical gate" in report.recommended_action
        assert "4000.0" in report.recommended_action

    def test_first_observed_survives_subsequent_cycles(self, db, tmp_path):
        """upsert_open replaces evidence wholesale, so without carry-forward the
        very next cycle would erase what reconciliation preserved."""
        session = db.inspect()
        oldest = NOW - timedelta(days=29)
        seed_legacy(session, 12, at=oldest)
        mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        stamp = growth_alerts(db.inspect(), "open")[0].evidence[
            "condition_first_observed_at"
        ]
        assert stamp.startswith(oldest.strftime("%Y-%m-%d"))

        service = mo.MarketOpsAutopilotService(
            config=mo.MarketOpsConfig(
                include_probability_markets=False, include_crypto=False,
                include_backup_freshness_alert=False,
            ),
            alert_session_factory=db.session,
        )
        for _ in range(3):
            summary: dict = {}
            service._evaluate_db_growth(summary)

        row = growth_alerts(db.inspect(), "open")[0]
        assert row.evidence["condition_first_observed_at"] == stamp
        assert row.evidence["size_mb"] == 4000.0  # still refreshed

    def test_remaining_open_after_counts_unmatched_rows(self, db, tmp_path):
        """The dry run is the pre-flight check; a post-state estimate that
        under-reports is exactly the wrong thing to get wrong."""
        session = db.inspect()
        for i in range(3):
            session.add(MarketOpsAlert(
                alert_type=mo.ALERT_DB_GROWTH, severity="warning",
                status=mo.ALERT_STATUS_OPEN, title=f"Hand-written note {i}",
                message="manual", evidence={}, created_at=NOW,
            ))
        seed_legacy(session, 9)
        report = mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        assert report["remaining_open_matched_after"] == 1
        assert report["remaining_open_unmatched_after"] == 3
        assert report["remaining_open_after"] == 4
        assert len(growth_alerts(db.inspect(), "open")) == 4

    def test_report_carries_a_precise_inverse(self, db, tmp_path):
        """The realistic rollback is a targeted UPDATE, not restoring a 4.4 GB
        backup — so the tool must emit the exact stamp and id set."""
        session = db.inspect()
        seed_legacy(session, 20)
        report = mo.reconcile_db_growth_alerts(
            session, confirm=True, settings=settings_for(tmp_path)
        )
        assert report["resolved_at"] is not None
        assert len(report["resolve_ids"]) == report["resolved"] == 19
        assert len(growth_alerts(db.inspect(), "open")) == 1

        # apply the documented inverse
        undo = db.inspect()
        stamp = datetime.fromisoformat(report["resolved_at"])
        for alert in growth_alerts(undo, "resolved"):
            if alert.resolved_at and alert.resolved_at.replace(
                tzinfo=timezone.utc
            ) == stamp:
                alert.status = mo.ALERT_STATUS_OPEN
                alert.resolved_at = None
        undo.commit()
        assert len(growth_alerts(db.inspect(), "open")) == 20

    def test_dry_run_report_is_a_faithful_prediction(self, db, tmp_path):
        session = db.inspect()
        seed_legacy(session, 25)
        dry = mo.reconcile_db_growth_alerts(
            session, confirm=False, settings=settings_for(tmp_path)
        )
        wet = mo.reconcile_db_growth_alerts(
            db.inspect(), confirm=True, settings=settings_for(tmp_path)
        )
        assert wet["resolved"] == dry["would_resolve"]
        assert wet["canonical_id"] == dry["canonical_id"]
        assert sorted(wet["resolve_ids"]) == sorted(dry["resolve_ids"])
        assert len(growth_alerts(db.inspect(), "open")) == dry["remaining_open_after"]

    def test_healthy_cycle_never_closes_an_operator_pinned_row(self, db, tmp_path):
        """An unattended 5-minute timer must not be able to close a row a human
        wrote and pinned, merely because it shares the alert type."""
        session = db.inspect()
        session.add(MarketOpsAlert(
            alert_type=mo.ALERT_DB_GROWTH, severity="critical",
            status=mo.ALERT_STATUS_OPEN,
            title="MANUAL: pinned by operator, do not auto-close",
            message="investigation in progress", evidence={}, created_at=NOW,
        ))
        seed_legacy(session, 6)
        session.commit()

        service = mo.MarketOpsAutopilotService(
            config=mo.MarketOpsConfig(
                include_probability_markets=False, include_crypto=False,
                include_backup_freshness_alert=False,
            ),
            alert_session_factory=db.session,
        )
        # database drops below the warning gate -> healthy path resolves
        service_settings = settings_for(tmp_path, warn=99000.0, crit=99999.0)
        import app.services.marketops as _mo

        original = _mo.get_settings
        _mo.get_settings = lambda *a, **k: service_settings
        try:
            summary: dict = {}
            service._evaluate_db_growth(summary)
        finally:
            _mo.get_settings = original

        assert summary["db_growth"]["alert_action"] == "resolved"
        open_rows = growth_alerts(db.inspect(), "open")
        assert len(open_rows) == 1
        assert open_rows[0].title.startswith("MANUAL:")

    def test_legacy_matcher_rejects_non_ascii_digits(self):
        assert not mo.is_legacy_db_growth_title("Database at ٤٢٦٢ MiB")
        assert not mo.is_legacy_db_growth_title("Database at ４２６２ MiB")
        assert mo.is_legacy_db_growth_title("Database at 4262 MiB")

    def test_legacy_matcher_tolerates_a_non_string_title(self):
        assert mo.is_legacy_db_growth_title(None) is False
        assert mo.is_legacy_db_growth_title(4262) is False

    def test_message_has_a_single_source(self):
        """The cycle and the reconciliation must not drift apart."""
        source = (REPO / "app/services/marketops.py").read_text()
        assert source.count("review retention windows (db-growth-report)") == 1
        assert "_db_growth_message(" in source

    def test_dry_run_does_not_autoflush_a_dirty_caller_session(self, db, tmp_path):
        """A 'read-only' dry run must not write, even indirectly."""
        session = db.inspect()
        seed_legacy(session, 5)
        session.add(MarketOpsAlert(
            alert_type=mo.ALERT_DB_GROWTH, severity="critical",
            status=mo.ALERT_STATUS_OPEN, title="Database at 9999 MiB (critical)",
            message="pending", evidence={}, created_at=NOW,
        ))  # deliberately NOT committed
        mo.reconcile_db_growth_alerts(
            session, confirm=False, settings=settings_for(tmp_path)
        )
        assert len(growth_alerts(db.inspect(), "open")) == 5
