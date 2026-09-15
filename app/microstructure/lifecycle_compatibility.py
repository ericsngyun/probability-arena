"""Interval-completeness lifecycle compatibility (LIFECYCLE-COMPATIBILITY-AMENDMENT-002).

A series × TTE-bin pair is compatible iff it can structurally supply at least
one *complete evaluable observation unit* inside the target bin:

  1. feature lookback history (LOOKBACK_S),
  2. a full decision interval inside the target TTE bin,
  3. tradable lifecycle state at feature time (approximated by settlement lag),
  4. forward observation through the frozen max label horizon (300 s).

This is deliberately NOT "settlement_lag > session_duration" and NOT
"bin sits before the anchor ⇒ always compatible". Those proxies cost S04 and
S07.

Evidence (measured stop-vs-anchor) is stored separately from policy
(series × bin compatibility). Neither path reads activity, price, volume,
confirmation rows, or M0/M1 outputs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from app.microstructure.panel import (
    LOOKBACK_S, MIN_SESSION_REMAINING_S, TTE_APPROACHING, TTE_FAR,
    TTE_LATE_RESOLUTION, TTE_LIVE_EVENT, TTE_NEAR_EVENT, WARMUP_S, tte_bin,
)

#: Frozen label max horizon — also the forward half of a complete unit.
MAX_LABEL_HORIZON_S = 300
DECISION_TICK_S = 300
MIN_COMPLETE_INTERVALS = 1

#: Capture wall-clock length is an *ops* contract derived from the target bin,
#: not a statistical constant. Narrow bins do not need a 3 h socket.
SESSION_SECONDS_BY_BIN = {
    TTE_LATE_RESOLUTION: 10_800,
    TTE_LIVE_EVENT: 1_800,
    TTE_NEAR_EVENT: 7_200,
    TTE_APPROACHING: 10_800,
    TTE_FAR: 10_800,
}

#: TTE of the first post-warmup research tick under the frozen anchor rule.
#: Kept here so compatibility and the schedule-anchor tool share one geometry.
FIRST_TICK_TTE_S = {
    TTE_FAR: 21_600 + 10_800,
    TTE_APPROACHING: 21_600,
    TTE_NEAR_EVENT: 7_200,
    TTE_LIVE_EVENT: 900,
    TTE_LATE_RESOLUTION: -600,
}

LIVE_STATUSES = frozenset({"active", "open"})
DEAD_STATUSES = frozenset({"closed", "determined", "finalized"})


@dataclass(frozen=True)
class SeriesLifecycleEvidence:
    """Measured stop-vs-anchor evidence. Not a scheduling decision."""
    series: str
    observed_stop_vs_anchor_h: float
    provenance: str
    sample_count: int
    observation_date: str
    evidence_status: str

    @property
    def settlement_tte_s(self) -> float:
        """TTE at median settlement. Live while TTE > this value."""
        return -self.observed_stop_vs_anchor_h * 3600.0


#: Measured 2026-08-26 over 200 settled/finalized markets per series.
#: Negative lag ⇒ already settled when occurrence_datetime says the event occurs.
_LAG_H = {
    "KXMLBGAME": -0.04, "KXMLBHR": -0.22, "KXMLBTOTAL": -0.39,
    "KXWNBAGAME": -0.55, "KXWNBATOTAL": -0.61, "KXNFLGAME": +0.12,
    "KXATPMATCH": +3.82, "KXWTAMATCH": +5.80,
}
SETTLEMENT_LAG_MEASURED_AT = "2026-08-26"
SETTLEMENT_LAG_SAMPLE_PER_SERIES = 200

SERIES_LIFECYCLE_EVIDENCE: dict[str, SeriesLifecycleEvidence] = {
    s: SeriesLifecycleEvidence(
        series=s,
        observed_stop_vs_anchor_h=lag,
        provenance="median(settlement_ts - occurrence_datetime) over settled markets",
        sample_count=SETTLEMENT_LAG_SAMPLE_PER_SERIES,
        observation_date=SETTLEMENT_LAG_MEASURED_AT,
        evidence_status="measured",
    )
    for s, lag in _LAG_H.items()
}

# Backward-compatible alias used by coverage.py / older tests.
SERIES_SETTLEMENT_LAG_H = {
    s: e.observed_stop_vs_anchor_h for s, e in SERIES_LIFECYCLE_EVIDENCE.items()
}


@dataclass(frozen=True)
class SeriesBinCompatibility:
    """Policy view: can this series supply the frozen estimand in this bin?"""
    series: str
    bin: str
    complete_intervals_possible: int
    compatible: bool
    reason: str
    session_seconds: int

    def to_dict(self) -> dict:
        return asdict(self)


def session_seconds_for_bin(target_bin: str) -> int:
    if target_bin not in SESSION_SECONDS_BY_BIN:
        raise KeyError(f"unknown TTE bin: {target_bin}")
    return SESSION_SECONDS_BY_BIN[target_bin]


def _live_at_tte(evidence: SeriesLifecycleEvidence | None, tte: float) -> bool:
    if evidence is None:
        return True  # unmeasured ⇒ not excluded
    return tte > evidence.settlement_tte_s


def count_complete_observable_intervals(
        series: str, target_bin: str, session_seconds: int | None = None,
) -> int:
    """How many complete feature+label units fit in-bin while still live.

    A unit at decision tick with TTE=a requires:
      * tte_bin(a) == tte_bin(a - DECISION_TICK_S) == target_bin
      * live at feature time (a) and at label endpoint (a - MAX_LABEL_HORIZON_S)
      * lookback start (a + LOOKBACK_S) still before settlement (usually weaker)
      * session_remaining at the decision > MIN_SESSION_REMAINING_S
    """
    if session_seconds is None:
        session_seconds = session_seconds_for_bin(target_bin)
    if target_bin not in FIRST_TICK_TTE_S:
        raise KeyError(f"unknown TTE bin: {target_bin}")
    evidence = SERIES_LIFECYCLE_EVIDENCE.get(series)
    ft = FIRST_TICK_TTE_S[target_bin]
    n = 0
    # Research offsets from first post-warmup tick.
    max_offset = session_seconds - WARMUP_S
    offset = 0
    while offset + DECISION_TICK_S <= max_offset:
        remain = session_seconds - WARMUP_S - offset
        if remain <= MIN_SESSION_REMAINING_S:
            break
        tte_feature = ft - offset
        tte_label_end = tte_feature - MAX_LABEL_HORIZON_S
        tte_lookback_start = tte_feature + LOOKBACK_S
        if (tte_bin(tte_feature) == target_bin
                and tte_bin(tte_label_end) == target_bin
                and _live_at_tte(evidence, tte_feature)
                and _live_at_tte(evidence, tte_label_end)
                and _live_at_tte(evidence, tte_lookback_start)):
            n += 1
        offset += DECISION_TICK_S
    return n


def assess_compatibility(
        series: str, target_bin: str, session_seconds: int | None = None,
) -> SeriesBinCompatibility:
    if session_seconds is None:
        session_seconds = session_seconds_for_bin(target_bin)
    evidence = SERIES_LIFECYCLE_EVIDENCE.get(series)
    if evidence is None:
        return SeriesBinCompatibility(
            series=series, bin=target_bin, complete_intervals_possible=-1,
            compatible=True, session_seconds=session_seconds,
            reason=f"no settlement-lag measurement for {series}; not excluded")
    n = count_complete_observable_intervals(series, target_bin, session_seconds)
    ok = n >= MIN_COMPLETE_INTERVALS
    if ok:
        reason = (f"{series} can supply {n} complete observable interval(s) "
                  f"in {target_bin} under a {session_seconds}s capture "
                  f"(stop-vs-anchor {evidence.observed_stop_vs_anchor_h:+.2f} h)")
    else:
        reason = (f"{series} cannot supply a complete observable interval in "
                  f"{target_bin} under a {session_seconds}s capture "
                  f"(stop-vs-anchor {evidence.observed_stop_vs_anchor_h:+.2f} h; "
                  f"settlement TTE {evidence.settlement_tte_s:+.0f}s)")
    return SeriesBinCompatibility(
        series=series, bin=target_bin, complete_intervals_possible=n,
        compatible=ok, reason=reason, session_seconds=session_seconds)


def lifecycle_compatible(series: str, target_bin: str,
                         session_seconds: int | None = None) -> tuple[bool, str]:
    """Structural: can this series produce ≥1 complete unit in this bin?"""
    if session_seconds is None:
        session_seconds = session_seconds_for_bin(target_bin)
    result = assess_compatibility(series, target_bin, session_seconds)
    return result.compatible, result.reason


def compatible_series(target_bin: str, session_seconds: int | None = None,
                      universe: tuple[str, ...] | None = None) -> tuple:
    # Default universe = every series we hold stop-vs-anchor evidence for.
    # coverage.ELIGIBLE_SERIES is the scheduling universe and must stay equal.
    series = universe if universe is not None else tuple(SERIES_LIFECYCLE_EVIDENCE)
    if session_seconds is None:
        session_seconds = session_seconds_for_bin(target_bin)
    return tuple(s for s in series
                 if lifecycle_compatible(s, target_bin, session_seconds)[0])


# --- preflight: projected target-bin survival (lifecycle/clock/status only) -

TARGET_BIN_REACHABLE = "TARGET_BIN_REACHABLE"
TARGET_BIN_ALREADY_PASSED = "TARGET_BIN_ALREADY_PASSED"
ALL_CANDIDATES_CLOSED = "ALL_CANDIDATES_CLOSED"
INSUFFICIENT_FORWARD_WINDOW = "INSUFFICIENT_FORWARD_WINDOW"
NO_COMPATIBLE_SERIES = "NO_COMPATIBLE_SERIES"


def project_target_bin_reachability(
        *,
        target_bin: str,
        series: str,
        occurrence: datetime,
        session_start: datetime,
        session_seconds: int | None = None,
        now: datetime | None = None,
        candidate_statuses: dict[str, str | None] | None = None,
) -> tuple[str, str]:
    """Will this frozen arm still be able to harvest a complete in-bin unit?

    Uses only lifecycle evidence, clocks, and venue status — never activity or
    alpha. Refuse before burning a slot on a healthy silent socket.
    """
    if session_seconds is None:
        session_seconds = session_seconds_for_bin(target_bin)
    now = now or datetime.now(timezone.utc)
    if candidate_statuses:
        live = [t for t, st in candidate_statuses.items()
                if st in LIVE_STATUSES]
        unknown = [t for t, st in candidate_statuses.items() if st is None]
        if not live and not unknown:
            return ALL_CANDIDATES_CLOSED, (
                "every frozen candidate is closed/determined/finalized")

    ok, why = lifecycle_compatible(series, target_bin, session_seconds)
    if not ok:
        return NO_COMPATIBLE_SERIES, why

    # Has the geometric window for a complete in-bin unit already left?
    first_tick = session_start + timedelta(seconds=WARMUP_S)
    last_eligible = (session_start + timedelta(seconds=session_seconds)
                     - timedelta(seconds=MIN_SESSION_REMAINING_S + 1))
    if now > last_eligible:
        return INSUFFICIENT_FORWARD_WINDOW, (
            f"now={now.isoformat()} is past the last eligible decision tick "
            f"({last_eligible.isoformat()}) for a {session_seconds}s session")

    # If we are already past every TTE that still lies in the target bin with
    # room for a 300 s label, the bin has passed.
    tte_now = (occurrence - now).total_seconds()
    # Walk remaining decision ticks; if none remain complete+live+in-bin, refuse.
    evidence = SERIES_LIFECYCLE_EVIDENCE.get(series)
    t = max(first_tick, now)
    # Snap forward to the next cadence boundary after first_tick.
    if t < first_tick:
        t = first_tick
    else:
        elapsed = (t - first_tick).total_seconds()
        steps = int(elapsed // DECISION_TICK_S)
        if elapsed % DECISION_TICK_S:
            steps += 1
        t = first_tick + timedelta(seconds=steps * DECISION_TICK_S)

    found = False
    while t + timedelta(seconds=DECISION_TICK_S) <= session_start + timedelta(
            seconds=session_seconds):
        remain = (session_start + timedelta(seconds=session_seconds) - t
                  ).total_seconds()
        if remain <= MIN_SESSION_REMAINING_S:
            break
        a = (occurrence - t).total_seconds()
        if (tte_bin(a) == target_bin and tte_bin(a - MAX_LABEL_HORIZON_S) == target_bin
                and _live_at_tte(evidence, a)
                and _live_at_tte(evidence, a - MAX_LABEL_HORIZON_S)):
            found = True
            break
        t += timedelta(seconds=DECISION_TICK_S)

    if not found:
        if tte_now < FIRST_TICK_TTE_S[target_bin] - session_seconds:
            return TARGET_BIN_ALREADY_PASSED, (
                f"TTE now={tte_now:.0f}s; no remaining complete interval in "
                f"{target_bin}")
        return INSUFFICIENT_FORWARD_WINDOW, (
            f"no complete live in-bin interval remains before session end "
            f"(TTE now={tte_now:.0f}s)")
    return TARGET_BIN_REACHABLE, (
        f"at least one complete {target_bin} unit remains reachable for {series}")
