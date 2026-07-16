"""FORECAST-SCORABILITY-AUDIT-001 tests — read-only forecast-scorability audit.

Covers the per-forecast scorability state model (all states incl. stale-score
adversarial cases), the scorability funnel, cohort denominators + segmentation,
latency (incl. negative-duration findings and missing denominators),
representation deltas + thin-sample labels, deterministic verdict precedence,
bounded examples, --since/--until window handling + invalid-window rejection,
JSON/text parity, and the hard guarantees: zero provider calls, zero DB writes,
no outcome sync, no scoring, no MarketOps interaction, no trading vocabulary,
plus AST + no-network safety audits. In-memory SQLite; no network anywhere.
"""

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import (
    ForecastScoreRecord,
    Market,
    MarketForecastRecord,
    MarketOutcomeRecord,
    MarketResearchPacket,
    MarketResolutionAssessment,
)
from app.services.calibration import brier_score
from app.services import forecast_scorability as fs

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "app/services/forecast_scorability.py"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


_n = {"i": 0}


def mk(session, *, prob=0.7, depth="source_backed", risk="low", forecaster="template",
       version="v1", created=None, close=None, market=True, packet=True, res=True,
       domain="baseball", completeness=0.8, research_risk="low", res_risk="low",
       tradeability="researchable",
       outcome_status=None, winning_side=None, settled=None,
       score_status=None, score_brier="auto", score_created=None):
    """Seed one forecast (+ optional market/packet/resolution/outcome/score) with
    full field control. Returns the forecast row."""
    _n["i"] += 1
    ticker = f"KX-{_n['i']:04d}"
    created = created or NOW - timedelta(days=2)
    if market:
        session.add(Market(ticker=ticker, title="t", status="finalized", close_time=close))
    packet_id = res_id = None
    if packet:
        p = MarketResearchPacket(
            market_ticker=ticker, collector_name="template", collector_version="v1",
            domain=domain, research_completeness_score=completeness, research_risk=research_risk)
        session.add(p)
        session.flush()
        packet_id = p.id
    if res:
        r = MarketResolutionAssessment(
            market_ticker=ticker, model_name="rule", clarity_score=0.9,
            resolution_risk=res_risk, tradeability=tradeability)
        session.add(r)
        session.flush()
        res_id = r.id
    f = MarketForecastRecord(
        market_ticker=ticker, forecaster_name=forecaster, forecaster_version=version,
        estimated_probability=prob, confidence=0.6, evidence_depth=depth, forecast_risk=risk,
        research_packet_id=packet_id, resolution_assessment_id=res_id, created_at=created)
    session.add(f)
    session.flush()
    if outcome_status is not None:
        session.add(MarketOutcomeRecord(
            market_ticker=ticker, outcome_status=outcome_status, winning_side=winning_side,
            close_time=close, settled_time=settled))
    if score_status is not None:
        if score_brier == "auto":
            y = 1.0 if winning_side == "yes" else 0.0 if winning_side == "no" else None
            score_brier = brier_score(prob, y) if y is not None else None
        session.add(ForecastScoreRecord(
            forecast_id=f.id, market_ticker=ticker, score_status=score_status,
            brier_score=score_brier, was_resolved=(score_status == "scored"),
            created_at=score_created or (settled + timedelta(minutes=5) if settled else created)))
    session.commit()
    return f


def _report(session, **kw):
    kw.setdefault("now", NOW)
    return fs.build_scorability_report(session, **kw)


# --- classify_forecast: all states (reqs 2-15) ----------------------------------


def _classify(session, **kw):
    f = mk(session, **kw)
    outcome = session.execute(select(MarketOutcomeRecord).where(
        MarketOutcomeRecord.market_ticker == f.market_ticker)).scalar_one_or_none()
    score = session.execute(select(ForecastScoreRecord).where(
        ForecastScoreRecord.forecast_id == f.id).order_by(ForecastScoreRecord.id.desc())
    ).scalars().first()
    return fs.classify_forecast(f, outcome, score)


def test_no_outcome(session):
    assert _classify(session, outcome_status=None) == fs.PENDING_NO_OUTCOME


def test_open_outcome(session):
    assert _classify(session, outcome_status="open") == fs.PENDING_MARKET_OPEN


def test_closed_unsettled(session):
    assert _classify(session, outcome_status="closed") == fs.PENDING_MARKET_CLOSED_UNSETTLED


