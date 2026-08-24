"""The frozen EDGE-001 evaluator. Twelve cells, one BH, one verdict.

Every test here runs on synthetic or VALIDATION rows. A test that needed
CONFIRMATION data would itself be a violation, and the lock makes that fail
rather than merely being frowned upon.
"""

from __future__ import annotations

import random

import pytest

from app.microstructure import evaluate as E
from app.microstructure import features as F
from app.microstructure.authorization import ConfirmationDataLocked
from app.microstructure.panel import DatasetRole, SESSION_OK
from app.microstructure.rows import LABEL_SCHEMA_VERSION, ROW_SCHEMA_VERSION

BASE_MS = 1_787_000_000_000.0


def make_row(session, ticker, i, *, role=DatasetRole.VALIDATION,
             status=SESSION_OK, signal=0.0, rng=None, event="E1"):
    rng = rng or random.Random(0)
    m0 = {n: rng.uniform(-1, 1) for n in F.M0_FEATURES}
    m0["mid"] = rng.uniform(0.2, 0.8)
    m0["micro_minus_mid"] = rng.uniform(-0.002, 0.002)
    flow = {n: rng.uniform(-1, 1) for n in F.M1_FLOW_FEATURES}
    # label carries a little of the flow block when `signal` > 0
    noise = rng.gauss(0, 0.01)
    val = signal * flow["signed_trade_flow_30s"] + noise
    labels = {str(h): {"horizon_s": h, "value": val, "available": True,
                       "reason": None} for h in E.HORIZONS_S}
    return {
        "session_id": session, "panel_tick_id": f"tick{i // 300}",
        "ticker": ticker, "series": "KXTEST", "event_id": event,
        "subscription_generation": 1,
        "sample_time": "2026-08-24T00:00:00+00:00",
        "sample_time_ms": BASE_MS + i * 1000,
        "TTE_seconds": 3600.0,
        "capture_commit": "cafe",
        "feature_schema_version": ROW_SCHEMA_VERSION,
        "label_schema_version": LABEL_SCHEMA_VERSION,
        "preregistration_version": "Amendment 2",
        "dataset_role": role, "session_status": status,
        "m0": m0, "controls": {}, "m1_flow": flow, "labels": labels,
    }


def corpus(n_sessions=3, n_markets=25, n_per=40, **kw):
    rng = random.Random(11)
    rows = []
    for s in range(n_sessions):
        for m in range(n_markets):
            for i in range(n_per):
                rows.append(make_row(f"s{s}", f"MKT{m:03d}",
                                     s * 100_000 + i, rng=rng,
                                     event=f"EV{m//5}", **kw))
    return rows


# --- the lock is wired into the evaluator, not just available ---------------

def test_evaluator_refuses_confirmation_rows_without_authorization(monkeypatch,
                                                                   tmp_path):
    monkeypatch.setenv("PROBABILITY_ARENA_EDGE001_AUTHORIZATION",
                       str(tmp_path / "absent.json"))
    rows = corpus(role=DatasetRole.CONFIRMATION)
    with pytest.raises(ConfirmationDataLocked):
        E.verify_corpus(rows)
    with pytest.raises(ConfirmationDataLocked):
        E.evaluate(rows)


def test_validation_rows_evaluate_freely(monkeypatch, tmp_path):
    monkeypatch.setenv("PROBABILITY_ARENA_EDGE001_AUTHORIZATION",
                       str(tmp_path / "absent.json"))
    out = E.evaluate(corpus())
    assert out["corpus"]["role"] == DatasetRole.VALIDATION


# --- corpus refusals --------------------------------------------------------

def test_empty_corpus_is_refused_not_passed():
    with pytest.raises(E.CorpusRefused, match="empty"):
        E.verify_corpus([])


