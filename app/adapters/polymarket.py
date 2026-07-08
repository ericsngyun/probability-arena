"""Polymarket adapter (POLY-001, broadened by POLY-COVERAGE-001): read-only GETs
against the public, no-authentication Polymarket endpoints — the Gamma market
catalog (gamma-api.polymarket.com/markets), the Gamma public search
(gamma-api.polymarket.com/public-search) and the CLOB read-only order book
(clob.polymarket.com/book). No credentials, no API keys, no wallet, no
signing are used or required.

Every fetch method returns [] (or None for a single lookup) on HTTP errors,
rate limits, timeouts, empty responses, or schema drift — the observer lane
degrades to "nothing observed this pass" instead of failing.

POLY-COVERAGE-001 adds bounded catalog PAGINATION, category (`tag_id`) and
resolution-window (`end_date_min`/`end_date_max`) filters, and public search —
purely to widen the READ-ONLY observation sample. Every request stays a public
GET. Coverage expansion identifies no arbitrage, computes no EV, recommends no
trade, sizes nothing, places no order, and touches no wallet/key/signing.

Query-parameter contract (verified against the live public API; the Gamma API
returns HTTP 200 and SILENTLY IGNORES unknown parameters, so only parameters
observed to actually change the result set are used here):
  * ``/markets``       — ``limit`` (server caps the page at 100), ``offset``
                         (real pagination), ``active``, ``closed``, ``order``,
                         ``ascending``, ``tag_id``, ``end_date_min``,
                         ``end_date_max``.
  * ``/public-search`` — ``q``, ``limit_per_type``, ``page`` (1-based). NOTE:
                         ``offset`` is IGNORED by this endpoint; paginating it
                         with ``offset`` silently re-fetches page 1 forever.

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

# Hard ceilings for a *manual* coverage scan. A read-only observer must never
# fan out without a bound, regardless of what the caller asks for.
MAX_PAGE_SIZE = 100        # the Gamma /markets endpoint caps a page at 100 rows
MAX_CATALOG_PAGES = 20     # => at most 2000 catalog rows per scan
MAX_SEARCH_PAGES = 5       # per query
MAX_TOTAL_MARKETS = 1000   # union cap across catalog + every search query


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


@dataclass
class MarketFetchResult:
    """Outcome of one bounded, possibly multi-page read-only fetch. Carries the
    de-duplicated markets plus provenance counters so a coverage scan can record
    HOW the sample was obtained (audit spine). Counters only — no advice."""

    markets: list[PolymarketMarketData] = field(default_factory=list)
    pages_fetched: int = 0
    provider_errors: int = 0
    duplicates_dropped: int = 0
    truncated: bool = False

    def merge(self, other: "MarketFetchResult") -> "MarketFetchResult":
        """Union two results, de-duplicating by market_id (first occurrence wins
        — callers pass the higher-priority source first)."""
        seen = {m.market_id for m in self.markets}
        merged = list(self.markets)
        dupes = self.duplicates_dropped + other.duplicates_dropped
        for m in other.markets:
            if m.market_id in seen:
                dupes += 1
                continue
            seen.add(m.market_id)
            merged.append(m)
        return MarketFetchResult(
            markets=merged,
            pages_fetched=self.pages_fetched + other.pages_fetched,
            provider_errors=self.provider_errors + other.provider_errors,
            duplicates_dropped=dupes,
            truncated=self.truncated or other.truncated,
        )


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


def _as_utc(dt: datetime | None) -> datetime | None:
    """Gamma mixes offset-aware ("...Z") and naive ("2026-07-20T03:59:00")
    timestamps; comparing the two raises. Treat naive as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _within_window(
    end_date: datetime | None, window_min: datetime | None, window_max: datetime | None
) -> bool:
    """Resolution-window membership. A market with NO resolution time cannot be
    shown to resolve inside a requested window, so it is excluded rather than
    optimistically admitted."""
    if window_min is None and window_max is None:
        return True
    end = _as_utc(end_date)
    if end is None:
        return False
    if window_min is not None and end < window_min:
        return False
    if window_max is not None and end > window_max:
        return False
    return True


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


