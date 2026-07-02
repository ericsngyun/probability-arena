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


def _dollars_to_cents(value) -> int | None:
    """Convert a '0.4100'-style dollar string to integer cents; 0/None -> None."""
    if value in (None, ""):
        return None
    cents = round(float(value) * 100)
    return cents if cents > 0 else None


def _price_from(raw: dict, field: str) -> int | None:
    """Read a price: legacy integer-cent field, else the '<field>_dollars'
    string the API returns since the dollars/fp migration."""
    if raw.get(field) is not None:
        return _parse_price(raw[field])
    return _dollars_to_cents(raw.get(f"{field}_dollars"))


def _count_from(raw: dict, field: str) -> int:
    """Read a contract count: legacy integer field, else '<field>_fp'
    fixed-point string (fractional trading), rounded to whole contracts."""
    if raw.get(field) is not None:
        return int(raw[field])
    fp = raw.get(f"{field}_fp")
    if fp in (None, ""):
        return 0
    return round(float(fp))


def _parse_liquidity(raw: dict) -> int:
    """Liquidity in cents. The list endpoint no longer populates
    liquidity/liquidity_dollars, so fall back to a deterministic proxy: the
    notional value (cents) resting at the top of the book on both sides."""
    if raw.get("liquidity") is not None:
        return int(raw["liquidity"])
    direct = _dollars_to_cents(raw.get("liquidity_dollars"))
    if direct:
        return direct
    notional = 0
    yes_bid = _price_from(raw, "yes_bid")
    yes_ask = _price_from(raw, "yes_ask")
    if yes_bid:
        notional += yes_bid * _count_from(raw, "yes_bid_size")
    if yes_ask:
        # A resting yes ask is a no-side commitment of (100 - ask) per contract
        notional += (100 - yes_ask) * _count_from(raw, "yes_ask_size")
    return notional


def parse_market(raw: dict) -> MarketData:
    """Normalize one raw Kalshi market object into MarketData.

    Handles both the legacy integer-cent payload shape and the current
    '*_dollars' / '*_fp' string shape.
    """
    return MarketData(
        ticker=raw["ticker"],
        event_ticker=raw.get("event_ticker"),
        title=raw.get("title") or "",
        category=raw.get("category"),
        status=raw.get("status") or "unknown",
        yes_bid=_price_from(raw, "yes_bid"),
        yes_ask=_price_from(raw, "yes_ask"),
        no_bid=_price_from(raw, "no_bid"),
        no_ask=_price_from(raw, "no_ask"),
        last_price=_price_from(raw, "last_price"),
        volume=_count_from(raw, "volume"),
        volume_24h=_count_from(raw, "volume_24h"),
        open_interest=_count_from(raw, "open_interest"),
        liquidity=_parse_liquidity(raw),
        close_time=_parse_timestamp(raw.get("close_time")),
        expiration_time=_parse_timestamp(raw.get("expiration_time")),
        rules_primary=raw.get("rules_primary"),
        raw=raw,
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

        mve_filter = get_settings().kalshi_mve_filter
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            while len(results) < limit:
                params = {"status": "open", "limit": min(PAGE_SIZE, limit - len(results))}
                if mve_filter:
                    params["mve_filter"] = mve_filter
                if cursor:
                    params["cursor"] = cursor
                response = await client.get(MARKETS_PATH, params=params)
                response.raise_for_status()
                markets, cursor = parse_markets_response(response.json())
                results.extend(markets)
                if not cursor or not markets:
                    break

        return results[:limit]

    async def _get_json(self, path: str) -> dict | None:
        """GET a detail endpoint; None on any HTTP/network error (detail
        enrichment is best-effort and must never break the pipeline)."""
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
                response = await client.get(path)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            logger.warning("Kalshi detail fetch failed for %s: %s", path, exc)
            return None

    async def get_market_detail(self, ticker: str) -> dict | None:
        """Full market object from GET /markets/{ticker}; None if unavailable."""
        payload = await self._get_json(f"/markets/{ticker}")
        return (payload or {}).get("market") or None

    async def get_event_detail(self, event_ticker: str) -> dict | None:
        """Event object (carries series_ticker, settlement_sources, category)."""
        payload = await self._get_json(f"/events/{event_ticker}")
        return (payload or {}).get("event") or None

    async def get_series_detail(self, series_ticker: str) -> dict | None:
        """Series object (carries settlement_sources, contract URLs, category)."""
        payload = await self._get_json(f"/series/{series_ticker}")
        return (payload or {}).get("series") or None
