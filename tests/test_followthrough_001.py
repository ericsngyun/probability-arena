"""FOLLOWTHROUGH-001 tests: read-only diagnostic for negative gap follow-through.

Gap-sign handling, moved-away/reverted classification, pre-measurement move
relation (chasing detection), timing bucket math, cohort breakdowns, the
deterministic verdict rules (adverse_selection_candidate / stale_or_chasing_move
/ measurement_artifact_possible / too_thin / promising_needs_more_sample /
neutral), failure-example sections, CLI rendering, and safety (no forbidden
vocabulary, no live calls, nothing persisted or changed). In-memory SQLite.
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
from app.services.edge_followthrough import (
    ARTIFACT_SNAPSHOT_P50_SECONDS,
    MIN_VERDICT_SAMPLES,
    VERDICT_ADVERSE_SELECTION,
    VERDICT_ARTIFACT,
    VERDICT_NEUTRAL,
    VERDICT_PROMISING,
    VERDICT_STALE_OR_CHASING,
    VERDICT_TOO_THIN,
    EdgeFollowthroughDiagnosticService,
    _age_bucket,
    _snapshot_age_bucket,
    classify_path,
    gap_vs_pre_move,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def tick(session, ticker, *, at, mid, spread=1, liquidity=1_000_000):
    session.add(MarketPriceTick(
        market_ticker=ticker, observed_at=at,
        yes_bid=int(mid * 100) - 1, yes_ask=int(mid * 100) + 1, midpoint=mid,
        spread=spread, volume_24h=100, liquidity_proxy=liquidity, created_at=at,
    ))


def seed(
    session,
    ticker,
    *,
    gap=0.10,
    midpoint=0.50,
    minutes_ago=90,
    later_mid=None,           # single tick 5 min after measurement (all horizons)
    pre_mid=None,             # tick 8 min BEFORE measurement (pre-move window)
    forecast_age_s=60,
    snapshot_age_s=10,
    signal_type="price_move_threshold",
    signal_minutes_before=2,
    market_type="total",
    conf=0.62,
    spread=1,
    liquidity=1_000_000,
    persistence=1,
    later_spread=None,
    later_liquidity=None,
):
    created = NOW - timedelta(minutes=minutes_ago)
    f = MarketForecastRecord(
        market_ticker=ticker, forecaster_name="baseball_evidence",
        forecaster_version="v1", prompt_version="v1",
        estimated_probability=midpoint + gap, confidence=conf,
        evidence_depth="source_backed", forecast_risk="medium",
        created_at=created - timedelta(seconds=forecast_age_s),
    )
    session.add(f)
    session.flush()
    sig = OpportunitySignal(
        market_ticker=ticker, signal_type=signal_type,
        signal_status="forecast_refreshed",
        observed_at=created - timedelta(minutes=signal_minutes_before),
        reason="seeded",
        created_at=created - timedelta(minutes=signal_minutes_before),
    )
    session.add(sig)
    session.flush()
    row = EdgePrecheckSnapshot(
        market_ticker=ticker, signal_id=sig.id, forecast_id=f.id,
        forecaster_name="baseball_evidence", evidence_depth="source_backed",
        forecast_probability=midpoint + gap, forecast_confidence=conf,
        market_midpoint=midpoint, spread_cents=spread,
        liquidity_proxy_cents=liquidity, probability_gap=gap,
        abs_probability_gap=abs(gap), status="watchlist",
        invalidation_reasons=[], persistence_count=persistence,
        forecast_age_seconds=forecast_age_s,
        market_snapshot_age_seconds=snapshot_age_s,
        tags=["domain:sports_baseball", f"market_type:{market_type}"],
        created_at=created,
    )
    session.add(row)
    if pre_mid is not None:
        tick(session, ticker, at=created - timedelta(minutes=8), mid=pre_mid)
    if later_mid is not None:
        tick(
            session, ticker, at=created + timedelta(minutes=5), mid=later_mid,
            spread=later_spread if later_spread is not None else spread,
            liquidity=later_liquidity if later_liquidity is not None else liquidity,
        )
    session.commit()
    return row


def build(session, hours=24, top=5):
    return EdgeFollowthroughDiagnosticService().build(session, hours=hours, top=top)


def rows(session, hours=24):
    return EdgeFollowthroughDiagnosticService().build_row_diagnostics(session, hours)


# --- gap sign / direction math -------------------------------------------------


class TestDirectionMath:
    def test_positive_gap_toward_means_midpoint_rises(self, session):
        # forecast above market (gap +0.10); midpoint rises 0.04 => closure +0.4
        seed(session, "KXA-1", gap=0.10, midpoint=0.50, later_mid=0.54)
        (r,) = rows(session)
        assert r.closures["60m"] == pytest.approx(0.4)
        assert r.paths["60m"] == "reverted_toward"

    def test_positive_gap_away_means_midpoint_falls(self, session):
        seed(session, "KXA-1", gap=0.10, midpoint=0.50, later_mid=0.46)
        (r,) = rows(session)
        assert r.closures["60m"] == pytest.approx(-0.4)
        assert r.paths["60m"] == "continued_away"

    def test_negative_gap_toward_means_midpoint_falls(self, session):
        # forecast below market (gap -0.10); midpoint falls => toward
        seed(session, "KXA-1", gap=-0.10, midpoint=0.50, later_mid=0.46)
        (r,) = rows(session)
        assert r.closures["60m"] == pytest.approx(0.4)
        assert r.paths["60m"] == "reverted_toward"

    def test_negative_gap_away_means_midpoint_rises(self, session):
        seed(session, "KXA-1", gap=-0.10, midpoint=0.50, later_mid=0.54)
        (r,) = rows(session)
        assert r.closures["60m"] == pytest.approx(-0.4)
        assert r.paths["60m"] == "continued_away"

    def test_flat_path_between_thresholds(self):
        assert classify_path(0.1) == "flat"
        assert classify_path(-0.1) == "flat"
        assert classify_path(-0.25) == "continued_away"
        assert classify_path(0.25) == "reverted_toward"
        assert classify_path(None) == "no_sample"

    def test_no_later_tick_yields_no_sample(self, session):
        seed(session, "KXA-1", gap=0.10, later_mid=None)
        (r,) = rows(session)
        assert r.closures == {}
        assert all(p == "no_sample" for p in r.paths.values())


# --- pre-move / chasing detection -----------------------------------------------


class TestPreMove:
    def test_gap_opposes_move_when_forecast_lags_a_rise(self, session):
        # market rose 0.44 -> 0.50 just before measurement; forecast (older) at
        # 0.40 => gap -0.10 points back down: the classic lagging-forecast shape.
        seed(session, "KXA-1", gap=-0.10, midpoint=0.50, pre_mid=0.44)
        (r,) = rows(session)
        assert r.pre_move == pytest.approx(0.06)
        assert r.sharp_pre_move is True
        assert r.gap_move_relation == "opposes_move"

    def test_gap_follows_move_when_forecast_leads(self, session):
        # market rose and the forecast is still ABOVE it: gap + with pre-move +
        seed(session, "KXA-1", gap=0.10, midpoint=0.50, pre_mid=0.44)
        (r,) = rows(session)
        assert r.gap_move_relation == "follows_move"

    def test_no_pre_tick_is_unknown(self, session):
        seed(session, "KXA-1", gap=0.10, pre_mid=None)
        (r,) = rows(session)
        assert r.pre_move is None
        assert r.gap_move_relation == "unknown"

    def test_relation_helper_edge_cases(self):
        assert gap_vs_pre_move(None, 0.05) == "unknown"
        assert gap_vs_pre_move(0.1, None) == "unknown"
        assert gap_vs_pre_move(0.1, 0.0) == "no_pre_move"
        assert gap_vs_pre_move(0.0, 0.05) == "no_gap"
        assert gap_vs_pre_move(-0.1, 0.05) == "opposes_move"
        assert gap_vs_pre_move(0.1, 0.05) == "follows_move"

    def test_small_pre_move_not_sharp(self, session):
        seed(session, "KXA-1", gap=0.10, midpoint=0.50, pre_mid=0.49)
        (r,) = rows(session)
        assert r.sharp_pre_move is False


# --- timing buckets --------------------------------------------------------------


class TestTiming:
    def test_age_bucket_math(self):
        assert _age_bucket(None) == "unknown"
        assert _age_bucket(60) == "<2m"
        assert _age_bucket(120) == "2-5m"
        assert _age_bucket(299) == "2-5m"
        assert _age_bucket(300) == "5-15m"
        assert _age_bucket(900) == ">15m"

    def test_snapshot_age_bucket_math(self):
        assert _snapshot_age_bucket(None) == "unknown"
        assert _snapshot_age_bucket(10) == "<15s"
        assert _snapshot_age_bucket(15) == "15-60s"
        assert _snapshot_age_bucket(60) == "15-60s"
        assert _snapshot_age_bucket(61) == ">60s"

    def test_signal_age_measured_from_signal_row(self, session):
        seed(session, "KXA-1", signal_minutes_before=3)
        (r,) = rows(session)
        assert r.signal_age_s == pytest.approx(180, abs=2)

    def test_timing_dimensions_present(self, session):
        seed(session, "KXA-1", forecast_age_s=400, snapshot_age_s=70, later_mid=0.52)
        report = build(session)
        assert "5-15m" in report["dimensions"]["forecast_age_bucket"]
        assert ">60s" in report["dimensions"]["snapshot_age_bucket"]
        assert "2-5m" in report["dimensions"]["signal_age_bucket"]


# --- cohort breakdown / verdicts ---------------------------------------------------


def seed_cohort(session, n, *, prefix, later_for=lambda i: 0.46, **kw):
    for i in range(n):
        seed(session, f"{prefix}-{i}", later_mid=later_for(i), **kw)


class TestVerdicts:
    def test_too_thin_below_sample_floor(self, session):
        seed_cohort(session, MIN_VERDICT_SAMPLES - 1, prefix="KXT")
        report = build(session)
        assert report["overall"]["verdict"] == VERDICT_TOO_THIN

    def test_adverse_selection_label(self, session):
        """Low toward rate + continuation dominating, with fresh forecasts and
        fresh snapshots (so staleness rules don't fire first)."""
        seed_cohort(
            session, 14, prefix="KXADV", gap=0.10,
            later_for=lambda i: 0.46,        # every row continues away
            forecast_age_s=30, snapshot_age_s=5,
        )
        report = build(session)
        o = report["overall"]
        assert o["toward_rate_final"] == 0.0
        assert o["continued_away_rate"] == 1.0
        assert o["verdict"] == VERDICT_ADVERSE_SELECTION

    def test_stale_or_chasing_label(self, session):
        """Gap opposes the pre-move in every row AND forecasts are old."""
        seed_cohort(
            session, 14, prefix="KXCHASE", gap=-0.10,
            later_for=lambda i: 0.54,        # continues away too
            forecast_age_s=600,              # stale forecast (p50 > 240s)
            snapshot_age_s=5,
        )
        # add the pre-move rise to every row
        for i in range(14):
            snap = session.query(EdgePrecheckSnapshot).filter_by(
                market_ticker=f"KXCHASE-{i}").one()
            tick(session, f"KXCHASE-{i}",
                 at=snap.created_at.replace(tzinfo=timezone.utc) - timedelta(minutes=8),
                 mid=0.44)
        session.commit()
        report = build(session)
        o = report["overall"]
        assert o["gap_opposes_move_share"] == 1.0
        assert o["verdict"] == VERDICT_STALE_OR_CHASING

    def test_artifact_label_on_stale_snapshots(self, session):
        seed_cohort(
            session, 14, prefix="KXART", gap=0.10,
            later_for=lambda i: 0.51 if i % 2 else 0.49,  # mixed, mild
            forecast_age_s=30,
            snapshot_age_s=ARTIFACT_SNAPSHOT_P50_SECONDS + 30,
        )
        report = build(session)
        assert report["overall"]["verdict"] == VERDICT_ARTIFACT

    def test_promising_needs_more_sample(self, session):
        seed_cohort(
            session, 14, prefix="KXPRO", gap=0.10,
            later_for=lambda i: 0.54,        # every row reverts toward
            forecast_age_s=30, snapshot_age_s=5,
        )
        report = build(session)
        o = report["overall"]
        assert o["toward_rate_final"] == 1.0
        assert o["verdict"] == VERDICT_PROMISING

    def test_neutral_when_no_mechanism_dominates(self, session):
        # 50/50 toward/away, fresh everything, big sample
        seed_cohort(
            session, 40, prefix="KXN", gap=0.10,
            later_for=lambda i: 0.54 if i % 2 else 0.46,
            forecast_age_s=30, snapshot_age_s=5,
        )
        report = build(session)
        assert report["overall"]["verdict"] == VERDICT_NEUTRAL

    def test_cohort_dimensions_split(self, session):
        seed(session, "KXW-1", market_type="winner", later_mid=0.54)
        seed(session, "KXS-1", market_type="spread", later_mid=0.46)
        report = build(session)
        mt = report["dimensions"]["market_type"]
        assert set(mt) == {"winner", "spread"}
        assert mt["winner"]["toward_rate_final"] == 1.0
        assert mt["spread"]["toward_rate_final"] == 0.0

    def test_gap_sign_dimension(self, session):
        seed(session, "KXP-1", gap=0.10, later_mid=0.54)
        seed(session, "KXM-1", gap=-0.10, later_mid=0.54)
        report = build(session)
        gs = report["dimensions"]["gap_sign"]
        assert gs["positive"]["toward_rate_final"] == 1.0
        assert gs["negative"]["toward_rate_final"] == 0.0

    def test_series_and_forecaster_dimensions(self, session):
        seed(session, "KXMLBTOTAL-X1", later_mid=0.52)
        report = build(session)
        assert "KXMLBTOTAL" in report["dimensions"]["series"]
        assert "baseball_evidence" in report["dimensions"]["forecaster"]
        assert "source_backed" in report["dimensions"]["evidence"]


# --- microstructure drift ---------------------------------------------------------


class TestMicrostructure:
    def test_spread_and_liquidity_change_measured(self, session):
        seed(session, "KXA-1", spread=1, liquidity=1_000_000,
             later_mid=0.46, later_spread=4, later_liquidity=1_500_000)
        (r,) = rows(session)
        assert r.spread_change_60m == 3
        assert r.liquidity_change_60m == 500_000


# --- failure examples ---------------------------------------------------------------


class TestFailureExamples:
    def test_largest_negative_closure_sorted(self, session):
        seed(session, "KXBAD-1", gap=0.10, midpoint=0.50, later_mid=0.40)  # closure -1.0
        seed(session, "KXOK-1", gap=0.10, midpoint=0.50, later_mid=0.48)   # closure -0.2
        report = build(session, top=2)
        worst = report["failure_examples"]["largest_negative_closure"]
        assert worst[0]["ticker"] == "KXBAD-1"

    def test_repeated_ticker_failures(self, session):
        for i in range(3):
            seed(session, "KXREPEAT-1", minutes_ago=90 + i * 70, later_mid=0.46)
        seed(session, "KXFINE-1", later_mid=0.54)
        report = build(session)
        repeats = report["failure_examples"]["repeated_ticker_failures"]
        assert len(repeats) == 1
        assert repeats[0]["ticker"] == "KXREPEAT-1"
        assert repeats[0]["rows"] == 3

    def test_fresh_forecast_adverse_section(self, session):
        seed(session, "KXFRESH-1", forecast_age_s=30, later_mid=0.46)
        seed(session, "KXOLD-1", forecast_age_s=900, later_mid=0.46)
        report = build(session)
        fresh = report["failure_examples"]["fresh_forecast_adverse"]
        assert [e["ticker"] for e in fresh] == ["KXFRESH-1"]

    def test_stale_snapshot_section(self, session):
        seed(session, "KXSTALE-1", snapshot_age_s=120, later_mid=0.52)
        seed(session, "KXFRESH-2", snapshot_age_s=5, later_mid=0.52)
        report = build(session)
        stale = report["failure_examples"]["stale_snapshot_rows"]
        assert [e["ticker"] for e in stale] == ["KXSTALE-1"]


# --- CLI / rendering ---------------------------------------------------------------


class TestCLI:
    def test_cli_parses_options(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "edge_followthrough_diagnostic_report", fake)
        rc = cli.main(["edge-followthrough-diagnostic-report", "--hours", "48", "--top", "3"])
        assert rc == 0
        assert captured == {"hours": 48, "top": 3}

    def test_cli_renders_report(self, session, capsys):
        seed(session, "KXA-1", later_mid=0.46, pre_mid=0.44)
        n = asyncio.run(cli.edge_followthrough_diagnostic_report(session=session))
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "OVERALL VERDICT" in out
        assert "gap_opposes_move_share" in out
        assert "failure examples" in out

    def test_cli_empty_window_ok(self, session, capsys):
        n = asyncio.run(cli.edge_followthrough_diagnostic_report(session=session))
        assert n == 0
        assert "rows=0" in capsys.readouterr().out


# --- safety -----------------------------------------------------------------------


class TestSafety:
    def test_analysis_persists_nothing(self, session):
        seed(session, "KXA-1", later_mid=0.46)
        session.commit()
        import sqlalchemy

        counts_before = {
            t: session.execute(sqlalchemy.text(f"select count(*) from {t}")).scalar()
            for t in ("edge_precheck_snapshots", "market_price_ticks",
                      "market_forecasts", "opportunity_signals")
        }
        build(session)
        session.commit()
        counts_after = {
            t: session.execute(sqlalchemy.text(f"select count(*) from {t}")).scalar()
            for t in counts_before
        }
        assert counts_before == counts_after

    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "edge_followthrough.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        # "execute_trade"/"execution", not bare "execute" — session.execute() is
        # SQLAlchemy's query API, unrelated to trade execution.
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "opportunity_cost", "pnl", "profit", "swap", "jupiter",
                    "recommend_trade", "execute_trade", "execution"):
            assert bad not in code

    def test_verdict_vocabulary_is_measurement_language(self):
        from app.services import edge_followthrough as mod

        values = " ".join(
            str(getattr(mod, n)) for n in dir(mod) if n.startswith("VERDICT_")
        ).lower()
        for bad in ("trade", "buy", "sell", "bet", "ev", "profit", "edge_found"):
            assert bad not in values

    def test_no_live_network(self):
        src = (REPO / "app" / "services" / "edge_followthrough.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src

    def test_report_has_no_advice_fields(self, session):
        seed(session, "KXA-1", later_mid=0.46)
        report = build(session)
        blob = " ".join(report["overall"].keys()).lower()
        for bad in ("side", "size", "action", "recommendation_to", "order"):
            assert bad not in blob
