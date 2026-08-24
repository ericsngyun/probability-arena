"""The SECONDARY family: separate module, separate lock, separate BH."""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone

import pytest

from app.microstructure import authorization as A
from app.microstructure import evaluate as E
from app.microstructure import tte_heterogeneity as T
from app.microstructure.authorization import ConfirmationDataLocked
from app.microstructure.panel import DatasetRole
from tests.test_microstructure_evaluate_001 import corpus, make_row


def _tte_valid(**over):
    from app.microstructure.rows import LABEL_SCHEMA_VERSION, ROW_SCHEMA_VERSION
    d = {"milestone": A.TTE_MILESTONE, "operator": "eric",
         "authorized_at_utc": datetime.now(timezone.utc).isoformat(),
         "sessions_complete": 20, "statement": A.TTE_REQUIRED_STATEMENT,
         "evaluator_fingerprint": A.tte_evaluator_fingerprint(),
         "preregistration_fingerprint": A.tte_preregistration_fingerprint(),
         "expected_row_schema": ROW_SCHEMA_VERSION,
         "expected_label_schema": LABEL_SCHEMA_VERSION}
    d.update(over)
    return d


def spread_corpus(n_sessions=3, n_markets=25, n_per=40):
    """Rows spread across all five TTE bins."""
    rng = random.Random(3)
    ttes = [30_000, 10_000, 3_000, 400, -2_000]      # one per frozen bin
    rows = []
    for s in range(n_sessions):
        for m in range(n_markets):
            for i in range(n_per):
                r = make_row(f"s{s}", f"MKT{m:03d}", s * 100_000 + i,
                             role=DatasetRole.VALIDATION, rng=rng,
                             event=f"EV{m // 5}")
                r["TTE_seconds"] = float(ttes[i % 5])
                rows.append(r)
    return rows


# --- the two locks are independent -----------------------------------------

def test_tte_lock_is_separate_from_the_primary_lock(tmp_path, monkeypatch):
    monkeypatch.setenv(A.TTE_AUTH_ENV, str(tmp_path / "tte.json"))
    monkeypatch.setenv(A.AUTH_ENV, str(tmp_path / "edge.json"))
    rows = spread_corpus(2, 5, 10)
    for r in rows:
        r["dataset_role"] = DatasetRole.CONFIRMATION
    with pytest.raises(ConfirmationDataLocked, match="TTE-HETEROGENEITY"):
        T.verify_corpus(rows)


def test_an_edge001_authorization_does_not_unlock_the_secondary(
        tmp_path, monkeypatch):
    """The whole point of two locks: one key must not open both.

    Exercised THROUGH `T.verify_corpus`, not by calling the gate directly.
    Calling `require_tte_readable` here proves only that the function works --
    it says nothing about whether the module uses it, and a mutation swapping
    in the primary gate passed 17/17 against the direct form.
    """
    edge = tmp_path / "edge.json"
    tte = tmp_path / "tte.json"
    monkeypatch.setenv(A.AUTH_ENV, str(edge))
    monkeypatch.setenv(A.TTE_AUTH_ENV, str(tte))
    from tests.test_microstructure_authorization_001 import _valid
    edge.write_text(json.dumps(_valid()))
    A.require_readable(DatasetRole.CONFIRMATION)          # primary unlocked

    rows = spread_corpus(2, 5, 10)
    for r in rows:
        r["dataset_role"] = DatasetRole.CONFIRMATION
    # the SECONDARY module must still refuse, via its own gate
    with pytest.raises(ConfirmationDataLocked, match="TTE-HETEROGENEITY"):
        T.verify_corpus(rows)
    with pytest.raises(ConfirmationDataLocked, match="TTE-HETEROGENEITY"):
        T.evaluate(rows)
    # and the gate itself refuses too
    with pytest.raises(ConfirmationDataLocked):
        A.require_tte_readable(DatasetRole.CONFIRMATION)


def test_an_edge001_authorization_copied_into_the_tte_slot_is_refused(
        tmp_path, monkeypatch):
    tte = tmp_path / "tte.json"
    monkeypatch.setenv(A.TTE_AUTH_ENV, str(tte))
    monkeypatch.setenv(A.AUTH_ENV, str(tmp_path / "edge.json"))
    from tests.test_microstructure_authorization_001 import _valid
    tte.write_text(json.dumps(_valid()))                  # wrong milestone
    with pytest.raises(ConfirmationDataLocked, match="never unlock"):
        A.require_tte_readable(DatasetRole.CONFIRMATION)


def test_tte_statement_must_acknowledge_it_is_secondary(tmp_path, monkeypatch):
    tte = tmp_path / "tte.json"
    monkeypatch.setenv(A.TTE_AUTH_ENV, str(tte))
    tte.write_text(json.dumps(_tte_valid(statement="fine by me")))
    with pytest.raises(ConfirmationDataLocked, match="secondary"):
        A.require_tte_readable(DatasetRole.CONFIRMATION)
    assert "cannot rescue" in A.TTE_REQUIRED_STATEMENT


def test_tte_authorization_is_version_bound(tmp_path, monkeypatch):
    tte = tmp_path / "tte.json"
    monkeypatch.setenv(A.TTE_AUTH_ENV, str(tte))
    tte.write_text(json.dumps(_tte_valid(evaluator_fingerprint="0" * 64)))
    with pytest.raises(ConfirmationDataLocked, match="does not match"):
        A.require_tte_readable(DatasetRole.CONFIRMATION)


