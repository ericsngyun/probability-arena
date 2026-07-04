"""DEX Screener adapter (CRYPTO-001): read-only GETs against the public
api.dexscreener.com endpoints (no credentials). Solana-only filtering happens
here so downstream services never see other chains unless asked.

Every fetch method returns [] (or None for single lookups) on HTTP errors,
rate limits, empty responses, or schema drift — the crypto lane degrades to
"nothing discovered this pass" instead of failing.

Hard boundary: this module reads market data only. No wallets, no swaps, no
transaction construction, no execution — see docs/SAFETY_BOUNDARIES.md.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

DEXSCREENER_API_BASE = "https://api.dexscreener.com"
SOURCE_NAME = "dexscreener"


@dataclass(frozen=True)
class TokenProfile:
    """One entry from token-profiles/latest or token-boosts/latest."""

    chain: str
    token_address: str
    url: str | None = None
    description: str | None = None
    boost_amount: float | None = None  # only for boost entries
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class PairData:
    """One DEX pair snapshot (normalized from the /latest/dex payloads)."""

    chain: str
    pair_address: str
    base_token_address: str
    base_token_symbol: str | None = None
    base_token_name: str | None = None
    quote_token_address: str | None = None
    dex_id: str | None = None
    url: str | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    volume_5m_usd: float | None = None
    volume_1h_usd: float | None = None
    volume_24h_usd: float | None = None
    price_change_5m: float | None = None  # percent
    price_change_1h: float | None = None  # percent
    market_cap: float | None = None
    fdv: float | None = None
    pair_created_at: datetime | None = None
    boosts_active: int | None = None
    raw: dict = field(default_factory=dict, repr=False)


def _float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_pair(payload: dict) -> PairData | None:
    """Normalize one pair dict; None when required identifiers are missing
    (schema drift tolerance)."""
    if not isinstance(payload, dict):
        return None
    chain = payload.get("chainId")
    pair_address = payload.get("pairAddress")
    base = payload.get("baseToken") or {}
    base_address = base.get("address")
    if not chain or not pair_address or not base_address:
        return None
    quote = payload.get("quoteToken") or {}
    volume = payload.get("volume") or {}
    change = payload.get("priceChange") or {}
    liquidity = payload.get("liquidity") or {}
    created_ms = payload.get("pairCreatedAt")
    pair_created_at = (
        datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
        if isinstance(created_ms, (int, float)) and created_ms > 0
        else None
    )
    boosts = payload.get("boosts") or {}
    return PairData(
        chain=chain,
        pair_address=pair_address,
        base_token_address=base_address,
        base_token_symbol=base.get("symbol"),
        base_token_name=base.get("name"),
        quote_token_address=quote.get("address"),
        dex_id=payload.get("dexId"),
        url=payload.get("url"),
        price_usd=_float(payload.get("priceUsd")),
        liquidity_usd=_float(liquidity.get("usd")),
        volume_5m_usd=_float(volume.get("m5")),
        volume_1h_usd=_float(volume.get("h1")),
        volume_24h_usd=_float(volume.get("h24")),
        price_change_5m=_float(change.get("m5")),
        price_change_1h=_float(change.get("h1")),
        market_cap=_float(payload.get("marketCap")),
        fdv=_float(payload.get("fdv")),
        pair_created_at=pair_created_at,
        boosts_active=boosts.get("active"),
        raw=payload,
    )


def _parse_profiles(payload, chain: str) -> list[TokenProfile]:
    """Normalize token-profiles/token-boosts lists, keeping only `chain`."""
    if not isinstance(payload, list):
        return []
    profiles: list[TokenProfile] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if entry.get("chainId") != chain or not entry.get("tokenAddress"):
            continue
        profiles.append(
            TokenProfile(
                chain=entry["chainId"],
                token_address=entry["tokenAddress"],
                url=entry.get("url"),
                description=entry.get("description"),
                boost_amount=_float(entry.get("totalAmount") or entry.get("amount")),
                raw=entry,
            )
        )
    return profiles


class DexScreenerAdapter:
    """Thin async client. All methods are read-only GETs and never raise on
    network/HTTP/schema problems — they log and return empty results."""

    source_name = SOURCE_NAME

    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self.chain = settings.crypto_chain
        self.timeout = 15.0

    async def _get(self, path: str) -> dict | list | None:
        url = f"{DEXSCREENER_API_BASE}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url)
                if response.status_code == 429:
                    logger.warning("DEX Screener rate limit hit for %s", url)
                    return None
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("DEX Screener fetch failed for %s: %s", url, exc)
            return None

    async def fetch_latest_token_profiles(self) -> list[TokenProfile]:
        """Latest token profiles (rate limit 60 rpm), filtered to our chain."""
        return _parse_profiles(await self._get("/token-profiles/latest/v1"), self.chain)

    async def fetch_latest_boosted_tokens(self) -> list[TokenProfile]:
        """Latest boosted tokens (rate limit 60 rpm), filtered to our chain."""
        return _parse_profiles(await self._get("/token-boosts/latest/v1"), self.chain)

    async def fetch_pairs_for_token(self, token_address: str) -> list[PairData]:
        """All pairs/pools for one token address (rate limit 300 rpm)."""
        payload = await self._get(f"/token-pairs/v1/{self.chain}/{token_address}")
        if not isinstance(payload, list):
            return []
        pairs = [_parse_pair(entry) for entry in payload]
        return [pair for pair in pairs if pair is not None and pair.chain == self.chain]

    async def fetch_pair(self, pair_address: str) -> PairData | None:
        """One pair by its pair address (rate limit 300 rpm)."""
        payload = await self._get(f"/latest/dex/pairs/{self.chain}/{pair_address}")
        if not isinstance(payload, dict):
            return None
        entries = payload.get("pairs") or payload.get("pair") or []
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries:
            pair = _parse_pair(entry)
            if pair is not None and pair.chain == self.chain:
                return pair
        return None

    async def search_pairs(self, query: str) -> list[PairData]:
        """Free-text pair search (rate limit 300 rpm), filtered to our chain."""
        payload = await self._get(f"/latest/dex/search?q={query}")
        if not isinstance(payload, dict):
            return []
        pairs = [_parse_pair(entry) for entry in payload.get("pairs") or []]
        return [pair for pair in pairs if pair is not None and pair.chain == self.chain]