def test_settled_yes_no_score(session):
    assert _classify(session, outcome_status="settled", winning_side="yes",
                     settled=NOW - timedelta(hours=1)) == fs.SCORABLE_SCORE_MISSING


def test_settled_no_no_score(session):
    assert _classify(session, outcome_status="settled", winning_side="no",
                     settled=NOW - timedelta(hours=1)) == fs.SCORABLE_SCORE_MISSING


def test_settled_with_current_score(session):
    assert _classify(session, prob=0.7, outcome_status="settled", winning_side="yes",
                     settled=NOW - timedelta(hours=1), score_status="scored") == fs.SCORED_CURRENT


def test_settled_with_stale_pending_score(session):
    assert _classify(session, outcome_status="settled", winning_side="yes",
                     settled=NOW - timedelta(hours=1),
                     score_status="pending_outcome", score_brier=None) == fs.SCORABLE_SCORE_STALE


def test_settled_with_stale_unscorable_score(session):
    assert _classify(session, outcome_status="settled", winning_side="no",
                     settled=NOW - timedelta(hours=1),
                     score_status="unscorable", score_brier=None) == fs.SCORABLE_SCORE_STALE


def test_settled_scored_but_stale_value_flip(session):
    # outcome now settled NO (y=0) but the persisted scored row's brier reflects YES
    assert _classify(session, prob=0.7, outcome_status="settled", winning_side="no",
                     settled=NOW - timedelta(hours=1), score_status="scored",
                     score_brier=brier_score(0.7, 1.0)) == fs.SCORABLE_SCORE_STALE


def test_open_outcome_with_stale_scored_row(session):
    assert _classify(session, outcome_status="open", score_status="scored",
                     score_brier=0.09) == fs.PENDING_SCORE_STALE


def test_canceled_outcome(session):
    assert _classify(session, outcome_status="canceled") == fs.UNSCORABLE_CANCELED


def test_unknown_outcome(session):
    assert _classify(session, outcome_status="unknown") == fs.UNSCORABLE_UNKNOWN


def test_settled_void_missing_winner(session):
    assert _classify(session, outcome_status="settled", winning_side="void",
                     settled=NOW - timedelta(hours=1)) == fs.UNSCORABLE_VOID_OR_MISSING_WINNER


def test_unscorable_with_stale_scored_row(session):
    assert _classify(session, outcome_status="canceled", score_status="scored",
                     score_brier=0.09) == fs.UNSCORABLE_SCORE_STALE


def test_state_inconsistent_bad_status(session):
    assert _classify(session, outcome_status="garbage") == fs.STATE_INCONSISTENT


def test_latest_score_selected_from_history(session):
    # append-only: an old pending score then a newer scored row -> latest wins
    f = mk(session, prob=0.7, outcome_status="settled", winning_side="yes",
           settled=NOW - timedelta(hours=2), score_status="pending_outcome", score_brier=None)
    session.add(ForecastScoreRecord(forecast_id=f.id, market_ticker=f.market_ticker,
                                    score_status="scored", brier_score=brier_score(0.7, 1.0),
                                    was_resolved=True, created_at=NOW - timedelta(hours=1)))
    session.commit()
    r = _report(session, hours=240)
    assert r["state_histogram"].get(fs.SCORED_CURRENT) == 1
    # exactly one forecast, not double counted
    assert r["counts"]["forecasts"] == 1


# --- funnel + counts (req 16) ---------------------------------------------------


def test_full_funnel_counts(session):
    mk(session, outcome_status="settled", winning_side="yes", settled=NOW - timedelta(hours=1),
       score_status="scored")                                   # scored_current
    mk(session, outcome_status="open")                          # pending
    mk(session, outcome_status="settled", winning_side="yes",
       settled=NOW - timedelta(hours=1))                        # scorable_score_missing
    mk(session, market=False, packet=False, res=False, outcome_status=None)  # no metadata
    r = _report(session, hours=240)
    steps = {s["step"]: s["count"] for s in r["scorability_funnel"]["scorability_steps"]}
    assert steps["all_forecasts"] == 4
    assert steps["has_local_market_metadata"] == 3
    assert steps["has_research_packet"] == 3
    assert steps["outcome_settled_yes_no"] == 2
    assert steps["valid_scored_calibration_row"] == 1
    g = r["counts"]
    assert g["scored_current"] == 1
    assert g["legitimately_pending"] == 2  # open + the no-metadata no-outcome one
    assert g["scorable_backlog"] == 1


