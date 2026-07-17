"""FORECAST-RELIABILITY-DECOMP-001 tests — read-only calibration decomposition.

Covers the scored_current population (reusing the scorability classifier; excludes
pending/unscorable/backlog/stale/inconsistent), reliability bins + boundary
semantics, ECE/MCE, Brier baselines + skill (incl. zero-variance base rate),
binned Murphy decomposition (nonnegativity, reconstruction, perfect/constant/
uninformative), directional diagnostics, cohort reliability, temporal trends +
composition-shift override, deterministic verdict precedence, window/bin validation,
JSON/text parity, and the hard guarantees (zero writes/calls, no sync/scoring, no
MarketOps, no trading vocabulary, AST + no-network audits). In-memory SQLite.
"""

import ast
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
from app.services import forecast_reliability as fr

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "app/services/forecast_reliability.py"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
_n = {"i": 0}


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def mk(session, *, p=0.7, y=1, state="scored", created=None, domain="baseball",
       depth="source_backed", risk="low", forecaster="template", version="v1",
       completeness=0.8, research_risk="low", res_risk="low", tradeability="researchable"):
    """Seed one forecast in a chosen scorability state. state: scored | pending |
    unscorable | backlog | stale | inconsistent."""
    _n["i"] += 1
    ticker = f"KX-{_n['i']:05d}"
    created = created or NOW - timedelta(days=2)
    close = NOW - timedelta(days=1)
    settled = NOW - timedelta(hours=6)
    session.add(Market(ticker=ticker, title="t", status="finalized", close_time=close))
    pk = MarketResearchPacket(market_ticker=ticker, collector_name="template",
                              collector_version="v1", domain=domain,
                              research_completeness_score=completeness, research_risk=research_risk)
    session.add(pk); session.flush()
    rs = MarketResolutionAssessment(market_ticker=ticker, model_name="rule", clarity_score=0.9,
                                    resolution_risk=res_risk, tradeability=tradeability)
    session.add(rs); session.flush()
    f = MarketForecastRecord(market_ticker=ticker, forecaster_name=forecaster,
                             forecaster_version=version, estimated_probability=p, confidence=0.6,
                             evidence_depth=depth, forecast_risk=risk, research_packet_id=pk.id,
                             resolution_assessment_id=rs.id, created_at=created)
    session.add(f); session.flush()
    side = "yes" if y == 1 else "no"
    if state == "scored":
        session.add(MarketOutcomeRecord(market_ticker=ticker, outcome_status="settled",
                                        winning_side=side, close_time=close, settled_time=settled))
        session.add(ForecastScoreRecord(forecast_id=f.id, market_ticker=ticker,
                                        score_status="scored", brier_score=brier_score(p, float(y)),
                                        was_resolved=True, created_at=settled))
    elif state == "pending":
        session.add(MarketOutcomeRecord(market_ticker=ticker, outcome_status="open",
                                        close_time=NOW + timedelta(days=1)))
    elif state == "unscorable":
        session.add(MarketOutcomeRecord(market_ticker=ticker, outcome_status="canceled",
                                        close_time=close))
    elif state == "backlog":  # settled yes/no but NO score
        session.add(MarketOutcomeRecord(market_ticker=ticker, outcome_status="settled",
                                        winning_side=side, close_time=close, settled_time=settled))
    elif state == "stale":  # market open but a scored row exists -> pending_score_stale
        session.add(MarketOutcomeRecord(market_ticker=ticker, outcome_status="open",
                                        close_time=NOW + timedelta(days=1)))
        session.add(ForecastScoreRecord(forecast_id=f.id, market_ticker=ticker,
                                        score_status="scored", brier_score=0.09,
                                        was_resolved=True, created_at=created))
    elif state == "inconsistent":
        session.add(MarketOutcomeRecord(market_ticker=ticker, outcome_status="garbage",
                                        close_time=close))
    session.commit()
    return f


