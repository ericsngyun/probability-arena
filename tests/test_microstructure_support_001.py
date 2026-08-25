"""The blind support ledger: missingness only, provably.

Safe to run during a blind tranche because it CANNOT read a value, not because
it promises not to.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from app.microstructure import features as F
from app.microstructure import support as S
from app.microstructure.labels import HORIZONS_S
from tests.test_microstructure_evaluate_001 import corpus, make_row


# --- blindness, structurally -------------------------------------------------

def test_presence_mask_carries_no_magnitudes():
    fields = set(S.PresenceMask.__dataclass_fields__)
    assert fields == {"session_id", "cluster", "m0_complete", "m1_complete",
                      "label_available"}
    m = S.presence_of(make_row("s", "MKT", 0))
    assert isinstance(m.m0_complete, bool)
    assert all(isinstance(v, bool) for v in m.label_available.values())


def test_values_are_only_ever_tested_for_none():
    """The projection may ask IF a value exists, never WHAT it is."""
    tree = ast.parse(inspect.getsource(S.presence_of))
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            ops = {type(o).__name__ for o in node.ops}
            assert ops <= {"Is", "IsNot", "In", "NotIn"}, (
                f"presence_of uses a value comparison {ops}; only identity "
                f"tests against None are permitted")


def test_the_ledger_never_does_arithmetic_on_a_value():
    """Counting masks is fine; summing features or labels is not."""
    tree = ast.parse(inspect.getsource(S.support_ledger))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    # `m0_complete` / `m1_complete` are BOOLEAN presence flags, not values, so
    # a bare "m0_" substring flags a correct module. Allow the flags by name
    # and ban the value-bearing identifiers.
    allowed = {"m0_complete", "m1_complete", "m0_complete_rows",
               "m1_complete_rows", "joint_m0_evaluable_rows",
               "joint_m1_evaluable_rows"}
    for banned in ("value", "mid", "spread", "delta", "microprice",
                   "imbalance", "depth", "m1_flow", "signed_", "realized_vol"):
        hits = {n for n in names if banned in n.lower()} - allowed
        assert not hits, f"support ledger touches {hits}"


def test_output_contains_only_counts():
    out = S.support_ledger(corpus(n_sessions=2, n_markets=4, n_per=10))
    def leaves(o):
        if isinstance(o, dict):
            for v in o.values():
                yield from leaves(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                yield from leaves(v)
        else:
            yield o
    for v in leaves(out["sessions"]):
        assert isinstance(v, int), f"non-count leaked into the ledger: {v!r}"


# --- it measures the thing it claims to measure -----------------------------

def test_a_one_sided_book_is_not_m0_complete():
    """S03's actual failure mode: publishable but no midpoint."""
    r = make_row("s", "MKT", 0)
    r["m0"]["mid"] = None
    r["m0"]["spread"] = None
    m = S.presence_of(r)
    assert m.m0_complete is False
    assert m.m1_complete is False, "M1 requires M0 to be complete"


def test_an_unavailable_label_is_not_evaluable_at_that_horizon_only():
    r = make_row("s", "MKT", 0)
    r["labels"]["300"] = {"horizon_s": 300, "value": None,
                          "available": False, "reason": "x"}
    m = S.presence_of(r)
    assert m.label_available[300] is False
    assert m.label_available[30] is True, "other horizons are unaffected"


def test_joint_evaluability_requires_both_features_and_a_label():
    rows = [make_row("s", "MKT", i) for i in range(4)]
    rows[0]["m0"]["mid"] = None                                  # no features
    rows[1]["labels"]["30"] = {"horizon_s": 30, "value": None,
                               "available": False, "reason": "x"}  # no label
    out = S.support_ledger(rows)["sessions"]["s"]["by_horizon"]["30"]
    assert out["label_available_rows"] == 3
    assert out["joint_M0_evaluable_rows"] == 2, (
        "a row needs BOTH complete features and an available label")


def test_ledger_counts_reconcile_with_row_counts():
    rows = corpus(n_sessions=2, n_markets=5, n_per=8)
    out = S.support_ledger(rows)
    assert sum(v["rows_emitted"] for v in out["sessions"].values()) == len(rows)


def test_it_changes_no_frozen_floor():
    from app.microstructure import evaluate as E
    assert E.MIN_TEST_ROWS == 200 and E.MIN_TEST_CLUSTERS == 20
    out = S.support_ledger(corpus(n_sessions=2, n_markets=3, n_per=5))
    assert out["changes_no_floor"] is True
    for k in ("verdict", "underpowered", "passes", "fdr"):
        assert not any(k in str(key).lower() for key in out)


def test_support_ledger_is_not_reachable_from_the_evaluator():
    """A diagnostic must not become an input to the frozen test.

    Checked over the IMPORT GRAPH. `evaluate.py` says "support" in prose --
    "a cell without this much support", "the corpus cannot support" -- so a
    text scan condemns a module that is behaving correctly.
    """
    from app.microstructure import evaluate as E
    tree = ast.parse(inspect.getsource(E))
    mods = {n.module for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
             for a in n.names}
    assert not any("support" in m for m in mods), mods
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not any("support_ledger" in n for n in names)