# --- cohorts (reqs 17-24) -------------------------------------------------------


def test_cohort_denominators_and_segmentation(session):
    mk(session, domain="baseball", depth="source_backed", forecaster="a", version="v1",
       risk="low", outcome_status="settled", winning_side="yes",
       settled=NOW - timedelta(hours=1), score_status="scored")
    mk(session, domain="soccer", depth="template_only", forecaster="b", version="v2",
       risk="high", outcome_status="open")
    r = _report(session, hours=240)
    doms = {c["name"]: c for c in r["cohorts"]["domain"]}
    assert doms["baseball"]["total"] == 1 and doms["baseball"]["scored_current"] == 1
    assert doms["soccer"]["total"] == 1 and doms["soccer"]["scored_current"] == 0
    # denominators reconcile
    assert sum(c["total"] for c in r["cohorts"]["domain"]) == r["counts"]["forecasts"]
    fc = {c["name"] for c in r["cohorts"]["forecaster"]}
    assert fc == {"a:v1", "b:v2"}
    dep = {c["name"] for c in r["cohorts"]["evidence_depth"]}
    assert dep == {"source_backed", "template_only"}
    comp = {c["name"] for c in r["cohorts"]["research_completeness"]}
    assert "0.75-0.89" in comp
    assert {c["name"] for c in r["cohorts"]["resolution_risk"]} == {"low"}
    assert {c["name"] for c in r["cohorts"]["tradeability"]} == {"researchable"}


def test_thin_sample_label(session):
    mk(session, outcome_status="settled", winning_side="yes",
       settled=NOW - timedelta(hours=1), score_status="scored")
    r = _report(session, hours=240)
    assert r["cohorts"]["domain"][0]["sample_label"] == "too_thin"


# --- latency (reqs 25-29) -------------------------------------------------------


def test_latency_forecast_to_settlement_and_close(session):
    mk(session, created=NOW - timedelta(days=3), close=NOW - timedelta(days=1),
       outcome_status="settled", winning_side="yes", settled=NOW - timedelta(hours=12),
       score_status="scored", score_created=NOW - timedelta(hours=11))
    r = _report(session, hours=240)
    lat = r["latency"]
    assert lat["creation_to_settlement"]["count"] == 1
    assert lat["creation_to_close"]["count"] == 1
    assert lat["close_to_settlement"]["count"] == 1
    assert lat["settlement_to_score"]["count"] == 1
    assert lat["settlement_to_score"]["median_s"] == pytest.approx(3600, abs=1)


def test_negative_duration_finding(session):
    # settled BEFORE forecast creation -> impossible/negative, reported not clamped
    mk(session, created=NOW - timedelta(hours=1), close=NOW - timedelta(hours=3),
       outcome_status="settled", winning_side="yes", settled=NOW - timedelta(hours=2))
    r = _report(session, hours=240)
    assert r["latency"]["creation_to_settlement"]["negative_findings"] == 1
    assert r["examples"]["impossible_timestamps"]  # surfaced


def test_missing_denominator_reported(session):
    mk(session, outcome_status=None)  # no settlement timestamp
    r = _report(session, hours=240)
    assert r["latency"]["creation_to_settlement"]["missing"] == 1
    assert r["latency"]["creation_to_settlement"]["count"] == 0


# --- representation (reqs 30-31) ------------------------------------------------


def test_representation_delta_and_thin_label(session):
    for _ in range(3):
        mk(session, domain="baseball", outcome_status="settled", winning_side="yes",
           settled=NOW - timedelta(hours=1), score_status="scored")
    mk(session, domain="soccer", outcome_status="open")
    r = _report(session, hours=240)
    dom = {d["name"]: d for d in r["representation"]["domain"]}
    # baseball is 75% of all but 100% of scored -> positive delta; soccer negative
    assert dom["baseball"]["representation_delta_pp"] > 0
    assert dom["soccer"]["representation_delta_pp"] < 0
    # small scored sample -> too_thin labels
    assert dom["baseball"]["label"] == "too_thin"


# --- verdict precedence (req 32) ------------------------------------------------


