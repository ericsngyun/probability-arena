"""Deterministic candidate hygiene: eligibility gating applied before ranking.

Every fetched market is assessed against configurable thresholds. Markets
failing any enabled gate collect machine-readable rejection_reasons and are
excluded from default candidate output (their snapshots persist with score
0.0). Assessments are persisted to market_eligibility_assessments for audit.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel

from app.config import Settings, get_settings
from app.schemas import MarketData

REASON_NO_QUOTES = "no_quotes"
REASON_ONE_SIDED_QUOTE = "one_sided_quote"
REASON_SPREAD_TOO_WIDE = "spread_too_wide"
REASON_LIQUIDITY_BELOW_MIN = "liquidity_below_min"
REASON_VOLUME_24H_BELOW_MIN = "volume_24h_below_min"
REASON_EXPIRES_TOO_SOON = "expires_too_soon"
REASON_EXPIRES_TOO_FAR = "expires_too_far"
REASON_MISSING_EXPIRATION = "missing_expiration"

WARNING_PARLAY_LIKE = "parlay_like_market"
WARNING_NO_OPEN_INTEREST = "no_open_interest"

# Ticker fragments Kalshi uses for multivariate/parlay-style combo markets
MULTIVARIATE_TICKER_MARKERS = ("KXMVE", "CROSSCATEGORY", "MULTIGAME", "PARLAY")


@dataclass(frozen=True)
class EligibilityThresholds:
    require_two_sided_quote: bool = True
    exclude_zero_quote_markets: bool = True
    min_liquidity: int = 100
    min_volume_24h: int = 25
    max_spread_cents: int = 20
    min_days_to_expiration: float = 0.25
    max_days_to_expiration: float = 45.0

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "EligibilityThresholds":
        s = settings or get_settings()
        return cls(
            require_two_sided_quote=s.require_two_sided_quote,
            exclude_zero_quote_markets=s.exclude_zero_quote_markets,
            min_liquidity=s.min_liquidity,
            min_volume_24h=s.min_volume_24h,
            max_spread_cents=round(s.max_spread * 100),
            min_days_to_expiration=s.min_days_to_expiration,
            max_days_to_expiration=s.max_days_to_expiration,
        )


class EligibilityAssessment(BaseModel):
    is_eligible: bool
    has_two_sided_quote: bool
    has_nonzero_quotes: bool
    spread_ok: bool
    liquidity_ok: bool
    volume_ok: bool
    expiration_ok: bool
    market_type_flags: dict[str, bool]
    rejection_reasons: list[str]
    warnings: list[str]
    spread: int | None
    expiration_days: float | None


def market_type_flags(market: MarketData) -> dict[str, bool]:
    ticker = market.ticker.upper()
    title = market.title.lower()
    return {
        "multivariate": any(marker in ticker for marker in MULTIVARIATE_TICKER_MARKERS),
        # Combo titles read like "yes A,yes B,no C" — several comma-joined legs
        "combo_title": title.count(",") >= 2 and ("yes " in title or "no " in title),
    }


def assess_market(
    market: MarketData,
    thresholds: EligibilityThresholds | None = None,
    now: datetime | None = None,
) -> EligibilityAssessment:
    thresholds = thresholds or EligibilityThresholds.from_settings()
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    warnings: list[str] = []

    has_nonzero_quotes = market.yes_bid is not None or market.yes_ask is not None
    has_two_sided_quote = market.yes_bid is not None and market.yes_ask is not None
    spread = market.spread

    if not has_nonzero_quotes:
        if thresholds.exclude_zero_quote_markets:
            reasons.append(REASON_NO_QUOTES)
        elif thresholds.require_two_sided_quote:
            reasons.append(REASON_ONE_SIDED_QUOTE)
    elif not has_two_sided_quote and thresholds.require_two_sided_quote:
        reasons.append(REASON_ONE_SIDED_QUOTE)

    spread_ok = (
        has_two_sided_quote and spread is not None and 0 <= spread <= thresholds.max_spread_cents
    )
    if has_two_sided_quote and not spread_ok:
        reasons.append(REASON_SPREAD_TOO_WIDE)

    liquidity_ok = market.liquidity >= thresholds.min_liquidity
    if not liquidity_ok:
        reasons.append(REASON_LIQUIDITY_BELOW_MIN)

    volume_ok = market.volume_24h >= thresholds.min_volume_24h
    if not volume_ok:
        reasons.append(REASON_VOLUME_24H_BELOW_MIN)

    close = market.close_time or market.expiration_time
    if close is None:
        expiration_days = None
        expiration_ok = False
        reasons.append(REASON_MISSING_EXPIRATION)
    else:
        expiration_days = (close - now).total_seconds() / 86_400
        expiration_ok = (
            thresholds.min_days_to_expiration <= expiration_days <= thresholds.max_days_to_expiration
        )
        if expiration_days < thresholds.min_days_to_expiration:
            reasons.append(REASON_EXPIRES_TOO_SOON)
        elif expiration_days > thresholds.max_days_to_expiration:
            reasons.append(REASON_EXPIRES_TOO_FAR)

    flags = market_type_flags(market)
    if any(flags.values()):
        warnings.append(WARNING_PARLAY_LIKE)
    if market.open_interest == 0:
        warnings.append(WARNING_NO_OPEN_INTEREST)

    return EligibilityAssessment(
        is_eligible=not reasons,
        has_two_sided_quote=has_two_sided_quote,
        has_nonzero_quotes=has_nonzero_quotes,
        spread_ok=spread_ok,
        liquidity_ok=liquidity_ok,
        volume_ok=volume_ok,
        expiration_ok=expiration_ok,
        market_type_flags=flags,
        rejection_reasons=reasons,
        warnings=warnings,
        spread=spread,
        expiration_days=round(expiration_days, 4) if expiration_days is not None else None,
    )
