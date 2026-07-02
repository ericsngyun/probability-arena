"""Market ranking: score each market 0-1 on tradability signals.

Components (each normalized to [0, 1]):
- spread: tighter yes bid/ask spread is better; unquoted markets score 0
- liquidity: log-scaled resting liquidity
- volume: log-scaled 24h volume
- expiration: sweet spot between "resolves too soon to act" and "capital
  parked for months"
- resolution_clarity: PLACEHOLDER — returns a neutral constant until a real
  rules-text analyzer exists
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from app.schemas import MarketData, RankedMarket, ScoreComponents


@dataclass(frozen=True)
class RankingWeights:
    spread: float = 0.30
    liquidity: float = 0.25
    volume: float = 0.20
    expiration: float = 0.15
    resolution_clarity: float = 0.10


DEFAULT_WEIGHTS = RankingWeights()

# log-scale ceilings: values at or above these get a full component score
LIQUIDITY_CEILING = 1_000_000  # cents of resting liquidity
VOLUME_CEILING = 100_000  # contracts traded in 24h

# expiration sweet spot, in days from now
EXPIRATION_MIN_DAYS = 0.25  # < 6 hours out: too late to build a position
EXPIRATION_IDEAL_LOW = 1.0
EXPIRATION_IDEAL_HIGH = 30.0
EXPIRATION_MAX_DAYS = 180.0


def spread_score(market: MarketData) -> float:
    spread = market.spread
    if spread is None or spread < 0:
        return 0.0
    return max(0.0, 1.0 - spread / 100.0)


def _log_score(value: int, ceiling: int) -> float:
    if value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(ceiling))


def liquidity_score(market: MarketData) -> float:
    return _log_score(market.liquidity, LIQUIDITY_CEILING)


def volume_score(market: MarketData) -> float:
    return _log_score(market.volume_24h, VOLUME_CEILING)


def expiration_score(market: MarketData, now: datetime | None = None) -> float:
    """Trapezoid over days-to-close: 0 below MIN, ramps to 1 across the ideal
    band, decays to 0 at MAX."""
    close = market.close_time or market.expiration_time
    if close is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    days = (close - now).total_seconds() / 86_400
    if days <= EXPIRATION_MIN_DAYS or days >= EXPIRATION_MAX_DAYS:
        return 0.0
    if days < EXPIRATION_IDEAL_LOW:
        return (days - EXPIRATION_MIN_DAYS) / (EXPIRATION_IDEAL_LOW - EXPIRATION_MIN_DAYS)
    if days <= EXPIRATION_IDEAL_HIGH:
        return 1.0
    return (EXPIRATION_MAX_DAYS - days) / (EXPIRATION_MAX_DAYS - EXPIRATION_IDEAL_HIGH)


def resolution_clarity_score(market: MarketData) -> float:
    """PLACEHOLDER: neutral 0.5 for every market.

    TODO(MVP-002): score rules_primary text for objective, verifiable
    resolution criteria (named data source, unambiguous threshold, no
    discretionary language).
    """
    return 0.5


def score_market(
    market: MarketData,
    weights: RankingWeights = DEFAULT_WEIGHTS,
    now: datetime | None = None,
) -> RankedMarket:
    components = ScoreComponents(
        spread=spread_score(market),
        liquidity=liquidity_score(market),
        volume=volume_score(market),
        expiration=expiration_score(market, now=now),
        resolution_clarity=resolution_clarity_score(market),
    )
    total_weight = (
        weights.spread
        + weights.liquidity
        + weights.volume
        + weights.expiration
        + weights.resolution_clarity
    )
    score = (
        components.spread * weights.spread
        + components.liquidity * weights.liquidity
        + components.volume * weights.volume
        + components.expiration * weights.expiration
        + components.resolution_clarity * weights.resolution_clarity
    ) / total_weight
    return RankedMarket(market=market, score=round(score, 6), components=components)


def rank_markets(
    markets: list[MarketData],
    weights: RankingWeights = DEFAULT_WEIGHTS,
    now: datetime | None = None,
) -> list[RankedMarket]:
    """Score and sort descending; ties broken by ticker for determinism."""
    now = now or datetime.now(timezone.utc)
    ranked = [score_market(m, weights=weights, now=now) for m in markets]
    ranked.sort(key=lambda r: (-r.score, r.market.ticker))
    return ranked
