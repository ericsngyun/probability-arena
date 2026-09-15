"""Whole-tranche feasibility under frozen coverage + lifecycle constraints.

Proves — before any capture — that the remaining confirmation obligations can
be assigned to series without relaxing a frozen statistical constant.

Inputs: ledger SessionRecords. Outputs: a deterministic obligation list and a
feasible series assignment (or an explicit impossibility report). No activity,
price, or alpha enters.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.microstructure.coverage import (
    BIN_ORDER, BIN_TARGETS, ELIGIBLE_SERIES, MAX_SESSIONS_PER_SERIES,
    MIN_SERIES_REPRESENTED, MIN_WEEKEND_SESSIONS, PLANNED_SESSIONS,
    SessionRecord, coverage_deficit, state_from_ledger,
)
from app.microstructure.lifecycle_compatibility import (
    compatible_series, session_seconds_for_bin,
)
from app.microstructure.panel import (
    TTE_APPROACHING, TTE_FAR, TTE_LATE_RESOLUTION, TTE_LIVE_EVENT,
    TTE_NEAR_EVENT,
)


@dataclass(frozen=True)
class PlannedSession:
    attempt: str
    target_bin: str
    kind: str  # "quota" | "replacement"
    discharges_debt_of: str | None
    preferred_series: str
    compatible_series: tuple
    session_seconds: int
    is_weekend_et: bool

    def to_dict(self) -> dict:
        d = asdict(self)
        d["compatible_series"] = list(self.compatible_series)
        return d


def _series_used_from_records(records: list[SessionRecord]) -> dict[str, int]:
    """Every spent slot consumes series budget, counted or not."""
    used: dict[str, int] = {s: 0 for s in ELIGIBLE_SERIES}
    for r in records:
        for part in r.series.split("|"):
            if part in used:
                used[part] += 1
                break  # one slot → one series budget hit (primary)
    return used


def remaining_obligations(records: list[SessionRecord]) -> list[dict]:
    """Quota fills + named debts whose bin quota is already met.

    A miss against a still-open quota is discharged by one of that bin's
    remaining quota fills (tagged). A miss against a filled quota appends an
    extra replacement session (S21+), matching the ledger's five-counted-
    late_resolution end state.
    """
    state = state_from_ledger(records)
    deficit = coverage_deficit(records)
    remaining = dict(state.bin_remaining())
    out: list[dict] = []
    tagged: set[str] = set()

    for b in BIN_ORDER:
        for i in range(remaining.get(b, 0)):
            discharges = None
            if i == 0:
                for debt in deficit["replacement_debt"]:
                    if debt["bin"] == b and debt["session"] not in tagged:
                        discharges = debt["session"]
                        tagged.add(debt["session"])
                        break
            out.append({"bin": b, "kind": "quota",
                        "discharges_debt_of": discharges})

    for debt in deficit["replacement_debt"]:
        if debt["session"] in tagged:
            continue
        out.append({"bin": debt["bin"], "kind": "replacement",
                    "discharges_debt_of": debt["session"]})
        tagged.add(debt["session"])
    return out


def _weekend_needed(records: list[SessionRecord], plan_so_far: list,
                    n_remaining_after: int) -> bool:
    state = state_from_ledger([r for r in records if r.counted])
    weekends = state.weekend_sessions + sum(1 for p in plan_so_far if p.is_weekend_et)
    still_need = max(0, MIN_WEEKEND_SESSIONS - weekends)
    return still_need > 0 and still_need >= n_remaining_after


def plan_remaining_tranche(records: list[SessionRecord],
                           *, prefer_weekend: bool = True) -> dict:
    """Greedy deterministic assignment. Returns feasible plan or impossibility."""
    obligations = remaining_obligations(records)
    used = _series_used_from_records(records)
    plan: list[PlannedSession] = []
    spent = len(records)

    for idx, obl in enumerate(obligations):
        b = obl["bin"]
        seconds = session_seconds_for_bin(b)
        compat = [s for s in compatible_series(b, seconds, ELIGIBLE_SERIES)
                  if used.get(s, 0) < MAX_SESSIONS_PER_SERIES]
        if not compat:
            return {
                "feasible": False,
                "reason": f"no lifecycle-compatible series with remaining "
                          f"budget for {b}",
                "obligation_index": idx,
                "obligation": obl,
                "series_used": used,
                "planned": [p.to_dict() for p in plan],
            }

        # Prefer unrepresented series, then most remaining budget, then name.
        represented = {s for s, n in used.items() if n > 0}
        def rank(s: str) -> tuple:
            return (0 if s not in represented else 1,
                    -(MAX_SESSIONS_PER_SERIES - used.get(s, 0)),
                    s)
        preferred = sorted(compat, key=rank)[0]

        attempt_n = spent + idx + 1
        if attempt_n <= PLANNED_SESSIONS:
            attempt = f"S{attempt_n:02d}"
        else:
            attempt = f"S{attempt_n}"

        # Weekend: mark binding slots as weekend when quota still open.
        remaining_after = len(obligations) - idx
        need_weekend = prefer_weekend and _weekend_needed(
            records, plan, remaining_after)
        # We cannot invent calendar days here; record the obligation flag.
        is_weekend = bool(need_weekend)

        plan.append(PlannedSession(
            attempt=attempt, target_bin=b, kind=obl["kind"],
            discharges_debt_of=obl["discharges_debt_of"],
            preferred_series=preferred,
            compatible_series=tuple(compat),
            session_seconds=seconds,
            is_weekend_et=is_weekend,
        ))
        used[preferred] = used.get(preferred, 0) + 1

    represented = sum(1 for s, n in used.items() if n > 0)
    weekends = (state_from_ledger([r for r in records if r.counted]).weekend_sessions
                + sum(1 for p in plan if p.is_weekend_et))
    # Series diversity / weekend may still need calendar help; series caps and
    # lifecycle are the hard structural gates this planner certifies.
    structural_ok = all(
        used.get(s, 0) <= MAX_SESSIONS_PER_SERIES for s in ELIGIBLE_SERIES)
    return {
        "feasible": structural_ok and len(plan) == len(obligations),
        "reason": "all remaining obligations assignable under lifecycle + "
                  f"series cap without relaxing frozen constants",
        "obligations_total": len(obligations),
        "bin_counts": {b: sum(1 for o in obligations if o["bin"] == b)
                       for b in BIN_ORDER},
        "replacement_debts_discharged": [
            p.discharges_debt_of for p in plan if p.discharges_debt_of],
        "series_used_projected": used,
        "series_represented_projected": represented,
        "series_diversity_met": represented >= MIN_SERIES_REPRESENTED,
        "weekend_sessions_projected": weekends,
        "weekend_quota_met": weekends >= MIN_WEEKEND_SESSIONS,
        "planned": [p.to_dict() for p in plan],
        "unchanged_constants": {
            "BIN_TARGETS": dict(BIN_TARGETS),
            "MAX_SESSIONS_PER_SERIES": MAX_SESSIONS_PER_SERIES,
            "MIN_SERIES_REPRESENTED": MIN_SERIES_REPRESENTED,
            "MIN_WEEKEND_SESSIONS": MIN_WEEKEND_SESSIONS,
            "ELIGIBLE_SERIES": list(ELIGIBLE_SERIES),
            "TTE_BINS": [TTE_LATE_RESOLUTION, TTE_LIVE_EVENT, TTE_NEAR_EVENT,
                         TTE_APPROACHING, TTE_FAR],
        },
    }
