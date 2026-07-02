"""Read-only Kalshi REST adapter.

Fetches active markets from the public trade API (v2). No authentication is
required for public market data, and this module deliberately contains no
order-placement capability.
"""

import logging
from datetime import datetime

import httpx

from app.config import get_settings
from app.schemas import MarketData

logger = logging.getLogger(__name__)

MARKETS_PATH = "/markets"
PAGE_SIZE = 200  # Kalshi max per-page limit


def _parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable timestamp from Kalshi: %r", value)
        return None


def _parse_price(value) -> int | None:
    """Kalshi quotes are integer cents; 0 means 'no order at that side'."""
    if value is None:
        return None
    price = int(value)
    return price if price > 0 else None


def parse_market(raw: dict) -> MarketData:
    """Normalize one raw Kalshi market object into MarketData."""
    return MarketData(
        ticker=raw["ticker"],
        event_ticker=raw.get("event_ticker"),
        title=raw.get("title") or "",
        category=raw.get("category"),
        status=raw.get("status") or "unknown",
        yes_bid=_parse_price(raw.get("yes_bid")),
        yes_ask=_parse_price(raw.get("yes_ask")),
        no_bid=_parse_price(raw.get("no_bid")),
        no_ask=_parse_price(raw.get("no_ask")),
        last_price=_parse_price(raw.get("last_price")),
        volume=int(raw.get("volume") or 0),
        volume_24h=int(raw.get("volume_24h") or 0),
        open_interest=int(raw.get("open_interest") or 0),
        liquidity=int(raw.get("liquidity") or 0),
        close_time=_parse_timestamp(raw.get("close_time")),
        expiration_time=_parse_timestamp(raw.get("expiration_time")),
        rules_primary=raw.get("rules_primary"),
    )


def parse_markets_response(payload: dict) -> tuple[list[MarketData], str | None]:
    """Parse a GET /markets response page. Returns (markets, next_cursor)."""
    markets = []
    for raw in payload.get("markets", []):
        try:
            markets.append(parse_market(raw))
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Skipping unparseable market %r: %s", raw.get("ticker"), exc)
    return markets, payload.get("cursor") or None


class KalshiRestAdapter:
    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.kalshi_api_base).rstrip("/")
        self.timeout = timeout or settings.kalshi_request_timeout_seconds

    async def fetch_active_markets(self, max_markets: int | None = None) -> list[MarketData]:
        """Fetch open markets, paging with cursors up to max_markets."""
        limit = max_markets or get_settings().scanner_max_markets
        results: list[MarketData] = []
        cursor: str | None = None

        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            while len(results) < limit:
                params = {"status": "open", "limit": min(PAGE_SIZE, limit - len(results))}
                if cursor:
                    params["cursor"] = cursor
                response = await client.get(MARKETS_PATH, params=params)
                response.raise_for_status()
                markets, cursor = parse_markets_response(response.json())
                results.extend(markets)
                if not cursor or not markets:
                    break

        return results[:limit]
