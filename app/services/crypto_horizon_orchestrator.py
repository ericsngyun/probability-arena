"""CRYPTO-HORIZON-ORCHESTRATOR-001 host-native one-shot observations.

This module schedules bounded research-data collection for an already-frozen
crypto horizon cohort. The existing horizon planner remains the sole source of
window eligibility. User-level systemd provides durable timing; every worker
rechecks the planner, performs at most one observation attempt, and exits.

Observation only: no recurring timer, daemon, cohort admission, SolanaTracker,
trade, recommendation, EV, sizing, order, wallet, key, swap, signing, capital
allocation, or execution capability exists here.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CryptoHorizonObservation, MarketOpsRun
from app.services.crypto_horizon import (
    OBS_REQUEST_FAILED,
    STATUS_ALREADY_OBSERVED,
    STATUS_DUE_NOW,
    STATUS_OVERDUE_UNOBSERVED,
    CryptoHorizonService,
)
from app.services.crypto_horizon_schedule import (
    build_schedule_report,
    format_los_angeles,
    format_utc,
)
from app.services.crypto_tape import _aware, _is_db_locked, _now

logger = logging.getLogger(__name__)

HOST_HOME = Path("/home/miko_node_001")
PROJECT_ROOT = HOST_HOME / "projects" / "probability-arena"
PYTHON_PATH = PROJECT_ROOT / ".venv" / "bin" / "python"
UNIT_DIR = HOST_HOME / ".config" / "systemd" / "user"
OBSERVATION_ROOT = HOST_HOME / "crypto-horizon-observation"
UNIT_PREFIX = "probability-arena-horizon"
MARKETOPS_HEALTH_MAX_AGE = timedelta(minutes=30)
DB_LOCK_MAX_ATTEMPTS = 2
DB_LOCK_RETRY_SECONDS = 3.0

BOUNDARY_DISCLAIMER = (
    "Host-native one-shot research-data collection only. No recurring timer, "
    "daemon, automatic cohort creation, SolanaTracker use, EV, recommendation, "
    "sizing, order, wallet, key, signing, swap, capital allocation, or execution."
)


def _validate_positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def unit_base(cohort_id: int, job_id: int) -> str:
    cohort_id = _validate_positive_int(cohort_id, "cohort_id")
    job_id = _validate_positive_int(job_id, "job_id")
    return f"{UNIT_PREFIX}-c{cohort_id}-j{job_id}"


def _parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    aware = _aware(parsed)
    if aware is None:
        raise ValueError("timestamp is required")
    return aware.astimezone(timezone.utc)


def _systemd_calendar(value: str | datetime) -> str:
    return _parse_time(value).strftime("%Y-%m-%d %H:%M:%S.%f UTC")


def _json_default(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    raise TypeError(f"cannot JSON encode {type(value).__name__}")


@dataclass
class OrchestratorStore:
    root: Path = OBSERVATION_ROOT

    def cohort_dir(self, cohort_id: int) -> Path:
        return self.root / f"cohort-{_validate_positive_int(cohort_id, 'cohort_id')}"

    def manifest_path(self, cohort_id: int) -> Path:
        return self.cohort_dir(cohort_id) / "manifest.json"

    def status_path(self, cohort_id: int, job_id: int) -> Path:
        return self.cohort_dir(cohort_id) / f"job-{_validate_positive_int(job_id, 'job_id')}.json"

    def log_path(self, cohort_id: int, job_id: int) -> Path:
        return self.cohort_dir(cohort_id) / f"job-{_validate_positive_int(job_id, 'job_id')}.log"

    def report_dir(self, cohort_id: int, job_id: int) -> Path:
        return self.cohort_dir(cohort_id) / f"job-{_validate_positive_int(job_id, 'job_id')}-reports"

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return None

    @staticmethod
    def _write(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n"
        )
        temporary.replace(path)

    def read_manifest(self, cohort_id: int) -> dict | None:
        return self._read(self.manifest_path(cohort_id))

    def write_manifest(self, cohort_id: int, payload: dict) -> None:
        self._write(self.manifest_path(cohort_id), payload)

    def remove_manifest(self, cohort_id: int) -> None:
        self.manifest_path(cohort_id).unlink(missing_ok=True)

    def read_status(self, cohort_id: int, job_id: int) -> dict | None:
        return self._read(self.status_path(cohort_id, job_id))

    def write_status(self, cohort_id: int, job_id: int, payload: dict) -> None:
        self._write(self.status_path(cohort_id, job_id), payload)


class SystemdUserManager:
    """Narrow systemd boundary. Commands are argv arrays; no shell is used."""

    def __init__(
        self,
        unit_dir: Path = UNIT_DIR,
        project_root: Path = PROJECT_ROOT,
        python_path: Path = PYTHON_PATH,
        runner: Callable | None = None,
        enforce_host: bool = True,
    ):
        self.unit_dir = unit_dir
        self.project_root = project_root
        self.python_path = python_path
        self.runner = runner or subprocess.run
        self.enforce_host = enforce_host

    def _check_host(self) -> None:
        if self.enforce_host and (
            Path.home() != HOST_HOME
            or self.project_root != PROJECT_ROOT
            or not self.project_root.is_dir()
            or not self.python_path.is_file()
        ):
            raise RuntimeError("real arming is restricted to the validated EVO-X2 paths")

    def validate_host(self) -> None:
        self._check_host()

    def _run(self, argv: list[str], *, check: bool = True):
        result = self.runner(argv, capture_output=True, text=True, timeout=30)
        if check and result.returncode != 0:
            message = (result.stderr or result.stdout or "systemctl failed").strip()
            raise RuntimeError(message)
        return result

    def _paths(self, cohort_id: int, job_id: int) -> tuple[Path, Path]:
        base = unit_base(cohort_id, job_id)
        return self.unit_dir / f"{base}.service", self.unit_dir / f"{base}.timer"

    def render_service(self, job: dict, store: OrchestratorStore) -> str:
        cohort_id, job_id = job["cohort_id"], job["job_id"]
        log_path = store.log_path(cohort_id, job_id)
        return (
            "[Unit]\n"
            f"Description=Probability Arena horizon cohort {cohort_id} job {job_id} (one-shot observation)\n"
            "Wants=network-online.target\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"WorkingDirectory={self.project_root}\n"
            f"EnvironmentFile={self.project_root / '.env'}\n"
            f"ExecStart={self.python_path} -m app.cli crypto-horizon-run-job "
            f"--cohort-id {cohort_id} --job-id {job_id}\n"
            f"StandardOutput=append:{log_path}\n"
            f"StandardError=append:{log_path}\n"
            "NoNewPrivileges=true\n"
            "TimeoutStartSec=10min\n"
        )

    @staticmethod
    def render_timer(job: dict) -> str:
        cohort_id, job_id = job["cohort_id"], job["job_id"]
        base = unit_base(cohort_id, job_id)
        return (
            "[Unit]\n"
            f"Description=Run horizon cohort {cohort_id} job {job_id} once\n\n"
            "[Timer]\n"
            f"OnCalendar={_systemd_calendar(job['execute_at_utc'])}\n"
            "AccuracySec=1us\n"
            "RandomizedDelaySec=0\n"
            "Persistent=true\n"
            f"Unit={base}.service\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )

    def install_jobs(self, jobs: list[dict], store: OrchestratorStore) -> list[str]:
        self._check_host()
        if not jobs:
            return []
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        timers = [f"{unit_base(j['cohort_id'], j['job_id'])}.timer" for j in jobs]
        try:
            for job in jobs:
                store.cohort_dir(job["cohort_id"]).mkdir(parents=True, exist_ok=True)
                service_path, timer_path = self._paths(job["cohort_id"], job["job_id"])
                if service_path.exists() or timer_path.exists():
                    raise RuntimeError(f"unit already exists: {unit_base(job['cohort_id'], job['job_id'])}")
                service_path.write_text(self.render_service(job, store))
                timer_path.write_text(self.render_timer(job))
                created.extend((service_path, timer_path))
            self._run(["systemctl", "--user", "daemon-reload"])
            self._run(["systemctl", "--user", "enable", "--now", *timers])
            return timers
        except Exception:
            self._run(
                ["systemctl", "--user", "disable", "--now", *timers], check=False
            )
            for path in created:
                path.unlink(missing_ok=True)
            with contextlib.suppress(Exception):
                self._run(["systemctl", "--user", "daemon-reload"], check=False)
            raise

    def list_units(self, cohort_id: int) -> list[str]:
        cohort_id = _validate_positive_int(cohort_id, "cohort_id")
        pattern = re.compile(
            rf"^{re.escape(UNIT_PREFIX)}-c{cohort_id}-j[1-9][0-9]*\.(service|timer)$"
        )
        if not self.unit_dir.exists():
            return []
        return sorted(
            path.name for path in self.unit_dir.iterdir()
            if path.is_file() and pattern.fullmatch(path.name)
        )

    def remove_job(self, cohort_id: int, job_id: int) -> list[str]:
        self._check_host()
        service_path, timer_path = self._paths(cohort_id, job_id)
        timer_name = timer_path.name
        self._run(
            ["systemctl", "--user", "disable", "--now", timer_name], check=False
        )
        removed = []
        for path in (timer_path, service_path):
            if path.exists():
                path.unlink()
                removed.append(path.name)
        self._run(["systemctl", "--user", "daemon-reload"], check=False)
        return removed

    def remove_cohort(self, cohort_id: int) -> list[str]:
        self._check_host()
        units = self.list_units(cohort_id)
        job_ids = sorted({
            int(re.search(r"-j([1-9][0-9]*)\.", name).group(1))
            for name in units
        })
        removed: list[str] = []
        for job_id in job_ids:
            removed.extend(self.remove_job(cohort_id, job_id))
        return sorted(removed)


def build_arm_plan(
    session: Session,
    cohort_id: int,
    now: datetime | None = None,
    store: OrchestratorStore | None = None,
) -> dict:
    """Pure scheduling plan. No provider, filesystem, systemd, or DB writes."""
    cohort_id = _validate_positive_int(cohort_id, "cohort_id")
    now = _parse_time(now or _now())
    store = store or OrchestratorStore()
    schedule = build_schedule_report(session, cohort_id, now=now)
    jobs = []
    if schedule["status"] == "ok":
        for job_id, group in enumerate(schedule["pass_groups"], start=1):
            execute_at = _parse_time(group["suggested_action_at"])
            jobs.append({
                "job_id": job_id,
                "cohort_id": cohort_id,
                "execute_at_utc": execute_at.isoformat(),
                "execute_at_los_angeles": format_los_angeles(execute_at),
                "unit_base": unit_base(cohort_id, job_id),
                "limit": schedule["cohort_size"],
                "affected": group["affected"],
                "affected_tokens": group["affected_tokens"],
                "affected_observations": group["affected_observations"],
                "window_intersection_start": format_utc(
                    group["window_intersection_start"]
                ),
                "window_intersection_end": format_utc(group["window_intersection_end"]),
                "command": (
                    f"{PYTHON_PATH} -m app.cli crypto-horizon-run-job "
                    f"--cohort-id {cohort_id} --job-id {job_id}"
                ),
                "log_path": str(store.log_path(cohort_id, job_id)),
            })
    status = schedule["status"]
    if status == "ok" and not jobs:
        status = "no_future_windows"
    return {
        "status": status,
        "cohort_id": cohort_id,
        "cohort_size": schedule.get("cohort_size", 0),
        "generated_at": now.isoformat(),
        "jobs": jobs,
        "expected_jobs": len(jobs),
        "warnings": schedule.get("warnings", []),
        "external_calls": 0,
        "persisted": False,
        "installed": False,
        "disclaimer": BOUNDARY_DISCLAIMER,
    }


def marketops_health(
    session: Session,
    now: datetime | None = None,
    max_age: timedelta = MARKETOPS_HEALTH_MAX_AGE,
) -> dict:
    now = _parse_time(now or _now())
    latest = session.execute(
        select(MarketOpsRun)
        .where(MarketOpsRun.status.notin_(("running", "skipped")))
        .order_by(MarketOpsRun.id.desc())
        .limit(1)
    ).scalars().first()
    if latest is None:
        return {"healthy": False, "reason": "no_completed_runs", "run_id": None}
    finished = _aware(latest.finished_at) or _aware(latest.started_at)
    age_seconds = (now - finished).total_seconds() if finished else None
    healthy = (
        latest.status == "ok"
        and age_seconds is not None
        and 0 <= age_seconds <= max_age.total_seconds()
    )
    reason = "healthy" if healthy else (
        f"latest_status_{latest.status}" if latest.status != "ok" else "latest_run_stale"
    )
    return {
        "healthy": healthy,
        "reason": reason,
        "run_id": latest.id,
        "status": latest.status,
        "finished_at": finished.isoformat() if finished else None,
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
    }


def _record_health_alert(session: Session, cohort_id: int, health: dict) -> None:
    try:
        from app.services.marketops import MarketOpsAlertService

        MarketOpsAlertService().create(
            session,
            alert_type="crypto_horizon_orchestrator",
            severity="warning",
            title=f"Horizon cohort {cohort_id} observation skipped",
            message=f"MarketOps health check failed: {health['reason']}",
            evidence={
                "cohort_id": cohort_id,
                "marketops_run_id": health.get("run_id"),
                "reason": health["reason"],
            },
        )
        session.commit()
    except Exception as exc:  # alert failure must not trigger provider work
        session.rollback()
        logger.warning("could not persist horizon health alert: %s", type(exc).__name__)


async def render_post_observation_reports(
    session: Session, cohort_id: int, output_dir: Path,
) -> list[str]:
    """Render the four existing read-only reports to separate text files."""
    from app import cli

    output_dir.mkdir(parents=True, exist_ok=True)
    reports = (
        ("observation-report.txt", cli.crypto_horizon_observation_report, {}),
        ("pair-selection-report.txt", cli.crypto_horizon_pair_selection_report, {}),
        (
            "outcome-reconciliation-report.txt",
            cli.crypto_horizon_outcome_reconciliation_report,
            {},
        ),
        ("schedule-report.txt", cli.crypto_horizon_schedule_report, {"top": None}),
    )
    written = []
    for filename, reporter, kwargs in reports:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            await reporter(cohort_id=cohort_id, session=session, **kwargs)
        path = output_dir / filename
        path.write_text(buffer.getvalue())
        written.append(str(path))
    return written


class CryptoHorizonOrchestrator:
    def __init__(
        self,
        store: OrchestratorStore | None = None,
        systemd: SystemdUserManager | None = None,
    ):
        self.store = store or OrchestratorStore()
        self.systemd = systemd or SystemdUserManager()

    def arm(
        self,
        session: Session,
        cohort_id: int,
        *,
        dry_run: bool = False,
        confirm: bool = False,
        now: datetime | None = None,
    ) -> dict:
        plan = build_arm_plan(session, cohort_id, now=now, store=self.store)
        if plan["status"] != "ok":
            return plan
        if dry_run:
            return plan
        if not confirm:
            return {**plan, "status": "confirmation_required"}
        if self.store.read_manifest(cohort_id) is not None or self.systemd.list_units(cohort_id):
            return {**plan, "status": "already_armed"}
        manifest = {
            **plan,
            "status": "arming",
            "persisted": True,
            "installed": False,
            "installed_units": [],
        }
        self.systemd.validate_host()
        self.store.write_manifest(cohort_id, manifest)
        try:
            installed = self.systemd.install_jobs(plan["jobs"], self.store)
        except Exception:
            self.systemd.remove_cohort(cohort_id)
            self.store.remove_manifest(cohort_id)
            raise
        manifest.update({
            "status": "armed",
            "installed": True,
            "installed_units": installed,
        })
        try:
            self.store.write_manifest(cohort_id, manifest)
        except Exception:
            self.systemd.remove_cohort(cohort_id)
            self.store.remove_manifest(cohort_id)
            raise
        return manifest

    def disarm(self, cohort_id: int, *, confirm: bool = False) -> dict:
        cohort_id = _validate_positive_int(cohort_id, "cohort_id")
        units = self.systemd.list_units(cohort_id)
        if not confirm:
            return {
                "status": "confirmation_required" if units else "already_disarmed",
                "cohort_id": cohort_id,
                "units": units,
                "removed": [],
            }
        removed = self.systemd.remove_cohort(cohort_id)
        self.store.remove_manifest(cohort_id)
        return {
            "status": "disarmed",
            "cohort_id": cohort_id,
            "units": units,
            "removed": removed,
        }

    async def run_job(
        self,
        session: Session,
        cohort_id: int,
        job_id: int,
        *,
        now: datetime | None = None,
        observer: CryptoHorizonService | None = None,
        report_runner: Callable[[Session, int, Path], Awaitable[list[str]]] | None = None,
        sleeper: Callable[[float], Awaitable] | None = None,
    ) -> dict:
        cohort_id = _validate_positive_int(cohort_id, "cohort_id")
        job_id = _validate_positive_int(job_id, "job_id")
        now = _parse_time(now or _now())
        observer = observer or CryptoHorizonService()
        report_runner = report_runner or render_post_observation_reports
        sleeper = sleeper or asyncio.sleep
        manifest = self.store.read_manifest(cohort_id)
        job = next(
            (item for item in (manifest or {}).get("jobs", []) if item["job_id"] == job_id),
            None,
        )
        if job is None:
            return self._finish_job(
                cohort_id, job_id, now, "failed", "job_not_planned", 1
            )

        plan = observer.build_plan(session, cohort_id, now=now)
        by_key = {(entry.token_address, entry.horizon): entry.status for entry in plan}
        intended = [
            by_key.get((item["token_address"], item["horizon"]), "missing")
            for item in job["affected"]
        ]
        due_count = intended.count(STATUS_DUE_NOW)
        allowed = {STATUS_DUE_NOW, STATUS_ALREADY_OBSERVED}

        if due_count == 0:
            if intended and all(status == STATUS_ALREADY_OBSERVED for status in intended):
                return self._finish_job(
                    cohort_id, job_id, now, "completed", "already_observed", 0
                )
            status = "missed" if STATUS_OVERDUE_UNOBSERVED in intended else "failed"
            reason = "window_missed" if status == "missed" else "planner_disagreement"
            return self._finish_job(cohort_id, job_id, now, status, reason, 0 if status == "missed" else 1)
        if any(status not in allowed for status in intended):
            return self._finish_job(
                cohort_id, job_id, now, "failed", "planner_disagreement", 1
            )

        health = marketops_health(session, now=now)
        if not health["healthy"]:
            _record_health_alert(session, cohort_id, health)
            return self._finish_job(
                cohort_id, job_id, now, "failed", "marketops_unhealthy", 1,
                marketops_health=health,
            )

        summary = None
        last_exc: BaseException | None = None
        for attempt in range(1, DB_LOCK_MAX_ATTEMPTS + 1):
            try:
                summary = await observer.observe_once(
                    session,
                    cohort_id=cohort_id,
                    limit=_validate_positive_int(job["limit"], "limit"),
                    dry_run=False,
                )
                break
            except Exception as exc:
                last_exc = exc
                session.rollback()
                if _is_db_locked(exc) and attempt < DB_LOCK_MAX_ATTEMPTS:
                    await sleeper(DB_LOCK_RETRY_SECONDS)
                    continue
                break
        if summary is None:
            reason = "database_locked" if _is_db_locked(last_exc) else "observation_error"
            return self._finish_job(
                cohort_id, job_id, now, "failed", reason, 1,
                error_type=type(last_exc).__name__ if last_exc else None,
            )
        if (summary.get("outcome_counts") or {}).get(OBS_REQUEST_FAILED, 0):
            return self._finish_job(
                cohort_id, job_id, now, "failed", "provider_failure", 1,
                observation_summary=summary,
            )
        if not summary.get("due_observations") and not summary.get("external_calls"):
            return self._finish_job(
                cohort_id, job_id, now, "missed", "planner_changed_before_observe", 0,
                observation_summary=summary,
            )

        try:
            report_paths = await report_runner(
                session, cohort_id, self.store.report_dir(cohort_id, job_id)
            )
        except Exception as exc:
            return self._finish_job(
                cohort_id, job_id, now, "failed", "post_report_error", 1,
                observation_summary=summary,
                error_type=type(exc).__name__,
                marketops_health=health,
            )
        return self._finish_job(
            cohort_id, job_id, now, "completed", "observation_attempt_complete", 0,
            observation_summary=summary,
            report_paths=report_paths,
            marketops_health=health,
        )

    @staticmethod
    def _job_result(
        cohort_id: int,
        job_id: int,
        now: datetime,
        status: str,
        reason: str,
        exit_code: int,
        **extra,
    ) -> dict:
        return {
            "cohort_id": cohort_id,
            "job_id": job_id,
            "status": status,
            "reason": reason,
            "finished_at": now.isoformat(),
            "last_exit_code": exit_code,
            **extra,
        }

    def _finish_job(
        self,
        cohort_id: int,
        job_id: int,
        now: datetime,
        status: str,
        reason: str,
        exit_code: int,
        **extra,
    ) -> dict:
        cleanup_error = None
        try:
            removed = self.systemd.remove_job(cohort_id, job_id)
        except Exception as exc:  # status must survive cleanup failure
            removed = []
            cleanup_error = type(exc).__name__
        result = self._job_result(
            cohort_id, job_id, now, status, reason, exit_code,
            removed_units=removed,
            cleanup_error=cleanup_error,
            **extra,
        )
        self.store.write_status(cohort_id, job_id, result)
        return result

    def report(
        self, session: Session, cohort_id: int, now: datetime | None = None,
    ) -> dict:
        cohort_id = _validate_positive_int(cohort_id, "cohort_id")
        now = _parse_time(now or _now())
        manifest = self.store.read_manifest(cohort_id)
        installed = self.systemd.list_units(cohort_id)
        installed_set = set(installed)
        jobs = []
        for job in (manifest or {}).get("jobs", []):
            state = self.store.read_status(cohort_id, job["job_id"])
            timer = f"{job['unit_base']}.timer"
            if state is None:
                execute_at = _parse_time(job["execute_at_utc"])
                status = "pending" if timer in installed_set or execute_at >= now else "missed"
                state = {"status": status, "reason": "awaiting_execution", "last_exit_code": None}
            jobs.append({
                **job,
                "status": state["status"],
                "reason": state.get("reason"),
                "last_exit_code": state.get("last_exit_code"),
                "timer_installed": timer in installed_set,
                "log_path": str(self.store.log_path(cohort_id, job["job_id"])),
            })

        counts = dict(session.execute(
            select(CryptoHorizonObservation.horizon, func.count())
            .where(CryptoHorizonObservation.cohort_id == cohort_id)
            .group_by(CryptoHorizonObservation.horizon)
        ).all())
        pending_times = [
            _parse_time(job["execute_at_utc"])
            for job in jobs if job["status"] == "pending"
        ]
        terminal = [job for job in jobs if job["status"] != "pending"]
        return {
            "status": "ok" if manifest else "unarmed",
            "cohort_id": cohort_id,
            "planned_jobs": len(jobs),
            "installed_jobs": len([name for name in installed if name.endswith(".timer")]),
            "jobs": jobs,
            "status_counts": {
                label: sum(job["status"] == label for job in jobs)
                for label in ("pending", "completed", "failed", "missed")
            },
            "next_execution_time": min(pending_times).isoformat() if pending_times else None,
            "observation_counts_by_horizon": {
                label: counts.get(label, 0) for label in ("15m", "1h", "6h", "24h")
            },
            "last_exit_code": terminal[-1]["last_exit_code"] if terminal else None,
            "any_timer_installed": any(name.endswith(".timer") for name in installed),
            "timer_remains_installed_after_completion": any(
                job["status"] == "completed" and job["timer_installed"] for job in jobs
            ),
            "marketops_health": marketops_health(session, now=now),
            "disclaimer": BOUNDARY_DISCLAIMER,
        }