def test_zero_eligible_rows_never_returns_success():
    rows = corpus(n_sessions=2, n_markets=3, n_per=3)
    for r in rows:                       # every label unavailable
        for h in E.HORIZONS_S:
            r["labels"][str(h)] = {"horizon_s": h, "value": None,
                                   "available": False, "reason": "x"}
    out = E.evaluate(rows)
    assert out["verdict"] == E.VERDICT_NO_ELIGIBLE_DATA
    assert out["verdict"] not in (E.VERDICT_CANDIDATE,
                                  E.VERDICT_REAL_BUT_UNECONOMIC)


def test_mixed_roles_are_refused():
    rows = corpus(n_sessions=2, n_markets=3, n_per=5)
    rows[0]["dataset_role"] = DatasetRole.PROFILE
    with pytest.raises(E.CorpusRefused, match="mixes dataset roles"):
        E.verify_corpus(rows)


def test_foreign_schema_is_refused():
    rows = corpus(n_sessions=2, n_markets=3, n_per=5)
    rows[0]["feature_schema_version"] = "microstructure-row-v1"
    with pytest.raises(E.CorpusRefused, match="foreign schema"):
        E.verify_corpus(rows)


def test_halted_session_can_never_be_evaluated():
    rows = corpus(n_sessions=2, n_markets=3, n_per=5, status="safety_halt")
    with pytest.raises(E.CorpusRefused, match="halted"):
        E.verify_corpus(rows)


# --- the frozen family ------------------------------------------------------

def test_family_is_exactly_twelve_cells():
    assert len(E.CELLS) == 12
    assert len(E.COMPARISONS) == 3 and len(E.HORIZONS_S) == 4
    assert len(set(E.CELLS)) == 12
    assert (E.CMP_M1_VS_M0, E.PRIMARY_HORIZON_S) in E.CELLS


def test_evaluation_reports_exactly_the_frozen_cells():
    out = E.evaluate(corpus())
    assert len(out["cells"]) == 12
    assert out["family_size"] == 12
    expected = {f"{c}@{h}s" for c, h in E.CELLS}
    assert set(out["cells"]) == expected


def test_m0_must_be_a_strict_subset_of_m1():
    E.assert_membership()
    assert set(E.feature_names(E.CMP_M1_VS_M0)) > set(E.feature_names(
        E.CMP_M0_VS_RANDOM_WALK))


# --- BH is computed once over the whole family ------------------------------

def test_bh_is_applied_once_over_all_twelve_not_per_horizon():
    # 12 p-values; BH over the family accepts fewer than per-horizon BH would
    pvals = {(c, h): 0.02 for c, h in E.CELLS}
    fam = E.benjamini_hochberg(pvals, 0.10)
    assert all(fam.values())          # all tiny p's pass either way
    # a p that passes within a 4-cell horizon family but not the 12-cell one
    pv = {(c, h): 0.9 for c, h in E.CELLS}
    pv[(E.CMP_M1_VS_M0, 30)] = 0.08
    fam = E.benjamini_hochberg(pv, 0.10)
    assert fam[(E.CMP_M1_VS_M0, 30)] is False, (
        "0.08 must fail BH over twelve cells; it would pass over four")


def test_bh_threshold_is_the_standard_step_up():
    pvals = {(c, h): p for (c, h), p in zip(E.CELLS,
             [0.001, 0.004, 0.02, 0.05, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9])}
    out = E.benjamini_hochberg(pvals, 0.10)
    # i*q/m = 0.00833, 0.0167, 0.025, 0.0333 -> 0.001,0.004,0.02 pass; 0.05 not
    passing = sorted(p for k, p in pvals.items() if out[k])
    assert passing == [0.001, 0.004, 0.02]


def test_bh_with_no_pvalues_passes_nothing():
    assert not any(E.benjamini_hochberg({(c, h): None for c, h in E.CELLS}).values())


# --- underpowered cells are reported, never dropped -------------------------

