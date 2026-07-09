"""COST-MODEL-001 tests: read-only cost-adjusted follow-through measurement.

Spread math, bid/ask touch direction math (both gap signs), missing-quote
handling, the conservative fee assumption (config default + override), the
label ladder (too_thin / cost_killed / neutral / promising_friction_adjusted_
shadow with the out-of-sample requirement for pre-registered policy cohorts),
cohort breakdowns, report rendering, no persistence, no network, no forbidden
vocabulary. In-memory SQLite; nothing live is touched.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.config import Settings
from app.db import Base
from app.models import (
    EdgePrecheckSnapshot,
    MarketForecastRecord,
    MarketPriceTick,
    OpportunitySignal,
)
from app.services.edge_cost import (
    LABEL_COST_KILLED,
    LABEL_NEUTRAL,
    LABEL_PROMISING,
    LABEL_TOO_THIN,
    EdgeCostShadowReportService,
    compute_row_cost,
    fee_pts,
    half_spread_pts,
    label_cohort,
    touch_move_pts,
    toward_move_pts,
)
from app.services.edge_followthrough import RowDiagnostic
from app.services.edge_selection import WINDOW_DISCOVERY, WINDOW_VALIDATION

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]


# --- pure math ---------------------------------------------------------------------


class TestSpreadMath:
    def test_half_spread_pts(self):
        assert half_spread_pts(2) == pytest.approx(0.01)
        assert half_spread_pts(1) == pytest.approx(0.005)
        assert half_spread_pts(0) == 0.0
        assert half_spread_pts(None) is None
        assert half_spread_pts(-1) is None

    def test_toward_move_signs(self):
        # gap > 0 (forecast above): rising market = toward
        assert toward_move_pts(0.50, 0.56, 0.10) == pytest.approx(0.06)
        # gap < 0 (forecast below): falling market = toward
        assert toward_move_pts(0.50, 0.44, -0.10) == pytest.approx(0.06)
        assert toward_move_pts(0.50, 0.56, -0.10) == pytest.approx(-0.06)


class TestTouchMath:
    def test_gap_positive_scores_ask_to_bid(self):
        # forecast above market: trigger ask 0.45 -> horizon bid 0.55
        assert touch_move_pts(0.43, 0.45, 0.55, 0.57, 0.10) == pytest.approx(0.10)

    def test_gap_negative_scores_bid_to_ask(self):
        # forecast below market: trigger bid 0.55 -> horizon ask 0.45
        assert touch_move_pts(0.55, 0.57, 0.43, 0.45, -0.10) == pytest.approx(0.10)

    def test_touch_is_harsher_than_midpoint(self):
        # same midpoints as toward_move (0.50 -> 0.56 = +0.06) but the touch
        # round trip pays both spreads: ask0 0.51 -> bid_h 0.55 = +0.04
        assert touch_move_pts(0.49, 0.51, 0.55, 0.57, 0.10) == pytest.approx(0.04)

    def test_missing_quotes_return_none(self):
        assert touch_move_pts(None, None, 0.55, 0.57, 0.10) is None
        assert touch_move_pts(0.49, 0.51, None, None, 0.10) is None
        assert touch_move_pts(0.49, None, 0.55, 0.57, 0.10) is None   # need ask0
        assert touch_move_pts(None, 0.51, 0.55, 0.57, -0.10) is None  # need bid0


class TestFeeMath:
    def test_fee_charged_at_both_ends(self):
        # 0.07 * (0.5*0.5 + 0.56*0.44)
        assert fee_pts(0.50, 0.56, 0.07) == pytest.approx(0.034748)

    def test_fee_rate_zero(self):
        assert fee_pts(0.50, 0.56, 0.0) == 0.0

    def test_fee_smaller_at_extreme_probabilities(self):
        assert fee_pts(0.95, 0.97, 0.07) < fee_pts(0.50, 0.50, 0.07)

    def test_config_default_documented_conservative(self):
        assert Settings(_env_file=None).kalshi_fee_rate_assumption == 0.07


# --- compute_row_cost (pure; fabricated ticks) --------------------------------------


def mk_row(gap=0.10, forecast=0.60, spread_cents=1, **kw) -> RowDiagnostic:
    defaults = dict(
        snapshot_id=1, market_ticker="KXMLBTOTAL-26JUL09AAA-7", series="KXMLBTOTAL",
        created_at=NOW - timedelta(minutes=90), market_type="total",
        signal_type="price_move_threshold", gap=gap, abs_gap=abs(gap),
        forecast_probability=forecast, spread_cents=spread_cents,
    )
    defaults.update(kw)
    return RowDiagnostic(**defaults)


def mk_tick(mid, bid=None, ask=None, spread=None):
    return MarketPriceTick(
        market_ticker="KXMLBTOTAL-26JUL09AAA-7", observed_at=NOW,
        yes_bid=bid, yes_ask=ask, midpoint=mid, spread=spread,
    )


class TestComputeRowCost:
    def test_full_cost_stack(self):
        c = compute_row_cost(
            mk_row(), mk_tick(0.44, bid=45, ask=47), mk_tick(0.56, bid=55, ask=57),
            fee_rate=0.07,
        )
        assert c.midpoint_trigger == pytest.approx(0.50)   # forecast - gap
        assert c.frictionless_closure == pytest.approx(0.6)      # 0.06/0.10
        assert c.frictionless_toward is True

    def test_numbers(self):
        c = compute_row_cost(
            mk_row(), mk_tick(0.44, bid=45, ask=47), mk_tick(0.56, bid=55, ask=57),
            fee_rate=0.07,
        )
        assert c.net_half_spread_closure == pytest.approx(0.55)  # (0.06-0.005)/0.10
        # (0.06 - 0.005 - 0.0347448)/0.10
        assert c.fee_adjusted_closure == pytest.approx(0.2026, abs=1e-4)
        assert c.touch_closure == pytest.approx((0.55 - 0.47) / 0.10)
        assert c.touch_covered is True

    def test_missing_trigger_quotes_uncovered_but_measured(self):
        c = compute_row_cost(
            mk_row(), None, mk_tick(0.56, bid=55, ask=57), fee_rate=0.07
        )
        assert c is not None
        assert c.touch_covered is False
        assert c.touch_closure is None
        assert c.frictionless_closure == pytest.approx(0.6)

    def test_missing_horizon_tick_unmeasurable(self):
        assert compute_row_cost(mk_row(), mk_tick(0.44, bid=45, ask=47), None, 0.07) is None

    def test_spread_falls_back_to_trigger_tick(self):
        c = compute_row_cost(
            mk_row(spread_cents=None), mk_tick(0.44, bid=45, ask=47, spread=2),
            mk_tick(0.56, bid=55, ask=57), fee_rate=0.0,
        )
        assert c.net_half_spread_closure == pytest.approx((0.06 - 0.01) / 0.10)

    def test_zero_fee_rate_makes_fee_equal_half_spread(self):
        c = compute_row_cost(
            mk_row(), mk_tick(0.44, bid=45, ask=47), mk_tick(0.56, bid=55, ask=57),
            fee_rate=0.0,
        )
        assert c.fee_adjusted_closure == pytest.approx(c.net_half_spread_closure)

    def test_gap_negative_direction(self):
        # forecast 0.40, m0 0.50, horizon mid 0.44: toward move +0.06
        c = compute_row_cost(
            mk_row(gap=-0.10, forecast=0.40),
            mk_tick(0.50, bid=49, ask=51), mk_tick(0.44, bid=43, ask=45),
            fee_rate=0.0,
        )
        assert c.frictionless_closure == pytest.approx(0.6)
        assert c.touch_closure == pytest.approx((0.49 - 0.45) / 0.10)


# --- labels (pure) -------------------------------------------------------------------


def summ(n=100, dimension="market_type", toward=0.60, frictionless=0.5,
         fee_adj=0.10, touch=0.10, ticker=0.10, game=0.20):
    return {
        "final_n": n, "dimension": dimension, "toward_rate_60m": toward,
        "frictionless_closure_60m": frictionless,
        "fee_adjusted_net_closure_60m": fee_adj,
        "executable_touch_closure_60m": touch,
        "max_ticker_share": ticker, "max_game_share": game,
    }


class TestLabels:
    def test_promising_friction_adjusted(self):
        label, reason = label_cohort(summ(), WINDOW_DISCOVERY)
        assert label == LABEL_PROMISING
        assert "authorizes nothing" in reason

    def test_prereg_policy_requires_out_of_sample(self):
        label, reason = label_cohort(
            summ(dimension="preregistered_policy"), WINDOW_DISCOVERY
        )
        assert label == LABEL_NEUTRAL
        assert "out-of-sample" in reason
        label, _ = label_cohort(
            summ(dimension="preregistered_policy"), WINDOW_VALIDATION
        )
        assert label == LABEL_PROMISING

    def test_cost_killed_positive_frictionless_negative_after_fees(self):
        label, reason = label_cohort(summ(fee_adj=-0.05, touch=0.02), WINDOW_VALIDATION)
        assert label == LABEL_COST_KILLED
        assert "does not survive" in reason

    def test_cost_killed_positive_frictionless_negative_touch(self):
        label, _ = label_cohort(summ(fee_adj=0.05, touch=-0.02), WINDOW_VALIDATION)
        assert label == LABEL_COST_KILLED

    def test_too_thin(self):
        label, _ = label_cohort(summ(n=5), WINDOW_VALIDATION)
        assert label == LABEL_TOO_THIN

    def test_neutral_when_never_positive(self):
        label, _ = label_cohort(
            summ(toward=0.30, frictionless=-0.4, fee_adj=-0.6, touch=-0.7),
            WINDOW_VALIDATION,
        )
        assert label == LABEL_NEUTRAL

    def test_promising_blocked_by_concentration(self):
        label, _ = label_cohort(summ(ticker=0.50), WINDOW_VALIDATION)
        assert label != LABEL_PROMISING

    def test_promising_needs_n75(self):
        label, _ = label_cohort(summ(n=40), WINDOW_VALIDATION)
        assert label != LABEL_PROMISING

    def test_promising_needs_toward_bar(self):
        label, _ = label_cohort(summ(toward=0.52), WINDOW_VALIDATION)
        assert label != LABEL_PROMISING


# --- end-to-end over DB ---------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def tick(session, ticker, *, at, mid, bid=None, ask=None, spread=1):
    session.add(MarketPriceTick(
        market_ticker=ticker, observed_at=at,
        yes_bid=bid if bid is not None else int(mid * 100) - 1,
        yes_ask=ask if ask is not None else int(mid * 100) + 1,
        midpoint=mid, spread=spread, volume_24h=100,
        liquidity_proxy=1_000_000, created_at=at,
    ))


def seed(session, ticker, *, gap=0.10, midpoint=0.50, minutes_ago=90,
         later_mid=None, pre_mid=None, market_type="total", spread=1):
    created = NOW - timedelta(minutes=minutes_ago)
    f = MarketForecastRecord(
        market_ticker=ticker, forecaster_name="baseball_evidence",
        forecaster_version="v1", prompt_version="v1",
        estimated_probability=midpoint + gap, confidence=0.62,
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
        forecast_probability=midpoint + gap, forecast_confidence=0.62,
        market_midpoint=midpoint, spread_cents=spread,
        liquidity_proxy_cents=1_000_000, probability_gap=gap,
        abs_probability_gap=abs(gap), status="watchlist",
        invalidation_reasons=[], persistence_count=1,
        forecast_age_seconds=60, market_snapshot_age_seconds=10,
        tags=["domain:sports_baseball", f"market_type:{market_type}"],
        created_at=created,
    ))
    if pre_mid is not None:
        tick(session, ticker, at=created - timedelta(minutes=8), mid=pre_mid)
    if later_mid is not None:
        tick(session, ticker, at=created + timedelta(minutes=5), mid=later_mid)
    session.commit()


def build(session, **kw):
    return EdgeCostShadowReportService().build(session, **kw)


def cohort(report, name):
    return next(c for c in report["cohorts"] if c["name"] == name)


class TestEndToEnd:
    def test_report_structure_and_dimensions(self, session):
        seed(session, "KXMLBTOTAL-GAAA-7", pre_mid=0.44, later_mid=0.56)
        seed(session, "KXMLBSPREAD-GBBB-2", market_type="spread",
             pre_mid=0.56, later_mid=0.42)
        r = build(session)
        dims = {c["dimension"] for c in r["cohorts"]}
        assert {"baseline", "preregistered_policy", "market_type", "gap_vs_move",
                "liquidity_bucket", "spread_bucket", "confidence_bucket",
                "series"} <= dims
        prereg = [c["name"] for c in r["cohorts"]
                  if c["dimension"] == "preregistered_policy"]
        assert "require_gap_follows_move_totals_only" in prereg
        assert "spread_only" in prereg
        assert r["rows_measurable"] == 2
        assert r["touch_coverage"] == 1.0
        assert "MVP-005B remains blocked" in r["mvp_005b_note"]

    def test_baseline_cost_numbers_from_seeded_book(self, session):
        seed(session, "KXMLBTOTAL-GAAA-7", pre_mid=0.44, later_mid=0.56)
        r = build(session)
        base = cohort(r, "baseline_all_rows")
        assert base["frictionless_closure_60m"] == pytest.approx(0.6)
        assert base["net_closure_after_half_spread_60m"] == pytest.approx(0.55)
        # touch: ask0=0.45 -> bid_h=0.55 = +0.10 -> closure 1.0
        assert base["executable_touch_closure_60m"] == pytest.approx(1.0)

    def test_fee_rate_override(self, session):
        seed(session, "KXMLBTOTAL-GAAA-7", pre_mid=0.44, later_mid=0.56)
        r0 = build(session, fee_rate=0.0)
        base = cohort(r0, "baseline_all_rows")
        assert base["fee_adjusted_net_closure_60m"] == pytest.approx(
            base["net_closure_after_half_spread_60m"]
        )
        assert r0["fee_rate_assumption"] == 0.0

    def test_missing_quote_coverage_counted(self, session):
        seed(session, "KXMLBTOTAL-GAAA-7", pre_mid=0.44, later_mid=0.56)
        # row with a horizon tick that has no quotes -> measurable, uncovered
        created = NOW - timedelta(minutes=90)
        seed(session, "KXMLBTOTAL-GCCC-9", later_mid=None, pre_mid=None)
        session.add(MarketPriceTick(
            market_ticker="KXMLBTOTAL-GCCC-9", observed_at=created + timedelta(minutes=5),
            yes_bid=None, yes_ask=None, midpoint=0.56, spread=None,
            volume_24h=0, liquidity_proxy=0, created_at=created,
        ))
        session.commit()
        r = build(session)
        assert r["rows_measurable"] == 2
        assert r["touch_coverage"] == pytest.approx(0.5)

    def test_survivor_list_requires_positive_after_costs(self, session):
        # strongly toward with tight book -> survives; adverse spread -> not
        for i in range(15):
            seed(session, f"KXMLBTOTAL-G{i:03d}A-7", pre_mid=0.44, later_mid=0.62)
        for i in range(15):
            seed(session, f"KXMLBSPREAD-S{i:03d}B-2", market_type="spread",
                 pre_mid=0.56, later_mid=0.40)
        r = build(session)
        assert "market_type:total" in r["cohorts_positive_after_costs"]
        assert "market_type:spread" not in r["cohorts_positive_after_costs"]

    def test_cost_killed_cohort_live_path(self, session):
        # positive frictionless (+0.02 pts) but half-spread+fees+touch eat it:
        # m0 0.50 -> mh 0.52 with a 4c spread book
        for i in range(15):
            seed(session, f"KXMLBTOTAL-G{i:03d}A-7", spread=4,
                 pre_mid=0.44, later_mid=0.52)
        # widen the seeded book: pre tick ask=0.46+? default helper: bid/ask ±1c of mid
        r = build(session)
        base = cohort(r, "baseline_all_rows")
        assert base["frictionless_closure_60m"] > 0
        assert base["fee_adjusted_net_closure_60m"] < 0
        assert base["label"] == LABEL_COST_KILLED


# --- CLI ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_parses(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "edge_cost_shadow_report", fake)
        rc = cli.main(["edge-cost-shadow-report", "--hours", "48", "--top", "3"])
        assert rc == 0
        assert captured == {"hours": 48, "top": 3}

    def test_cli_renders(self, session, capsys):
        seed(session, "KXMLBTOTAL-GAAA-7", pre_mid=0.44, later_mid=0.56)
        n = asyncio.run(cli.edge_cost_shadow_report(session=session))
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "baseline_all_rows" in out
        assert "MVP-005B remains blocked" in out
        assert "cohorts positive after costs" in out

    def test_cli_empty_window(self, session, capsys):
        n = asyncio.run(cli.edge_cost_shadow_report(session=session))
        assert n == 0
        assert "population=0" in capsys.readouterr().out


# --- safety --------------------------------------------------------------------------


class TestSafety:
    def test_persists_nothing(self, session):
        seed(session, "KXMLBTOTAL-GAAA-7", pre_mid=0.44, later_mid=0.56)
        import sqlalchemy

        tables = ("edge_precheck_snapshots", "market_price_ticks", "market_forecasts")
        before = {
            t: session.execute(sqlalchemy.text(f"select count(*) from {t}")).scalar()
            for t in tables
        }
        build(session)
        session.commit()
        after = {
            t: session.execute(sqlalchemy.text(f"select count(*) from {t}")).scalar()
            for t in tables
        }
        assert before == after

    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "edge_cost.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "jupiter", "recommend_trade",
                    "execute_trade", "execution", "entry_price", "exit_price",
                    "buy", "sell"):
            assert bad not in code, bad

    def test_no_live_network(self):
        src = (REPO / "app" / "services" / "edge_cost.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src

    def test_note_language(self, session):
        seed(session, "KXMLBTOTAL-GAAA-7", pre_mid=0.44, later_mid=0.56)
        r = build(session)
        assert "never advice" in r["note"]
        assert "not EV" in r["note"]