def _search_market_payload(event: dict, market: dict) -> dict:
    """Reshape one `/public-search` nested market into the same payload shape the
    `/markets` catalog returns, so `_parse_market` stays the single normalizer.

    Two fields differ and both matter:

    * ``events`` is absent on a nested market, so `_derive_category` would yield
      None and every search-sourced market would land in `uncategorized`. We
      re-attach the parent event's grouping fields.
    * ``endDate`` is frequently absent on a nested market while the parent event
      carries it. The POLY-002 matcher can only emit `comparable_market_candidate`
      when BOTH venues expose a resolution time, so dropping `endDate` here would
      make every search-sourced market structurally incapable of ever being
      comparable (it would silently fall to `unresolved_semantic_match`). We
      inherit it from the parent event when the market omits it.

    Inheriting is honest, not a fabrication: the nested market's own `endDate`,
    where present, equals its parent event's. Missing on both stays missing.
    """
    payload = dict(market)
    payload.setdefault("events", [{
        "title": event.get("title"),
        "ticker": event.get("ticker"),
        "slug": event.get("slug"),
    }])
    if not payload.get("endDate") and event.get("endDate"):
        payload["endDate"] = event["endDate"]
    if not payload.get("startDate") and event.get("startDate"):
        payload["startDate"] = event["startDate"]
    return payload


def _markets_from_search(payload: dict) -> list[PolymarketMarketData]:
    """Flatten a `/public-search` response (events -> nested markets)."""
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return []
    out: list[PolymarketMarketData] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            parsed = _parse_market(_search_market_payload(event, market))
            if parsed is not None:
                out.append(parsed)
    return out


