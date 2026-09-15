"""LIFECYCLE-COMPATIBILITY-AMENDMENT-002 — mutation guards + tranche feasibility.

These tests lock the exact failures S04/S07 taught us. Mutations that restore
the old proxies must fail the suite.
"""

from __future__ import annotations

import ast
import inspect

import app.microstructure.lifecycle_compatibility as LC
import app.microstructure.tranche_feasibility as TF
from app.microstructure import coverage as C
from app.microstructure.panel import (
    TTE_APPROACHING, TTE_FAR, TTE_LATE_RESOLUTION, TTE_LIVE_EVENT,
    TTE_NEAR_EVENT,
)


# --- named failure cases ----------------------------------------------------

def test_KXMLBHR_late_resolution_refuses():
    ok, _ = LC.lifecycle_compatible("KXMLBHR", TTE_LATE_RESOLUTION)
    assert ok is False


def test_KXMLBHR_live_event_refuses():
    ok, _ = LC.lifecycle_compatible("KXMLBHR", TTE_LIVE_EVENT)
    assert ok is False


def test_KXMLBHR_approaching_allows():
    ok, _ = LC.lifecycle_compatible("KXMLBHR", TTE_APPROACHING)
    assert ok is True


def test_ATP_WTA_live_event_allows():
    assert LC.lifecycle_compatible("KXATPMATCH", TTE_LIVE_EVENT)[0] is True
    assert LC.lifecycle_compatible("KXWTAMATCH", TTE_LIVE_EVENT)[0] is True


def test_3h_crossing_anchor_still_allows_when_intervals_complete():
    """Session wall-clock crossing the anchor is not a refusal criterion."""
    assert LC.lifecycle_compatible("KXWTAMATCH", TTE_LATE_RESOLUTION,
                                   session_seconds=10_800)[0] is True
    n = LC.count_complete_observable_intervals(
        "KXWTAMATCH", TTE_LATE_RESOLUTION, 10_800)
    assert n >= 1


# --- mutations: old proxies must not be resurrectable as the rule -----------

def test_mutation_lag_ge_session_seconds_is_not_the_rule():
    """Replace interval rule with series_lag >= SESSION_SECONDS — must disagree.

    Under the old proxy, KXMLBGAME × live_event would be accepted (pre-anchor
    bypass) OR NFL × late_resolution with session=300s would pass. The interval
    rule and the lag>=session rule must not be observationally equivalent.
    """
    # Construct the old proxy decision for a case the new rule settles differently.
    lag = LC.SERIES_SETTLEMENT_LAG_H["KXMLBHR"]
    session = LC.session_seconds_for_bin(TTE_LIVE_EVENT)
    old_proxy_would_skip_gate = True  # live_event was not in _POST_ANCHOR_BINS
    new_ok = LC.lifecycle_compatible("KXMLBHR", TTE_LIVE_EVENT, session)[0]
    assert old_proxy_would_skip_gate and new_ok is False

    # And the lag>=session form: tennis late_resolution with session=10800
    # passes both, but NFL late_resolution with a *short* session shows the
    # divergence — old proxy accepts session=300 (0.12h > 300/3600), new rule
    # refuses because FIRST_TICK=-600 is already past NFL settlement.
    old_nfl_short = LC.SERIES_SETTLEMENT_LAG_H["KXNFLGAME"] >= (300 / 3600.0)
    new_nfl = LC.lifecycle_compatible("KXNFLGAME", TTE_LATE_RESOLUTION,
                                      session_seconds=300)[0]
    assert old_nfl_short is True
    assert new_nfl is False


def test_mutation_reject_any_bin_crossing_anchor_must_fail():
    """`if bin crosses anchor: reject` would kill late_resolution tennis."""
    # A crossing-anchor ban would refuse late_resolution entirely.
    # The real rule allows tennis.
    assert LC.lifecycle_compatible("KXATPMATCH", TTE_LATE_RESOLUTION)[0] is True


def test_mutation_ignoring_max_label_horizon_is_detectable():
    """Compatibility source must reference the frozen max label horizon."""
    src = inspect.getsource(LC.count_complete_observable_intervals)
    assert "MAX_LABEL_HORIZON_S" in src
    assert "LOOKBACK_S" in src or "tte_lookback" in src


def test_evidence_table_is_separate_from_policy():
    ev = LC.SERIES_LIFECYCLE_EVIDENCE["KXMLBHR"]
    assert isinstance(ev, LC.SeriesLifecycleEvidence)
    policy = LC.assess_compatibility("KXMLBHR", TTE_LIVE_EVENT)
    assert isinstance(policy, LC.SeriesBinCompatibility)
    assert ev.observed_stop_vs_anchor_h == -0.22
    assert policy.compatible is False
    # Evidence fields must not include scheduling policy
    assert not hasattr(ev, "compatible")
    assert not hasattr(ev, "complete_intervals_possible")


def test_per_bin_session_duration_ops_contract():
    assert LC.SESSION_SECONDS_BY_BIN[TTE_LIVE_EVENT] == 1_800
    assert LC.SESSION_SECONDS_BY_BIN[TTE_NEAR_EVENT] == 7_200
    assert LC.SESSION_SECONDS_BY_BIN[TTE_APPROACHING] == 10_800
    assert LC.SESSION_SECONDS_BY_BIN[TTE_FAR] == 10_800
    # live_event at 1800s still yields ≥1 complete unit for tennis
    assert LC.count_complete_observable_intervals(
        "KXATPMATCH", TTE_LIVE_EVENT, 1_800) >= 1


