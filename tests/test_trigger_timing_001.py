"""TRIGGER-TIMING-001 tests: shadow simulation of measurement timing.

Delayed-measurement tick selection (post-trigger only), missing-tick and
gap-evaporation losses, follow-through measured FROM the delayed time,
flat/spread-stable/gap-follows condition policies, the conservative label
ladder reuse, comparison content, rendering, no persistence, no network, no
forbidden vocabulary. TickSeries policies are pure Python (no DB needed);
end-to-end runs use in-memory SQLite. Nothing live is touched.
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
from app.services.edge_followthrough import RowDiagnostic
from app.services.trigger_timing import (
    LOSS_CONDITION_NEVER_MET,
    LOSS_GAP_EVAPORATED,
    LOSS_NO_TICK,
    MIN_ABS_GAP,
    Tick,
    TickSeries,
    TriggerTimingShadowReportService,
    measure_at,
)

NOW = datetime.now(timezone.utc)
T0 = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[1]


def mk_row(ticker="KXA-1", forecast=0.60, created=T0, market_type="total") -> RowDiagnostic:
    return RowDiagnostic(
        snapshot_id=1, market_ticker=ticker, series=ticker.split("-")[0],
        created_at=created, market_type=market_type,
        signal_type="price_move_threshold", forecaster="baseball_evidence",
        evidence_depth="source_backed", gap=0.10, abs_gap=0.10,
        forecast_probability=forecast,
    )


def mk_series(points) -> TickSeries:
    """points: [(minutes_from_T0, mid[, spread[, liquidity]])]"""
    ticks = []
    for p in points:
        minutes, mid = p[0], p[1]
        spread = p[2] if len(p) > 2 else 1
        liq = p[3] if len(p) > 3 else 1_000_000
        ticks.append(Tick(at=T0 + timedelta(minutes=minutes), mid=mid,
                          spread=spread, liquidity=liq))
    return TickSeries(ticks)


# --- measure_at (pure) ------------------------------------------------------------


class TestMeasureAt:
    def test_delayed_measurement_uses_last_post_trigger_tick(self):
        series = mk_series([(-5, 0.48), (1, 0.50), (4, 0.45), (7, 0.44)])
        sim = measure_at(mk_row(), series, T0 + timedelta(minutes=5))
        assert not isinstance(sim, str)
        assert sim.midpoint == pytest.approx(0.45)      # tick at +4m, not +7m
        assert sim.gap == pytest.approx(0.15)
        assert sim.delay_s == pytest.approx(300)

    def test_delayed_measurement_never_uses_pre_trigger_tick(self):
        # only pre-trigger ticks exist => a delayed measurement is LOST, not
        # silently taken on the stale pre-trigger book
        series = mk_series([(-5, 0.48), (-1, 0.50)])
        result = measure_at(mk_row(), series, T0 + timedelta(minutes=5))
        assert result == LOSS_NO_TICK

    def test_immediate_measurement_uses_book_at_trigger(self):
        series = mk_series([(-1, 0.50), (3, 0.45)])
        sim = measure_at(mk_row(), series, T0)
        assert sim.midpoint == pytest.approx(0.50)
        assert sim.delay_s == 0

    def test_gap_evaporated_when_market_reached_forecast(self):
        # by +10m the market moved to 0.58; |0.60-0.58| < MIN_ABS_GAP
        series = mk_series([(1, 0.50), (9, 0.58), (20, 0.60)])
        result = measure_at(mk_row(), series, T0 + timedelta(minutes=10))
        assert result == LOSS_GAP_EVAPORATED

    def test_followthrough_measured_from_delayed_time(self):
        # delayed measurement at +10m (mid 0.45, gap 0.15); market reverts to
        # 0.525 by +40m => closure from DELAYED point = (0.525-0.45)/0.15 = +0.5
        series = mk_series([(1, 0.50), (9, 0.45), (40, 0.525)])
        sim = measure_at(mk_row(), series, T0 + timedelta(minutes=10))
        assert sim.closures["60m"] == pytest.approx(0.5)
        assert sim.paths["60m"] == "reverted_toward"

    def test_pre_move_and_opposes_at_delayed_time(self):
        # at +10m: mid 0.45; 10m-before window (0m..10m) earliest tick 0.50 =>
        # pre_move -0.05 (falling); gap +0.15 (forecast above) => opposes False?
        # gap>0, pre_move<0 -> (True) != (False) -> opposes True
        series = mk_series([(1, 0.50), (9, 0.45), (30, 0.45)])
        sim = measure_at(mk_row(), series, T0 + timedelta(minutes=10))
        assert sim.pre_move == pytest.approx(-0.05)
        assert sim.sharp_pre_move is True
        assert sim.gap_opposes_move is True

    def test_gap_follows_when_market_moving_toward_forecast(self):
        # market RISING toward the 0.60 forecast: gap + and pre_move + => follows
        series = mk_series([(1, 0.40), (9, 0.48), (30, 0.50)])
        sim = measure_at(mk_row(), series, T0 + timedelta(minutes=10))
        assert sim.gap_opposes_move is False

    def test_missing_forecast_probability_lost(self):
        row = mk_row()
        row.forecast_probability = None
        assert measure_at(row, mk_series([(1, 0.5)]), T0) == LOSS_NO_TICK


# --- condition detection (pure) -----------------------------------------------------


class TestConditions:
    def test_flat_window_detected(self):
        # volatile until +10m, then flat 0.45±0.005 for 6 minutes
        series = mk_series([
            (1, 0.50), (3, 0.46), (5, 0.52), (8, 0.47),
            (10, 0.45), (12, 0.452), (14, 0.449), (15, 0.451),
        ])
        end = series.first_flat_end(T0, band=0.01, window_min=5, max_wait_min=30)
        assert end == T0 + timedelta(minutes=15)   # anchor at +10m + 5m window

    def test_flat_never_met_returns_none(self):
        series = mk_series([(i, 0.40 + 0.03 * ((i // 2) % 2)) for i in range(1, 30, 2)])
        assert series.first_flat_end(T0, 0.01, 5, 30) is None

    def test_flat_requires_two_ticks_in_window(self):
        series = mk_series([(1, 0.50), (20, 0.50)])   # isolated ticks
        assert series.first_flat_end(T0, 0.01, 5, 30) is None

    def test_spread_stable_detected(self):
        series = mk_series([
        (1, 0.50, 5), (3, 0.48, 3), (6, 0.47, 1), (8, 0.46, 1), (10, 0.46, 1),
        ])
        end = series.first_spread_stable_end(T0, window_min=5, max_wait_min=30)
        assert end == T0 + timedelta(minutes=11)   # anchor +6m + 5m

    def test_spread_stable_never_met(self):
        series = mk_series([(i, 0.5, i % 4 + 1) for i in range(1, 20, 2)])
        assert series.first_spread_stable_end(T0, 5, 30) is None


# --- end-to-end over DB ---------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def tick_db(session, ticker, *, minutes, mid, spread=1):
    at = NOW - timedelta(minutes=180) + timedelta(minutes=minutes)
    session.add(MarketPriceTick(
        market_ticker=ticker, observed_at=at,
        yes_bid=int(mid * 100) - 1, yes_ask=int(mid * 100) + 1, midpoint=mid,
        spread=spread, volume_24h=100, liquidity_proxy=1_000_000, created_at=at,
    ))


def seed(session, ticker, *, forecast=0.60, midpoint=0.50, ticks=()):
    """Watchlist row measured at T=(NOW-180m); `ticks` are (minutes, mid[, spread])
    relative to that trigger time (minute 0 = trigger)."""
    created = NOW - timedelta(minutes=180)
    f = MarketForecastRecord(
        market_ticker=ticker, forecaster_name="baseball_evidence",
        forecaster_version="v1", prompt_version="v1",
        estimated_probability=forecast, confidence=0.62,
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
        forecast_probability=forecast, forecast_confidence=0.62,
        market_midpoint=midpoint, spread_cents=1, liquidity_proxy_cents=1_000_000,
        probability_gap=round(forecast - midpoint, 4),
        abs_probability_gap=abs(round(forecast - midpoint, 4)),
        status="watchlist", invalidation_reasons=[], persistence_count=1,
        forecast_age_seconds=60, market_snapshot_age_seconds=10,
        tags=["domain:sports_baseball", "market_type:total"],
        created_at=created,
    ))
    for t in ticks:
        tick_db(session, ticker, minutes=t[0], mid=t[1],
                spread=(t[2] if len(t) > 2 else 1))
    session.commit()


def build(session, hours=24, top=5):
    return TriggerTimingShadowReportService().build(session, hours=hours, top=top)


def policy(report, name):
    return next(p for p in report["policies"] if p["name"] == name)


class TestEndToEnd:
    def test_all_policies_present_with_baseline_first(self, session):
        seed(session, "KXA-1", ticks=[(0, 0.50), (30, 0.46), (70, 0.44)])
        r = build(session)
        names = [p["name"] for p in r["policies"]]
        assert names[0] == "baseline_immediate"
        assert set(names) == set(TriggerTimingShadowReportService.POLICY_NAMES)

    def test_cooldown_improves_when_market_reverts_after_trigger_spike(self, session):
        """Market spikes down at trigger, keeps falling briefly, then reverts:
        immediate measurement rides the continuation (negative closure); a 10m
        delayed measurement catches the reversion (positive closure)."""
        seed(session, "KXREV-1", forecast=0.60, midpoint=0.50, ticks=[
            (0, 0.50), (8, 0.44), (35, 0.52), (65, 0.52),
        ])
        r = build(session)
        base = policy(r, "baseline_immediate")
        d10 = policy(r, "delay_10m")
        assert base["follow_through"]["60m"]["mean_gap_closure_pct"] < 0.3
        assert d10["follow_through"]["60m"]["mean_gap_closure_pct"] > 0.4
        ex = r["examples"]["improved_by_delay_10m"]
        assert ex and ex[0]["ticker"] == "KXREV-1"

    def test_gap_evaporation_counted_per_policy(self, session):
        # market reaches the forecast by +10m => the 10m delay loses the row
        seed(session, "KXEV-1", forecast=0.60, midpoint=0.50, ticks=[
            (0, 0.50), (9, 0.58), (40, 0.58),
        ])
        r = build(session)
        d10 = policy(r, "delay_10m")
        assert d10["rows_measurable"] == 0
        assert d10["rows_lost"].get(LOSS_GAP_EVAPORATED) == 1

    def test_missing_later_tick_counted(self, session):
        seed(session, "KXNT-1", ticks=[(0, 0.50)])   # nothing after trigger
        r = build(session)
        d5 = policy(r, "delay_5m")
        assert d5["rows_lost"].get(LOSS_NO_TICK) == 1

    def test_condition_never_met_counted(self, session):
        # perpetually volatile: flat/stable/follows never satisfied
        seed(session, "KXVOL-1", forecast=0.90, midpoint=0.50, ticks=[
            (i, 0.40 + 0.06 * ((i // 2) % 2), i % 4 + 1) for i in range(0, 29, 2)
        ])
        r = build(session)
        flat = policy(r, "wait_until_midpoint_flat_5m")
        assert flat["rows_lost"].get(LOSS_CONDITION_NEVER_MET) == 1

    def test_wait_gap_follows_move_measures_at_first_follows_tick(self, session):
        """Market falls away from the forecast (gap opposes while falling), then
        turns and rises toward it — the wait policy measures once the gap
        follows the move."""
        seed(session, "KXWF-1", forecast=0.60, midpoint=0.50, ticks=[
            (0, 0.50), (5, 0.44), (10, 0.42),          # falling: gap + / move - => opposes
            (16, 0.46), (20, 0.48),                    # rising: gap + / move + => follows
            (50, 0.52), (80, 0.52),
        ])
        r = build(session)
        wf = policy(r, "wait_until_gap_follows_move")
        assert wf["rows_measurable"] == 1
        assert (wf["gap_opposes_move_share"] or 0) == 0.0

    def test_labels_attached_via_shared_ladder(self, session):
        seed(session, "KXA-1", ticks=[(0, 0.50), (30, 0.46), (70, 0.44)])
        r = build(session)
        for p in r["policies"]:
            assert p["label"] in ("too_thin", "worse_than_baseline", "neutral",
                                  "promising_shadow", "reject_policy")

    def test_comparison_sections_present(self, session):
        seed(session, "KXA-1", ticks=[(0, 0.50), (30, 0.46), (70, 0.44)])
        r = build(session)
        c = r["comparison"]
        assert "does_delay_reduce_gap_opposes_share" in c
        assert "does_delay_improve_closure" in c
        assert "cooldown_vs_condition_filter" in c
        assert "explicitly-accepted milestone" in c["caveat"]


# --- CLI -----------------------------------------------------------------------------


class TestCLI:
    def test_cli_parses(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "trigger_timing_shadow_report", fake)
        rc = cli.main(["trigger-timing-shadow-report", "--hours", "48", "--top", "3"])
        assert rc == 0
        assert captured == {"hours": 48, "top": 3}

    def test_cli_renders(self, session, capsys):
        seed(session, "KXA-1", ticks=[(0, 0.50), (30, 0.46), (70, 0.44)])
        n = asyncio.run(cli.trigger_timing_shadow_report(session=session))
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "baseline_immediate" in out
        assert "comparison" in out

    def test_cli_empty_window(self, session, capsys):
        n = asyncio.run(cli.trigger_timing_shadow_report(session=session))
        assert n == 0
        assert "population=0" in capsys.readouterr().out


# --- safety --------------------------------------------------------------------------


class TestSafety:
    def test_persists_nothing(self, session):
        seed(session, "KXA-1", ticks=[(0, 0.50), (30, 0.46)])
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

        src = (REPO / "app" / "services" / "trigger_timing.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "jupiter", "recommend_trade",
                    "execute_trade", "execution"):
            assert bad not in code

    def test_no_live_network(self):
        src = (REPO / "app" / "services" / "trigger_timing.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src

    def test_note_and_caveat_language(self, session):
        seed(session, "KXA-1", ticks=[(0, 0.50), (30, 0.46)])
        r = build(session)
        assert "never advice" in r["note"]
        assert "held" in r["comparison"]["caveat"]  # forecast held fixed caveat