def test_verdict_insufficient_data(session):
    for _ in range(3):
        mk(session, outcome_status="open")
    assert _report(session, hours=240)["verdict"]["primary"] == fs.VERDICT_INSUFFICIENT


def test_verdict_immature(session):
    # 30 forecasts, markets close in the FUTURE -> not matured
    for _ in range(30):
        mk(session, close=NOW + timedelta(days=1), outcome_status="open")
    assert _report(session, hours=240)["verdict"]["primary"] == fs.VERDICT_IMMATURE


def test_verdict_scoring_backlog(session):
    # 30 matured settled forecasts, none scored -> scoring backlog
    for _ in range(30):
        mk(session, close=NOW - timedelta(days=1), outcome_status="settled", winning_side="yes",
           settled=NOW - timedelta(hours=6))
    v = _report(session, hours=240)["verdict"]
    assert v["primary"] == fs.VERDICT_BACKLOG


def test_verdict_healthy(session):
    for _ in range(30):
        mk(session, close=NOW - timedelta(days=1), outcome_status="settled", winning_side="yes",
           settled=NOW - timedelta(hours=6), score_status="scored")
    assert _report(session, hours=240)["verdict"]["primary"] == fs.VERDICT_HEALTHY


def test_verdict_multiple_blockers(session):
    for _ in range(15):  # settled unscored -> backlog
        mk(session, close=NOW - timedelta(days=1), outcome_status="settled", winning_side="yes",
           settled=NOW - timedelta(hours=6))
    for _ in range(15):  # matured but no outcome -> sync gap
        mk(session, close=NOW - timedelta(days=1), outcome_status=None)
    assert _report(session, hours=240)["verdict"]["primary"] == fs.VERDICT_MULTIPLE


# --- examples + window (reqs 33-36) ---------------------------------------------


def test_bounded_examples(session):
    for _ in range(5):
        mk(session, close=NOW - timedelta(days=1), outcome_status="settled", winning_side="yes",
           settled=NOW - timedelta(hours=1))
    r = _report(session, hours=240, top=2)
    assert len(r["examples"]["settled_no_score"]) == 2


def test_since_until_window(session):
    mk(session, created=NOW - timedelta(days=10), outcome_status="open")
    mk(session, created=NOW - timedelta(days=1), outcome_status="open")
    r = fs.build_scorability_report(
        session, now=NOW, since=NOW - timedelta(days=3), until=NOW)
    assert r["counts"]["forecasts"] == 1
    # --hours ignored when since given handled at CLI; here since restricts window
    r2 = fs.build_scorability_report(session, now=NOW, until=NOW - timedelta(days=5))
    assert r2["counts"]["forecasts"] == 1  # only the 10-day-old one is before until


def test_invalid_window_rejected(session):
    with pytest.raises(ValueError):
        fs.build_scorability_report(session, now=NOW, since=NOW, until=NOW - timedelta(days=1))


async def test_json_text_parity(session, capsys):
    mk(session, outcome_status="settled", winning_side="yes",
       settled=NOW - timedelta(hours=1), score_status="scored")
    r_obj = _report(session, hours=240)
    # text and json derive from the same builder (await the managed loop; no asyncio.run)
    text_rc = await cli.forecast_scorability_audit_report(session=session, hours=240, fmt="text")
    json_rc = await cli.forecast_scorability_audit_report(session=session, hours=240, fmt="json")
    assert text_rc == 0 and json_rc == 0
    out = capsys.readouterr().out
    assert "forecast scorability audit" in out
    assert r_obj["verdict"]["primary"] in out


async def test_cli_invalid_window_returns_2(session):
    rc = await cli.forecast_scorability_audit_report(
        session=session, since=NOW.isoformat(), until=(NOW - timedelta(days=1)).isoformat())
    assert rc == 2


# --- hard guarantees (reqs 37-45) -----------------------------------------------


def test_zero_writes_no_scoring_no_sync(session):
    mk(session, outcome_status="settled", winning_side="yes",
       settled=NOW - timedelta(hours=1))  # settled, unscored
    before_scores = session.scalar(select(func.count()).select_from(ForecastScoreRecord))
    before_out = session.scalar(select(func.count()).select_from(MarketOutcomeRecord))
    before_fc = session.scalar(select(func.count()).select_from(MarketForecastRecord))
    r = _report(session, hours=240)
    assert r["external_calls"] == 0 and r["writes"] == 0 and r["persisted"] is False
    # no score was created (would prove implicit scoring), no outcome synced
    assert session.scalar(select(func.count()).select_from(ForecastScoreRecord)) == before_scores
    assert session.scalar(select(func.count()).select_from(MarketOutcomeRecord)) == before_out
    assert session.scalar(select(func.count()).select_from(MarketForecastRecord)) == before_fc


