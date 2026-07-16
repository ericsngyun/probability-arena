"""CRYPTO-HORIZON-ORCHESTRATOR-001 bounded one-shot scheduler tests."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from app import cli
from app.models import (
    CryptoHorizonCohortMember,
    CryptoHorizonObservation,
    MarketOpsAlert,
    MarketOpsRun,
)
from app.services.crypto_horizon import OBS_OBSERVED, OBS_REQUEST_FAILED, CryptoHorizonService
from app.services.crypto_horizon_orchestrator import (
    BOUNDARY_DISCLAIMER,
    CryptoHorizonOrchestrator,
    OrchestratorStore,
    SystemdUserManager,
    build_arm_plan,
    render_post_observation_reports,
    unit_base,
)
from tests.test_crypto_horizon_obs_001 import session  # fixture reuse
from tests.test_crypto_horizon_schedule_001 import add_cohort

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[1]


class RecordingRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, argv, **kwargs):
        self.commands.append(argv)
        # DUE-NOW-001: `systemctl show` on a healthy just-installed timer reports
        # a future NextElapseUSecRealtime; return one so post-install
        # verification passes for the normal armed path.
        if "show" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout="NextElapseUSecRealtime=Sat 2099-01-01 00:00:00 UTC\nLastTriggerUSec=\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class FailingEnableRunner(RecordingRunner):
    def __call__(self, argv, **kwargs):
        if argv[2:4] == ["enable", "--now"]:
            self.commands.append(argv)
            return SimpleNamespace(returncode=1, stdout="", stderr="enable failed")
        return super().__call__(argv, **kwargs)


class ScriptedObserver:
    def __init__(self, session, results):
        self.planner = CryptoHorizonService()
        self.session = session
        self.results = list(results)
        self.calls = []

    def build_plan(self, session, cohort_id, now=None):
        return self.planner.build_plan(session, cohort_id, now=now)

    async def observe_once(self, session, cohort_id, limit, dry_run=False):
        self.calls.append({
            "cohort_id": cohort_id, "limit": limit, "dry_run": dry_run,
        })
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class NoopReports:
    def __init__(self):
        self.calls = []

    async def __call__(self, session, cohort_id, output_dir):
        self.calls.append((cohort_id, output_dir))
        return [str(output_dir / "observation-report.txt")]


def stack(tmp_path, runner=None):
    runner = runner or RecordingRunner()
    project = tmp_path / "project"
    python = project / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    manager = SystemdUserManager(
        unit_dir=tmp_path / "units",
        project_root=project,
        python_path=python,
        runner=runner,
        enforce_host=False,
    )
    store = OrchestratorStore(tmp_path / "observations")
    return CryptoHorizonOrchestrator(store=store, systemd=manager), runner


def seed_marketops(session, status="ok", now=NOW):
    row = MarketOpsRun(
        status=status,
        started_at=now - timedelta(minutes=2),
        finished_at=now - timedelta(minutes=1),
        created_at=now - timedelta(minutes=2),
    )
    session.add(row)
    session.flush()
    return row


def armed_manifest(orchestrator, session, cohort_id, now):
    plan = build_arm_plan(
        session, cohort_id, now=now, store=orchestrator.store
    )
    assert plan["jobs"]
    orchestrator.store.write_manifest(
        cohort_id, {**plan, "status": "armed", "persisted": True}
    )
    return plan


def success_summary(**overrides):
    result = {
        "status": "ok",
        "external_calls": 1,
        "due_observations": 1,
        "observations_recorded": 1,
        "ticks_written": 1,
        "outcome_counts": {OBS_OBSERVED: 1},
    }
    result.update(overrides)
    return result


class TestPlanningAndUnits:
    def test_dry_run_exact_timestamps_deduplicates_and_installs_nothing(
        self, session, tmp_path,
    ):
        cid = add_cohort(session, [
            ("a" * 25, NOW),
            ("b" * 25, NOW + timedelta(minutes=2)),
        ])
        orchestrator, runner = stack(tmp_path)
        before = session.query(CryptoHorizonObservation).count()

        result = orchestrator.arm(session, cid, dry_run=True, now=NOW)

        assert result["status"] == "ok"
        assert result["expected_jobs"] == 4
        assert result["jobs"][0]["execute_at_utc"] == (
            NOW + timedelta(minutes=9, seconds=30)
        ).isoformat()
        assert result["jobs"][0]["affected_tokens"] == 2
        assert result["jobs"][0]["affected_observations"] == 2
        assert all(job["limit"] == 2 for job in result["jobs"])
        assert result["external_calls"] == 0
        assert result["persisted"] is result["installed"] is False
        assert runner.commands == []
        assert not orchestrator.store.root.exists()
        assert session.query(CryptoHorizonObservation).count() == before

    def test_real_arm_requires_confirmation(self, session, tmp_path):
        cid = add_cohort(session, [("a" * 25, NOW)])
        orchestrator, runner = stack(tmp_path)
        result = orchestrator.arm(session, cid, now=NOW)
        assert result["status"] == "confirmation_required"
        assert runner.commands == []
        assert not orchestrator.store.root.exists()

    def test_confirmed_arm_writes_only_nonrecurring_persistent_one_shots(
        self, session, tmp_path,
    ):
        cid = add_cohort(session, [("a" * 25, NOW)])
        orchestrator, runner = stack(tmp_path)
        result = orchestrator.arm(session, cid, confirm=True, now=NOW)

        assert result["status"] == "armed"
        assert result["expected_jobs"] == 4
        timers = sorted((tmp_path / "units").glob("*.timer"))
        assert len(timers) == 4
        for path, job in zip(timers, result["jobs"]):
            text = path.read_text()
            assert "Persistent=true" in text
            assert "AccuracySec=1us" in text
            expected = datetime.fromisoformat(job["execute_at_utc"]).strftime(
                "%Y-%m-%d %H:%M:%S.%f UTC"
            )
            assert f"OnCalendar={expected}" in text
            assert "OnUnitActiveSec" not in text
            assert "OnBootSec" not in text
            assert "Restart=" not in text
        service = next((tmp_path / "units").glob("*.service")).read_text()
        assert "crypto-horizon-run-job" in service
        assert " /bin/" not in service
        assert "while" not in service.lower()
        assert "--limit" not in service  # worker reads the frozen cohort size
        assert any(cmd[2:4] == ["enable", "--now"] for cmd in runner.commands)

    def test_rejects_cohort_without_future_windows(self, session, tmp_path):
        cid = add_cohort(session, [("a" * 25, NOW - timedelta(days=3))])
        orchestrator, runner = stack(tmp_path)
        result = orchestrator.arm(session, cid, dry_run=True, now=NOW)
        assert result["status"] == "no_future_windows"
        assert result["jobs"] == []
        assert runner.commands == []

    def test_partial_install_failure_disables_and_removes_only_created_units(
        self, session, tmp_path,
    ):
        cid = add_cohort(session, [("a" * 25, NOW)])
        runner = FailingEnableRunner()
        orchestrator, _ = stack(tmp_path, runner=runner)
        with pytest.raises(RuntimeError, match="enable failed"):
            orchestrator.arm(session, cid, confirm=True, now=NOW)
        assert list((tmp_path / "units").glob("probability-arena-horizon-*")) == []
        assert orchestrator.store.read_manifest(cid) is None
        assert any(cmd[2:4] == ["disable", "--now"] for cmd in runner.commands)

    def test_no_provider_call_during_planning(self, session, tmp_path, monkeypatch):
        cid = add_cohort(session, [("a" * 25, NOW)])

        def forbidden(*args, **kwargs):
            raise AssertionError("planning must not construct a provider")

        monkeypatch.setattr(CryptoHorizonService, "adapter", property(forbidden))
        orchestrator, _ = stack(tmp_path)
        assert orchestrator.arm(session, cid, dry_run=True, now=NOW)["expected_jobs"] == 4

    @pytest.mark.parametrize("value", [0, -1, True, "1;touch /tmp/injected"])
    def test_integer_validation_blocks_command_injection(self, value):
        with pytest.raises(ValueError):
            unit_base(value, 1)


class TestOneShotExecution:
    async def test_success_rechecks_planner_observes_once_reports_and_cleans_up(
        self, session, tmp_path,
    ):
        cid = add_cohort(session, [("a" * 25, NOW - timedelta(hours=1))])
        seed_marketops(session)
        orchestrator, _ = stack(tmp_path)
        plan = armed_manifest(orchestrator, session, cid, NOW)
        observer = ScriptedObserver(session, [success_summary()])
        reports = NoopReports()

        result = await orchestrator.run_job(
            session, cid, plan["jobs"][0]["job_id"], now=NOW,
            observer=observer, report_runner=reports,
        )

        assert result["status"] == "completed"
        assert result["last_exit_code"] == 0
        assert len(observer.calls) == 1
        assert observer.calls[0] == {
            "cohort_id": cid, "limit": 1, "dry_run": False,
        }
        assert len(reports.calls) == 1
        assert result["cleanup_error"] is None

    async def test_overdue_window_is_missed_without_provider_call(
        self, session, tmp_path,
    ):
        cid = add_cohort(session, [("a" * 25, NOW)])
        orchestrator, _ = stack(tmp_path)
        plan = armed_manifest(orchestrator, session, cid, NOW)
        observer = ScriptedObserver(session, [success_summary()])

        result = await orchestrator.run_job(
            session, cid, plan["jobs"][0]["job_id"],
            now=NOW + timedelta(hours=1), observer=observer,
        )

        assert result["status"] == "missed"
        assert result["reason"] == "window_missed"
        assert observer.calls == []

    async def test_already_observed_window_skips_provider(self, session, tmp_path):
        cid = add_cohort(session, [("a" * 25, NOW)])
        member = session.query(CryptoHorizonCohortMember).one()
        orchestrator, _ = stack(tmp_path)
        plan = armed_manifest(orchestrator, session, cid, NOW)
        first_job = plan["jobs"][0]
        for affected in first_job["affected"]:
            session.add(CryptoHorizonObservation(
                cohort_id=cid, member_id=member.id, chain="solana",
                token_address=affected["token_address"],
                horizon=affected["horizon"], status=OBS_OBSERVED,
                observed_at=NOW, created_at=NOW,
            ))
        session.flush()
        observer = ScriptedObserver(session, [success_summary()])

        result = await orchestrator.run_job(
            session, cid, first_job["job_id"],
            now=datetime.fromisoformat(first_job["execute_at_utc"]), observer=observer,
        )
        assert result["status"] == "completed"
        assert result["reason"] == "already_observed"
        assert observer.calls == []

    async def test_provider_failure_records_failure_without_retry(
        self, session, tmp_path,
    ):
        cid = add_cohort(session, [("a" * 25, NOW - timedelta(hours=1))])
        seed_marketops(session)
        orchestrator, _ = stack(tmp_path)
        plan = armed_manifest(orchestrator, session, cid, NOW)
        observer = ScriptedObserver(session, [success_summary(
            ticks_written=0,
            outcome_counts={OBS_REQUEST_FAILED: 1},
        )])

        result = await orchestrator.run_job(
            session, cid, plan["jobs"][0]["job_id"], now=NOW, observer=observer,
        )
        assert result["status"] == "failed"
        assert result["reason"] == "provider_failure"
        assert len(observer.calls) == 1

    async def test_planner_change_between_check_and_observe_is_honest_miss(
        self, session, tmp_path,
    ):
        cid = add_cohort(session, [("a" * 25, NOW - timedelta(hours=1))])
        seed_marketops(session)
        orchestrator, _ = stack(tmp_path)
        plan = armed_manifest(orchestrator, session, cid, NOW)
        observer = ScriptedObserver(session, [success_summary(
            due_observations=0, external_calls=0,
            observations_recorded=0, ticks_written=0, outcome_counts={},
        )])
        result = await orchestrator.run_job(
            session, cid, plan["jobs"][0]["job_id"], now=NOW, observer=observer,
        )
        assert result["status"] == "missed"
        assert result["reason"] == "planner_changed_before_observe"

    async def test_database_lock_gets_one_bounded_retry(self, session, tmp_path):
        cid = add_cohort(session, [("a" * 25, NOW - timedelta(hours=1))])
        seed_marketops(session)
        orchestrator, _ = stack(tmp_path)
        plan = armed_manifest(orchestrator, session, cid, NOW)
        locked = OperationalError("INSERT", {}, Exception("database is locked"))
        observer = ScriptedObserver(session, [locked, success_summary()])
        sleeps = []

        async def sleeper(seconds):
            sleeps.append(seconds)

        result = await orchestrator.run_job(
            session, cid, plan["jobs"][0]["job_id"], now=NOW,
            observer=observer, report_runner=NoopReports(), sleeper=sleeper,
        )
        assert result["status"] == "completed"
        assert len(observer.calls) == 2
        assert sleeps == [3.0]

    async def test_marketops_degradation_skips_and_alerts(self, session, tmp_path):
        cid = add_cohort(session, [("a" * 25, NOW - timedelta(hours=1))])
        seed_marketops(session, status="error")
        orchestrator, _ = stack(tmp_path)
        plan = armed_manifest(orchestrator, session, cid, NOW)
        observer = ScriptedObserver(session, [success_summary()])

        result = await orchestrator.run_job(
            session, cid, plan["jobs"][0]["job_id"], now=NOW, observer=observer,
        )
        assert result["reason"] == "marketops_unhealthy"
        assert observer.calls == []
        alert = session.query(MarketOpsAlert).one()
        assert alert.alert_type == "crypto_horizon_orchestrator"
        assert alert.evidence["cohort_id"] == cid


class TestMonitoringCleanupAndSafety:
    async def test_post_observation_renderer_saves_all_four_reports(
        self, session, tmp_path, monkeypatch,
    ):
        calls = []

        async def reporter(cohort_id, session, **kwargs):
            calls.append((cohort_id, kwargs))
            print(f"report for {cohort_id}")
            return 0

        monkeypatch.setattr(cli, "crypto_horizon_observation_report", reporter)
        monkeypatch.setattr(cli, "crypto_horizon_pair_selection_report", reporter)
        monkeypatch.setattr(cli, "crypto_horizon_outcome_reconciliation_report", reporter)
        monkeypatch.setattr(cli, "crypto_horizon_schedule_report", reporter)

        paths = await render_post_observation_reports(session, 7, tmp_path / "reports")
        assert len(paths) == 4
        assert len(calls) == 4
        assert all(Path(path).read_text() == "report for 7\n" for path in paths)

    def test_disarm_preview_and_confirm_touch_only_selected_cohort(
        self, session, tmp_path,
    ):
        cid = add_cohort(session, [("a" * 25, NOW)])
        orchestrator, _ = stack(tmp_path)
        orchestrator.arm(session, cid, confirm=True, now=NOW)
        unrelated = tmp_path / "units" / "probability-arena-marketops.timer"
        unrelated.write_text("unrelated")

        preview = orchestrator.disarm(cid)
        assert preview["status"] == "confirmation_required"
        assert all(f"-c{cid}-" in name for name in preview["units"])
        assert unrelated.exists()

        removed = orchestrator.disarm(cid, confirm=True)
        assert removed["status"] == "disarmed"
        assert unrelated.exists()
        assert orchestrator.store.read_manifest(cid) is None
        assert orchestrator.disarm(cid, confirm=True)["removed"] == []

    def test_monitor_reports_jobs_observations_health_and_timer_state(
        self, session, tmp_path,
    ):
        cid = add_cohort(session, [("a" * 25, NOW)])
        seed_marketops(session)
        orchestrator, _ = stack(tmp_path)
        armed = orchestrator.arm(session, cid, confirm=True, now=NOW)
        orchestrator.store.write_status(cid, 1, {
            "status": "completed", "reason": "done", "last_exit_code": 0,
        })
        member = session.query(CryptoHorizonCohortMember).one()
        session.add(CryptoHorizonObservation(
            cohort_id=cid, member_id=member.id, chain="solana",
            token_address=member.token_address, horizon="15m",
            status=OBS_OBSERVED, observed_at=NOW, created_at=NOW,
        ))
        session.flush()

        report = orchestrator.report(session, cid, now=NOW)
        assert report["planned_jobs"] == armed["expected_jobs"]
        assert report["installed_jobs"] == armed["expected_jobs"]
        assert report["status_counts"]["completed"] == 1
        assert report["observation_counts_by_horizon"]["15m"] == 1
        assert report["last_exit_code"] == 0
        assert report["marketops_health"]["healthy"] is True
        assert report["timer_remains_installed_after_completion"] is True
        assert report["disclaimer"] == BOUNDARY_DISCLAIMER

    def test_cli_wiring_and_source_boundaries(self):
        import argparse

        holder = {}
        original = argparse.ArgumentParser.parse_args

        def fake_parse(parser, *args, **kwargs):
            holder["parser"] = parser
            raise SystemExit(0)

        argparse.ArgumentParser.parse_args = fake_parse
        try:
            with pytest.raises(SystemExit):
                cli.main([])
        finally:
            argparse.ArgumentParser.parse_args = original
        actions = holder["parser"]._subparsers._group_actions[0].choices
        assert {
            "crypto-horizon-arm-cohort",
            "crypto-horizon-run-job",
            "crypto-horizon-orchestrator-report",
            "crypto-horizon-disarm-cohort",
        } <= set(actions)

        source = (REPO / "app/services/crypto_horizon_orchestrator.py").read_text()
        for forbidden in (
            "while True", "while 1", "OnUnitActiveSec=", "OnBootSec=",
            "Restart=", "shell=True", "create_cohort(", "SolanaTrackerAdapter",
        ):
            assert forbidden not in source
        assert "Persistent=true" in source
        assert "subprocess.run" in source