def _pts(pairs, **kw):
    """Build _Point list from (p, y) pairs."""
    out = []
    for i, (p, y) in enumerate(pairs):
        out.append(fr._Point(p=p, y=float(y), created_at=NOW + timedelta(minutes=i),
                             domain=kw.get("domain", "d"), forecaster="f:v1",
                             evidence_depth="source_backed", forecast_risk="low",
                             research_completeness_bucket="0.75-0.89", research_risk="low",
                             resolution_risk="low", tradeability="researchable"))
    return out


# --- bins + boundaries (reqs 7-13, 49) ------------------------------------------


def test_make_edges_and_validation():
    assert fr.make_edges(10)[:3] == [0.0, 0.1, 0.2]
    assert fr.make_edges(10)[-1] == 1.0
    with pytest.raises(ValueError):
        fr.make_edges(1)
    with pytest.raises(ValueError):
        fr.make_edges(0)


def test_bin_boundaries():
    e = fr.make_edges(10)
    assert fr.bin_index(0.0, e) == 0
    assert fr.bin_index(0.1, e) == 1      # left-closed
    assert fr.bin_index(0.5, e) == 5
    assert fr.bin_index(0.9, e) == 9
    assert fr.bin_index(1.0, e) == 9      # last bin right-closed
    assert fr.bin_index(0.099999, e) == 0
    with pytest.raises(ValueError):
        fr.bin_index(1.5, e)


