"""CRYPTO-HORIZON-ORCHESTRATOR-DUE-NOW-001 regression tests.

A due-now (already-open) horizon window must never be armed with an OnCalendar
at or before unit-enable time (which systemd marks `active (elapsed)` and never
triggers). Arming applies an activation grace and verifies each installed timer
is actually scheduled. Everything is local — no provider, no live systemd.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.crypto_horizon_orchestrator import (
    ACTIVATION_GRACE,
    SystemdUserManager,
    build_arm_plan,
)
from tests.test_crypto_horizon_obs_001 import session  # fixture reuse
from tests.test_crypto_horizon_orchestrator_001 import (
    NOW,
    RecordingRunner,
    stack,
)
from tests.test_crypto_horizon_schedule_001 import add_cohort

GRACE = ACTIVATION_GRACE


def due_now_cohort(session, offset_min=8):
    """Cohort whose 15m window is OPEN at NOW (born offset_min ago)."""
    return add_cohort(session, [("a" * 25, NOW - timedelta(minutes=offset_min))])


class ShowRunner(RecordingRunner):
    """systemctl `show` returns a scripted NextElapse/LastTrigger pair."""

    def __init__(self, next_elapse="", last_trigger=""):
        super().__init__()
        self._next = next_elapse
        self._last = last_trigger

    def __call__(self, argv, **kwargs):
        if "show" in argv:
            self.commands.append(argv)
            return SimpleNamespace(
                returncode=0,
                stdout=f"NextElapseUSecRealtime={self._next}\nLastTriggerUSec={self._last}\n",
                stderr="",
            )
        return super().__call__(argv, **kwargs)


# --- scheduling: grace pushes due-now strictly into the future --------------


def test_due_now_arming_schedules_strictly_in_future(session):
    cid = due_now_cohort(session)
    plan = build_arm_plan(session, cid, now=NOW)
    assert plan["status"] == "ok"
    j15 = plan["jobs"][0]  # earliest group = the due-now 15m window
    execute_at = datetime.fromisoformat(j15["execute_at_utc"])
    assert execute_at == NOW + GRACE
    assert execute_at > NOW  # never at/before now (== enable time)


def test_activation_grace_stays_inside_window(session):
    cid = due_now_cohort(session)
    plan = build_arm_plan(session, cid, now=NOW)
    j15 = plan["jobs"][0]
    execute_at = datetime.fromisoformat(j15["execute_at_utc"])
    window_end = datetime.fromisoformat(
        j15["window_intersection_end"].replace("Z", "+00:00")
    )
    assert execute_at <= window_end


def test_insufficient_window_rejects_before_installation(session, tmp_path):
    # 15m window closes ~10s from now -> grace (45s) exceeds it -> reject.
    cid = add_cohort(session, [("b" * 25, NOW - timedelta(seconds=1340))])
    plan = build_arm_plan(session, cid, now=NOW)
    assert plan["status"] == "activation_window_too_narrow"
    assert plan["jobs"] == []
    assert plan["rejected"]["job_id"] == 1
    orchestrator, runner = stack(tmp_path)
    result = orchestrator.arm(session, cid, confirm=True, now=NOW)
    assert result["status"] == "activation_window_too_narrow"
    assert runner.commands == []  # nothing installed


def test_future_windows_timestamps_unchanged(session):
    # Fresh birth at NOW: every window opens in the future; grace is a no-op.
    cid = add_cohort(session, [("c" * 25, NOW)])
    plan = build_arm_plan(session, cid, now=NOW)
    assert plan["status"] == "ok"
    for job in plan["jobs"]:
        execute_at = datetime.fromisoformat(job["execute_at_utc"])
        window_open = datetime.fromisoformat(
            job["window_intersection_start"].replace("Z", "+00:00")
        )
        # future window_open is far beyond now+grace -> unchanged
        assert execute_at == window_open
        assert execute_at >= NOW + GRACE


# --- post-install verification ----------------------------------------------


def test_verify_installed_detects_elapsed_without_trigger():
    mgr = SystemdUserManager(runner=ShowRunner(next_elapse="", last_trigger=""),
                             enforce_host=False)
    jobs = [{"cohort_id": 1, "job_id": 1, "unit_base": "u"}]
    assert mgr.verify_installed(jobs) == [
        {"job_id": 1, "unit_base": "u", "next_elapse": "", "last_trigger": ""}
    ]


def test_verify_installed_passes_when_scheduled_or_triggered():
    scheduled = SystemdUserManager(
        runner=ShowRunner(next_elapse="Sat 2099-01-01 00:00:00 UTC"),
        enforce_host=False,
    )
    triggered = SystemdUserManager(
        runner=ShowRunner(next_elapse="", last_trigger="Wed 2026-07-15 12:00:00 UTC"),
        enforce_host=False,
    )
    jobs = [{"cohort_id": 1, "job_id": 1, "unit_base": "u"}]
    assert scheduled.verify_installed(jobs) == []
    assert triggered.verify_installed(jobs) == []


def test_elapsed_timer_causes_arming_failure_and_cohort_cleanup(session, tmp_path):
    cid = due_now_cohort(session)
    orchestrator, _ = stack(tmp_path)
    # timers install (enable ok) but show reports elapsed-without-trigger
    orchestrator.systemd.runner = ShowRunner(next_elapse="", last_trigger="")
    result = orchestrator.arm(session, cid, confirm=True, now=NOW)
    assert result["status"] == "arming_verification_failed"
    assert result["installed"] is False
    assert result["verification_failures"]
    # only this cohort's units removed; manifest gone
    assert list((tmp_path / "units").glob(f"*-c{cid}-*")) == []
    assert orchestrator.store.read_manifest(cid) is None


def test_failed_verification_leaves_unrelated_units_untouched(session, tmp_path):
    cid = due_now_cohort(session)
    orchestrator, _ = stack(tmp_path)
    orchestrator.systemd.runner = ShowRunner(next_elapse="", last_trigger="")
    unrelated = tmp_path / "units" / "probability-arena-marketops.timer"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("unrelated")
    result = orchestrator.arm(session, cid, confirm=True, now=NOW)
    assert result["status"] == "arming_verification_failed"
    assert unrelated.exists()


# --- persistence / dry-run / purity / boundary ------------------------------


def test_reboot_persistence_relies_on_planner_never_backfills(session, tmp_path):
    # Persistent=true is preserved (runtime run_job planner recheck is the guard
    # against backfilling overdue windows — unchanged by DUE-NOW-001).
    cid = due_now_cohort(session)
    plan = build_arm_plan(session, cid, now=NOW)
    timer = SystemdUserManager(enforce_host=False).render_timer(plan["jobs"][0])
    assert "Persistent=true" in timer
    assert "OnUnitActiveSec" not in timer and "OnBootSec" not in timer


def test_dry_run_shows_adjusted_due_now_time_and_installs_nothing(session, tmp_path):
    cid = due_now_cohort(session)
    orchestrator, runner = stack(tmp_path)
    result = orchestrator.arm(session, cid, dry_run=True, now=NOW)
    assert result["status"] == "ok"
    assert result["installed"] is False and result["persisted"] is False
    execute_at = datetime.fromisoformat(result["jobs"][0]["execute_at_utc"])
    assert execute_at == NOW + GRACE  # adjusted, shown
    assert runner.commands == []  # installed nothing
    assert not orchestrator.store.root.exists()


def test_no_provider_call_during_arming_and_verification(session, tmp_path, monkeypatch):
    from app.services.crypto_horizon import CryptoHorizonService

    def forbidden(*a, **k):
        raise AssertionError("arming/verification must not construct a provider")

    monkeypatch.setattr(CryptoHorizonService, "adapter", property(forbidden))
    cid = due_now_cohort(session)
    orchestrator, _ = stack(tmp_path)
    result = orchestrator.arm(session, cid, confirm=True, now=NOW)
    assert result["status"] == "armed"
    assert result["external_calls"] == 0


def test_no_trading_capability_in_due_now_fix():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "app/services/crypto_horizon_orchestrator.py"
    ).read_text()
    assert "ACTIVATION_GRACE" in source
    # implementation-surface terms (boundary-statement words in the disclaimer
    # docstring are expected and excluded); no recurring/daemon/retry directive.
    for banned in ("expected_value", "place_order", "position_siz", "submit_order",
                   "OnUnitActiveSec=", "OnBootSec=", "Restart=", "while True",
                   "while 1"):
        assert banned not in source
