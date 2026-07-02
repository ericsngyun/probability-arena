from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MarketData(BaseModel):
    """Normalized view of a Kalshi market, decoupled from raw API payload shape.

    Prices are integer cents (0-100). Missing quotes stay None rather than 0 so
    ranking can distinguish 'no bid' from 'bid at 0'.
    """

    ticker: str
    event_ticker: str | None = None
    title: str = ""
    category: str | None = None
    status: str = "unknown"
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    last_price: int | None = None
    volume: int = 0
    volume_24h: int = 0
    open_interest: int = 0
    liquidity: int = 0
    close_time: datetime | None = None
    expiration_time: datetime | None = None
    rules_primary: str | None = None
    # Original API payload, persisted to market_snapshots.raw_payload for debugging
    raw: dict | None = None

    @property
    def spread(self) -> int | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid


class ScoreComponents(BaseModel):
    spread: float
    liquidity: float
    volume: float
    expiration: float
    resolution_clarity: float


class RankedMarket(BaseModel):
    market: MarketData
    score: float
    components: ScoreComponents


class ResolutionAssessment(BaseModel):
    """Structured verdict on how clear and objective a market's resolution
    criteria are. Produced by a ResolutionJudge (rule-based, mock, or LLM)."""

    clarity_score: float = Field(ge=0.0, le=1.0)
    resolution_risk: Literal["low", "medium", "high", "unknown"]
    tradeability: Literal["researchable", "avoid", "needs_manual_review"]
    settlement_source: str | None = None
    resolution_summary: str = ""
    ambiguity_flags: list[str] = []
    rejection_reasons: list[str] = []
    llm_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # Raw judge output persisted for audit; never serialized in API responses
    raw_response: dict | None = Field(default=None, exclude=True, repr=False)


class ResolutionAssessmentOut(ResolutionAssessment):
    """A persisted resolution assessment, as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    market_ticker: str
    model_name: str
    prompt_version: str
    scanner_run_id: int | None = None
    created_at: datetime


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    title: str
    status: str
    yes_bid: int | None
    yes_ask: int | None
    spread: int | None
    volume_24h: int
    open_interest: int
    liquidity: int
    close_time: datetime | None
    score: float
    components: ScoreComponents
    is_eligible: bool = True
    warnings: list[str] = []
    # Latest persisted resolution assessment; populated only when
    # include_resolution=true and an assessment exists for this ticker
    resolution: ResolutionAssessmentOut | None = None


class RejectedMarketOut(BaseModel):
    """Debug view of a market that failed the eligibility gate."""

    ticker: str
    title: str
    status: str
    is_eligible: bool = False
    score: float = 0.0
    rejection_reasons: list[str]
    warnings: list[str] = []
    yes_bid: int | None = None
    yes_ask: int | None = None
    spread: int | None = None
    liquidity: int = 0
    volume_24h: int = 0
    expiration_days: float | None = None
    market_type_flags: dict[str, bool] = {}


class CandidatesResponse(BaseModel):
    scanner_run_id: int | None
    as_of: datetime
    cached: bool = False
    markets_assessed: int = 0
    eligible_count: int = 0
    rejected_count: int = 0
    candidates: list[CandidateOut]
    # Populated only when include_rejected=true
    rejected: list[RejectedMarketOut] = []
