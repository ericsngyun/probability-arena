"""EDGE-FILTER-001 tests: read-only shadow adverse-selection filters.

Policy inclusion/exclusion (gap-vs-move, sharp pre-move, market types, series,
worst-series derivation), the conservative label ladder (too_thin /
promising_shadow with concentration guards / reject_policy / worse_than_baseline
/ neutral), interpretation answers, report rendering, no persistence, no
network, no forbidden vocabulary. In-memory SQLite; nothing live is touched.
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
from app.services.edge_filter_shadow import (
    MIN_READABLE_FINAL_N,
    POLICIES,
    POLICY_NEUTRAL,
    POLICY_PROMISING,
    POLICY_REJECT,
    POLICY_TOO_THIN,
    POLICY_WORSE,
    PROMISING_MIN_FINAL_N,
    EdgeFilterShadowReportService,
    game_of,
    label_policy,
    summarize_policy,
    worst_series,
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
    later_mid=None,
    pre_mid=None,
    market_type="total",
    signal_type="price_move_threshold",
    spread=1,
    liquidity=1_000_000,
    persistence=1,
):
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
        market_ticker=ticker, signal_type=signal_type,
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
        liquidity_proxy_cents=liquidity, probability_gap=gap,
        abs_probability_gap=abs(gap), status="watchlist",
        invalidation_reasons=[], persistence_count=persistence,
        forecast_age_seconds=60, market_snapshot_age_seconds=10,
        tags=["domain:sports_baseball", f"market_type:{market_type}"],
        created_at=created,
    ))
    if pre_mid is not None:
        tick(session, ticker, at=created - timedelta(minutes=8), mid=pre_mid)
    if later_mid is not None:
        tick(session, ticker, at=created + timedelta(minutes=5), mid=later_mid)
    session.commit()


def build(session, hours=24, top=5):
    return EdgeFilterShadowReportService().build(session, hours=hours, top=top)


def policy(report, name):
    return next(p for p in report["policies"] if p["name"] == name)


# --- predicates / inclusion-exclusion --------------------------------------------


class TestPolicyMembership:
    def test_gap_opposes_vs_follows_filtering(self, session):
        # opposes: market rose (0.44->0.50), forecast below (gap -0.10)
        seed(session, "KXOPP-1", gap=-0.10, pre_mid=0.44, later_mid=0.46)
        # follows: market rose, forecast above (gap +0.10)
        seed(session, "KXFOL-1", gap=0.10, pre_mid=0.44, later_mid=0.54)
        # unknown: no pre-tick
        seed(session, "KXUNK-1", gap=0.10, later_mid=0.54)
        r = build(session)

        assert policy(r, "baseline_all_watchlist")["included"] == 3
        excl = policy(r, "exclude_gap_opposes_recent_move")
        assert excl["included"] == 2 and excl["excluded"] == 1   # unknown kept
        req = policy(r, "require_gap_follows_recent_move")
        assert req["included"] == 1                               # only KXFOL

    def test_sharp_pre_move_policies_differ_on_unknown(self, session):
        seed(session, "KXSHARP-1", gap=0.10, pre_mid=0.44, later_mid=0.54)  # sharp (0.06)
        seed(session, "KXCALM-1", gap=0.10, pre_mid=0.49, later_mid=0.54)   # not sharp
        seed(session, "KXUNK-1", gap=0.10, later_mid=0.54)                  # unknown
        r = build(session)

        assert policy(r, "exclude_sharp_pre_move")["included"] == 2      # calm + unknown
        assert policy(r, "require_no_sharp_pre_move")["included"] == 1   # calm only

    def test_market_type_policies(self, session):
        seed(session, "KXT-1", market_type="total", later_mid=0.54)
        seed(session, "KXS-1", market_type="spread", later_mid=0.46)
        seed(session, "KXW-1", market_type="winner", later_mid=0.54)
        r = build(session)

        assert policy(r, "exclude_spread_markets")["included"] == 2
        assert policy(r, "spread_only")["included"] == 1
        assert policy(r, "total_only")["included"] == 1
        assert policy(r, "winner_only")["included"] == 1
        assert policy(r, "spread_only")["market_type_mix"] == {"spread": 1}

    def test_signal_type_policy(self, session):
        seed(session, "KXP-1", signal_type="price_move_threshold", later_mid=0.54)
        seed(session, "KXN-1", signal_type="newly_two_sided", later_mid=0.54)
        r = build(session)
        assert policy(r, "exclude_price_move_threshold")["included"] == 1

    def test_kxmlbspread_exclusion(self, session):
        seed(session, "KXMLBSPREAD-26JUL0-X1", later_mid=0.46)
        seed(session, "KXMLBTOTAL-26JUL0-Y1", later_mid=0.54)
        r = build(session)
        p = policy(r, "exclude_kxmlbspread")
        assert p["included"] == 1
        assert "KXMLBSPREAD" not in p["series_mix"]

    def test_combined_follows_policies(self, session):
        seed(session, "KXA-1", gap=0.10, pre_mid=0.44, later_mid=0.54,
             market_type="total", spread=1, liquidity=2_000_000, persistence=2)
        seed(session, "KXB-1", gap=0.10, pre_mid=0.44, later_mid=0.54,
             market_type="spread", spread=5, liquidity=50_000, persistence=1)
        r = build(session)

        assert policy(r, "gap_follows_move_and_tight_spread")["included"] == 1
        assert policy(r, "gap_follows_move_and_high_liquidity")["included"] == 1
        assert policy(r, "gap_follows_move_and_persistence_gt1")["included"] == 1
        assert policy(r, "require_gap_follows_move_exclude_spreads")["included"] == 1
        assert policy(r, "require_gap_follows_move_totals_only")["included"] == 1
        assert policy(r, "totals_only_no_sharp_pre_move")["included"] == 0  # both sharp/spread

    def test_survival_ratio(self, session):
        for i in range(4):
            seed(session, f"KXT-{i}", market_type="total", later_mid=0.54)
        seed(session, "KXS-1", market_type="spread", later_mid=0.46)
        r = build(session)
        assert policy(r, "total_only")["survival_ratio"] == pytest.approx(0.8)


# --- worst series -------------------------------------------------------------------


class TestWorstSeries:
    def test_worst_series_derived_from_data(self, session):
        for i in range(10):
            seed(session, f"KXBAD-G{i}-X", later_mid=0.46)   # series KXBAD, all away
        for i in range(10):
            seed(session, f"KXGOOD-G{i}-X", later_mid=0.54)  # series KXGOOD, all toward
        r = build(session)
        assert r["worst_series"] == "KXBAD"
        assert policy(r, "exclude_worst_series")["included"] == 10

    def test_worst_series_requires_min_sample(self, session):
        for i in range(3):
            seed(session, f"KXFEW-G{i}-X", later_mid=0.46)   # only 3 rows
        r = build(session)
        assert r["worst_series"] is None                      # nothing qualifies
        assert policy(r, "exclude_worst_series")["included"] == 3  # excludes nothing

    def test_game_of_parsing(self):
        assert game_of("KXMLBTOTAL-26JUL082210COLLAD-14") == "26JUL082210COLLAD"
        assert game_of("KXWEIRD") == "KXWEIRD"


# --- label ladder --------------------------------------------------------------------


def mk_summary(**kw) -> dict:
    base = {
        "name": "x", "included": 50, "excluded": 50, "survival_ratio": 0.5,
        "final_n": 50,
        "follow_through": {
            "30m": {"samples": 50, "moved_toward_rate": 0.3, "mean_gap_closure_pct": -0.2},
            "60m": {"samples": 50, "moved_toward_rate": 0.3, "mean_gap_closure_pct": -0.2},
        },
        "max_ticker_share": 0.1, "max_game_share": 0.2,
    }
    base.update(kw)
    return base


BASELINE = mk_summary(name="baseline_all_watchlist")


class TestLabelLadder:
    def test_too_thin(self):
        s = mk_summary(final_n=MIN_READABLE_FINAL_N - 1)
        assert label_policy(s, BASELINE)[0] == POLICY_TOO_THIN

    def test_promising_by_rate(self):
        ft = {
            "30m": {"samples": 40, "moved_toward_rate": 0.60, "mean_gap_closure_pct": 0.1},
            "60m": {"samples": 40, "moved_toward_rate": 0.50, "mean_gap_closure_pct": 0.05},
        }
        s = mk_summary(final_n=PROMISING_MIN_FINAL_N, follow_through=ft)
        assert label_policy(s, BASELINE)[0] == POLICY_PROMISING

    def test_promising_by_material_closure(self):
        ft = {
            "30m": {"samples": 40, "moved_toward_rate": 0.4, "mean_gap_closure_pct": 0.1},
            "60m": {"samples": 40, "moved_toward_rate": 0.4, "mean_gap_closure_pct": 0.25},
        }
        s = mk_summary(final_n=40, follow_through=ft)
        assert label_policy(s, BASELINE)[0] == POLICY_PROMISING

    def test_promising_blocked_by_ticker_concentration(self):
        ft = {
            "30m": {"samples": 40, "moved_toward_rate": 0.60, "mean_gap_closure_pct": 0.1},
            "60m": {"samples": 40, "moved_toward_rate": 0.60, "mean_gap_closure_pct": 0.1},
        }
        s = mk_summary(final_n=40, follow_through=ft, max_ticker_share=0.5)
        label, reason = label_policy(s, BASELINE)
        assert label == POLICY_NEUTRAL
        assert "concentration" in reason

    def test_promising_blocked_by_game_concentration(self):
        ft = {
            "30m": {"samples": 40, "moved_toward_rate": 0.60, "mean_gap_closure_pct": 0.1},
            "60m": {"samples": 40, "moved_toward_rate": 0.60, "mean_gap_closure_pct": 0.1},
        }
        s = mk_summary(final_n=40, follow_through=ft, max_game_share=0.8)
        assert label_policy(s, BASELINE)[0] == POLICY_NEUTRAL

    def test_reject_when_survival_tiny(self):
        s = mk_summary(final_n=15, survival_ratio=0.05)
        assert label_policy(s, BASELINE)[0] == POLICY_REJECT

    def test_bar_clearing_but_undersampled_is_too_thin_not_reject(self):
        """Found live: the strongest cohorts (toward >0.5, n=15-26, survival
        <10%) were labeled reject_policy. A selective filter that clears the
        promising bar but lacks sample is YOUNG, not structurally unusable —
        it must label too_thin with a keep-observing reason."""
        ft = {
            "30m": {"samples": 26, "moved_toward_rate": 0.54, "mean_gap_closure_pct": 0.4},
            "60m": {"samples": 26, "moved_toward_rate": 0.5385, "mean_gap_closure_pct": 0.4218},
        }
        s = mk_summary(final_n=26, survival_ratio=0.099, follow_through=ft)
        label, reason = label_policy(s, BASELINE)
        assert label == POLICY_TOO_THIN
        assert "keep observing" in reason

    def test_worse_than_baseline(self):
        ft = {
            "30m": {"samples": 20, "moved_toward_rate": 0.2, "mean_gap_closure_pct": -0.5},
            "60m": {"samples": 20, "moved_toward_rate": 0.2, "mean_gap_closure_pct": -0.5},
        }
        s = mk_summary(final_n=20, follow_through=ft)
        assert label_policy(s, BASELINE)[0] == POLICY_WORSE

    def test_neutral_default(self):
        s = mk_summary()   # same as baseline
        assert label_policy(s, BASELINE)[0] == POLICY_NEUTRAL

    def test_promising_end_to_end(self, session):
        """A follows-move cohort that genuinely reverts, across many games and
        tickers, must label promising_shadow through the full pipeline."""
        for i in range(PROMISING_MIN_FINAL_N + 2):
            seed(session, f"KXG-GM{i}-T{i}", gap=0.10, pre_mid=0.44, later_mid=0.58)
        r = build(session)
        p = policy(r, "require_gap_follows_recent_move")
        assert p["final_n"] >= PROMISING_MIN_FINAL_N
        assert p["label"] == POLICY_PROMISING


# --- report content -------------------------------------------------------------------


class TestReportContent:
    def test_examples_removed_and_retained(self, session):
        seed(session, "KXOPP-1", gap=-0.10, pre_mid=0.44, later_mid=0.60)  # opposes, big away
        seed(session, "KXFOL-1", gap=0.10, pre_mid=0.44, later_mid=0.56)   # follows, toward
        r = build(session, top=3)
        p = policy(r, "require_gap_follows_recent_move")
        assert p["examples_removed"][0]["ticker"] == "KXOPP-1"
        assert p["examples_retained"][0]["ticker"] == "KXFOL-1"

    def test_interpretation_section_present(self, session):
        seed(session, "KXA-1", gap=0.10, pre_mid=0.44, later_mid=0.54)
        r = build(session)
        i = r["interpretation"]
        assert "excluding_gap_opposes_improves" in i
        assert "spreads_primary_adverse_source" in i
        assert i["mvp_005b"]["blocked"] is True   # tiny sample can't clear the bar
        assert "explicit human acceptance" in i["mvp_005b"]["note"]

    def test_all_policies_present_in_order(self, session):
        seed(session, "KXA-1", later_mid=0.54)
        r = build(session)
        assert [p["name"] for p in r["policies"]] == [name for name, _ in POLICIES]
        assert r["policies"][0]["name"] == "baseline_all_watchlist"

    def test_path_and_drift_stats(self, session):
        seed(session, "KXA-1", gap=0.10, midpoint=0.50, later_mid=0.40)  # continued away
        r = build(session)
        b = policy(r, "baseline_all_watchlist")
        assert b["continued_away_rate"] == 1.0
        assert b["flat_rate"] == 0.0


# --- CLI ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_parses(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "edge_filter_shadow_report", fake)
        rc = cli.main(["edge-filter-shadow-report", "--hours", "48", "--top", "3"])
        assert rc == 0
        assert captured == {"hours": 48, "top": 3}

    def test_cli_renders(self, session, capsys):
        seed(session, "KXA-1", gap=0.10, pre_mid=0.44, later_mid=0.54)
        n = asyncio.run(cli.edge_filter_shadow_report(session=session))
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "baseline_all_watchlist" in out
        assert "interpretation" in out

    def test_cli_empty_window(self, session, capsys):
        n = asyncio.run(cli.edge_filter_shadow_report(session=session))
        assert n == 0
        assert "population=0" in capsys.readouterr().out


# --- safety ------------------------------------------------------------------------


class TestSafety:
    def test_persists_nothing(self, session):
        seed(session, "KXA-1", later_mid=0.54)
        session.commit()
        import sqlalchemy

        before = {
            t: session.execute(sqlalchemy.text(f"select count(*) from {t}")).scalar()
            for t in ("edge_precheck_snapshots", "market_price_ticks",
                      "market_forecasts", "opportunity_signals")
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

        src = (REPO / "app" / "services" / "edge_filter_shadow.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "jupiter", "recommend_trade",
                    "execute_trade", "execution"):
            assert bad not in code

    def test_no_live_network(self):
        src = (REPO / "app" / "services" / "edge_filter_shadow.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src

    def test_labels_are_measurement_language(self):
        for label in (POLICY_TOO_THIN, POLICY_WORSE, POLICY_NEUTRAL,
                      POLICY_PROMISING, POLICY_REJECT):
            for bad in ("trade", "buy", "sell", "bet", "ev"):
                assert bad not in label

    def test_policy_summaries_have_no_advice_fields(self, session):
        seed(session, "KXA-1", later_mid=0.54)
        r = build(session)
        blob = " ".join(policy(r, "baseline_all_watchlist").keys()).lower()
        for bad in ("side", "size", "action", "order", "recommendation_to"):
            assert bad not in blob
