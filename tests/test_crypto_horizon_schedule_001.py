"""CRYPTO-HORIZON-SCHEDULE-001 static manual scheduling tests."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import cli
from app.models import (
    CryptoHorizonCohort,
    CryptoHorizonCohortMember,
    CryptoHorizonObservation,
    CryptoPriceTick,
)
from app.services.crypto_horizon import (
    OBS_OBSERVED,
    STATUS_ALREADY_OBSERVED,
    STATUS_DUE_NOW,
    STATUS_NOT_DUE,
    STATUS_OVERDUE_UNOBSERVED,
    plan_observations,
)
from app.services.crypto_horizon_schedule import (
    STATUS_CLOSES_SOON,
    STATUS_OPENS_SOON,
    build_reminder_plan,
    build_schedule_report,
    format_los_angeles,
    schedule_status,
)
from tests.test_crypto_horizon_obs_001 import session  # fixture reuse

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[1]


def member(address="a" * 25, *, birth_at=NOW, member_id=1, symbol="TOK"):
    return type("Member", (), {
        "token_address": address,
        "symbol": symbol,
        "id": member_id,
        "first_evidence_at": birth_at,
        "birth_observed_at": birth_at,
    })()


def add_cohort(session, births: list[tuple[str, datetime]]) -> int:
    cohort = CryptoHorizonCohort(
        chain="solana", member_limit=len(births), window_hours=48,
        note="schedule test", created_at=NOW,
    )
    session.add(cohort)
    session.flush()
    for index, (address, birth_at) in enumerate(births, start=1):
        session.add(CryptoHorizonCohortMember(
            cohort_id=cohort.id, chain="solana", token_address=address,
            symbol=f"T{index}", first_evidence_at=birth_at,
            birth_observed_at=birth_at, added_at=NOW,
        ))
    session.flush()
    return cohort.id


def by_horizon(plan):
    return {entry.horizon: entry for entry in plan}


class TestTiming:
    def test_exact_targets_and_windows_for_all_horizons(self):
        entries = by_horizon(plan_observations([member()], {}, set(), NOW))
        expected = {"15m": 15, "1h": 60, "6h": 360, "24h": 1440}
        for label, minutes in expected.items():
            entry = entries[label]
            assert entry.target_at == NOW + timedelta(minutes=minutes)
            assert entry.window_start == NOW + timedelta(minutes=minutes * 0.5)
            assert entry.window_end == NOW + timedelta(minutes=minutes * 1.5)

    def test_status_transitions_and_boundary_equality(self):
        anchor = NOW
        window_start = anchor + timedelta(minutes=30)
        window_end = anchor + timedelta(minutes=90)

        def status(at):
            entry = by_horizon(plan_observations([member(birth_at=anchor)], {}, set(), at))["1h"]
            return entry.status, schedule_status(entry, at)

        assert status(window_start - timedelta(minutes=61)) == (STATUS_NOT_DUE, STATUS_NOT_DUE)
        assert status(window_start - timedelta(minutes=60)) == (STATUS_NOT_DUE, STATUS_OPENS_SOON)
        assert status(window_start) == (STATUS_DUE_NOW, STATUS_DUE_NOW)
        assert status(window_end - timedelta(minutes=10)) == (STATUS_DUE_NOW, STATUS_CLOSES_SOON)
        assert status(window_end) == (STATUS_DUE_NOW, STATUS_CLOSES_SOON)
        assert status(window_end + timedelta(microseconds=1)) == (
            STATUS_OVERDUE_UNOBSERVED, STATUS_OVERDUE_UNOBSERVED,
        )

    def test_already_observed_precedence(self):
        existing = {("a" * 25, "1h"): OBS_OBSERVED}
        entry = by_horizon(plan_observations(
            [member()], existing, {"a" * 25}, NOW + timedelta(days=3)
        ))["1h"]
        assert entry.status == STATUS_ALREADY_OBSERVED
        assert schedule_status(entry, NOW + timedelta(days=3)) == STATUS_ALREADY_OBSERVED

    def test_los_angeles_conversion_and_dst_fallback(self):
        summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
        before_fallback = datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc)
        after_fallback = datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc)
        assert format_los_angeles(summer) == "2026-07-15T05:00:00-07:00"
        assert format_los_angeles(before_fallback) == "2026-11-01T01:30:00-07:00"
        assert format_los_angeles(after_fallback) == "2026-11-01T01:30:00-08:00"


class TestReports:
    def test_overlap_dedup_and_recommended_action(self, session):
        cid = add_cohort(session, [
            ("a" * 25, NOW),
            ("b" * 25, NOW + timedelta(minutes=2)),
        ])
        report_now = NOW + timedelta(minutes=1)
        report = build_schedule_report(session, cid, now=report_now)
        reminders = build_reminder_plan(session, cid, now=report_now)["reminders"]

        assert len(report["entries"]) == 8
        assert len(reminders) == 4
        first = reminders[0]
        assert first["affected_observations"] == 2
        assert first["affected_tokens"] == 2
        assert first["can_share_bounded_pass"] is True
        assert first["suggested_action_at"] == NOW + timedelta(minutes=9, seconds=30)
        assert first["suggested_reminder_at"] == NOW + timedelta(minutes=4, seconds=30)
        assert first["real_command_requires_explicit_human_invocation"] is True
        first_rows = [row for row in report["entries"] if row["pass_group_id"] == 1]
        assert all(
            row["recommended_next_manual_action_at"] == first["suggested_action_at"]
            for row in first_rows
        )

    def test_schedule_eligibility_matches_observation_planner(self, session):
        birth = NOW - timedelta(hours=1)
        cid = add_cohort(session, [("a" * 25, birth)])
        for at in (
            birth + timedelta(minutes=29, seconds=59),
            birth + timedelta(minutes=30),
            birth + timedelta(minutes=90),
            birth + timedelta(minutes=90, microseconds=1),
        ):
            report = build_schedule_report(session, cid, now=at)
            row = next(item for item in report["entries"] if item["horizon"] == "1h")
            planner = by_horizon(plan_observations([member(birth_at=birth)], {}, set(), at))["1h"]
            assert row["observe_eligible_now"] is (planner.status == STATUS_DUE_NOW)
            assert row["planner_status"] == planner.status
            assert (row["status"] in {STATUS_DUE_NOW, STATUS_CLOSES_SOON}) is row[
                "observe_eligible_now"
            ]

    def test_summary_warnings_and_manual_command(self, session):
        cid = add_cohort(session, [("a" * 25, NOW - timedelta(hours=1, minutes=29))])
        report = build_schedule_report(session, cid, now=NOW)
        assert report["currently_due"] >= 1
        assert any("currently open" in warning for warning in report["warnings"])
        assert any("close in 10 minutes" in warning for warning in report["warnings"])
        assert report["recommended_dry_run_command"].endswith("--limit 1 --dry-run")

    def test_already_observed_count_and_no_persistence(self, session):
        cid = add_cohort(session, [("a" * 25, NOW - timedelta(hours=1))])
        cohort_member = session.query(CryptoHorizonCohortMember).one()
        session.add(CryptoHorizonObservation(
            cohort_id=cid, member_id=cohort_member.id, chain="solana",
            token_address="a" * 25, horizon="1h", status=OBS_OBSERVED,
            observed_at=NOW, created_at=NOW,
        ))
        session.flush()
        before = {
            "cohorts": session.query(CryptoHorizonCohort).count(),
            "members": session.query(CryptoHorizonCohortMember).count(),
            "observations": session.query(CryptoHorizonObservation).count(),
            "ticks": session.query(CryptoPriceTick).count(),
        }
        report = build_schedule_report(session, cid, now=NOW)
        plan = build_reminder_plan(session, cid, now=NOW)
        after = {
            "cohorts": session.query(CryptoHorizonCohort).count(),
            "members": session.query(CryptoHorizonCohortMember).count(),
            "observations": session.query(CryptoHorizonObservation).count(),
            "ticks": session.query(CryptoPriceTick).count(),
        }
        assert report["already_observed"] == 1
        assert report["external_calls"] == plan["external_calls"] == 0
        assert report["persisted"] is plan["persisted"] is False
        assert before == after


class TestCLIAndSafety:
    async def test_clis_render_static_labels(self, session, capsys, monkeypatch):
        cid = add_cohort(session, [("a" * 25, NOW)])
        import app.services.crypto_horizon_schedule as schedule_module
        monkeypatch.setattr(schedule_module, "_now", lambda: NOW)
        await cli.crypto_horizon_schedule_report(cid, session=session)
        output = capsys.readouterr().out
        assert "America/Los_Angeles" in output
        assert "recommended manual dry-run" in output
        await cli.crypto_horizon_reminder_plan(cid, session=session)
        output = capsys.readouterr().out
        assert "static only, not installed" in output
        assert "REQUIRES EXPLICIT HUMAN INVOCATION" in output

    def test_main_wires_commands(self):
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
            "crypto-horizon-schedule-report", "crypto-horizon-reminder-plan",
        } <= set(actions)

    def test_source_has_no_write_provider_timer_or_auto_observe_surface(self):
        source = (REPO / "app/services/crypto_horizon_schedule.py").read_text()
        for forbidden in (
            ".commit(", ".flush(", "fetch_pairs_for_token(", "DexScreenerAdapter(",
            "observe_once(", "systemctl", "crontab", "create_task(", "subprocess",
        ):
            assert forbidden not in source
        assert "expected_value" not in source
        assert "place_order" not in source
        assert "recommended_side" not in source
