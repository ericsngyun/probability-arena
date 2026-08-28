"""The coverage-level scheduler: which DATE should satisfy the next obligation.

A strict hierarchy, and the layers must not blur:

    coverage scheduler   ->  which coverage obligation to pursue, and on which
                             future date/slate
    anchor scheduler     ->  which exact occurrence on that slate
                             (frozen earliest-feasible rule, already alpha-blind)

This module reasons ONLY about experimental coverage obligations: remaining
per-bin targets, series budgets, series diversity, weekend quota and calendar
feasibility. It is structurally incapable of reading price, volume, wire
activity, spread, depth, prior confirmation rows or labels, returns, or any
M0/M1 result -- it is never handed them, and an AST guard fails the build if a
reference appears.

Why this exists: "let us wait until baseball tomorrow" is a scheduling decision
that, made by hand, becomes a quiet selection mechanism. Making it mechanical
removes the degree of freedom rather than trusting nobody uses it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from app.microstructure.panel import (
    TTE_APPROACHING, TTE_FAR, TTE_LATE_RESOLUTION, TTE_LIVE_EVENT,
    TTE_NEAR_EVENT,
)

#: The frozen 4/4/4/4/4 allocation.
BIN_TARGETS = {TTE_LATE_RESOLUTION: 4, TTE_LIVE_EVENT: 4, TTE_NEAR_EVENT: 4,
               TTE_APPROACHING: 4, TTE_FAR: 4}

#: Hard bins first: they are the ones that might prove infeasible, and that
#: must surface at session 2 rather than session 18.
BIN_ORDER = (TTE_LATE_RESOLUTION, TTE_LIVE_EVENT, TTE_NEAR_EVENT,
             TTE_APPROACHING, TTE_FAR)

ELIGIBLE_SERIES = ("KXMLBGAME", "KXMLBTOTAL", "KXMLBHR", "KXATPMATCH",
                   "KXWTAMATCH", "KXWNBATOTAL", "KXWNBAGAME", "KXNFLGAME")

MAX_SESSIONS_PER_SERIES = 4
MIN_SERIES_REPRESENTED = 6
MIN_WEEKEND_SESSIONS = 4
#: The PLANNED tranche. Not a cap: a coverage deficit is discharged by
#: appending replacement sessions (S21+), never by stealing quota from another
#: bin, widening a TTE definition, or retargeting a session after the fact.
PLANNED_SESSIONS = 20
TOTAL_SESSIONS = PLANNED_SESSIONS


@dataclass(frozen=True)
class SessionRecord:
    """What the ledger knows about a completed session. Coverage facts only.

    Two independent facts, deliberately not merged:

    * ``operationally_clean`` -- L1-L3 passed, so the rows belong in the corpus.
    * ``counted`` -- L4 earned the target bin, so the obligation is discharged.

    A session can be the first without the second. It stays in the corpus, is
    NOT relabelled to whichever bin it happened to touch, and leaves its
    obligation outstanding. Merging the two would force a choice between
    discarding good rows and faking coverage.
    """
    label: str
    target_bin: str
    series: str
    start_utc: str
    counted: bool
    operationally_clean: bool = True

    @property
    def in_corpus(self) -> bool:
        return self.operationally_clean

    @property
    def is_weekend_et(self) -> bool:
        """Weekend in ET, since a session's slate is a US sports calendar day.

        A Sunday 00:41Z session is Saturday evening in New York -- classifying
        it by UTC weekday would credit the wrong day and quietly satisfy the
        weekend quota with a weekday slate.
        """
        t = datetime.fromisoformat(self.start_utc.replace("Z", "+00:00"))
        return (t - timedelta(hours=4)).weekday() >= 5      # Sat/Sun in ET


@dataclass(frozen=True)
class CoverageState:
    bin_completed: dict
    series_used: dict
    weekend_sessions: int
    sessions_completed: int

    def bin_remaining(self) -> dict:
        return {b: BIN_TARGETS[b] - self.bin_completed.get(b, 0)
                for b in BIN_ORDER}

    def series_remaining(self) -> dict:
        return {s: MAX_SESSIONS_PER_SERIES - self.series_used.get(s, 0)
                for s in ELIGIBLE_SERIES}

    def series_represented(self) -> int:
        return sum(1 for s in ELIGIBLE_SERIES if self.series_used.get(s, 0) > 0)

    def weekend_remaining(self) -> int:
        return max(0, MIN_WEEKEND_SESSIONS - self.weekend_sessions)

    def to_dict(self) -> dict:
        return {**asdict(self),
                "bin_remaining": self.bin_remaining(),
                "series_remaining": self.series_remaining(),
                "series_represented": self.series_represented(),
                "series_still_required": max(
                    0, MIN_SERIES_REPRESENTED - self.series_represented()),
                "weekend_remaining": self.weekend_remaining()}


def state_from_ledger(records: list[SessionRecord]) -> CoverageState:
    # An obligation is discharged only by a session that EARNED its bin. A
    # clean session that missed its bin still consumes a series budget slot --
    # it really did subscribe to those markets -- but leaves the bin owing.
    counted = [r for r in records if r.counted]
    bins: dict = {}
    series: dict = {}
    for r in counted:
        bins[r.target_bin] = bins.get(r.target_bin, 0) + 1
        series[r.series] = series.get(r.series, 0) + 1
    return CoverageState(bins, series,
                         sum(1 for r in counted if r.is_weekend_et),
                         len(counted))


def next_obligation(state: CoverageState) -> str | None:
    """Which bin the next session must target. Hard bins first."""
    for b in BIN_ORDER:
        if state.bin_remaining()[b] > 0:
            return b
    return None


@dataclass(frozen=True)
class SlateOption:
    """A candidate date, described only in coverage terms."""
    day_et: str
    series_available: tuple


def _series_priority(state: CoverageState, series: str) -> tuple:
    """Deterministic preference, on coverage grounds only.

    Prefers a series that is not yet represented (the >=6-of-8 obligation),
    then one with more remaining budget, then ticker order. No activity,
    liquidity or outcome enters -- only how much of the design each choice
    still owes.
    """
    represented = 1 if state.series_used.get(series, 0) > 0 else 0
    return (represented, -state.series_remaining()[series], series)


def choose_slate(state: CoverageState, options: list[SlateOption],
                 *, target_bin: str) -> dict:
    """Pick the date/slate that best discharges unmet coverage obligations."""
    if not options:
        return {"target_bin": target_bin, "selected": None,
                "reason": "no eligible future slate offered"}

    viable = []
    for opt in sorted(options, key=lambda o: o.day_et):
        usable = tuple(sorted(
            s for s in opt.series_available
            if s in ELIGIBLE_SERIES and state.series_remaining()[s] > 0
            and lifecycle_compatible(s, target_bin)[0]))
        if usable:
            viable.append((opt, usable))
    if not viable:
        return {"target_bin": target_bin, "selected": None,
                "reason": "every offered slate is exhausted under the "
                          f"<= {MAX_SESSIONS_PER_SERIES} sessions-per-series rule"}

    need_weekend = state.weekend_remaining() > 0
    sessions_left = TOTAL_SESSIONS - state.sessions_completed
    weekend_is_binding = need_weekend and state.weekend_remaining() >= sessions_left

    def rank(item):
        opt, usable = item
        best = min(_series_priority(state, s) for s in usable)
        is_weekend = _is_weekend_et_day(opt.day_et)
        # A binding weekend quota dominates; otherwise it is a tie-break AFTER
        # series diversity, so it can never silently reshape series coverage.
        return ((0 if is_weekend else 1) if weekend_is_binding else 0,
                best, (0 if is_weekend else 1), opt.day_et)

    viable.sort(key=rank)
    chosen, usable = viable[0]
    preferred = min(usable, key=lambda s: _series_priority(state, s))
    after = dict(state.series_used)
    after[preferred] = after.get(preferred, 0) + 1

    return {
        "target_bin": target_bin,
        "bin_remaining_before": state.bin_remaining(),
        "series_budget_before": state.series_remaining(),
        "series_represented_before": state.series_represented(),
        "weekend_remaining_before": state.weekend_remaining(),
        "weekend_quota_binding": weekend_is_binding,
        "eligible_slates": [{"day_et": o.day_et,
                             "series_available": list(o.series_available)}
                            for o, _ in viable],
        "selected_day_et": chosen.day_et,
        "selected_is_weekend_et": _is_weekend_et_day(chosen.day_et),
        "series_usable_on_slate": list(usable),
        "preferred_series": preferred,
        "reason": _reason(state, preferred, weekend_is_binding,
                          _is_weekend_et_day(chosen.day_et)),
        "series_budget_after_projected": {
            s: MAX_SESSIONS_PER_SERIES - after.get(s, 0) for s in ELIGIBLE_SERIES},
        "selected": chosen.day_et,
    }


def _reason(state: CoverageState, series: str, weekend_binding: bool,
            picked_weekend: bool) -> str:
    bits = []
    if weekend_binding:
        bits.append(f"weekend quota is binding ({state.weekend_remaining()} "
                    f"needed, {TOTAL_SESSIONS - state.sessions_completed} "
                    f"sessions left)")
    if state.series_used.get(series, 0) == 0:
        bits.append(f"{series} is not yet represented "
                    f"({state.series_represented()}/{MIN_SERIES_REPRESENTED})")
    else:
        bits.append(f"{series} has "
                    f"{state.series_remaining()[series]} of "
                    f"{MAX_SESSIONS_PER_SERIES} sessions remaining")
    if picked_weekend:
        bits.append("slate is a weekend day in ET")
    bits.append("earliest qualifying date, ties broken by day then series")
    return "; ".join(bits)


def _is_weekend_et_day(day_et: str) -> bool:
    return date.fromisoformat(day_et).weekday() >= 5


def feasibility_report(state: CoverageState) -> dict:
    """Can the design still be satisfied at all? Reported, never enforced away."""
    left = TOTAL_SESSIONS - state.sessions_completed
    bins_needed = sum(state.bin_remaining().values())
    return {
        "sessions_remaining": left,
        "bin_sessions_still_required": bins_needed,
        "bin_quota_satisfiable": bins_needed <= left,
        "series_still_required": max(
            0, MIN_SERIES_REPRESENTED - state.series_represented()),
        "series_quota_satisfiable": max(
            0, MIN_SERIES_REPRESENTED - state.series_represented()) <= left,
        "weekend_still_required": state.weekend_remaining(),
        "weekend_quota_satisfiable": state.weekend_remaining() <= left,
    }


# ---------------------------------------------------------------------------
# Coverage deficit and replacement sessions (frozen 2026-08-24, before any
# target-bin failure had occurred)
# ---------------------------------------------------------------------------

def coverage_deficit(records: list[SessionRecord]) -> dict:
    """Which obligations are outstanding, and what it takes to discharge them.

    The frozen contingency rule, stated once:

    * a session that is operationally valid but **fails its target-bin
      coverage stays in the corpus** -- its rows are good rows;
    * it is **never relabelled** to another bin, however many intervals it
      happened to land there;
    * its obligation remains outstanding and is discharged by a
      **deterministic replacement session for that same bin**, appended after
      the planned tranche as S21+;
    * quota is **never stolen** from another bin, no TTE definition is
      widened, and no completed session is retroactively retargeted.

    "20 sessions" therefore remains the planned tranche while the experiment
    may require more sessions purely to satisfy already-frozen coverage floors.
    No statistical hypothesis changes: the cells, horizons, family and verdicts
    are untouched.
    """
    state = state_from_ledger(records)
    clean = [r for r in records if r.in_corpus]
    missed = [r for r in records if r.in_corpus and not r.counted]

    remaining = state.bin_remaining()
    outstanding = {b: n for b, n in remaining.items() if n > 0}
    planned_left = max(0, PLANNED_SESSIONS - len(clean))

    # TWO SEPARATE FACTS, deliberately not merged.
    #
    # `bin_obligations_outstanding` is the QUOTA: how far each bin is from its
    # target. `replacement_debt` is attached to the MISSED SESSION ITSELF and
    # survives the quota being filled by other sessions.
    #
    # They must not be collapsed. S05 and S06 discharged their OWN scheduled
    # obligations, not S04's -- so `late_resolution` can read 4/4 while S04
    # still owes a replacement, and the corpus legitimately ends with five
    # counted late_resolution sessions.
    #
    # An earlier form inferred the debt from slot arithmetic
    # (`needed - planned_left`). That happened to give the right number while
    # S04 was the only miss, but it named no session, and any later shift in
    # the counts could have cancelled a real debt silently -- a scheduler
    # would then have concluded S21 was unnecessary.
    replacement_debt = [{"session": r.label, "bin": r.target_bin} for r in missed]
    quota_shortfall = max(0, sum(outstanding.values()) - planned_left)

    return {
        "sessions_in_corpus": len(clean),
        "sessions_discharging_a_bin": state.sessions_completed,
        "sessions_clean_but_bin_missed": [r.label for r in missed],
        "bin_obligations_outstanding": outstanding,
        "obligations_total": sum(outstanding.values()),
        "planned_sessions_remaining": planned_left,
        # the quota view
        "quota_shortfall_beyond_planned_tranche": quota_shortfall,
        # the debt view -- independent of whether the bin later filled
        "replacement_debt": replacement_debt,
        "replacement_sessions_required": len(replacement_debt),
        "next_replacement_index": PLANNED_SESSIONS + 1,
        "rule": ("a clean session that misses its bin stays in the corpus, is "
                 "never relabelled, and its obligation is discharged by an "
                 "appended replacement session for the SAME bin. The debt is "
                 "attached to the MISSED SESSION and is NOT cancelled by "
                 "another session later filling that bin's quota."),
    }


def replacement_obligations(records: list[SessionRecord]) -> list[str]:
    """Outstanding bins, in the frozen hard-first order. Deterministic."""
    remaining = state_from_ledger(records).bin_remaining()
    out = []
    for b in BIN_ORDER:
        out.extend([b] * remaining[b])
    return out


# ---------------------------------------------------------------------------
# Lifecycle compatibility (frozen 2026-08-26, after S04)
#
# S04 captured 27 frames in 16 ms because all 24 KXMLBHR candidates had
# FINALIZED before the session opened. The cause is a contract property, not an
# activity one:
#
#   `occurrence_datetime` is the anchor the whole schedule is built on, and for
#   MLB/WNBA/NFL the contract SETTLES at or before that instant. `close_time`
#   is a +69 h settlement DEADLINE and says nothing about when settlement
#   actually happens.
#
# So `late_resolution` -- defined as TTE < 0, i.e. after `occurrence_datetime`
# -- is *after settlement* for those series. There is no live market to
# observe, however the session is scheduled. Tennis settles +4 to +6 h after
# occurrence, which is why S01 (WTA) was dense in the same bin.
#
# Derived from published contract metadata over 200 settled markets per series.
# It uses NO activity, NO confirmation outcome and NO result from S01-S04 --
# the same table would have been computable before the tranche began.
# ---------------------------------------------------------------------------

#: Median hours between `settlement_ts` and `occurrence_datetime`, measured
#: 2026-08-26 over 200 settled/finalized markets per series. Negative means the
#: contract is already settled when the anchor field says the event occurs.
SERIES_SETTLEMENT_LAG_H = {
    "KXMLBGAME": -0.04, "KXMLBHR": -0.22, "KXMLBTOTAL": -0.39,
    "KXWNBAGAME": -0.55, "KXWNBATOTAL": -0.61, "KXNFLGAME": +0.12,
    "KXATPMATCH": +3.82, "KXWTAMATCH": +5.80,
}
SETTLEMENT_LAG_MEASURED_AT = "2026-08-26"
SETTLEMENT_LAG_SAMPLE_PER_SERIES = 200

#: A `late_resolution` session must have live market for its whole research
#: window, so the series must stay unsettled for at least that long past the
#: anchor. Other bins sit at TTE > 0, before the anchor, where every series is
#: still live.
_POST_ANCHOR_BINS = (TTE_LATE_RESOLUTION,)


def lifecycle_compatible(series: str, target_bin: str,
                         session_seconds: int = 10_800) -> tuple[bool, str]:
    """Can this series produce a live market throughout this bin's window?

    A structural question about the contract, answered before any capture.
    """
    if target_bin not in _POST_ANCHOR_BINS:
        return True, "bin sits before the anchor; the market is still live"
    lag = SERIES_SETTLEMENT_LAG_H.get(series)
    if lag is None:
        return True, f"no settlement-lag measurement for {series}; not excluded"
    need_h = session_seconds / 3600.0
    if lag <= 0:
        return False, (
            f"{series} settles {abs(lag):.2f} h BEFORE its occurrence_datetime, "
            f"so a {target_bin} session (TTE < 0) begins after the contract is "
            f"finalized; there is no live market to observe")
    if lag < need_h:
        return False, (
            f"{series} settles only {lag:.2f} h after occurrence, shorter than "
            f"the {need_h:.2f} h research window a {target_bin} session needs")
    return True, (f"{series} stays unsettled {lag:.2f} h past occurrence, "
                  f"covering the {need_h:.2f} h window")


def compatible_series(target_bin: str, session_seconds: int = 10_800) -> tuple:
    return tuple(s for s in ELIGIBLE_SERIES
                 if lifecycle_compatible(s, target_bin, session_seconds)[0])
