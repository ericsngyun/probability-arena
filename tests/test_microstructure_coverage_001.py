"""The coverage scheduler: obligations only, and provably blind to everything else."""

from __future__ import annotations

import ast
import inspect

import pytest

from app.microstructure import coverage as C
from app.microstructure.panel import (
    TTE_APPROACHING, TTE_FAR, TTE_LATE_RESOLUTION, TTE_LIVE_EVENT,
    TTE_NEAR_EVENT,
)


def rec(label, bin_, series, start, counted=True):
    return C.SessionRecord(label, bin_, series, start, counted)


S01 = rec("S01", TTE_LATE_RESOLUTION, "KXWTAMATCH", "2026-08-24T00:41:58Z")
S02 = rec("S02", TTE_LIVE_EVENT, "KXATPMATCH", "2026-08-24T16:40:00Z")


# --- blindness, structurally ------------------------------------------------

#: Precise rather than sweeping. `SessionRecord.label` is a session NAME, not
#: a research label, so banning the bare word "label" flags a correct module --
#: the research concept is the plural `labels` dict on a row. A guard that
#: fires on the wrong thing gets weakened or deleted, which is worse than a
#: narrower guard that holds.
FORBIDDEN = ("price", "volume", "spread", "depth", "activity", "liquidity",
             "return", "loss", "coefficient", "markout", "imbalance",
             "microprice", "m0_", "m1_", "labels", "delta_mid", "alpha",
             "pnl", "r2", "p_value", "verdict")


def test_scheduler_cannot_reference_any_alpha_quantity():
    """AST identifiers and non-docstring literals -- the module's own prose
    names the forbidden fields while asserting it never reads them."""
    tree = ast.parse(inspect.getsource(C))
    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            d = ast.get_docstring(n, clean=False)
            if d:
                docs.add(d)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    used |= {n.value for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and n.value not in docs}
    used = {u.lower() for u in used}
    for banned in FORBIDDEN:
        hits = {u for u in used if banned in u}
        assert not hits, f"coverage scheduler references {hits}"


def test_scheduler_imports_nothing_that_can_read_rows_or_results():
    tree = ast.parse(inspect.getsource(C))
    mods = {n.module for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
             for a in n.names}
    for banned in ("evaluate", "rows", "features", "labels",
                   "tte_heterogeneity", "linalg"):
        assert not any(banned in m for m in mods), f"imports {banned}: {mods}"


def test_session_record_carries_only_coverage_facts():
    fields = set(C.SessionRecord.__dataclass_fields__)
    assert fields == {"label", "target_bin", "series", "start_utc", "counted",
                      "operationally_clean"}


# --- obligations ------------------------------------------------------------

def test_hard_bins_come_first():
    assert C.BIN_ORDER[0] == TTE_LATE_RESOLUTION
    assert C.BIN_ORDER[1] == TTE_LIVE_EVENT
    assert set(C.BIN_ORDER) == set(C.BIN_TARGETS)
    assert sum(C.BIN_TARGETS.values()) == C.TOTAL_SESSIONS == 20


def test_next_obligation_after_s01_and_s02_is_late_resolution():
    st = C.state_from_ledger([S01, S02])
    assert st.bin_completed[TTE_LATE_RESOLUTION] == 1
    assert st.bin_completed[TTE_LIVE_EVENT] == 1
    assert C.next_obligation(st) == TTE_LATE_RESOLUTION


def test_uncounted_sessions_do_not_discharge_an_obligation():
    st = C.state_from_ledger([S01, rec("X", TTE_LATE_RESOLUTION, "KXMLBGAME",
                                       "2026-08-25T00:00:00Z", counted=False)])
    assert st.bin_remaining()[TTE_LATE_RESOLUTION] == 3


def test_all_bins_complete_yields_no_obligation():
    recs = [rec(f"r{i}", b, "KXMLBGAME", "2026-08-25T00:00:00Z")
            for b in C.BIN_ORDER for i in range(4)]
    assert C.next_obligation(C.state_from_ledger(recs)) is None


# --- series budget ----------------------------------------------------------

def test_series_budget_caps_at_four():
    recs = [rec(f"t{i}", TTE_FAR, "KXATPMATCH", "2026-08-25T00:00:00Z")
            for i in range(4)]
    st = C.state_from_ledger(recs)
    assert st.series_remaining()["KXATPMATCH"] == 0
    out = C.choose_slate(st, [C.SlateOption("2026-08-26", ("KXATPMATCH",))],
                         target_bin=TTE_FAR)
    assert out["selected"] is None
    assert "sessions-per-series" in out["reason"]


