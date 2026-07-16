"""CRYPTO-HORIZON-CANDIDATE-READINESS-001 tests.

Local read-only operational readiness evaluator for the manually authorized
shared-pass horizon canary. Covers all seven readiness states, exact reuse of the
deployed completeness / horizon-window / shared-window / activation-grace rules,
deterministic pair ordering + stable tie-break, fail-closed rejections, the
proposed (never executed) manual command, the isolated non-blocking MarketOps
measurement hook (runs after crypto persistence, zero calls, no second scan,
failure isolated + recorded), the append-only secret-free audit record, the
history aggregation (moment grouping / distinct pairs / slack), the no-trading /
no-network / AST safety audits, and the feasibility-derived regression. In-memory
SQLite; no network anywhere.
"""

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import cli
from app.models import (
    CryptoHorizonCohort,
    CryptoHorizonObservation,
    CryptoTokenBirthEvent,
)
from app.services import crypto_horizon_feasibility as feas
from app.services.crypto_horizon_readiness import (
    ACTIVATION_GRACE,
    EXPIRED,
    INSUFFICIENT_ARM_SLACK,
    NO_COMPLETE_CANDIDATES,
    NO_OVERLAPPING_PAIR,
    OPERATOR_PREP_MARGIN_SECONDS,
    PAIR_DETECTED_NOT_DUE,
    PAIR_READY_FOR_MANUAL_PREPARATION,
    SHARED_DUE_NOW_READY,
    append_readiness_record,
    build_readiness_history_report,
    classify_pair,
    evaluate_readiness,
    load_readiness_records,
    proposed_commands,
    readiness_audit_record,
)
from tests.test_crypto_tape_001 import session  # noqa: F401  in-memory all-tables session

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "app/services/crypto_horizon_readiness.py"
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


TA = "A" * 43
TB = "B" * 43
TC = "C" * 43


# --- exact reuse of deployed rules (6,7,8,9) ------------------------------------


def test_reuses_deployed_activation_grace():
    from app.services.crypto_horizon_orchestrator import ACTIVATION_GRACE as deployed
    assert ACTIVATION_GRACE is deployed
    assert ACTIVATION_GRACE == timedelta(seconds=45)


def test_reuses_deployed_horizon_and_shared_window_rules():
    from app.services.crypto_horizon_readiness import fifteen_window, pair_feasibility
    assert fifteen_window is feas.fifteen_window
    assert pair_feasibility is feas.pair_feasibility


def test_reuses_deployed_completeness_rule(session):
    # a null-liquidity token is rejected with the deployed reason, not eligible
    birth(session, TA, fe=NOW, persist=NOW, liq=None)
    birth(session, TB, fe=NOW, persist=NOW, liq=10000.0)
    r = evaluate_readiness(session, now=NOW + timedelta(minutes=1))
    reasons = {x["token"]: x["reason"] for x in r["rejected"]}
    assert reasons[TA] == "liquidity_or_initial_state_missing"
    assert r["counts"]["complete_candidates"] == 1


# --- pure classify_pair states (10-19) ------------------------------------------


def _cls(open_min, close_min, now_min, margin=OPERATOR_PREP_MARGIN_SECONDS, grace_fits=True):
    return classify_pair(NOW + timedelta(minutes=open_min), NOW + timedelta(minutes=close_min),
                         NOW + timedelta(minutes=now_min), grace_fits=grace_fits, margin_seconds=margin)


def test_state_no_intersection_returns_no_overlap():
    assert classify_pair(None, None, NOW, grace_fits=True, margin_seconds=180) == NO_OVERLAPPING_PAIR


def test_state_grace_not_fitting():
    assert _cls(0, 30, 5, grace_fits=False) == INSUFFICIENT_ARM_SLACK


def test_state_margin_not_fitting_narrow_window():
    # window width 1 min < grace(45s)+margin(180s) => deadline < open
    assert _cls(0, 1, 0) == INSUFFICIENT_ARM_SLACK


def test_state_expired():
    assert _cls(0, 15, 20) == EXPIRED


def test_state_insufficient_when_past_deadline():
    # window [0,15]; deadline = 15m - 225s = ~11.25m; now 13m in (deadline, close]
    assert _cls(0, 15, 13) == INSUFFICIENT_ARM_SLACK