def _search_has_more(payload: dict) -> bool:
    pagination = payload.get("pagination") if isinstance(payload, dict) else None
    return bool(pagination.get("hasMore")) if isinstance(pagination, dict) else False


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
        params = {k: v for k, v in (params or {}).items() if v is not None}
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

    async def fetch_markets_page(
        self,
        limit: int = 50,
        active: bool = True,
        closed: bool = False,
        offset: int = 0,
        tag_id: int | None = None,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
    ) -> list[PolymarketMarketData] | None:
        """One read-only page of the Gamma market catalog, most-liquid first.

        Returns None on a provider problem (HTTP error, rate limit, timeout,
        schema drift) and [] for a genuinely empty page. The paginator relies on
        this distinction: an exhausted catalog is not a provider error, and the
        difference is persisted in the audit spine.

        `tag_id` scopes to a Polymarket category; `end_date_min`/`end_date_max`
        (ISO-8601) scope to a resolution window — both are coverage filters, and
        neither implies any judgement about the markets they return."""
        payload = await self._get(
            GAMMA_API_BASE,
            "/markets",
            params={
                "limit": max(1, min(int(limit), MAX_PAGE_SIZE)),
                "offset": max(0, int(offset)) or None,
                "active": str(active).lower(),
                "closed": str(closed).lower(),
                "order": "volume24hr",
                "ascending": "false",
                "tag_id": tag_id,
                "end_date_min": end_date_min,
                "end_date_max": end_date_max,
            },
        )
        if not isinstance(payload, list):
            return None
        markets = [_parse_market(entry) for entry in payload]
        return [m for m in markets if m is not None]

    async def fetch_markets(self, *args, **kwargs) -> list[PolymarketMarketData]:
        """Backwards-compatible page fetch that degrades a provider problem to an
        empty result ("nothing observed this pass")."""
        return await self.fetch_markets_page(*args, **kwargs) or []

    async def fetch_market_catalog(
        self,
        total_limit: int = 50,
        page_size: int = MAX_PAGE_SIZE,
        active: bool = True,
        closed: bool = False,
        tag_id: int | None = None,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
        max_pages: int = MAX_CATALOG_PAGES,
    ) -> MarketFetchResult:
        """Bounded, de-duplicated, multi-page read-only catalog walk using the
        Gamma `offset` cursor. Stops at `total_limit`, `max_pages`, a short page
        (catalog exhausted), or the first provider error — a coverage scan must
        never hammer a public endpoint. Never raises."""
        total_limit = max(1, min(int(total_limit), MAX_TOTAL_MARKETS))
        # never request a larger page than the budget can absorb (offsets stay
        # consistent because every page requests exactly `page_size`)
        page_size = max(1, min(int(page_size), MAX_PAGE_SIZE, total_limit))
        max_pages = max(1, min(int(max_pages), MAX_CATALOG_PAGES))

        result = MarketFetchResult()
        seen: set[str] = set()
        for page in range(max_pages):
            remaining = total_limit - len(result.markets)
            if remaining <= 0:
                result.truncated = True
                break
            page_markets = await self.fetch_markets_page(
                limit=page_size,
                active=active,
                closed=closed,
                offset=page * page_size,
                tag_id=tag_id,
                end_date_min=end_date_min,
                end_date_max=end_date_max,
            )
            result.pages_fetched += 1
            if page_markets is None:
                result.provider_errors += 1  # provider problem — stop, do not hammer
                break
            if not page_markets:
                break  # exhausted catalog — not an error
            for m in page_markets:
                if m.market_id in seen:
                    result.duplicates_dropped += 1
                    continue
                if len(result.markets) >= total_limit:
                    result.truncated = True
                    break
                seen.add(m.market_id)
                result.markets.append(m)
            if len(page_markets) < page_size:
                break  # short page — no more rows upstream
        return result

    async def search_markets(
        self,
        query: str,
        limit_per_type: int = 20,
        max_pages: int = MAX_SEARCH_PAGES,
        active_only: bool = True,
        include_closed: bool = False,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
    ) -> MarketFetchResult:
        """Bounded read-only `/public-search` walk for one query, flattened to
        markets. Paginates with `page` (1-based) — this endpoint IGNORES `offset`.

        active/closed AND the resolution window are filtered CLIENT-side: this
        endpoint exposes no date parameter, and its status parameters were not
        observed to actually filter. Silently-ignored server parameters must never
        be relied on, so the filtering happens here where it is verifiable.
        Never raises."""
        if not query or not query.strip():
            return MarketFetchResult()
        limit_per_type = max(1, min(int(limit_per_type), MAX_PAGE_SIZE))
        max_pages = max(1, min(int(max_pages), MAX_SEARCH_PAGES))
        window_min, window_max = _parse_dt(end_date_min), _parse_dt(end_date_max)

        result = MarketFetchResult()
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            payload = await self._get(
                GAMMA_API_BASE,
                "/public-search",
                params={"q": query.strip(), "limit_per_type": limit_per_type, "page": page},
            )
            result.pages_fetched += 1
            if not isinstance(payload, dict):
                result.provider_errors += 1
                break
            markets = _markets_from_search(payload)
            if not markets:
                break
            for m in markets:
                if active_only and not m.active:
                    continue
                if m.closed and not include_closed:
                    continue
                if not _within_window(m.end_date, window_min, window_max):
                    continue
                if m.market_id in seen:
                    result.duplicates_dropped += 1
                    continue
                seen.add(m.market_id)
                result.markets.append(m)
            if not _search_has_more(payload):
                break
        return result

    async def fetch_orderbook(self, token_id: str) -> PolymarketOrderbook | None:
        """One read-only CLOB order-book snapshot for a token id. None on any
        provider problem. This reads the book; it never places an order."""
        payload = await self._get(CLOB_API_BASE, "/book", params={"token_id": token_id})
        if not isinstance(payload, dict):
            return None
        return _parse_orderbook(payload, token_id)