def test_an_unrepresented_series_is_preferred_over_a_used_one():
    st = C.state_from_ledger([S01, S02])
    out = C.choose_slate(st, [C.SlateOption(
        "2026-08-26", ("KXATPMATCH", "KXMLBGAME"))],
        target_bin=TTE_NEAR_EVENT)
    assert out["preferred_series"] == "KXMLBGAME"
    assert "not yet represented" in out["reason"]


def test_tennis_is_still_chosen_when_it_is_the_only_option():
    """Diversity is an obligation, not a prohibition."""
    st = C.state_from_ledger([S01, S02])
    out = C.choose_slate(st, [C.SlateOption("2026-08-26", ("KXATPMATCH",))],
                         target_bin=TTE_LATE_RESOLUTION)
    assert out["preferred_series"] == "KXATPMATCH"
    assert out["selected"] == "2026-08-26"


# --- weekend quota ----------------------------------------------------------

def test_weekend_is_classified_in_et_not_utc():
    """S01 ran 00:41Z Monday, which is Sunday evening in New York."""
    sunday_et = rec("x", TTE_FAR, "KXMLBGAME", "2026-08-24T00:41:58Z")
    assert sunday_et.is_weekend_et is True
    monday_et = rec("y", TTE_FAR, "KXMLBGAME", "2026-08-24T16:40:00Z")
    assert monday_et.is_weekend_et is False


def test_weekend_dominates_only_when_it_becomes_binding():
    used = [rec(f"r{i}", TTE_FAR, "KXMLBGAME", "2026-08-25T18:00:00Z")
            for i in range(16)]
    st = C.state_from_ledger(used)          # 16 done, 4 left, 4 weekends needed
    out = C.choose_slate(st, [C.SlateOption("2026-08-27", ("KXMLBTOTAL",)),
                              C.SlateOption("2026-08-29", ("KXMLBHR",))],
                         target_bin=TTE_FAR)
    assert out["weekend_quota_binding"] is True
    assert out["selected_is_weekend_et"] is True, "must take the Saturday"


def test_weekend_is_only_a_tiebreak_when_not_binding():
    st = C.state_from_ledger([S01, S02])
    # NEAR_EVENT: a pre-anchor bin, so lifecycle compatibility is not the
    # thing under test here -- diversity-vs-weekend ordering is.
    out = C.choose_slate(st, [C.SlateOption("2026-08-26", ("KXMLBGAME",)),
                              C.SlateOption("2026-08-29", ("KXATPMATCH",))],
                         target_bin=TTE_NEAR_EVENT)
    assert out["weekend_quota_binding"] is False
    # diversity wins: the unrepresented series on the weekday beats the
    # already-used series on the weekend
    assert out["preferred_series"] == "KXMLBGAME"


# --- determinism and audit --------------------------------------------------

def test_choice_is_deterministic():
    st = C.state_from_ledger([S01, S02])
    opts = [C.SlateOption("2026-08-27", ("KXMLBHR", "KXMLBGAME")),
            C.SlateOption("2026-08-26", ("KXWNBAGAME",))]
    first = C.choose_slate(st, opts, target_bin=TTE_NEAR_EVENT)
    for _ in range(5):
        assert C.choose_slate(st, list(reversed(opts)),
                              target_bin=TTE_NEAR_EVENT) == first


def test_audit_artifact_has_every_required_field():
    st = C.state_from_ledger([S01, S02])
    out = C.choose_slate(st, [C.SlateOption("2026-08-26", ("KXMLBGAME",))],
                         target_bin=TTE_NEAR_EVENT)
    required = {"target_bin", "bin_remaining_before", "series_budget_before",
                "weekend_remaining_before", "eligible_slates",
                "selected_day_et", "reason", "series_budget_after_projected"}
    assert required <= set(out)


def test_projected_budget_reflects_the_choice():
    st = C.state_from_ledger([S01, S02])
    out = C.choose_slate(st, [C.SlateOption("2026-08-26", ("KXMLBGAME",))],
                         target_bin=TTE_NEAR_EVENT)
    assert out["series_budget_before"]["KXMLBGAME"] == 4
    assert out["series_budget_after_projected"]["KXMLBGAME"] == 3


def test_feasibility_is_reported_not_enforced():
    st = C.state_from_ledger([S01, S02])
    rep = C.feasibility_report(st)
    assert rep["sessions_remaining"] == 18
    assert rep["bin_sessions_still_required"] == 18
    assert rep["bin_quota_satisfiable"] is True
    assert rep["series_still_required"] == 4
    assert rep["weekend_still_required"] == 3


# --- the frozen contingency rule: coverage deficit -> replacement session ----