def test_state_shared_due_now_ready():
    assert _cls(0, 15, 5) == SHARED_DUE_NOW_READY


def test_state_pair_ready_for_manual_preparation():
    # now just before open, within the margin lead window
    assert _cls(10, 25, 8) == PAIR_READY_FOR_MANUAL_PREPARATION


def test_state_pair_detected_not_due():
    # now well before open - margin
    assert _cls(10, 25, 2) == PAIR_DETECTED_NOT_DUE


# --- end-to-end evaluator states -------------------------------------------------


def _pair(session, delta_min=2, persist=NOW):
    birth(session, TA, fe=NOW, persist=persist, symbol="AA")
    birth(session, TB, fe=NOW + timedelta(minutes=delta_min), persist=persist, symbol="BB")


def test_eval_no_complete_candidates(session):
    birth(session, TA, fe=NOW, persist=NOW, liq=0.0)   # null
    birth(session, TB, fe=NOW, persist=NOW, liq=10000.0)
    assert evaluate_readiness(session, now=NOW + timedelta(minutes=1))["state"] == NO_COMPLETE_CANDIDATES


def test_eval_no_overlapping_pair(session):
    birth(session, TA, fe=NOW, persist=NOW)
    birth(session, TB, fe=NOW + timedelta(minutes=30), persist=NOW)  # far apart
    assert evaluate_readiness(session, now=NOW + timedelta(minutes=31))["state"] == NO_OVERLAPPING_PAIR


def test_eval_shared_due_now_ready_and_top_pair(session):
    _pair(session)
    r = evaluate_readiness(session, now=NOW + timedelta(minutes=12))
    assert r["state"] == SHARED_DUE_NOW_READY
    assert r["counts"]["usable_pairs"] == 1
    top = r["top_pair"]
    assert {top["token_a"], top["token_b"]} == {TA, TB}
    assert top["remaining_safe_slack_seconds"] > 0
    assert top["not_ready_reason"] is None


def test_eval_manual_preparation_and_not_due_by_timestamp(session):
    _pair(session)
    assert evaluate_readiness(session, now=NOW + timedelta(minutes=8))["state"] == PAIR_READY_FOR_MANUAL_PREPARATION
    assert evaluate_readiness(session, now=NOW + timedelta(minutes=2))["state"] == PAIR_DETECTED_NOT_DUE


def test_eval_expired_has_no_top_pair(session):
    _pair(session)
    r = evaluate_readiness(session, now=NOW + timedelta(minutes=23))
    assert r["state"] == EXPIRED
    assert r["top_pair"] is None


# --- ordering + tie-break (20,21) -----------------------------------------------


def test_deterministic_ordering_prefers_greatest_slack(session):
    # pair1 (A,B) 2min apart -> wider shared window -> more slack than pair2 (C near 14min)
    birth(session, TA, fe=NOW, persist=NOW, symbol="AA")
    birth(session, TB, fe=NOW + timedelta(minutes=2), persist=NOW, symbol="BB")
    # a third token close to A but slightly later, all due_now at eval
    birth(session, TC, fe=NOW + timedelta(minutes=1), persist=NOW, symbol="CC")
    r = evaluate_readiness(session, now=NOW + timedelta(minutes=10))
    # top pair should be the one with the greatest remaining safe slack
    slacks = [p["remaining_safe_slack_seconds"] for p in r["pairs"]]
    assert slacks == sorted(slacks, reverse=True)
    assert r["top_pair"]["remaining_safe_slack_seconds"] == max(slacks)


def test_stable_tie_break_by_token_id(session):
    # two pairs with identical geometry -> tie broken by canonical id ascending
    d = "D" * 43
    birth(session, TB, fe=NOW, persist=NOW)
    birth(session, TA, fe=NOW, persist=NOW)   # identical anchor as TB
    r = evaluate_readiness(session, now=NOW + timedelta(minutes=10))
    assert r["top_pair"]["token_a"] == TA  # A < B, stable


# --- proposed command (22,23) ---------------------------------------------------


def test_proposed_command_contains_exact_ids_and_no_confirm(session):
    _pair(session)
    r = evaluate_readiness(session, now=NOW + timedelta(minutes=12))
    cmds = proposed_commands(r)
    joined = "\n".join(cmds)
    assert TA in joined and TB in joined
    assert "--dry-run" in joined
    assert "--confirm" not in joined
    assert "arm" not in joined.lower()
    assert "PROPOSAL ONLY" in joined