def test_the_two_fingerprints_are_different():
    assert A.tte_evaluator_fingerprint() != A.evaluator_fingerprint()
    assert "app/microstructure/tte_heterogeneity.py" in A.TTE_FROZEN_FILES
    assert "app/microstructure/tte_heterogeneity.py" not in A.FROZEN_EVALUATOR_FILES


# --- the frozen secondary family --------------------------------------------

def test_family_is_twelve_omnibus_tests_not_sixty_cells():
    assert len(T.CELLS) == 12
    assert len(T.FROZEN_BINS) == 5
    assert len(T.CELLS) * len(T.FROZEN_BINS) == 60, "60 is what we did NOT do"


def test_all_five_bins_always_enter_every_omnibus():
    out = T.evaluate(spread_corpus())
    for cell in out["omnibus"].values():
        assert set(cell["bin_means"]) == set(T.FROZEN_BINS)
        assert set(cell["bin_blocks"]) == set(T.FROZEN_BINS)


def test_bins_are_never_merged_or_dropped():
    """AST identifiers, not source text.

    The module docstring says "never merged, split, reweighted or dropped"
    while asserting exactly that, so a substring scan flags the module for
    containing its own guarantee -- the third time this pattern has appeared,
    and the reason TESTING_POLICY now requires structural guards.
    """
    import ast, inspect
    tree = ast.parse(inspect.getsource(T))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    called |= {n.func.attr for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for banned in ("merge_bins", "drop_bin", "collapse_bins", "reweight_bins"):
        assert banned not in called, f"module calls {banned}"
    # behavioural: every report keeps all five bins
    out = T.evaluate(spread_corpus(2, 5, 20))
    for cell in out["omnibus"].values():
        assert len(cell["bin_means"]) == 5, "a bin disappeared from a report"
        assert set(cell["bin_means"]) == set(T.FROZEN_BINS)


def test_underpowered_bins_are_reported_not_removed():
    out = T.evaluate(spread_corpus(2, 5, 20))          # few clusters per bin
    assert out["underpowered"], "expected underpowered bins"
    for cell_name, bins in out["underpowered"].items():
        assert bins
        assert len(out["omnibus"][cell_name]["bin_means"]) == 5


def test_support_floors_are_frozen():
    assert T.MIN_BIN_BLOCKS == 100 and T.MIN_BIN_CLUSTERS == 20


# --- it cannot rescue the primary -------------------------------------------

def test_secondary_result_carries_no_primary_verdict():
    out = T.evaluate(spread_corpus())
    assert out["cannot_rescue_primary"] is True
    assert "cannot rescue" in out["statement"]
    for key in ("FLOW_NOT_ADDITIVE_LANE_STOPS", "CANDIDATE_FOR_PROSPECTIVE_TEST",
                "VOID_POSITIVE_CONTROL_FAILED"):
        assert key not in json.dumps(out), f"secondary emitted a primary verdict {key}"
    assert out["verdict"] in (T.VERDICT_NO_ELIGIBLE_DATA,
                              T.VERDICT_NO_HETEROGENEITY,
                              T.VERDICT_HETEROGENEITY_PRESENT)


def test_a_surviving_omnibus_demands_a_new_preregistration():
    assert "new prospective" in T.CANNOT_RESCUE.lower()
    assert "NEW_HYPOTHESIS_REQUIRED" in T.VERDICT_HETEROGENEITY_PRESENT


def test_primary_evaluator_has_no_path_into_the_secondary():
    """No flag, no branch, no import -- checked over the IMPORT GRAPH.

    The primary's docstring says heterogeneity lives elsewhere, so a text scan
    fails on a module that is behaving correctly. What matters is that the
    primary never imports or calls into the secondary.
    """
    import ast, inspect
    tree = ast.parse(inspect.getsource(E))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            if n.module:
                imported.add(n.module)
            imported |= {a.name for a in n.names}
    assert not any("tte" in i.lower() or "heterogen" in i.lower()
                   for i in imported), f"primary imports {imported}"
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not any("heterogen" in n.lower() for n in names)
    # the dependency runs one way only: secondary -> primary
    sec = ast.parse(inspect.getsource(T))
    sec_imports = {n.module for n in ast.walk(sec)
                   if isinstance(n, ast.ImportFrom) and n.module}
    assert any("evaluate" in m for m in sec_imports), (
        "the secondary should reuse the primary's frozen split discipline")


def test_no_individual_bin_significance_is_ever_claimed():
    out = T.evaluate(spread_corpus())
    for cell in out["omnibus"].values():
        assert "passes_fdr" in cell                      # cell-level only
        for b in T.FROZEN_BINS:
            assert f"{b}_passes" not in cell
            assert f"{b}_p_value" not in cell


# --- separate BH family ------------------------------------------------------

def test_bh_is_its_own_family_at_the_same_q():
    assert T.FDR_Q == E.FDR_Q == 0.10
    out = T.evaluate(spread_corpus())
    assert out["family_size"] == 12
    assert out["family"] == "SECONDARY"


def test_null_data_does_not_manufacture_heterogeneity():
    """All five bins drawn from one distribution -> omnibus should not fire."""
    out = T.evaluate(spread_corpus())
    cell = out["omnibus"][out["primary_heterogeneity_cell"]]
    if cell["p_value"] is not None:
        assert cell["p_value"] > 0.01, (
            "an omnibus fired on exchangeable bins; the null distribution is "
            "not being built correctly")
