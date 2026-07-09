"""FORECAST-ANCHOR-001 tests: read-only forecaster-anchoring diagnostic.

Adjustment-ratio math and every anchor-bucket classification, prior-forecast
and market-baseline reconstruction, insufficient-data handling, cohort
verdicts (anchoring_confirmed / timing_adverse_selection /
insufficient_prior_forecast_data / market_type_specific / no_anchor /
too_thin), follow-through by anchor bucket, interpretation, rendering, no
persistence, no network, no forbidden vocabulary. In-memory SQLite; nothing
live is touched.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import (
    EdgePrecheckSnapshot,
    MarketForecastRecord,
    MarketPriceTick,
    OpportunitySignal,
)
from app.services.forecast_anchor import (
    BUCKET_AGAINST,
    BUCKET_ANCHORED,
    BUCKET_INSUFFICIENT,
    BUCKET_NO_PRIOR,
    BUCKET_PARTIAL,
    BUCKET_WITH,
    MIN_VERDICT_CLASSIFIED,
    VERDICT_ANCHORING,
    VERDICT_INSUFFICIENT_PRIOR,
    VERDICT_MARKET_TYPE,
    VERDICT_NO_ANCHOR,
    VERDICT_TIMING,
    VERDICT_TOO_THIN,
    ForecastAnchorDiagnosticService,
    classify_adjustment,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def tick(session, ticker, *, at, mid):
    session.add(MarketPriceTick(
        market_ticker=ticker, observed_at=at,
        yes_bid=int(mid * 100) - 1, yes_ask=int(mid * 100) + 1, midpoint=mid,
        spread=1, volume_24h=100, liquidity_proxy=1_000_000, created_at=at,
    ))


def seed(
    session,
    ticker,
    *,
    forecast_prob=0.60,
    midpoint=0.50,             # midpoint at measurement (gap = forecast - midpoint)
    minutes_ago=90,
    prior_prob=None,           # prior forecast probability (None => no prior)
    prior_minutes_before=20,   # prior forecast this many minutes before measurement
    baseline_mid=None,         # market midpoint at prior-forecast time (tick)
    later_mid=None,            # 5 min after measurement (follow-through)
    pre_mid=None,              # 8 min before measurement (pre-move window)
    market_type="total",
):
    created = NOW - timedelta(minutes=minutes_ago)
    if prior_prob is not None:
        prior_at = created - timedelta(minutes=prior_minutes_before)
        session.add(MarketForecastRecord(
            market_ticker=ticker, forecaster_name="baseball_evidence",
            forecaster_version="v1", prompt_version="v1",
            estimated_probability=prior_prob, confidence=0.62,
            evidence_depth="source_backed", forecast_risk="medium",
            created_at=prior_at,
        ))
        if baseline_mid is not None:
            tick(session, ticker, at=prior_at - timedelta(seconds=30), mid=baseline_mid)
    f = MarketForecastRecord(
        market_ticker=ticker, forecaster_name="baseball_evidence",
        forecaster_version="v1", prompt_version="v1",
        estimated_probability=forecast_prob, confidence=0.62,
        evidence_depth="source_backed", forecast_risk="medium",
        created_at=created - timedelta(seconds=60),
    )
    session.add(f)
    session.flush()
    sig = OpportunitySignal(
        market_ticker=ticker, signal_type="price_move_threshold",
        signal_status="forecast_refreshed",
        observed_at=created - timedelta(minutes=2), reason="seeded",
        created_at=created - timedelta(minutes=2),
    )
    session.add(sig)
    session.flush()
    session.add(EdgePrecheckSnapshot(
        market_ticker=ticker, signal_id=sig.id, forecast_id=f.id,
        forecaster_name="baseball_evidence", evidence_depth="source_backed",
        forecast_probability=forecast_prob, forecast_confidence=0.62,
        market_midpoint=midpoint, spread_cents=1,
        liquidity_proxy_cents=1_000_000,
        probability_gap=round(forecast_prob - midpoint, 4),
        abs_probability_gap=abs(round(forecast_prob - midpoint, 4)),
        status="watchlist", invalidation_reasons=[], persistence_count=1,
        forecast_age_seconds=60, market_snapshot_age_seconds=10,
        tags=["domain:sports_baseball", f"market_type:{market_type}"],
        created_at=created,
    ))
    if pre_mid is not None:
        tick(session, ticker, at=created - timedelta(minutes=8), mid=pre_mid)
    if later_mid is not None:
        tick(session, ticker, at=created + timedelta(minutes=5), mid=later_mid)
    session.commit()


def rows(session, hours=24):
    return ForecastAnchorDiagnosticService().build_rows(session, hours)


def build(session, hours=24, top=5):
    return ForecastAnchorDiagnosticService().build(session, hours=hours, top=top)


# --- classification math ---------------------------------------------------------


class TestClassifyAdjustment:
    def test_ratio_math(self):
        bucket, ratio = classify_adjustment(0.05, 0.10)
        assert ratio == pytest.approx(0.5)
        assert bucket == BUCKET_WITH          # same sign, ratio >= 0.5

    def test_anchored_static(self):
        bucket, ratio = classify_adjustment(0.005, 0.10)
        assert bucket == BUCKET_ANCHORED
        assert ratio == pytest.approx(0.05)

    def test_partial_adjustment(self):
        bucket, _ = classify_adjustment(0.03, 0.10)
        assert bucket == BUCKET_PARTIAL       # same sign, ratio 0.3 < 0.5

    def test_moved_with_market(self):
        bucket, _ = classify_adjustment(-0.08, -0.10)
        assert bucket == BUCKET_WITH          # negative direction also counts

    def test_moved_against_market(self):
        bucket, _ = classify_adjustment(-0.05, 0.10)
        assert bucket == BUCKET_AGAINST

    def test_tiny_market_move_is_insufficient(self):
        bucket, ratio = classify_adjustment(0.05, 0.01)
        assert bucket == BUCKET_INSUFFICIENT
        assert ratio is None

    def test_none_inputs_insufficient(self):
        assert classify_adjustment(None, 0.1)[0] == BUCKET_INSUFFICIENT
        assert classify_adjustment(0.1, None)[0] == BUCKET_INSUFFICIENT


# --- row reconstruction ------------------------------------------------------------


class TestReconstruction:
    def test_deltas_computed_over_prior_forecast_interval(self, session):
        # prior forecast 0.50 when market was 0.40; now forecast 0.60, market 0.50
        seed(session, "KXA-1", forecast_prob=0.60, midpoint=0.50,
             prior_prob=0.50, baseline_mid=0.40, later_mid=0.52)
        (a,) = rows(session)
        assert a.forecast_delta == pytest.approx(0.10)
        assert a.market_delta == pytest.approx(0.10)
        assert a.bucket == BUCKET_WITH
        assert a.adjustment_ratio == pytest.approx(1.0)
        assert a.market_moved_more is False

    def test_anchored_forecast_detected(self, session):
        # market moved 0.40 -> 0.50 but forecast stayed 0.60 -> 0.60
        seed(session, "KXA-1", forecast_prob=0.60, midpoint=0.50,
             prior_prob=0.60, baseline_mid=0.40, later_mid=0.52)
        (a,) = rows(session)
        assert a.forecast_delta == pytest.approx(0.0)
        assert a.bucket == BUCKET_ANCHORED
        assert a.market_moved_more is True

    def test_no_prior_forecast_bucket(self, session):
        seed(session, "KXA-1", prior_prob=None, later_mid=0.52)
        (a,) = rows(session)
        assert a.bucket == BUCKET_NO_PRIOR
        assert a.forecast_delta is None

    def test_missing_baseline_tick_is_insufficient(self, session):
        seed(session, "KXA-1", prior_prob=0.50, baseline_mid=None, later_mid=0.52)
        (a,) = rows(session)
        assert a.bucket == BUCKET_INSUFFICIENT

    def test_mid_before_reconstruction(self, session):
        seed(session, "KXA-1", prior_prob=0.50, baseline_mid=0.40,
             pre_mid=0.44, later_mid=0.52)
        (a,) = rows(session)
        assert a.mid_10m_before == pytest.approx(0.44)
        assert a.mid_at_measure == pytest.approx(0.50)

    def test_prior_forecast_age_recorded(self, session):
        seed(session, "KXA-1", prior_prob=0.50, baseline_mid=0.40,
             prior_minutes_before=20)
        (a,) = rows(session)
        # prior 20 min before measurement (+60s forecast offset)
        assert a.prior_forecast_age_s == pytest.approx(20 * 60, abs=5)


# --- cohort verdicts ------------------------------------------------------------------


def seed_many(session, n, prefix, **kw):
    for i in range(n):
        seed(session, f"{prefix}-{i}", **kw)


class TestVerdicts:
    def test_too_thin(self, session):
        seed_many(session, 3, "KXT", prior_prob=0.60, baseline_mid=0.40)
        r = build(session)
        assert r["overall"]["verdict"] == VERDICT_TOO_THIN

    def test_insufficient_prior_forecast_data(self, session):
        seed_many(session, MIN_VERDICT_CLASSIFIED + 2, "KXNP", prior_prob=None)
        r = build(session)
        assert r["overall"]["verdict"] == VERDICT_INSUFFICIENT_PRIOR

    def test_anchoring_confirmed(self, session):
        # every row: market moved 0.10, forecast static
        seed_many(session, MIN_VERDICT_CLASSIFIED + 2, "KXANC",
                  forecast_prob=0.60, midpoint=0.50,
                  prior_prob=0.60, baseline_mid=0.40, later_mid=0.46)
        r = build(session)
        o = r["overall"]
        assert o["bucket_shares"]["anchored_static"] == 1.0
        assert o["verdict"] == VERDICT_ANCHORING

    def test_no_anchor_when_forecast_keeps_up(self, session):
        # forecast moves 1:1 with market; follow-through positive
        seed_many(session, MIN_VERDICT_CLASSIFIED + 2, "KXOK",
                  forecast_prob=0.60, midpoint=0.50,
                  prior_prob=0.50, baseline_mid=0.40, later_mid=0.56)
        r = build(session)
        assert r["overall"]["bucket_shares"]["moved_with_market"] == 1.0
        assert r["overall"]["verdict"] == VERDICT_NO_ANCHOR

    def test_timing_when_forecast_keeps_up_but_selection_bad(self, session):
        """Forecast moves with the market, yet gaps oppose the pre-move and
        follow-through stays poor => timing/selection, not anchoring."""
        for i in range(MIN_VERDICT_CLASSIFIED + 2):
            # market rose 0.40->0.50 (pre_mid 0.44 marks the recent move);
            # forecast rose too (0.35->0.45, moved_with) but sits BELOW market
            # (gap -0.05, opposes the rise); midpoint keeps rising (0.54, away).
            seed(session, f"KXTIM-{i}", forecast_prob=0.45, midpoint=0.50,
                 prior_prob=0.35, baseline_mid=0.40, pre_mid=0.44, later_mid=0.54)
        r = build(session)
        o = r["overall"]
        assert o["bucket_shares"]["moved_with_market"] == 1.0
        assert o["gap_opposes_move_share"] == 1.0
        assert o["toward_rate_60m"] == 0.0
        assert o["verdict"] == VERDICT_TIMING

    def test_market_type_specific_override(self, session):
        # spreads: anchored; totals: keeping up, positive follow-through
        seed_many(session, MIN_VERDICT_CLASSIFIED + 2, "KXSPD",
                  market_type="spread", forecast_prob=0.60, midpoint=0.50,
                  prior_prob=0.60, baseline_mid=0.40, later_mid=0.46)
        seed_many(session, MIN_VERDICT_CLASSIFIED + 2, "KXTOT",
                  market_type="total", forecast_prob=0.60, midpoint=0.50,
                  prior_prob=0.50, baseline_mid=0.40, later_mid=0.56)
        r = build(session)
        assert r["overall"]["verdict"] == VERDICT_MARKET_TYPE
        mt = r["dimensions"]["market_type"]
        assert mt["spread"]["verdict"] == VERDICT_ANCHORING
        assert mt["total"]["verdict"] == VERDICT_NO_ANCHOR

    def test_followthrough_split_by_bucket(self, session):
        seed(session, "KXANC-1", prior_prob=0.60, forecast_prob=0.60,
             midpoint=0.50, baseline_mid=0.40, later_mid=0.46)   # anchored, away
        seed(session, "KXOK-1", prior_prob=0.50, forecast_prob=0.60,
             midpoint=0.50, baseline_mid=0.40, later_mid=0.56)   # with, toward
        r = build(session)
        fb = r["overall"]["follow_through_by_bucket"]
        assert fb["anchored_static"]["toward_rate_60m"] == 0.0
        assert fb["moved_with_market"]["toward_rate_60m"] == 1.0


# --- report content ---------------------------------------------------------------


class TestReportContent:
    def test_examples_sections(self, session):
        seed(session, "KXANC-1", prior_prob=0.60, forecast_prob=0.60,
             midpoint=0.50, baseline_mid=0.40, later_mid=0.40)   # worst anchored
        seed(session, "KXOK-1", prior_prob=0.50, forecast_prob=0.60,
             midpoint=0.50, baseline_mid=0.40, later_mid=0.56)   # adjusted
        seed(session, "KXBIG-1", prior_prob=0.60, forecast_prob=0.60,
             midpoint=0.50, baseline_mid=0.42, later_mid=0.52)   # sharp move, static
        r = build(session, top=3)
        ex = r["examples"]
        assert ex["worst_anchored_behind_market"][0]["ticker"] == "KXANC-1"
        assert ex["forecasts_that_adjusted"][0]["ticker"] == "KXOK-1"
        assert any(e["ticker"] == "KXBIG-1" for e in ex["sharp_market_move_forecast_static"])

    def test_adjusted_but_failed_section(self, session):
        seed(session, "KXAF-1", prior_prob=0.50, forecast_prob=0.60,
             midpoint=0.50, baseline_mid=0.40, later_mid=0.44)   # moved_with, away
        r = build(session)
        assert r["examples"]["adjusted_but_followthrough_failed"][0]["ticker"] == "KXAF-1"

    def test_interpretation_answers_present(self, session):
        seed(session, "KXA-1", prior_prob=0.60, forecast_prob=0.60,
             midpoint=0.50, baseline_mid=0.40, later_mid=0.46)
        r = build(session)
        i = r["interpretation"]
        assert "forecaster_failing_to_move_with_market" in i
        assert "anchoring_explains_negative_followthrough" in i
        assert "spreads_more_anchored_than_totals" in i
        assert "next_step_evidence" in i

    def test_next_step_language_is_gated(self, session):
        seed_many(session, MIN_VERDICT_CLASSIFIED + 2, "KXANC",
                  forecast_prob=0.60, midpoint=0.50,
                  prior_prob=0.60, baseline_mid=0.40, later_mid=0.46)
        r = build(session)
        nxt = r["interpretation"]["next_step_evidence"]
        assert "explicitly-accepted milestone" in nxt   # never an instruction to change


# --- CLI ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_parses(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "forecast_anchor_diagnostic_report", fake)
        rc = cli.main(["forecast-anchor-diagnostic-report", "--hours", "48", "--top", "3"])
        assert rc == 0
        assert captured == {"hours": 48, "top": 3}

    def test_cli_renders(self, session, capsys):
        seed(session, "KXA-1", prior_prob=0.60, forecast_prob=0.60,
             midpoint=0.50, baseline_mid=0.40, later_mid=0.46)
        n = asyncio.run(cli.forecast_anchor_diagnostic_report(session=session))
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "anchor buckets" in out
        assert "OVERALL VERDICT" in out

    def test_cli_empty_window(self, session, capsys):
        n = asyncio.run(cli.forecast_anchor_diagnostic_report(session=session))
        assert n == 0
        assert "rows=0" in capsys.readouterr().out


# --- safety -------------------------------------------------------------------------


class TestSafety:
    def test_persists_nothing(self, session):
        seed(session, "KXA-1", prior_prob=0.60, baseline_mid=0.40, later_mid=0.46)
        session.commit()
        import sqlalchemy

        before = {
            t: session.execute(sqlalchemy.text(f"select count(*) from {t}")).scalar()
            for t in ("edge_precheck_snapshots", "market_price_ticks", "market_forecasts")
        }
        build(session)
        session.commit()
        after = {
            t: session.execute(sqlalchemy.text(f"select count(*) from {t}")).scalar()
            for t in before
        }
        assert before == after

    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "forecast_anchor.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "jupiter", "recommend_trade",
                    "execute_trade", "execution"):
            assert bad not in code

    def test_no_live_network(self):
        src = (REPO / "app" / "services" / "forecast_anchor.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src

    def test_verdicts_are_measurement_language(self):
        for v in (VERDICT_ANCHORING, VERDICT_TIMING, VERDICT_INSUFFICIENT_PRIOR,
                  VERDICT_MARKET_TYPE, VERDICT_NO_ANCHOR, VERDICT_TOO_THIN):
            for bad in ("trade", "buy", "sell", "bet", "ev_"):
                assert bad not in v
