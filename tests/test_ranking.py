from datetime import datetime, timedelta, timezone

import pytest

from app.services.ranking import (
    RankingWeights,
    expiration_score,
    liquidity_score,
    rank_markets,
    resolution_clarity_score,
    score_market,
    spread_score,
    volume_score,
)
from tests.conftest import make_market

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def days_out(days: float) -> datetime:
    return NOW + timedelta(days=days)


class TestComponents:
    def test_tight_spread_beats_wide_spread(self):
        tight = spread_score(make_market(yes_bid=49, yes_ask=51))
        wide = spread_score(make_market(yes_bid=30, yes_ask=70))
        assert tight > wide

    def test_missing_quotes_score_zero(self):
        assert spread_score(make_market(yes_bid=None, yes_ask=None)) == 0.0

    def test_liquidity_and_volume_are_monotonic_and_bounded(self):
        assert liquidity_score(make_market(liquidity=0)) == 0.0
        low = liquidity_score(make_market(liquidity=1_000))
        high = liquidity_score(make_market(liquidity=500_000))
        assert 0 < low < high <= 1.0
        assert volume_score(make_market(volume_24h=10**9)) == 1.0

    def test_expiration_sweet_spot(self):
        imminent = expiration_score(make_market(close_time=days_out(0.1)), now=NOW)
        ideal = expiration_score(make_market(close_time=days_out(10)), now=NOW)
        distant = expiration_score(make_market(close_time=days_out(365)), now=NOW)
        assert imminent == 0.0
        assert ideal == 1.0
        assert distant == 0.0

    def test_expiration_missing_close_time_scores_zero(self):
        assert expiration_score(make_market(close_time=None), now=NOW) == 0.0

    def test_resolution_clarity_placeholder_is_neutral(self):
        assert resolution_clarity_score(make_market()) == 0.5


class TestScoreMarket:
    def test_score_in_unit_interval_with_components(self):
        ranked = score_market(make_market(close_time=days_out(7)), now=NOW)
        assert 0.0 <= ranked.score <= 1.0
        assert ranked.components.resolution_clarity == 0.5

    def test_custom_weights_change_ordering(self):
        liquid_wide = make_market(
            ticker="LIQUID", yes_bid=30, yes_ask=70, liquidity=900_000, close_time=days_out(7)
        )
        tight_dry = make_market(
            ticker="TIGHT", yes_bid=49, yes_ask=51, liquidity=100, close_time=days_out(7)
        )
        spread_heavy = RankingWeights(spread=1.0, liquidity=0.0, volume=0.0, expiration=0.0, resolution_clarity=0.0)
        liquidity_heavy = RankingWeights(spread=0.0, liquidity=1.0, volume=0.0, expiration=0.0, resolution_clarity=0.0)

        by_spread = rank_markets([liquid_wide, tight_dry], weights=spread_heavy, now=NOW)
        by_liquidity = rank_markets([liquid_wide, tight_dry], weights=liquidity_heavy, now=NOW)
        assert by_spread[0].market.ticker == "TIGHT"
        assert by_liquidity[0].market.ticker == "LIQUID"


class TestRankMarkets:
    def test_orders_descending_by_score(self):
        strong = make_market(
            ticker="STRONG", yes_bid=49, yes_ask=51, liquidity=800_000,
            volume_24h=50_000, close_time=days_out(7),
        )
        weak = make_market(
            ticker="WEAK", yes_bid=None, yes_ask=None, liquidity=0,
            volume_24h=0, close_time=None,
        )
        ranked = rank_markets([weak, strong], now=NOW)
        assert [r.market.ticker for r in ranked] == ["STRONG", "WEAK"]
        assert ranked[0].score > ranked[1].score

    def test_deterministic_tiebreak_by_ticker(self):
        a = make_market(ticker="AAA")
        b = make_market(ticker="BBB")
        ranked = rank_markets([b, a], now=NOW)
        assert [r.market.ticker for r in ranked] == ["AAA", "BBB"]

    def test_empty_input(self):
        assert rank_markets([], now=NOW) == []