# --- target-bin reachability preflight --------------------------------------

def test_reachability_all_candidates_closed():
    from datetime import datetime, timezone, timedelta
    occ = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
    start = occ - timedelta(seconds=LC.FIRST_TICK_TTE_S[TTE_LIVE_EVENT] + 300)
    code, _ = LC.project_target_bin_reachability(
        target_bin=TTE_LIVE_EVENT, series="KXATPMATCH",
        occurrence=occ, session_start=start,
        now=start + timedelta(seconds=60),
        candidate_statuses={"TICKERA": "finalized", "TICKERB": "closed"})
    assert code == LC.ALL_CANDIDATES_CLOSED


def test_reachability_incompatible_series():
    from datetime import datetime, timezone, timedelta
    occ = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
    start = occ - timedelta(seconds=LC.FIRST_TICK_TTE_S[TTE_LIVE_EVENT] + 300)
    code, _ = LC.project_target_bin_reachability(
        target_bin=TTE_LIVE_EVENT, series="KXMLBHR",
        occurrence=occ, session_start=start,
        now=start + timedelta(seconds=60),
        candidate_statuses={"T": "active"})
    assert code == LC.NO_COMPATIBLE_SERIES


def test_reachability_ok_for_tennis_live_event():
    from datetime import datetime, timezone, timedelta
    occ = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
    start = occ - timedelta(seconds=LC.FIRST_TICK_TTE_S[TTE_LIVE_EVENT] + 300)
    code, _ = LC.project_target_bin_reachability(
        target_bin=TTE_LIVE_EVENT, series="KXATPMATCH",
        occurrence=occ, session_start=start,
        now=start + timedelta(seconds=60),
        candidate_statuses={"T": "active"})
    assert code == LC.TARGET_BIN_REACHABLE


# --- whole-tranche feasibility ----------------------------------------------

def _ledger_after_s07():
    return [
        C.SessionRecord("MMEDGE-S01-late_resolution-20260824",
                        TTE_LATE_RESOLUTION, "KXWTAMATCH",
                        "2026-08-24T00:41:58Z", counted=True),
        C.SessionRecord("MMEDGE-S02-live_event-20260824",
                        TTE_LIVE_EVENT, "KXATPMATCH",
                        "2026-08-24T16:40:00Z", counted=True),
        C.SessionRecord("MMEDGE-S03-late_resolution-20260825",
                        TTE_LATE_RESOLUTION, "KXMLBGAME",
                        "2026-08-25T01:45:04Z", counted=True),
        C.SessionRecord("MMEDGE-S04-late_resolution-20260826",
                        TTE_LATE_RESOLUTION, "KXMLBHR",
                        "2026-08-26T01:45:02Z", counted=False,
                        operationally_clean=False),
        C.SessionRecord("MMEDGE-S05-late_resolution-20260826",
                        TTE_LATE_RESOLUTION, "KXATPMATCH",
                        "2026-08-26T21:05:01Z", counted=True),
        C.SessionRecord("MMEDGE-S06-late_resolution-20260827",
                        TTE_LATE_RESOLUTION, "KXWTAMATCH",
                        "2026-08-27T18:05:03Z", counted=True),
        C.SessionRecord("MMEDGE-S07-live_event-20260828",
                        TTE_LIVE_EVENT, "KXMLBHR",
                        "2026-08-28T04:25:06Z", counted=False,
                        operationally_clean=False),
    ]


def test_whole_tranche_feasibility_after_s07():
    """Solver finds a feasible schedule without relaxing frozen constants."""
    plan = TF.plan_remaining_tranche(_ledger_after_s07())
    assert plan["feasible"] is True, plan
    assert plan["bin_counts"][TTE_LIVE_EVENT] >= 3
    assert plan["bin_counts"][TTE_LATE_RESOLUTION] == 1  # S04 debt only
    assert "MMEDGE-S04-late_resolution-20260826" in plan[
        "replacement_debts_discharged"]
    assert "MMEDGE-S07-live_event-20260828" in plan[
        "replacement_debts_discharged"]
    # No plan row may prefer a lifecycle-incompatible series.
    for row in plan["planned"]:
        assert row["preferred_series"] in row["compatible_series"]
        ok, _ = LC.lifecycle_compatible(
            row["preferred_series"], row["target_bin"], row["session_seconds"])
        assert ok is True
        if row["target_bin"] == TTE_LIVE_EVENT:
            assert row["session_seconds"] == 1_800
            assert row["preferred_series"] != "KXMLBHR"


def test_feasibility_never_assigns_KXMLBHR_to_live_event_or_late():
    plan = TF.plan_remaining_tranche(_ledger_after_s07())
    for row in plan["planned"]:
        if row["target_bin"] in (TTE_LIVE_EVENT, TTE_LATE_RESOLUTION):
            assert "KXMLBHR" not in row["compatible_series"]


def test_lifecycle_module_has_no_post_anchor_bins_constant():
    src = inspect.getsource(LC)
    assert "_POST_ANCHOR_BINS" not in src
