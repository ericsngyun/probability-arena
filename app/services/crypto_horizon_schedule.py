"""CRYPTO-HORIZON-SCHEDULE-001 manual scheduling reports.

This module is compute-on-demand operational support for the existing manual
crypto horizon-observation lane. It reuses that lane's pure planner as the
single source of target/window truth, reads persisted cohort state, and emits
static report data. It never creates reminders, invokes observations, calls a
provider, or persists anything.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CryptoHorizonCohort,
    CryptoHorizonCohortMember,
    CryptoHorizonObservation,
)
from app.services.crypto_horizon import (
    INACTIVE_STATUSES,
    OBSERVE_MAX_CALLS,
    STATUS_ALREADY_OBSERVED,
    STATUS_DUE_NOW,
    STATUS_INACTIVE,
    STATUS_NOT_DUE,
    STATUS_OVERDUE_UNOBSERVED,
    HorizonPlanEntry,
    plan_observations,
)
from app.services.crypto_tape import _aware, _now

LOS_ANGELES = ZoneInfo("America/Los_Angeles")
OPENING_SOON_MINUTES = 60
URGENT_MINUTES = 10
REMINDER_LEAD_MINUTES = 5
SHORT_HORIZONS = frozenset({"15m", "1h"})

STATUS_OPENS_SOON = "opens_soon"
STATUS_CLOSES_SOON = "closes_soon"


def _utc(value: datetime) -> datetime:
    aware = _aware(value)
    if aware is None:
        raise ValueError("timestamp is required")
    return aware.astimezone(timezone.utc)


def _minutes_until(value: datetime | None, now: datetime) -> float | None:
    if value is None:
        return None
    return round((_utc(value) - now).total_seconds() / 60, 2)


def format_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value).isoformat().replace("+00:00", "Z")


def format_los_angeles(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value).astimezone(LOS_ANGELES).isoformat()


def schedule_status(entry: HorizonPlanEntry, now: datetime) -> str:
    """Add urgency labels without changing planner eligibility semantics."""
    now = _utc(now)
    if entry.status in {
        STATUS_ALREADY_OBSERVED,
        STATUS_INACTIVE,
        STATUS_OVERDUE_UNOBSERVED,
    }:
        return entry.status
    if entry.status == STATUS_NOT_DUE:
        until_open = _minutes_until(entry.window_start, now)
        if until_open is not None and 0 < until_open <= OPENING_SOON_MINUTES:
            return STATUS_OPENS_SOON
        return STATUS_NOT_DUE
    if entry.status == STATUS_DUE_NOW:
        until_close = _minutes_until(entry.window_end, now)
        if until_close is not None and 0 <= until_close <= URGENT_MINUTES:
            return STATUS_CLOSES_SOON
        return STATUS_DUE_NOW
    return entry.status


def _actionable(entry: HorizonPlanEntry, now: datetime) -> bool:
    return (
        entry.status not in {
            STATUS_ALREADY_OBSERVED,
            STATUS_INACTIVE,
            STATUS_OVERDUE_UNOBSERVED,
        }
        and entry.window_start is not None
        and entry.window_end is not None
        and _utc(entry.window_end) >= now
    )


def _overlap_groups(
    entries: list[HorizonPlanEntry], now: datetime,
) -> list[dict]:
    """Group windows only when one timestamp lies inside every member window."""
    candidates = [entry for entry in entries if _actionable(entry, now)]
    candidates.sort(key=lambda entry: (_utc(entry.window_start), _utc(entry.window_end)))
    raw_groups: list[dict] = []
    for entry in candidates:
        start = max(now, _utc(entry.window_start))
        end = _utc(entry.window_end)
        if not raw_groups or start > raw_groups[-1]["intersection_end"]:
            raw_groups.append({
                "entries": [entry],
                "intersection_start": start,
                "intersection_end": end,
            })
            continue
        group = raw_groups[-1]
        group["entries"].append(entry)
        group["intersection_start"] = max(group["intersection_start"], start)
        group["intersection_end"] = min(group["intersection_end"], end)

    groups: list[dict] = []
    for index, group in enumerate(raw_groups, start=1):
        action_at = group["intersection_start"]
        reminder_at = max(now, action_at - timedelta(minutes=REMINDER_LEAD_MINUTES))
        tokens = list(dict.fromkeys(entry.token_address for entry in group["entries"]))
        affected = [
            {
                "token_address": entry.token_address,
                "symbol": entry.symbol,
                "horizon": entry.horizon,
            }
            for entry in group["entries"]
        ]
        limit = max(1, min(len(tokens), OBSERVE_MAX_CALLS))
        groups.append({
            "id": index,
            "window_intersection_start": action_at,
            "window_intersection_end": group["intersection_end"],
            "suggested_action_at": action_at,
            "suggested_reminder_at": reminder_at,
            "affected": affected,
            "affected_observations": len(affected),
            "affected_tokens": len(tokens),
            "can_share_bounded_pass": len(affected) > 1,
            "limit": limit,
        })
    return groups


def _next(values: list[datetime | None], now: datetime) -> datetime | None:
    future = [_utc(value) for value in values if value is not None and _utc(value) >= now]
    return min(future) if future else None


def _load_plan(session: Session, cohort_id: int, now: datetime):
    cohort = session.get(CryptoHorizonCohort, cohort_id)
    members = list(session.execute(
        select(CryptoHorizonCohortMember)
        .where(CryptoHorizonCohortMember.cohort_id == cohort_id)
        .order_by(CryptoHorizonCohortMember.id)
    ).scalars().all())
    observations = list(session.execute(
        select(CryptoHorizonObservation)
        .where(CryptoHorizonObservation.cohort_id == cohort_id)
    ).scalars().all())
    existing = {(row.token_address, row.horizon): row.status for row in observations}
    inactive = {
        row.token_address for row in observations if row.status in INACTIVE_STATUSES
    }
    return cohort, members, plan_observations(members, existing, inactive, now)


def build_schedule_report(
    session: Session, cohort_id: int, now: datetime | None = None,
) -> dict:
    """Build a complete, non-persisted schedule for one frozen cohort."""
    now = _utc(now or _now())
    cohort, members, plan = _load_plan(session, cohort_id, now)
    if cohort is None:
        return {
            "status": "no_cohort", "cohort_id": cohort_id, "cohort_size": 0,
            "now": now, "entries": [], "pass_groups": [], "warnings": [],
            "external_calls": 0, "persisted": False,
        }

    groups = _overlap_groups(plan, now)
    group_by_key = {
        (affected["token_address"], affected["horizon"]): group
        for group in groups
        for affected in group["affected"]
    }
    entries: list[dict] = []
    for entry in plan:
        status = schedule_status(entry, now)
        group = group_by_key.get((entry.token_address, entry.horizon))
        action_at = group["suggested_action_at"] if group else None
        entries.append({
            "token_address": entry.token_address,
            "symbol": entry.symbol,
            "member_id": entry.member_id,
            "horizon": entry.horizon,
            "birth_at": _utc(entry.birth_at) if entry.birth_at else None,
            "target_at": _utc(entry.target_at) if entry.target_at else None,
            "window_start": _utc(entry.window_start) if entry.window_start else None,
            "window_end": _utc(entry.window_end) if entry.window_end else None,
            "now": now,
            "status": status,
            "planner_status": entry.status,
            "observe_eligible_now": entry.status == STATUS_DUE_NOW,
            "minutes_until_window_opens": _minutes_until(entry.window_start, now),
            "minutes_until_target": _minutes_until(entry.target_at, now),
            "minutes_until_window_closes": _minutes_until(entry.window_end, now),
            "recommended_next_manual_action_at": action_at,
            "pass_group_id": group["id"] if group else None,
            "can_share_bounded_pass": bool(group and group["can_share_bounded_pass"]),
            "shared_pass_observations": group["affected_observations"] if group else 0,
            "shared_pass_tokens": group["affected_tokens"] if group else 0,
        })

    unobserved_actionable = [entry for entry in plan if _actionable(entry, now)]
    already = sum(entry.status == STATUS_ALREADY_OBSERVED for entry in plan)
    due = sum(entry.status == STATUS_DUE_NOW for entry in plan)
    overdue = sum(entry.status == STATUS_OVERDUE_UNOBSERVED for entry in plan)
    opening_within = {
        minutes: sum(
            entry.status == STATUS_NOT_DUE
            and entry.window_start is not None
            and 0 < (_utc(entry.window_start) - now).total_seconds() / 60 <= minutes
            for entry in plan
        )
        for minutes in (10, 30, 60)
    }
    due_tokens = len({
        entry.token_address for entry in plan if entry.status == STATUS_DUE_NOW
    })
    command_limit = due_tokens or (groups[0]["affected_tokens"] if groups else 1)
    command_limit = max(1, min(command_limit, OBSERVE_MAX_CALLS))

    warnings: list[str] = []
    if opening_within[10]:
        warnings.append(f"{opening_within[10]} window(s) open in 10 minutes or less")
    if due:
        warnings.append(f"{due} observation window(s) currently open")
    closing_soon = sum(
        entry.status == STATUS_DUE_NOW
        and entry.window_end is not None
        and 0 <= (_utc(entry.window_end) - now).total_seconds() / 60 <= URGENT_MINUTES
        for entry in plan
    )
    if closing_soon:
        warnings.append(f"{closing_soon} window(s) close in 10 minutes or less")
    if overdue:
        warnings.append(f"{overdue} horizon window(s) missed")
    created_at = _utc(cohort.created_at)
    created_too_late = [
        entry for entry in plan
        if entry.horizon in SHORT_HORIZONS
        and entry.window_end is not None
        and created_at > _utc(entry.window_end)
    ]
    if created_too_late:
        warnings.append(
            f"cohort was created too late for {len(created_too_late)} short-horizon window(s)"
        )
    if due > 1:
        warnings.append("multiple due observations can be served by one bounded pass")
    if not unobserved_actionable:
        warnings.append("no future observation windows remain")

    return {
        "status": "ok",
        "cohort_id": cohort_id,
        "cohort_size": len(members),
        "cohort_created_at": created_at,
        "now": now,
        "entries": entries,
        "pass_groups": groups,
        "next_window_opening": _next(
            [entry.window_start for entry in unobserved_actionable], now
        ),
        "next_target_time": _next(
            [entry.target_at for entry in unobserved_actionable], now
        ),
        "next_window_closing": _next(
            [entry.window_end for entry in unobserved_actionable], now
        ),
        "already_observed": already,
        "currently_due": due,
        "opening_within_minutes": opening_within,
        "overdue": overdue,
        "recommended_dry_run_command": (
            "python -m app.cli crypto-horizon-observe-once "
            f"--cohort-id {cohort_id} --limit {command_limit} --dry-run"
        ),
        "warnings": warnings,
        "created_too_late_short_horizons": len(created_too_late),
        "external_calls": 0,
        "persisted": False,
    }


def build_reminder_plan(
    session: Session, cohort_id: int, now: datetime | None = None,
) -> dict:
    """Return a static reminder plan. Nothing is installed or persisted."""
    report = build_schedule_report(session, cohort_id, now=now)
    reminders = []
    for group in report["pass_groups"]:
        limit = group["limit"]
        reminders.append({
            **group,
            "suggested_dry_run_command": (
                "python -m app.cli crypto-horizon-observe-once "
                f"--cohort-id {cohort_id} --limit {limit} --dry-run"
            ),
            "suggested_real_command": (
                "python -m app.cli crypto-horizon-observe-once "
                f"--cohort-id {cohort_id} --limit {limit}"
            ),
            "real_command_requires_explicit_human_invocation": True,
        })
    return {
        "status": report["status"],
        "cohort_id": cohort_id,
        "cohort_size": report["cohort_size"],
        "generated_at": report["now"],
        "reminders": reminders,
        "warnings": report["warnings"],
        "installed": False,
        "external_calls": 0,
        "persisted": False,
    }