def test_no_marketops_or_sync_or_scoring_imports():
    # target actual usage (imports / instantiations / calls), not docstring prose
    src = MODULE.read_text()
    for banned in ("import marketops", "from app.services.marketops",
                   "CalibrationService(", "OutcomeService(", ".score_forecast(",
                   ".score_unscored(", ".sync_ticker(", ".sync_known_markets("):
        assert banned not in src, f"module must not use {banned}"
    # AST: confirm no import pulls in a mutating scorer/syncer/marketops surface
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                imported.add((node.module, a.name))
    for mod, name in imported:
        assert "marketops" not in (mod or "")
        assert name not in ("CalibrationService", "OutcomeService", "score_unscored")


def test_no_network_imports():
    src = MODULE.read_text()
    for banned in ("httpx", "requests", "aiohttp", "urllib.request", "socket", "adapter"):
        assert banned not in src.lower()


def test_no_trading_vocabulary_in_output(session):
    mk(session, outcome_status="settled", winning_side="yes",
       settled=NOW - timedelta(hours=1), score_status="scored")
    r = _report(session, hours=240)
    banned = {"expected", "value", "kelly", "position", "recommended", "side",
              "order", "wallet", "swap", "buy", "sell", "trade", "size", "ev"}

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


def test_ast_safety_audit():
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


def test_cli_help_and_dispatch_wired():
    parser = cli.build_parser()
    ns = parser.parse_args(["forecast-scorability-audit-report", "--hours", "24", "--format", "json"])
    assert ns.command == "forecast-scorability-audit-report"
    assert ns.hours == 24 and ns.fmt == "json"


def test_empty_inventory(session):
    r = _report(session, hours=24)
    assert r["counts"]["forecasts"] == 0
    assert r["verdict"]["primary"] == fs.VERDICT_INSUFFICIENT


def test_funnel_is_monotonic_subset_chain(session):
    # a mix incl. open forecasts carrying pending scores (the case that used to
    # let latest_score_exists exceed settled_yes_no) -> each step must attrit down
    for _ in range(4):
        mk(session, outcome_status="open", score_status="pending_outcome", score_brier=None)
    mk(session, outcome_status="settled", winning_side="yes",
       settled=NOW - timedelta(hours=1), score_status="scored")
    mk(session, market=False, packet=False, res=False, outcome_status=None)
    steps = _report(session, hours=240)["scorability_funnel"]["scorability_steps"]
    counts = [s["count"] for s in steps]
    assert counts == sorted(counts, reverse=True), counts  # non-increasing
    named = {s["step"]: s["count"] for s in steps}
    assert named["outcome_settled_yes_no"] >= named["latest_score_exists"]


def test_settled_winning_side_none_is_void_or_missing_winner(session):
    # distinct from winning_side="void": a settled row with NULL winning side
    assert _classify(session, outcome_status="settled", winning_side=None,
                     settled=NOW - timedelta(hours=1)) == fs.UNSCORABLE_VOID_OR_MISSING_WINNER


def test_verdict_scored_sample_not_representative(session):
    # 20 baseball all matured+settled+scored; 20 soccer of which only 2 scored,
    # 18 open with FUTURE close (legitimately pending, not eligible) -> healthy
    # eligible scored rate, no blocker, but the scored sample is strongly skewed.
    for _ in range(20):
        mk(session, domain="baseball", close=NOW - timedelta(days=1), outcome_status="settled",
           winning_side="yes", settled=NOW - timedelta(hours=6), score_status="scored")
    for _ in range(2):
        mk(session, domain="soccer", close=NOW - timedelta(days=1), outcome_status="settled",
           winning_side="yes", settled=NOW - timedelta(hours=6), score_status="scored")
    for _ in range(18):
        mk(session, domain="soccer", close=NOW + timedelta(days=2), outcome_status="open")
    v = _report(session, hours=240)["verdict"]
    assert v["blockers"] == []
    assert v["primary"] == fs.VERDICT_NOT_REPRESENTATIVE