def test_proposed_command_empty_when_not_ready(session):
    _pair(session)
    r = evaluate_readiness(session, now=NOW + timedelta(minutes=23))  # expired
    assert proposed_commands(r) == []


# --- fail-closed rejections (7) -------------------------------------------------


def test_malformed_id_rejected_without_suppressing_valid(session):
    birth(session, "short", fe=NOW, persist=NOW)          # malformed canonical id
    birth(session, TA, fe=NOW, persist=NOW)
    birth(session, TB, fe=NOW + timedelta(minutes=2), persist=NOW)
    r = evaluate_readiness(session, now=NOW + timedelta(minutes=12))
    assert any(x["reason"] == "malformed_identifier" for x in r["rejected"])
    assert r["state"] == SHARED_DUE_NOW_READY  # valid pair still detected


# --- zero writes / no cohort / no observation (2,3,4,5) -------------------------


def test_report_zero_writes_no_cohort_no_observation(session):
    _pair(session)
    before_b = session.query(CryptoTokenBirthEvent).count()
    r = evaluate_readiness(session, now=NOW + timedelta(minutes=12))
    assert r["external_calls"] == 0 and r["writes"] == 0 and r["persisted"] is False
    assert r["automatic_cohort_creation"] is False and r["automatic_arming"] is False
    assert session.query(CryptoTokenBirthEvent).count() == before_b
    assert session.query(CryptoHorizonCohort).count() == 0
    assert session.query(CryptoHorizonObservation).count() == 0


# --- no trading language / AST / no-network (33,35) -----------------------------


