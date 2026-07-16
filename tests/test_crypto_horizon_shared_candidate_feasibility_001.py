"""CRYPTO-HORIZON-SHARED-CANDIDATE-FEASIBILITY-001 tests.

Read-only local feasibility measurement for the horizon shared-pass canary.
Covers history-coverage reporting, the completeness+arming funnel, null-liquidity
and initial pair/price classification, 15m feasibility at persistence, the safe
due-now arm deadline, two-token shared-window intersection (overlap / non-overlap
/ grace-not-fitting / persisted-too-late / identical-birth / close-but-distinct),
multiple evidence sources, no anchor double-counting, and the hard guarantees:
zero provider calls, zero writes, no cohort/observation creation, no trading
surface, plus the AST + canonical safety audits over the new module. In-memory
SQLite; no network anywhere.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import cli
from app.models import (
    CryptoHorizonCohort,
    CryptoHorizonObservation,
    CryptoTokenBirthEvent,
)
from app.services.crypto_horizon_feasibility import (
    ACTIVATION_GRACE,
    build_feasibility_report,
    fifteen_window,
    pair_feasibility,
    safe_arm_deadline,
    shared_fifteen,
)
from tests.test_crypto_tape_001 import session  # noqa: F401  in-memory session fixture

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def birth(session, token, *, fe, persist, symbol="TOK", source="dexscreener:profile",
          dex="pumpswap", pair="P" * 20, price=1e-5, liq=10000.0):
    row = CryptoTokenBirthEvent(
        chain="solana", token_address=token, symbol=symbol, launch_source=source,
        first_dex_id=dex, first_pair_address=pair, initial_price_usd=price,
        initial_liquidity_usd=liq, first_evidence_at=fe, observed_at=fe, created_at=persist,
    )
    session.add(row)
    session.flush()
    return row


# --- pure geometry --------------------------------------------------------------


def test_fifteen_window_matches_deployed_tolerance():
    anchor = NOW
    open_, close = fifteen_window(anchor)
    # target = anchor + 15m; window = target +/- 7.5m
    assert open_ == anchor + timedelta(minutes=7.5)
    assert close == anchor + timedelta(minutes=22.5)


def test_safe_arm_deadline_subtracts_grace_and_margin():
    close = NOW + timedelta(minutes=22.5)
    assert safe_arm_deadline(close, 0.0) == close - ACTIVATION_GRACE
    assert safe_arm_deadline(close, 120.0) == close - ACTIVATION_GRACE - timedelta(seconds=120)


def test_two_token_shared_window_intersection_overlap():
    a = NOW
    b = NOW + timedelta(minutes=5)  # windows overlap
    inter = shared_fifteen(a, b)
    assert inter is not None
    lo, hi = inter
    assert lo == fifteen_window(b)[0]   # max of opens
    assert hi == fifteen_window(a)[1]   # min of closes


def test_non_overlapping_pair_rejected():
    a = NOW
    b = NOW + timedelta(minutes=20)  # 20min apart > 15m window width -> disjoint
    assert shared_fifteen(a, b) is None


def test_identical_birth_pair_full_overlap():
    inter = shared_fifteen(NOW, NOW)
    assert inter == fifteen_window(NOW)


def test_close_but_nonidentical_birth_pair_partial_overlap():
    a, b = NOW, NOW + timedelta(minutes=3)
    lo, hi = shared_fifteen(a, b)
    assert (hi - lo) == timedelta(minutes=12)  # 15m - 3m separation


def test_grace_not_fitting_when_intersection_too_narrow():
    # separation 14m59s -> shared width 1s < 45s grace
    a = NOW
    b = NOW + timedelta(minutes=15) - timedelta(seconds=1)
    pf = pair_feasibility(a, a, b, b, NOW)
    assert pf["overlap"] is True
    assert pf["grace_fits"] is False
    assert pf["usable"] is False


def test_pair_usable_when_persisted_in_time():
    a, b = NOW, NOW + timedelta(minutes=2)
    persist = NOW + timedelta(minutes=8)  # well before arm deadline
    pf = pair_feasibility(a, persist, b, persist, NOW)
    assert pf["overlap"] and pf["grace_fits"] and pf["shared_pass_eligible"]
    assert pf["usable"] is True


def test_candidate_persisted_too_late_is_unusable():
    a, b = NOW, NOW + timedelta(minutes=2)
    # shared 15m closes at min(close_a, close_b); persist after deadline
    late = NOW + timedelta(minutes=40)
    pf = pair_feasibility(a, late, b, late, NOW)
    assert pf["overlap"] is True
    assert pf["usable"] is False


# --- funnel + classification ----------------------------------------------------


def test_null_liquidity_classification_and_funnel(session):
    birth(session, "A" * 32, fe=NOW, persist=NOW + timedelta(minutes=5), liq=5000.0)
    birth(session, "B" * 32, fe=NOW, persist=NOW + timedelta(minutes=5), liq=None)   # missing
    birth(session, "C" * 32, fe=NOW, persist=NOW + timedelta(minutes=5), liq=0.0)    # null/zero
    r = build_feasibility_report(session, now=NOW + timedelta(hours=1))
    f = r["funnels"]["24h"]
    steps = {s["step"]: s["count"] for s in f["steps"]}
    assert steps["all_token_anchors"] == 3
    assert steps["positive_initial_liquidity"] == 1
    assert steps["complete_state_eligible"] == 1
    reasons = f["completeness_failure_reasons"]
    assert reasons.get("liquidity_or_initial_state_missing") == 1
    assert reasons.get("null_initial_liquidity") == 1
    assert reasons.get("complete") == 1


def test_missing_pair_and_price_classification(session):
    birth(session, "D" * 32, fe=NOW, persist=NOW, pair=None)
    birth(session, "E" * 32, fe=NOW, persist=NOW, price=None)
    r = build_feasibility_report(session, now=NOW + timedelta(hours=1))
    reasons = r["funnels"]["24h"]["completeness_failure_reasons"]
    assert reasons.get("invalid_pair") == 1
    assert reasons.get("missing_initial_price") == 1


def test_15m_feasibility_at_persistence(session):
    # in-window persist vs past-window persist
    birth(session, "F" * 32, fe=NOW, persist=NOW + timedelta(minutes=10), symbol="IN")
    birth(session, "G" * 32, fe=NOW, persist=NOW + timedelta(minutes=30), symbol="LATE")
    r = build_feasibility_report(session, now=NOW + timedelta(hours=1))
    steps = {s["step"]: s["count"] for s in r["funnels"]["24h"]["steps"]}
    assert steps["complete_state_eligible"] == 2
    assert steps["persisted_while_15m_feasible"] == 1   # only IN
    assert steps["persisted_with_safe_arm_margin"] == 1


def test_history_coverage_and_data_limited_ranges(session):
    birth(session, "H" * 32, fe=NOW - timedelta(days=2), persist=NOW - timedelta(days=2))
    birth(session, "I" * 32, fe=NOW, persist=NOW)
    r = build_feasibility_report(session, now=NOW + timedelta(minutes=1))
    cov = r["history_coverage"]
    assert cov["total_anchors"] == 2
    assert cov["anchor_span_days"] == 2.0
    ranges = {rr["range"]: rr["fully_covered_by_history"] for rr in cov["requested_ranges"]}
    assert ranges["24h"] is True        # <= 2d span
    assert ranges["7d"] is False        # exceeds 2d history -> data-limited
    assert ranges["30d"] is False


def test_multiple_evidence_sources_segmented(session):
    birth(session, "J" * 32, fe=NOW, persist=NOW, source="dexscreener:profile")
    birth(session, "K" * 32, fe=NOW, persist=NOW, source="dexscreener:boost")
    r = build_feasibility_report(session, now=NOW + timedelta(hours=1))
    names = {s["name"] for s in r["by_launch_source"]}
    assert names == {"dexscreener:profile", "dexscreener:boost"}


def test_no_double_counting_of_anchors(session):
    for i in range(5):
        birth(session, chr(65 + i) * 32, fe=NOW, persist=NOW)
    r = build_feasibility_report(session, now=NOW + timedelta(hours=1))
    assert r["funnels"]["24h"]["steps"][0]["count"] == 5
    # each anchor counted once across source segments
    assert sum(s["n"] for s in r["by_launch_source"]) == 5


def test_shared_window_usable_pair_end_to_end(session):
    # two complete tokens 2min apart, both persisted in-window -> 1 usable pair
    birth(session, "L" * 32, fe=NOW, persist=NOW + timedelta(minutes=8), symbol="LL")
    birth(session, "M" * 32, fe=NOW + timedelta(minutes=2), persist=NOW + timedelta(minutes=8), symbol="MM")
    r = build_feasibility_report(session, now=NOW + timedelta(hours=1))
    sw = r["shared_window"]
    assert sw["complete_pairs_in_neighborhood"] == 1
    assert sw["overlapping_15m_windows"] == 1
    assert sw["grace_compatible_shared_windows"] == 1
    assert sw["usable_pairs_persisted_in_time"] == 1
    assert sw["distinct_usable_moments"] == 1


def test_shared_window_late_persist_not_usable(session):
    birth(session, "N" * 32, fe=NOW, persist=NOW + timedelta(minutes=40), symbol="NN")
    birth(session, "O" * 32, fe=NOW + timedelta(minutes=2), persist=NOW + timedelta(minutes=40), symbol="OO")
    r = build_feasibility_report(session, now=NOW + timedelta(hours=1))
    sw = r["shared_window"]
    assert sw["overlapping_15m_windows"] == 1
    assert sw["usable_pairs_persisted_in_time"] == 0


# --- hard guarantees ------------------------------------------------------------


def test_zero_writes_and_no_cohort_or_observation_creation(session):
    birth(session, "P" * 32, fe=NOW, persist=NOW)
    before_births = session.query(CryptoTokenBirthEvent).count()
    before_cohorts = session.query(CryptoHorizonCohort).count()
    before_obs = session.query(CryptoHorizonObservation).count()
    r = build_feasibility_report(session, now=NOW + timedelta(hours=1))
    assert r["external_calls"] == 0 and r["writes"] == 0 and r["persisted"] is False
    assert session.query(CryptoTokenBirthEvent).count() == before_births
    assert session.query(CryptoHorizonCohort).count() == before_cohorts == 0
    assert session.query(CryptoHorizonObservation).count() == before_obs == 0


def test_report_dict_exposes_no_trading_or_capital_output(session):
    birth(session, "Q" * 32, fe=NOW, persist=NOW)
    r = build_feasibility_report(session, now=NOW + timedelta(hours=1))
    banned = {"expected", "value", "kelly", "position", "recommended", "side",
              "order", "wallet", "swap", "buy", "sell", "trade", "size", "sizing"}

    def keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield str(k)
                yield from keys(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from keys(v)

    # no structural field (key) contains a trading/capital token; the boundary
    # disclaimer prose in "note" legitimately names them and is excluded here.
    for key in keys(r):
        tokens = set(key.lower().replace("-", "_").split("_"))
        assert not (tokens & banned), key


def test_no_network_import_in_module():
    src = (REPO / "app/services/crypto_horizon_feasibility.py").read_text()
    for banned in ("httpx", "requests", "aiohttp", "urllib.request", "socket"):
        assert banned not in src


def test_ast_and_canonical_safety_audit():
    """AST-level: the module defines no banned trading identifier as a real name."""
    import ast

    src = (REPO / "app/services/crypto_horizon_feasibility.py").read_text()
    tree = ast.parse(src)
    banned = {"expected_value", "kelly", "position_size", "place_order", "submit_order",
              "create_order", "recommended_side", "trade_recommendation", "execute_trade"}
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert not (names & banned)


def test_cli_smoke_zero_calls(session, capsys):
    import asyncio

    birth(session, "R" * 32, fe=NOW, persist=NOW + timedelta(minutes=8))
    birth(session, "S" * 32, fe=NOW + timedelta(minutes=2), persist=NOW + timedelta(minutes=8))
    r = asyncio.run(cli.crypto_horizon_shared_candidate_feasibility_report(session=session))
    out = capsys.readouterr().out
    assert "shared-candidate feasibility" in out
    assert r["external_calls"] == 0 and r["writes"] == 0
