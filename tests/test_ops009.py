"""OPS-009 tests: minute-level domain-aware promotion freshness,
market-type priority, microstructure-aware readiness scoring, and the new
promotion stats. Promotion ordering only — never EV/value/trade language."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import MarketPriceTick
from app.services.marketops import (
    MARKET_TYPE_PLAYER,
    MarketOpsConfig,
    _market_type_for_promotion,
)
from tests.test_marketops import autopilot, seed_market, seed_signal

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def seed_tick(session, ticker, midpoint=0.5, spread=4, liquidity=2000, age_seconds=30):
    observed = NOW - timedelta(seconds=age_seconds)
    row = MarketPriceTick(
        market_ticker=ticker,
        observed_at=observed,
        yes_bid=int(midpoint * 100) - spread // 2 if midpoint is not None else None,
        yes_ask=int(midpoint * 100) + spread // 2 if midpoint is not None else None,
        midpoint=midpoint,
        spread=spread if midpoint is not None else None,
        volume_24h=100,
        liquidity_proxy=liquidity,
        created_at=observed,
    )
    session.add(row)
    session.commit()
    return row


def select(session, cfg=None):
    service = autopilot(cfg=cfg or MarketOpsConfig())
    return service.select_signals_for_promotion(session, NOW)


class TestMarketTypeClassification:
    def test_measurable_types(self):
        assert _market_type_for_promotion(
            "KXMLBTOTAL-26JUL021915STLATL-18", "sports_baseball"
        ) == "total"
        assert _market_type_for_promotion(
            "KXMLBSPREAD-26JUL021915STLATL-ATL3", "sports_baseball"
        ) == "spread"
        assert _market_type_for_promotion(
            "KXWCGAME-26JUN141800USAWAL-USA", "sports_soccer"
        ) == "winner"
        assert _market_type_for_promotion(
            "KXWCADVANCE-26JUN14USAWAL-WAL", "sports_soccer"
        ) == "advance"
        assert _market_type_for_promotion(
            "KXWCTOTAL-26JUN14USAWAL-3", "sports_soccer"
        ) == "total"

    def test_player_markets_detected(self):
        assert _market_type_for_promotion(
            "KXWCGOAL-26JUL03ARGCPV-CPVWSEMED17-1", "sports_soccer"
        ) == MARKET_TYPE_PLAYER
        assert _market_type_for_promotion(
            "KXMLBHRR-26JUL032210TORSEA-SEALRALEY20-1", "sports_baseball"
        ) == MARKET_TYPE_PLAYER
        assert _market_type_for_promotion(
            "KXWNBAPTS-26JUL03MINNY-NYJJONES35-20", "general"
        ) == MARKET_TYPE_PLAYER

    def test_unknown_fallback(self):
        assert _market_type_for_promotion("GEN-MKT-1", "general") == "unknown"


class TestDomainAgeWindows:
    def _window(self, domain, **cfg_overrides):
        return autopilot(cfg=MarketOpsConfig(**cfg_overrides))._age_window_minutes(domain)

    def test_minutes_supersede_hours(self):
        # hours=24 would allow 1440m; minute knobs tighten it
        assert self._window("sports_baseball", max_signal_age_hours=24) == 20
        assert self._window("general", max_signal_age_hours=24) == 60

    def test_hours_remain_a_coarse_upper_bound(self):
        # a very tight legacy hour knob still caps the minute windows
        assert self._window(
            "general", max_signal_age_hours=24, general_max_signal_age_minutes=60
        ) == 60
        # compat: hour bound tighter than minutes wins
        assert self._window(
            "sports_baseball",
            max_signal_age_hours=24,
            baseball_max_signal_age_minutes=90,
        ) == 90
        cfg_window = self._window("general", max_signal_age_hours=0)
        assert cfg_window == 0  # degenerate but deterministic

    def test_domain_specific_windows(self):
        assert self._window("sports_soccer") == 20
        assert self._window("sports_baseball") == 20
        assert self._window("sports_tennis") == 20  # live sports default
        assert self._window("general") == 60
        assert self._window(
            "sports_soccer", soccer_max_signal_age_minutes=5
        ) == 5

    def test_live_sports_rejects_stale_but_general_allows(self, session):
        seed_signal(session, ticker="KXMLB-STALE", age_minutes=30)  # > 20m baseball
        seed_signal(session, ticker="KXWC-STALE", age_minutes=25)  # > 20m soccer
        seed_signal(session, ticker="GEN-OK", age_minutes=45)  # < 60m general
        selected, seen, stats = select(session)
        assert seen == 1
        assert [s.market_ticker for s in selected] == ["GEN-OK"]
        assert stats["skipped_stale_count"] == 2

    def test_fresh_sports_signals_pass(self, session):
        seed_signal(session, ticker="KXMLB-FRESH", age_minutes=10)
        seed_signal(session, ticker="KXWC-FRESH", age_minutes=15)
        selected, seen, _ = select(session)
        assert seen == 2
        assert len(selected) == 2


class TestPriorityScoring:
    def test_measurable_market_types_beat_player_and_unknown(self, session):
        # same domain, same freshness, same signal type — market type decides
        seed_signal(session, ticker="KXMLBTOTAL-26JUL021915STLATL-18", age_minutes=10)
        seed_signal(
            session, ticker="KXMLBHRR-26JUL032210TORSEA-SEALRALEY20-1", age_minutes=10
        )
        seed_signal(session, ticker="KXMLB-UNKNOWN", age_minutes=10)
        selected, _, stats = select(session, cfg=MarketOpsConfig(promote_limit=3))
        order = [s.market_ticker for s in selected]
        assert order[0] == "KXMLBTOTAL-26JUL021915STLATL-18"  # measurable first
        assert order[-1] == "KXMLBHRR-26JUL032210TORSEA-SEALRALEY20-1"  # player last
        assert stats["promoted_by_market_type"] == {
            "total": 1, "unknown": 1, "player": 1
        }

    def test_book_quality_boosts_ordering(self, session):
        # identical signals; only one ticker has a fresh, tight, deep book
        seed_signal(session, ticker="KXMLB-BOOK", age_minutes=10)
        seed_signal(session, ticker="KXMLB-NOBOOK", age_minutes=10)
        seed_tick(session, "KXMLB-BOOK", midpoint=0.5, spread=4, liquidity=2000,
                  age_seconds=30)
        selected, _, stats = select(session, cfg=MarketOpsConfig(promote_limit=2))
        assert selected[0].market_ticker == "KXMLB-BOOK"
        assert stats["unmeasurable_candidates"] == 1  # NOBOOK has zero book score

    def test_wide_spread_book_scores_lower_than_tight(self, session):
        seed_signal(session, ticker="KXMLB-TIGHT", age_minutes=10)
        seed_signal(session, ticker="KXMLB-WIDE", age_minutes=10)
        seed_tick(session, "KXMLB-TIGHT", spread=4)
        seed_tick(session, "KXMLB-WIDE", spread=30)  # > EDGE max 10c
        selected, _, _ = select(session, cfg=MarketOpsConfig(promote_limit=2))
        assert selected[0].market_ticker == "KXMLB-TIGHT"

    def test_source_backed_domain_beats_general_at_same_freshness(self, session):
        seed_signal(session, ticker="GEN-A", age_minutes=10)
        seed_signal(session, ticker="KXWCGAME-26JUN141800USAWAL-USA", age_minutes=10)
        selected, _, stats = select(session, cfg=MarketOpsConfig(promote_limit=1))
        assert selected[0].market_ticker.startswith("KXWC")
        assert stats["promoted_by_domain"] == {"sports_soccer": 1}

    def test_freshness_dominates_within_domain(self, session):
        seed_signal(session, ticker="KXMLB-OLD", age_minutes=18)
        seed_signal(session, ticker="KXMLB-NEW", age_minutes=2)
        selected, _, _ = select(session, cfg=MarketOpsConfig(promote_limit=2))
        assert selected[0].market_ticker == "KXMLB-NEW"

    def test_one_per_ticker_and_refresh_skips_still_hold(self, session):
        seed_signal(session, ticker="KXMLB-A", signal_type="price_move_threshold",
                    age_minutes=10)
        seed_signal(session, ticker="KXMLB-A", signal_type="spread_tightened",
                    age_minutes=5)
        refreshed = seed_signal(session, ticker="KXMLB-R", status="forecast_refreshed",
                                age_minutes=10)
        refreshed.processed_at = NOW - timedelta(minutes=5)
        session.commit()
        seed_signal(session, ticker="KXMLB-R", age_minutes=3)  # recently refreshed
        selected, _, _ = select(session, cfg=MarketOpsConfig(promote_limit=5))
        tickers = [s.market_ticker for s in selected]
        assert tickers.count("KXMLB-A") == 1
        assert "KXMLB-R" not in tickers
        # within KXMLB-A, the better signal type won
        assert selected[0].signal_type == "price_move_threshold"

    def test_scores_are_promotion_priority_language_only(self):
        import re

        import app.services.marketops as module

        source = open(module.__file__).read()
        # the scoring function must not use trade/value vocabulary
        section = source[source.index("_measurement_readiness_score"):]
        section = section[:section.index("def select_signals_for_promotion")]
        for banned in ("expected_value", "profit", "trade_score", "pnl"):
            assert not re.search(rf"\b{banned}\b", section, re.IGNORECASE)


class TestRunSummaryStats:
    async def test_summary_includes_promotion_metrics(self, session):
        seed_market(session, "KXMLB-A")
        seed_signal(session, ticker="KXMLB-A", age_minutes=10)
        seed_signal(session, ticker="KXMLB-STALE", age_minutes=45)  # stale for baseball
        seed_tick(session, "KXMLB-A")
        run = await autopilot().run_once(session)
        promo = run.summary["promotion"]
        assert promo["skipped_stale_count"] == 1
        assert promo["promoted_by_domain"] == {"sports_baseball": 1}
        assert promo["promoted_by_signal_type"] == {"price_move_threshold": 1}
        # run_once uses real wall-clock; the suite may run this test a
        # couple of minutes after module import, so bound loosely
        assert 595 <= promo["promoted_signal_age_s_mean"] <= 900
        assert 595 <= promo["promoted_signal_age_s_max"] <= 900
        assert len(promo["readiness_scores"]) == 1
        assert promo["readiness_scores"][0] > 0

    async def test_marketops_report_cli_surfaces_promotion(self, session, capsys):
        from app import cli

        seed_market(session, "KXMLB-A")
        seed_signal(session, ticker="KXMLB-A", age_minutes=10)
        await autopilot().run_once(session)
        capsys.readouterr()
        await cli.marketops_report(session=session)
        output = capsys.readouterr().out
        assert "promotion (OPS-009):" in output
        assert "promoted by domain" in output
