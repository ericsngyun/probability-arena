"""Live Kalshi integration tests — OFF by default.

Enable with:
    RUN_LIVE_TESTS=true pytest tests/test_live_kalshi.py -v

These hit the real public Kalshi REST API (read-only, no credentials) and are
skipped in normal runs so the suite stays hermetic and CI-safe.
"""

import os

import pytest

from app.adapters.kalshi import KalshiRestAdapter
from app.services.ranking import rank_markets

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_TESTS", "").lower() != "true",
    reason="live Kalshi tests disabled; set RUN_LIVE_TESTS=true to enable",
)


async def test_live_fetch_active_markets_parses():
    adapter = KalshiRestAdapter()
    markets = await adapter.fetch_active_markets(max_markets=25)

    assert markets, "expected at least one open market on Kalshi"
    assert len(markets) <= 25
    for market in markets:
        assert market.ticker
        assert market.volume >= 0
        assert market.raw is not None
        if market.yes_bid is not None:
            assert 1 <= market.yes_bid <= 99
        if market.spread is not None:
            assert market.spread >= 0


async def test_live_markets_rank_deterministically():
    adapter = KalshiRestAdapter()
    markets = await adapter.fetch_active_markets(max_markets=25)
    ranked = rank_markets(markets)

    assert len(ranked) == len(markets)
    scores = [r.score for r in ranked]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= s <= 1.0 for s in scores)
    # Same input must produce the same ordering (fixed `now` not needed within one call window)
    assert [r.market.ticker for r in rank_markets(markets)] == [r.market.ticker for r in ranked]
