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
    assert fields == {"label", "target_bin", "series", "start_utc", "counted"}


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
        target_bin=TTE_LATE_RESOLUTION)
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
    out = C.choose_slate(st, [C.SlateOption("2026-08-26", ("KXMLBGAME",)),
                              C.SlateOption("2026-08-29", ("KXATPMATCH",))],
                         target_bin=TTE_LATE_RESOLUTION)
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
                         target_bin=TTE_LATE_RESOLUTION)
    required = {"target_bin", "bin_remaining_before", "series_budget_before",
                "weekend_remaining_before", "eligible_slates",
                "selected_day_et", "reason", "series_budget_after_projected"}
    assert required <= set(out)


def test_projected_budget_reflects_the_choice():
    st = C.state_from_ledger([S01, S02])
    out = C.choose_slate(st, [C.SlateOption("2026-08-26", ("KXMLBGAME",))],
                         target_bin=TTE_LATE_RESOLUTION)
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
