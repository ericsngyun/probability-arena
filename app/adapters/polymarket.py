"""Polymarket adapter (POLY-001): read-only GETs against the public,
no-authentication Polymarket endpoints — the Gamma market catalog
(gamma-api.polymarket.com) and the CLOB read-only order book
(clob.polymarket.com/book). No credentials, no API keys, no wallet, no
signing are used or required.

Every fetch method returns [] (or None for a single lookup) on HTTP errors,
rate limits, timeouts, empty responses, or schema drift — the observer lane
degrades to "nothing observed this pass" instead of failing.

Hard boundary (docs/SAFETY_BOUNDARIES.md): this module reads market data only.
It does NOT create/cancel orders, sign, hold keys/wallets, compute EV, size
positions, recommend trades, or execute anything. The authenticated CLOB
trading endpoints are deliberately NOT implemented.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
SOURCE_NAME = "polymarket"


@dataclass(frozen=True)
class PolymarketMarketData:
    """One normalized Polymarket market from the Gamma catalog (read-only
    metadata + microstructure proxies). Prices are informational quotes, never
    EV/advice."""

    market_id: str
    condition_id: str | None
    question: str | None
    slug: str | None
    category: str | None
    description: str | None
    active: bool
    closed: bool
    archived: bool
    restricted: bool
    enable_order_book: bool
    accepting_orders: bool
    outcomes: list[str] = field(default_factory=list)
    outcome_prices: list[float] = field(default_factory=list)
    clob_token_ids: list[str] = field(default_factory=list)
    best_bid: float | None = None
    best_ask: float | None = None
    last_trade_price: float | None = None
    spread: float | None = None
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    volume_total_usd: float | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def two_sided(self) -> bool:
        return self.best_bid is not None and self.best_ask is not None


@dataclass(frozen=True)
class OrderbookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class PolymarketOrderbook:
    """One read-only CLOB order-book snapshot for a token id, reduced to
    spread/depth/liquidity proxies. No order can be placed from this."""

    token_id: str
    market: str | None
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    spread: float | None
    bid_depth: float  # sum of bid sizes
    ask_depth: float  # sum of ask sizes
    total_depth: float
    num_bids: int
    num_asks: int
    liquidity_proxy: float | None  # total_depth * mid (shares × price)
    tick_size: float | None = None
    min_order_size: float | None = None
    last_trade_price: float | None = None
    raw: dict = field(default_factory=dict, repr=False)


def _float(value) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _bool(value) -> bool:
    return bool(value) if isinstance(value, bool) else str(value).lower() == "true"


def _json_list(value) -> list:
    """Gamma returns outcomes/outcomePrices/clobTokenIds as JSON-encoded
    strings (e.g. '["Yes","No"]'). Accept both string and native list; return
    [] on anything unparseable (schema-drift tolerance)."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _parse_dt(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _derive_category(payload: dict) -> str | None:
    """Best-effort read-only domain/category for a market. Polymarket has no
    single category field; use the parent event's title/ticker/slug as a
    grouping proxy. Returns None when nothing is available."""
    events = payload.get("events")
    if isinstance(events, list) and events:
        ev = events[0]
        if isinstance(ev, dict):
            for key in ("title", "ticker", "slug"):
                val = ev.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()[:48]
    return None


def _parse_market(payload: dict) -> PolymarketMarketData | None:
    """Normalize one Gamma market dict; None when the market id is missing
    (schema-drift tolerance)."""
    if not isinstance(payload, dict):
        return None
    market_id = payload.get("id")
    if market_id is None:
        return None
    prices = [p for p in (_float(x) for x in _json_list(payload.get("outcomePrices"))) if p is not None]
    return PolymarketMarketData(
        market_id=str(market_id),
        condition_id=payload.get("conditionId"),
        question=payload.get("question"),
        slug=payload.get("slug"),
        category=_derive_category(payload),
        description=payload.get("description"),
        active=_bool(payload.get("active")),
        closed=_bool(payload.get("closed")),
        archived=_bool(payload.get("archived")),
        restricted=_bool(payload.get("restricted")),
        enable_order_book=_bool(payload.get("enableOrderBook")),
        accepting_orders=_bool(payload.get("acceptingOrders")),
        outcomes=[str(o) for o in _json_list(payload.get("outcomes"))],
        outcome_prices=prices,
        clob_token_ids=[str(t) for t in _json_list(payload.get("clobTokenIds"))],
        best_bid=_float(payload.get("bestBid")),
        best_ask=_float(payload.get("bestAsk")),
        last_trade_price=_float(payload.get("lastTradePrice")),
        spread=_float(payload.get("spread")),
        liquidity_usd=_float(payload.get("liquidityNum")) or _float(payload.get("liquidity")),
        volume_24h_usd=_float(payload.get("volume24hr")),
        volume_total_usd=_float(payload.get("volumeNum")) or _float(payload.get("volume")),
        start_date=_parse_dt(payload.get("startDate")),
        end_date=_parse_dt(payload.get("endDate")),
        raw=payload,
    )


