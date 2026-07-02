from datetime import datetime, timezone

import httpx
import pytest
import respx

from app.adapters.kalshi import KalshiRestAdapter, parse_market, parse_markets_response


def test_parse_market_normalizes_fields(sample_kalshi_market):
    market = parse_market(sample_kalshi_market)
    assert market.ticker == "FED-25DEC-T4.00"
    assert market.event_ticker == "FED-25DEC"
    assert market.yes_bid == 43
    assert market.yes_ask == 45
    assert market.spread == 2
    assert market.volume_24h == 8500
    assert market.liquidity == 250000
    assert market.close_time == datetime(2025, 12, 10, 19, 0, tzinfo=timezone.utc)
    assert "federal funds" in market.rules_primary


def test_parse_market_treats_zero_quotes_as_missing(sample_kalshi_market):
    raw = dict(sample_kalshi_market, yes_bid=0, yes_ask=0)
    market = parse_market(raw)
    assert market.yes_bid is None
    assert market.yes_ask is None
    assert market.spread is None


def test_parse_market_tolerates_missing_optional_fields():
    market = parse_market({"ticker": "BARE-MKT"})
    assert market.ticker == "BARE-MKT"
    assert market.title == ""
    assert market.volume == 0
    assert market.close_time is None


def test_parse_markets_response_skips_malformed_and_returns_cursor(sample_kalshi_market):
    payload = {
        "markets": [sample_kalshi_market, {"title": "no ticker -> invalid"}],
        "cursor": "next-page",
    }
    markets, cursor = parse_markets_response(payload)
    assert [m.ticker for m in markets] == ["FED-25DEC-T4.00"]
    assert cursor == "next-page"


def test_parse_markets_response_empty_cursor_is_none(sample_markets_payload):
    markets, cursor = parse_markets_response(sample_markets_payload)
    assert len(markets) == 2
    assert cursor is None


@respx.mock
async def test_fetch_active_markets_pages_with_cursor(sample_kalshi_market):
    base = "https://kalshi.test/trade-api/v2"
    page_two_market = dict(sample_kalshi_market, ticker="CPI-26JAN-T3.0")
    route = respx.get(f"{base}/markets")
    route.side_effect = [
        httpx.Response(200, json={"markets": [sample_kalshi_market], "cursor": "page2"}),
        httpx.Response(200, json={"markets": [page_two_market], "cursor": ""}),
    ]

    adapter = KalshiRestAdapter(base_url=base)
    markets = await adapter.fetch_active_markets(max_markets=10)

    assert [m.ticker for m in markets] == ["FED-25DEC-T4.00", "CPI-26JAN-T3.0"]
    assert route.call_count == 2
    first_params = route.calls[0].request.url.params
    assert first_params["status"] == "open"
    assert "cursor" not in first_params
    assert route.calls[1].request.url.params["cursor"] == "page2"


@respx.mock
async def test_fetch_active_markets_respects_max(sample_kalshi_market):
    base = "https://kalshi.test/trade-api/v2"
    respx.get(f"{base}/markets").mock(
        return_value=httpx.Response(
            200,
            json={
                "markets": [dict(sample_kalshi_market, ticker=f"MKT-{i}") for i in range(5)],
                "cursor": "more",
            },
        )
    )
    adapter = KalshiRestAdapter(base_url=base)
    markets = await adapter.fetch_active_markets(max_markets=3)
    assert len(markets) == 3


@respx.mock
async def test_fetch_active_markets_raises_on_http_error():
    base = "https://kalshi.test/trade-api/v2"
    respx.get(f"{base}/markets").mock(return_value=httpx.Response(503))
    adapter = KalshiRestAdapter(base_url=base)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.fetch_active_markets(max_markets=5)