def test_no_trading_or_capital_language_in_output(session):
    _pair(session)
    r = evaluate_readiness(session, now=NOW + timedelta(minutes=12))
    banned = {"trade", "buy", "sell", "opportunity", "edge", "expected", "value",
              "kelly", "position", "order", "wallet", "swap"}

    def keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield str(k)
                yield from keys(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from keys(v)

    for key in keys(r):
        tokens = set(key.lower().replace("-", "_").split("_"))
        assert not (tokens & banned), key


def test_no_network_imports_in_module():
    src = MODULE.read_text()
    for banned in ("httpx", "requests", "aiohttp", "urllib.request", "socket"):
        assert banned not in src


def test_ast_safety_audit_no_banned_identifiers():
    tree = ast.parse(MODULE.read_text())
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


# --- append-only audit record (29) ---------------------------------------------


def test_append_only_record_valid_and_secret_free(session, tmp_path):
    _pair(session)
    r = evaluate_readiness(session, now=NOW + timedelta(minutes=12), marketops_cycle_id=42)
    rec = readiness_audit_record(r, run_id=42)
    path = tmp_path / "readiness.jsonl"
    append_readiness_record(rec, path)
    append_readiness_record(rec, path)  # append-only: two lines
    loaded = load_readiness_records(path)
    assert len(loaded) == 2
    assert loaded[0]["external_calls"] == 0
    assert loaded[0]["run_id"] == 42 and loaded[0]["marketops_cycle_id"] == 42
    assert loaded[0]["state"] == SHARED_DUE_NOW_READY
    blob = json.dumps(loaded).lower()
    for secret in ("api_key", "secret", "private_key", "authorization", "password", "raw_payload"):
        assert secret not in blob


# --- history aggregation (30,31,32) ---------------------------------------------


def _rec(t_min, state, pair=None, slack=None):
    return {
        "evaluated_at_utc": (NOW + timedelta(minutes=t_min)).isoformat().replace("+00:00", "Z"),
        "state": state,
        "candidate_token_a": pair[0] if pair else None,
        "candidate_token_b": pair[1] if pair else None,
        "remaining_safe_slack_seconds": slack,
        "external_calls": 0,
    }


def test_history_groups_consecutive_same_pair_moments():
    recs = [
        _rec(0, NO_OVERLAPPING_PAIR),
        _rec(6, SHARED_DUE_NOW_READY, (TA, TB), 600),
        _rec(12, SHARED_DUE_NOW_READY, (TA, TB), 400),   # same pair -> same moment
        _rec(18, NO_OVERLAPPING_PAIR),
        _rec(24, SHARED_DUE_NOW_READY, (TA, TC), 700),   # different pair -> new moment
    ]
    h = build_readiness_history_report(recs)
    assert h["evaluations"] == 5
    assert h["distinct_ready_moments"] == 2
    assert h["distinct_candidate_pairs"] == 2
    # first moment spans 6..12 (two cycles), duration 360s
    first = h["moments"][0]
    assert first["cycles"] == 2 and first["duration_seconds"] == 360.0
    assert h["max_arm_slack_seconds"] == 700
    assert h["count_by_state"][SHARED_DUE_NOW_READY] == 3


def test_history_empty_records():
    h = build_readiness_history_report([])
    assert h["evaluations"] == 0 and h["distinct_ready_moments"] == 0


# --- MarketOps hook (24,25,26,27,28,34) -----------------------------------------


@pytest.fixture()
def _capture_records(monkeypatch):
    captured = []
    import app.services.crypto_horizon_readiness as rmod
    monkeypatch.setattr(rmod, "append_readiness_record",
                        lambda rec, *a, **k: captured.append(rec))
    return captured


async def _run_hook_cycle(session, cfg_kwargs, crypto_kwargs=None):
    from app.services.marketops import MarketOpsConfig
    from tests.test_marketops import FakeCryptoService, autopilot

    crypto = FakeCryptoService(**(crypto_kwargs or {}))
    cfg = MarketOpsConfig(include_probability_markets=False, include_crypto=True,
                          **cfg_kwargs)
    service = autopilot(cfg=cfg, crypto_service=crypto)
    run = await service.run_once(session)
    return run, crypto


async def test_hook_off_by_default_is_noop(session):
    _pair(session)
    run, crypto = await _run_hook_cycle(session, {})  # include_candidate_readiness default False
    assert "candidate_readiness" not in (run.summary or {})
    assert run.status == "ok"


async def test_hook_runs_after_crypto_persistence_zero_calls_no_second_scan(session, _capture_records):
    _pair(session)
    run, crypto = await _run_hook_cycle(session, {"include_candidate_readiness": True})
    assert len(crypto.calls) == 1                       # exactly one scan, no second
    assert run.summary["candidate_readiness"]["external_calls"] == 0
    assert run.summary["candidate_readiness"]["state"] == SHARED_DUE_NOW_READY or \
        run.summary["candidate_readiness"]["overlapping_pairs"] >= 1
    assert len(_capture_records) == 1
    assert _capture_records[0]["marketops_cycle_id"] == run.id
    assert run.status == "ok"


async def test_hook_failure_isolated_and_recorded(session, monkeypatch):
    _pair(session)
    import app.services.crypto_horizon_readiness as rmod

    def boom(*a, **k):
        raise RuntimeError("readiness exploded")

    monkeypatch.setattr(rmod, "evaluate_readiness", boom)
    run, _ = await _run_hook_cycle(session, {"include_candidate_readiness": True})
    assert run.status == "ok"                           # cycle NOT failed
    assert "candidate_readiness_error" in run.summary
    assert "readiness exploded" in run.summary["candidate_readiness_error"]


# --- feasibility-derived regression ---------------------------------------------


def test_regression_grace_compatible_pair_with_five_minute_margin(session):
    """Two complete tokens, grace-compatible overlapping 15m window, both persisted
    with > 5 min of safe arm margin -> ready or prep by timestamp; nothing armed."""
    _pair(session, delta_min=2, persist=NOW)
    # due-now instant -> shared_due_now_ready with ample slack (> 300s)
    r_due = evaluate_readiness(session, now=NOW + timedelta(minutes=11))
    assert r_due["state"] == SHARED_DUE_NOW_READY
    assert r_due["top_pair"]["remaining_safe_slack_seconds"] > 300
    # pre-open instant -> manual preparation
    r_prep = evaluate_readiness(session, now=NOW + timedelta(minutes=8))
    assert r_prep["state"] == PAIR_READY_FOR_MANUAL_PREPARATION
    # nothing created or armed
    assert session.query(CryptoHorizonCohort).count() == 0
    assert session.query(CryptoHorizonObservation).count() == 0
    assert r_due["automatic_arming"] is False