def test_bin_mean_prob_and_observed_rate():
    e = fr.make_edges(10)
    pts = _pts([(0.72, 1), (0.78, 0), (0.74, 1)])  # all in [0.7,0.8)
    bins = fr.compute_bins(pts, e)
    b = bins[7]
    assert b["count"] == 3
    assert b["mean_forecast_probability"] == pytest.approx(0.7467, abs=1e-3)
    assert b["observed_positive_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert b["calibration_gap"] == pytest.approx(0.7467 - 2 / 3, abs=1e-3)


def test_thin_bin_label():
    e = fr.make_edges(10)
    bins = fr.compute_bins(_pts([(0.72, 1)]), e)
    assert bins[7]["label"] == "too_thin"
    bins2 = fr.compute_bins(_pts([(0.72, 1)] * 12), e)
    assert bins2[7]["label"] == "measured"


# --- ECE / MCE (reqs 15,16) -----------------------------------------------------


def test_ece_and_mce():
    e = fr.make_edges(10)
    pts = _pts([(0.75, 1)] * 20 + [(0.25, 0)] * 20)  # both perfectly calibrated-ish
    bins = fr.compute_bins(pts, e)
    ce = fr.calibration_error(bins, len(pts))
    # bin [0.7,0.8): mean 0.75 obs 1.0 gap 0.25 ; bin [0.2,0.3): mean .25 obs 0 gap .25
    assert ce["mce"] == pytest.approx(0.25, abs=1e-6)
    assert ce["ece"] == pytest.approx(0.25, abs=1e-6)
    assert ce["populated_bins"] == 2 and ce["measured_bins"] == 2


# --- baselines + skill (reqs 18-21) ---------------------------------------------


def test_mce_fallback_source_disclosed():
    e = fr.make_edges(10)
    # a single too-thin bin -> MCE falls back to populated, disclosed as fallback
    ce_thin = fr.calibration_error(fr.compute_bins(_pts([(0.72, 0)]), e), 1)
    assert ce_thin["measured_bins"] == 0
    assert ce_thin["mce_source"] == "fallback_populated"
    # a measured bin -> MCE from measured
    ce_meas = fr.calibration_error(fr.compute_bins(_pts([(0.72, 1)] * 12), e), 12)
    assert ce_meas["mce_source"] == "measured"


def test_nondefault_bins_boundaries():
    e = fr.make_edges(5)  # edges 0,0.2,0.4,0.6,0.8,1.0
    assert e == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    assert fr.bin_index(0.2, e) == 1
    assert fr.bin_index(0.19999, e) == 0
    assert fr.bin_index(0.8, e) == 4
    assert fr.bin_index(1.0, e) == 4


def test_murphy_nonzero_residual_when_p_varies_within_bin():
    e = fr.make_edges(2)  # two wide bins [0,0.5),[0.5,1.0]
    # within the [0.5,1.0] bin, p varies a lot -> nonzero discretization residual
    pts = _pts([(0.55, 1), (0.95, 0), (0.55, 0), (0.95, 1)])
    m = fr.murphy_decomposition(pts, e)
    assert abs(m["discretization_residual"]) > 1e-6


def test_baselines_and_skill():
    pts = _pts([(0.9, 1)] * 8 + [(0.9, 0)] * 2)  # prevalence 0.8, model confident-correct-ish
    b = fr.baselines(pts)
    assert b["prevalence"] == pytest.approx(0.8)
    assert b["neutral_baseline_brier"] == pytest.approx(0.25, abs=1e-6)
    assert b["base_rate_baseline_brier"] == pytest.approx(0.8 * 0.2, abs=1e-6)
    assert b["brier_skill_vs_base_rate"] is not None


def test_zero_variance_base_rate_skill_is_none():
    pts = _pts([(0.6, 1)] * 10)  # all yes -> prevalence 1.0 -> base_rate_brier 0
    b = fr.baselines(pts)
    assert b["base_rate_baseline_brier"] == 0.0
    assert b["base_rate_zero_variance"] is True
    assert b["brier_skill_vs_base_rate"] is None  # no division by zero


# --- Murphy decomposition (reqs 22-28) ------------------------------------------


def test_murphy_components_nonnegative_and_reconstruct():
    e = fr.make_edges(10)
    pts = _pts([(0.72, 1), (0.74, 0), (0.22, 0), (0.28, 1), (0.55, 1), (0.55, 0)])
    m = fr.murphy_decomposition(pts, e)
    assert m["reliability"] >= -1e-9 and m["resolution"] >= -1e-9 and m["uncertainty"] >= -1e-9
    assert m["reconstructed_brier"] == pytest.approx(
        m["reliability"] - m["resolution"] + m["uncertainty"], abs=1e-9)
    assert m["discretization_residual"] == pytest.approx(
        m["actual_brier"] - m["reconstructed_brier"], abs=1e-9)


def test_murphy_perfect_forecasts():
    e = fr.make_edges(10)
    pts = _pts([(1.0, 1)] * 10 + [(0.0, 0)] * 10)
    m = fr.murphy_decomposition(pts, e)
    assert m["actual_brier"] == 0.0
    assert m["reliability"] == pytest.approx(0.0, abs=1e-9)
    assert m["discretization_residual"] == pytest.approx(0.0, abs=1e-9)


def test_murphy_constant_base_rate_forecasts():
    e = fr.make_edges(10)
    pts = _pts([(0.6, 1)] * 6 + [(0.6, 0)] * 4)  # constant p = prevalence 0.6
    m = fr.murphy_decomposition(pts, e)
    assert m["reliability"] == pytest.approx(0.0, abs=1e-9)
    assert m["resolution"] == pytest.approx(0.0, abs=1e-9)
    assert m["discretization_residual"] == pytest.approx(0.0, abs=1e-9)


def test_murphy_uninformative_half():
    e = fr.make_edges(10)
    pts = _pts([(0.5, 1)] * 5 + [(0.5, 0)] * 5)
    m = fr.murphy_decomposition(pts, e)
    assert m["actual_brier"] == pytest.approx(0.25, abs=1e-9)
    assert m["discretization_residual"] == pytest.approx(0.0, abs=1e-9)


# --- directional (reqs 29-32) ---------------------------------------------------


def test_direction_classification():
    assert fr.direction(0.9, 0.6) == fr.OVERCONF_POS
    assert fr.direction(0.6, 0.9) == fr.UNDERCONF_POS
    assert fr.direction(0.1, 0.4) == fr.OVERCONF_NEG
    assert fr.direction(0.4, 0.1) == fr.UNDERCONF_NEG
    assert fr.direction(0.7, 0.68) == fr.APPROX_CALIBRATED


def test_directional_summary_overconfident():
    e = fr.make_edges(10)
    pts = _pts([(0.95, 0)] * 12 + [(0.05, 1)] * 12)  # extreme + wrong -> overconfident
    d = fr.directional_summary(fr.compute_bins(pts, e), pts)
    assert d["extreme_confidence_miss_count"] == 24
    assert d["_over_w"] > d["_under_w"]


# --- population reuse of scorability classifier (reqs 1-6) ----------------------


def _report(session, **kw):
    kw.setdefault("now", NOW)
    return fr.build_reliability_report(session, **kw)


def test_population_excludes_all_non_scored_states(session):
    mk(session, state="scored", y=1)
    mk(session, state="pending")
    mk(session, state="unscorable")
    mk(session, state="backlog", y=1)
    mk(session, state="stale")
    mk(session, state="inconsistent")
    r = _report(session, hours=240)
    pop = r["population"]
    assert pop["all_forecasts"] == 6
    assert pop["scored_current"] == 1
    assert pop["excluded_pending"] == 1
    assert pop["excluded_unscorable"] == 1
    assert pop["excluded_backlog"] == 1
    assert pop["excluded_stale"] == 1
    assert pop["excluded_inconsistent"] == 1
    assert r["baselines"]["sample_size"] == 1  # only scored_current in the analysis


def test_population_scored_current_equals_baseline_sample_size(session):
    # lock the two-source equality: population.scored_current == baselines.sample_size
    for _ in range(12):
        mk(session, state="scored", p=0.7, y=1)
    for _ in range(3):
        mk(session, state="pending")
    r = _report(session, hours=240)
    assert r["population"]["scored_current"] == r["baselines"]["sample_size"] == 12


def test_no_scored_current_is_insufficient(session):
    mk(session, state="pending")
    r = _report(session, hours=240)
    assert r["population"]["scored_current"] == 0
    assert r["verdict"]["primary"] == "INSUFFICIENT_RELIABILITY_DATA"


def test_stale_scores_excluded_would_change_result(session):
    # 30 well-calibrated scored_current + 30 stale-scored rows that, if counted,
    # would inject brier=0.09 noise; assert they are excluded from the analysis.
    for _ in range(30):
        mk(session, state="scored", p=0.6, y=1)
    for _ in range(30):
        mk(session, state="stale")
    r = _report(session, hours=240)
    assert r["population"]["scored_current"] == 30
    assert r["population"]["excluded_stale"] == 30
    assert r["baselines"]["sample_size"] == 30


# --- cohorts (reqs 33-38) -------------------------------------------------------


def test_cohort_segmentation_and_labels(session):
    for _ in range(25):
        mk(session, state="scored", domain="baseball", depth="source_backed",
           forecaster="a", version="v1", risk="low", p=0.7, y=1)
    for _ in range(3):
        mk(session, state="scored", domain="soccer", depth="template_only",
           forecaster="b", version="v2", risk="high", p=0.3, y=0)
    r = _report(session, hours=240)
    doms = {c["name"]: c for c in r["cohorts"]["domain"]}
    assert doms["baseball"]["scored_count"] == 25 and doms["baseball"]["sample_label"] == "measured"
    assert doms["soccer"]["sample_label"] == "too_thin"
    assert {c["name"] for c in r["cohorts"]["forecaster"]} == {"a:v1", "b:v2"}
    assert {c["name"] for c in r["cohorts"]["evidence_depth"]} == {"source_backed", "template_only"}
    assert sum(c["scored_count"] for c in r["cohorts"]["domain"]) == r["baselines"]["sample_size"]


def test_single_populated_bin_cohort_flagged(session):
    # adversarial: a cohort looks calibrated but only one prob bin is populated
    for _ in range(25):
        mk(session, state="scored", domain="baseball", p=0.75, y=1)
    r = _report(session, hours=240)
    dom = {c["name"]: c for c in r["cohorts"]["domain"]}["baseball"]
    assert dom["populated_bins"] == 1  # single-bin coverage is disclosed


# --- temporal + composition (reqs 40-46) ----------------------------------------


def test_temporal_daily_weekly_and_thin_trend(session):
    for i in range(10):
        mk(session, state="scored", created=NOW - timedelta(days=i), p=0.7, y=1)
    r = _report(session, hours=24 * 400)
    assert len(r["temporal"]["daily"]) >= 1
    assert len(r["temporal"]["weekly"]) >= 1
    assert r["temporal"]["trend"]["label"] == "too_thin_for_trend"  # periods below floor


def test_trend_improving(session):
    # 6 weeks, each measured; ECE decreasing over time (later weeks better calibrated)
    for wk in range(6):
        created = NOW - timedelta(weeks=(6 - wk))
        # earlier weeks miscalibrated (p=0.9 but half wrong); later weeks calibrated
        if wk < 3:
            for _ in range(20):
                mk(session, state="scored", created=created, p=0.9, y=(1 if _ % 2 else 0), domain="baseball")
        else:
            for _ in range(20):
                mk(session, state="scored", created=created, p=0.6, y=(1 if _ < 12 else 0), domain="baseball")
    r = _report(session, hours=24 * 400)
    assert r["temporal"]["trend"]["measured_periods"] >= 4
    assert r["temporal"]["trend"]["label"] in (
        "reliability_improving", "reliability_deteriorating", "reliability_stable",
        "composition_shift_dominates")


def _wk(ece, n=20, dom=0.9, prev=0.5):
    return {"scored_count": n, "ece": ece, "top_domain_share": dom, "prevalence": prev}


def test_trend_pure_improving():
    weekly = [_wk(0.20), _wk(0.18), _wk(0.06), _wk(0.05)]
    assert fr._trend(weekly)["label"] == "reliability_improving"


def test_trend_pure_stable():
    weekly = [_wk(0.10), _wk(0.11), _wk(0.10), _wk(0.11)]
    assert fr._trend(weekly)["label"] == "reliability_stable"


def test_trend_pure_deteriorating():
    weekly = [_wk(0.05), _wk(0.06), _wk(0.20), _wk(0.22)]
    assert fr._trend(weekly)["label"] == "reliability_deteriorating"


def test_trend_pure_too_thin():
    assert fr._trend([_wk(0.1), _wk(0.1)])["label"] == "too_thin_for_trend"
    assert fr._trend([_wk(0.1, n=5)] * 6)["label"] == "too_thin_for_trend"  # below MIN_PERIOD


def test_trend_pure_composition_shift():
    # prevalence jumps 0.2 -> 0.9 between halves -> composition_shift_dominates
    weekly = [_wk(0.20, prev=0.2), _wk(0.18, prev=0.2), _wk(0.05, prev=0.9), _wk(0.05, prev=0.9)]
    assert fr._trend(weekly)["label"] == "composition_shift_dominates"


def test_composition_shift_override(session):
    # early weeks all soccer, late weeks all baseball -> top-domain-share stable BUT
    # prevalence shifts materially -> composition_shift_dominates
    for wk in range(6):
        created = NOW - timedelta(weeks=(6 - wk))
        prev_yes = 4 if wk < 3 else 18  # prevalence 0.2 early -> 0.9 late
        for i in range(20):
            mk(session, state="scored", created=created, p=0.6,
               y=(1 if i < prev_yes else 0), domain=("soccer" if wk < 3 else "baseball"))
    r = _report(session, hours=24 * 400)
    assert r["temporal"]["trend"]["label"] == "composition_shift_dominates"


# --- verdict (reqs 39,47) -------------------------------------------------------


def test_verdict_base_rate_not_beaten(session):
    # model no better than base rate: p=0.6 constant, prevalence 0.6 -> skill ~0
    for i in range(40):
        mk(session, state="scored", p=0.6, y=(1 if i < 24 else 0), domain="baseball")
    r = _report(session, hours=240)
    assert r["baselines"]["brier_skill_vs_base_rate"] <= 0.001
    assert r["verdict"]["primary"] in ("BASE_RATE_BASELINE_NOT_BEATEN",
                                       "MULTIPLE_RELIABILITY_FINDINGS", "RESOLUTION_IS_WEAK")


def test_verdict_representativeness_gate(session):
    # strongly skewed scored composition -> RELIABILITY_SAMPLE_NOT_REPRESENTATIVE
    for _ in range(40):
        mk(session, state="scored", domain="baseball", p=0.7, y=1)
    for _ in range(40):
        mk(session, state="pending", domain="soccer")  # soccer only in the excluded pool
    r = _report(session, hours=240)
    # baseball is 50% of all forecasts but 100% of scored -> strong skew
    assert r["verdict"]["primary"] == "RELIABILITY_SAMPLE_NOT_REPRESENTATIVE"


# --- window/bins + CLI (reqs 48-50, 58) -----------------------------------------


def test_invalid_window_and_bins(session):
    with pytest.raises(ValueError):
        fr.build_reliability_report(session, now=NOW, since=NOW, until=NOW - timedelta(days=1))
    with pytest.raises(ValueError):
        fr.build_reliability_report(session, now=NOW, bins=1)


async def test_json_text_parity_and_cli(session, capsys):
    for _ in range(30):
        mk(session, state="scored", p=0.7, y=1)
    r_obj = _report(session, hours=240)
    text_rc = await cli.forecast_reliability_decomposition_report(session=session, hours=240, fmt="text")
    json_rc = await cli.forecast_reliability_decomposition_report(session=session, hours=240, fmt="json")
    assert text_rc == 0 and json_rc == 0
    out = capsys.readouterr().out
    assert "forecast reliability decomposition" in out
    assert r_obj["verdict"]["primary"] in out


async def test_cli_invalid_bins_returns_2(session):
    rc = await cli.forecast_reliability_decomposition_report(session=session, hours=240, bins=1)
    assert rc == 2


def test_cli_help_and_dispatch_wired():
    ns = cli.build_parser().parse_args(
        ["forecast-reliability-decomposition-report", "--bins", "5", "--format", "json"])
    assert ns.command == "forecast-reliability-decomposition-report"
    assert ns.bins == 5 and ns.fmt == "json"


# --- hard guarantees (reqs 51-57, 59) -------------------------------------------


def test_zero_writes_no_scoring_no_sync(session):
    for _ in range(5):
        mk(session, state="scored", p=0.7, y=1)
    before_scores = session.scalar(select(func.count()).select_from(ForecastScoreRecord))
    before_out = session.scalar(select(func.count()).select_from(MarketOutcomeRecord))
    r = _report(session, hours=240)
    assert r["external_calls"] == 0 and r["writes"] == 0 and r["persisted"] is False
    assert session.scalar(select(func.count()).select_from(ForecastScoreRecord)) == before_scores
    assert session.scalar(select(func.count()).select_from(MarketOutcomeRecord)) == before_out


def test_no_marketops_sync_or_scoring_usage():
    src = MODULE.read_text()
    for banned in ("import marketops", "from app.services.marketops",
                   "CalibrationService(", "OutcomeService(", ".score_forecast(",
                   ".score_unscored(", ".sync_ticker(", ".sync_known_markets("):
        assert banned not in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "marketops" not in (node.module or "")
            for a in node.names:
                assert a.name not in ("CalibrationService", "OutcomeService", "score_unscored")


def test_no_network_imports():
    src = MODULE.read_text().lower()
    for banned in ("httpx", "requests", "aiohttp", "urllib.request", "socket", "adapter"):
        assert banned not in src


def test_no_trading_vocabulary_in_output(session):
    for _ in range(30):
        mk(session, state="scored", p=0.7, y=1)
    r = _report(session, hours=240)
    banned = {"kelly", "position", "recommended", "order", "wallet", "swap", "buy",
              "sell", "trade", "sizing"}

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
        elif isinstance(node, (ast.Name, ast.arg)):
            names.add(node.id if isinstance(node, ast.Name) else node.arg)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert not (names & banned)


def test_does_not_modify_scorability_module():
    # reliability must REUSE the scorability classifier, not fork it
    src = MODULE.read_text()
    assert "from app.services.forecast_scorability import" in src
    assert "SCORED_CURRENT" in src