def _parse_levels(rows) -> list[OrderbookLevel]:
    levels: list[OrderbookLevel] = []
    if not isinstance(rows, list):
        return levels
    for row in rows:
        if not isinstance(row, dict):
            continue
        price = _float(row.get("price"))
        size = _float(row.get("size"))
        if price is not None and size is not None:
            levels.append(OrderbookLevel(price=price, size=size))
    return levels


def _parse_orderbook(payload: dict, token_id: str) -> PolymarketOrderbook | None:
    """Normalize a CLOB /book payload into spread/depth/liquidity proxies.
    None on schema drift."""
    if not isinstance(payload, dict):
        return None
    bids = _parse_levels(payload.get("bids"))
    asks = _parse_levels(payload.get("asks"))
    best_bid = max((lvl.price for lvl in bids), default=None)
    best_ask = min((lvl.price for lvl in asks), default=None)
    mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
    spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else None
    bid_depth = round(sum(lvl.size for lvl in bids), 4)
    ask_depth = round(sum(lvl.size for lvl in asks), 4)
    total_depth = round(bid_depth + ask_depth, 4)
    liquidity_proxy = round(total_depth * mid, 4) if mid is not None else None
    return PolymarketOrderbook(
        token_id=token_id,
        market=payload.get("market"),
        best_bid=best_bid,
        best_ask=best_ask,
        mid=round(mid, 6) if mid is not None else None,
        spread=round(spread, 6) if spread is not None else None,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        total_depth=total_depth,
        num_bids=len(bids),
        num_asks=len(asks),
        liquidity_proxy=liquidity_proxy,
        tick_size=_float(payload.get("tick_size")),
        min_order_size=_float(payload.get("min_order_size")),
        last_trade_price=_float(payload.get("last_trade_price")),
        raw=payload,
    )


class PolymarketAdapter:
    """Thin async client. Every method is a read-only public GET and never
    raises on network/HTTP/schema problems — it logs and returns empty results.
    No authentication headers, keys, wallets, or signing are used."""

    source_name = SOURCE_NAME

    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self.timeout = settings.polymarket_timeout_seconds

    async def _get(self, base: str, path: str, params: dict | None = None) -> dict | list | None:
        url = f"{base}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # No auth header, no cookies, no credentials — public data only.
                response = await client.get(url, params=params)
                if response.status_code == 429:
                    logger.warning("Polymarket rate limit hit for %s", url)
                    return None
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Polymarket fetch failed for %s: %s", url, exc)
            return None

    async def fetch_markets(
        self, limit: int = 50, active: bool = True, closed: bool = False
    ) -> list[PolymarketMarketData]:
        """Read-only Gamma market catalog, most-liquid first. Filtered to
        active/open by default. Returns [] on any provider problem."""
        params = {
            "limit": max(1, int(limit)),
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": "volume24hr",
            "ascending": "false",
        }
        payload = await self._get(GAMMA_API_BASE, "/markets", params=params)
        if not isinstance(payload, list):
            return []
        markets = [_parse_market(entry) for entry in payload]
        return [m for m in markets if m is not None]

    async def fetch_orderbook(self, token_id: str) -> PolymarketOrderbook | None:
        """One read-only CLOB order-book snapshot for a token id. None on any
        provider problem. This reads the book; it never places an order."""
        payload = await self._get(CLOB_API_BASE, "/book", params={"token_id": token_id})
        if not isinstance(payload, dict):
            return None
        return _parse_orderbook(payload, token_id)