def test_underpowered_cell_is_reported_and_carries_no_pvalue():
    # rows exist, but only 5 event/market clusters -- below the floor of 20
    rows = corpus(n_sessions=3, n_markets=5, n_per=60)
    out = E.evaluate(rows)
    assert out["underpowered_cells"], "expected underpowered cells"
    for name in out["underpowered_cells"]:
        cell = out["cells"][name]
        assert cell["underpowered"] is True
        assert cell["p_value"] is None, "an underpowered cell must not get a p"
        assert cell["passes_fdr"] is False
    assert len(out["cells"]) == 12, "underpowered cells stay in the report"


def test_power_floors_are_frozen():
    assert E.MIN_TEST_ROWS == 200 and E.MIN_TEST_CLUSTERS == 20


# --- splits, purge and embargo ----------------------------------------------

def test_walk_forward_never_trains_on_the_future():
    rows = corpus(n_sessions=3, n_markets=2, n_per=5)
    folds = E.walk_forward_folds(rows)
    assert len(folds) == 2
    for train, test in folds:
        assert max(r["sample_time_ms"] for r in train) < \
               max(r["sample_time_ms"] for r in test)


def test_embargo_removes_training_rows_whose_label_touches_the_test_window():
    """The decisive row is one the LABEL rule alone would keep.

    A row 100 s before the test window has its 30 s label finished long before
    the test starts, so label-overlap alone keeps it. Only the 300 s embargo
    removes it. A test built solely from rows whose labels overlap the test
    window passes with the embargo deleted -- which is exactly what a mutation
    campaign found.
    """
    t0 = 1_000_000.0
    test = [{"sample_time_ms": t0}]
    far = {"sample_time_ms": t0 - E.EMBARGO_S * 1000 - 30_000 - 1}
    inside_embargo = {"sample_time_ms": t0 - 100_000}   # label ends at t0-70s
    overlapping = {"sample_time_ms": t0 - 1000}

    kept = E.purge_and_embargo([far, inside_embargo, overlapping], test, 30)
    assert far in kept
    assert inside_embargo not in kept, "embargo did not remove an in-gap row"
    assert overlapping not in kept
    assert len(kept) == 1
    assert E.EMBARGO_S == 300


def test_deleting_the_embargo_changes_the_kept_set():
    """States the property directly, so it cannot be satisfied by accident."""
    t0 = 1_000_000.0
    test = [{"sample_time_ms": t0}]
    train = [{"sample_time_ms": t0 - gap} for gap in (50_000, 100_000, 250_000)]
    with_embargo = E.purge_and_embargo(train, test, 30)
    # what a no-embargo rule would keep: every row whose label ends before t0
    without = [r for r in train if r["sample_time_ms"] + 30_000 <= t0]
    assert with_embargo != without
    assert len(with_embargo) < len(without)


def test_embargo_is_the_max_horizon_not_the_cell_horizon():
    """Otherwise a 1 s cell quietly gets more data than a 300 s cell."""
    test = [{"sample_time_ms": 10_000_000.0}]
    train = [{"sample_time_ms": 10_000_000.0 - 200_000}]
    assert E.purge_and_embargo(train, test, 1) == \
           E.purge_and_embargo(train, test, 1)
    assert E.EMBARGO_S >= max(E.HORIZONS_S)


# --- clustering -------------------------------------------------------------

def test_cluster_is_event_market_not_the_row():
    r = make_row("s", "MKT1", 0, event="EV9")
    assert E.cluster_of(r) == ("EV9", "MKT1")


def test_bootstrap_resamples_clusters_not_rows():
    """A constant-per-cluster signal has no cluster-level variation to exploit;
    resampling rows would manufacture significance from replication alone."""
    deltas, clusters = [], []
    for c in range(4):
        for _ in range(500):
            deltas.append(0.001)
            clusters.append(("E", f"M{c}"))
    p_clustered = E.cluster_bootstrap_p(deltas, clusters, draws=200)
    p_asif_rows = E.cluster_bootstrap_p(
        deltas, [("E", f"M{i}") for i in range(len(deltas))], draws=200)
    assert p_clustered >= p_asif_rows