def test_clean_session_that_misses_its_bin_stays_in_corpus():
    """Operational validity and bin coverage are independent facts."""
    missed = C.SessionRecord("S03", TTE_LATE_RESOLUTION, "KXMLBGAME",
                             "2026-08-25T01:45:00Z", counted=False,
                             operationally_clean=True)
    assert missed.in_corpus is True, "good rows must not be discarded"
    d = C.coverage_deficit([S01, S02, missed])
    assert d["sessions_in_corpus"] == 3
    assert d["sessions_discharging_a_bin"] == 2
    assert "S03" in d["sessions_clean_but_bin_missed"]


def test_a_missed_bin_is_never_relabelled_to_a_bin_it_touched():
    missed = C.SessionRecord("S03", TTE_LATE_RESOLUTION, "KXMLBGAME",
                             "2026-08-25T01:45:00Z", counted=False)
    st = C.state_from_ledger([S01, S02, missed])
    # late_resolution still owes 3; nothing was credited anywhere else
    assert st.bin_remaining()[TTE_LATE_RESOLUTION] == 3
    assert sum(st.bin_remaining().values()) == 18
    assert C.next_obligation(st) == TTE_LATE_RESOLUTION


def test_a_missed_bin_generates_a_replacement_rather_than_stealing_quota():
    # 20 clean sessions, but two missed their bin
    recs = []
    obligations = [b for b in C.BIN_ORDER for _ in range(4)]
    for i, b in enumerate(obligations):
        recs.append(C.SessionRecord(f"S{i+1:02d}", b, "KXMLBGAME",
                                    "2026-08-25T18:00:00Z",
                                    counted=(i not in (0, 5)),
                                    operationally_clean=True))
    d = C.coverage_deficit(recs)
    assert d["sessions_in_corpus"] == 20
    assert d["obligations_total"] == 2
    assert d["planned_sessions_remaining"] == 0
    assert d["replacement_sessions_required"] == 2, (
        "the deficit must be discharged by APPENDED sessions, not by quota "
        "taken from another bin")
    assert d["next_replacement_index"] == 21


def test_replacement_obligations_are_deterministic_and_hard_bins_first():
    recs = [C.SessionRecord("a", TTE_FAR, "KXMLBGAME", "2026-08-25T18:00:00Z",
                            counted=True)]
    obs = C.replacement_obligations(recs)
    assert obs[0] == TTE_LATE_RESOLUTION
    assert obs.count(TTE_FAR) == 3
    assert len(obs) == 19
    assert C.replacement_obligations(recs) == obs


def test_planned_tranche_is_not_a_cap():
    assert C.PLANNED_SESSIONS == 20
    recs = [C.SessionRecord(f"S{i}", TTE_FAR, "KXMLBGAME",
                            "2026-08-25T18:00:00Z", counted=False)
            for i in range(20)]
    d = C.coverage_deficit(recs)
    assert d["replacement_sessions_required"] == 20
    assert d["obligations_total"] == 20


def test_no_hypothesis_quantity_appears_in_the_deficit_rule():
    import ast, inspect
    tree = ast.parse(inspect.getsource(C.coverage_deficit))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("cell", "horizon", "fdr", "verdict", "p_value", "loss"):
        assert not any(banned in n.lower() for n in names)


# --- lifecycle compatibility (forward rule, frozen after S04) ---------------

def test_late_resolution_is_only_compatible_with_series_that_outlive_the_anchor():
    """S04 captured 27 frames in 16ms because every candidate had FINALIZED.

    For MLB/WNBA the contract settles at or before `occurrence_datetime`, so a
    TTE<0 session begins after settlement. This is a contract property measured
    from published metadata, not an activity or outcome observation.
    """
    compat = C.compatible_series(TTE_LATE_RESOLUTION)
    assert set(compat) == {"KXATPMATCH", "KXWTAMATCH"}
    for dead in ("KXMLBHR", "KXMLBGAME", "KXMLBTOTAL", "KXWNBAGAME",
                 "KXWNBATOTAL", "KXNFLGAME"):
        ok, why = C.lifecycle_compatible(dead, TTE_LATE_RESOLUTION)
        assert ok is False and why


def test_pre_anchor_bins_exclude_nothing():
    """TTE>0 is before the anchor, so every series is still live there."""
    for b in (TTE_FAR, TTE_APPROACHING, TTE_NEAR_EVENT, TTE_LIVE_EVENT):
        assert set(C.compatible_series(b)) == set(C.ELIGIBLE_SERIES)


def test_the_rule_is_about_window_length_not_just_sign():
    """NFL settles AFTER occurrence, but only by 7 minutes."""
    ok, why = C.lifecycle_compatible("KXNFLGAME", TTE_LATE_RESOLUTION,
                                     session_seconds=10_800)
    assert ok is False and "shorter than" in why
    ok2, _ = C.lifecycle_compatible("KXNFLGAME", TTE_LATE_RESOLUTION,
                                    session_seconds=300)
    assert ok2 is True, "a short enough window would fit inside +0.12h"


def test_an_unmeasured_series_is_not_excluded():
    """Absence of a measurement is not evidence of incompatibility."""
    ok, why = C.lifecycle_compatible("KXNEWSERIES", TTE_LATE_RESOLUTION)
    assert ok is True and "not excluded" in why


def test_the_scheduler_will_not_offer_an_incompatible_series():
    st = C.state_from_ledger([S01])
    out = C.choose_slate(st, [C.SlateOption("2026-08-27",
                                            ("KXMLBHR", "KXMLBGAME"))],
                         target_bin=TTE_LATE_RESOLUTION)
    assert out["selected"] is None, "a dead-by-construction slate must not be chosen"


def test_lifecycle_uses_no_activity_or_outcome_input():
    import ast, inspect
    tree = ast.parse(inspect.getsource(C.lifecycle_compatible))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("activity", "volume", "price", "frames", "blocks",
                   "counted", "verdict", "rows"):
        assert not any(banned in n.lower() for n in names), banned


def test_the_measurement_provenance_is_recorded():
    assert C.SETTLEMENT_LAG_MEASURED_AT == "2026-08-26"
    assert C.SETTLEMENT_LAG_SAMPLE_PER_SERIES == 200
    assert set(C.SERIES_SETTLEMENT_LAG_H) == set(C.ELIGIBLE_SERIES)


def test_the_rule_is_forward_only_and_does_not_invalidate_S03():
    """S03 was KXMLBGAME/late_resolution and legitimately counted 12 intervals.
    A forward rule changes future scheduling, never a completed session."""
    s03 = rec("S03", TTE_LATE_RESOLUTION, "KXMLBGAME", "2026-08-25T01:45:04Z")
    st = C.state_from_ledger([S01, S02, s03])
    assert st.bin_completed[TTE_LATE_RESOLUTION] == 2


# --- replacement debt is NOT cancelled by the quota filling ------------------

def test_a_filled_quota_does_not_cancel_a_missed_session_s_debt():
    """S05 and S06 discharged their OWN obligations, not S04's.

    An earlier form inferred the debt from slot arithmetic, which named no
    session and could have cancelled a real debt silently -- a scheduler would
    then have concluded S21 was unnecessary.
    """
    recs = [rec("S01", TTE_LATE_RESOLUTION, "KXWTAMATCH", "2026-08-24T00:41:58Z"),
            rec("S03", TTE_LATE_RESOLUTION, "KXMLBGAME", "2026-08-25T01:45:04Z"),
            rec("S04", TTE_LATE_RESOLUTION, "KXMLBHR", "2026-08-26T01:45:02Z",
                counted=False),
            rec("S05", TTE_LATE_RESOLUTION, "KXATPMATCH", "2026-08-26T21:05:01Z"),
            rec("S06", TTE_LATE_RESOLUTION, "KXWTAMATCH", "2026-08-27T18:05:03Z")]
    st = C.state_from_ledger(recs)
    d = C.coverage_deficit(recs)
    # the quota is met
    assert st.bin_remaining()[TTE_LATE_RESOLUTION] == 0
    assert TTE_LATE_RESOLUTION not in d["bin_obligations_outstanding"]
    # and the debt survives it
    assert d["replacement_debt"] == [{"session": "S04",
                                      "bin": TTE_LATE_RESOLUTION}]
    assert d["replacement_sessions_required"] == 1
    assert d["next_replacement_index"] == 21


def test_the_debt_names_the_session_and_its_bin():
    recs = [rec("Sx", TTE_LIVE_EVENT, "KXMLBGAME", "2026-08-25T18:00:00Z",
                counted=False),
            rec("Sy", TTE_FAR, "KXMLBHR", "2026-08-25T18:00:00Z", counted=False)]
    d = C.coverage_deficit(recs)
    assert {x["session"] for x in d["replacement_debt"]} == {"Sx", "Sy"}
    assert {x["bin"] for x in d["replacement_debt"]} == {TTE_LIVE_EVENT, TTE_FAR}
    assert d["replacement_sessions_required"] == 2


def test_quota_shortfall_and_replacement_debt_are_separate_fields():
    recs = [rec(f"S{i}", b, "KXMLBGAME", "2026-08-25T18:00:00Z")
            for i, b in enumerate(C.BIN_ORDER) for _ in range(4)]
    d = C.coverage_deficit(recs)
    assert "quota_shortfall_beyond_planned_tranche" in d
    assert "replacement_debt" in d
    assert d["replacement_debt"] == []           # nothing missed
