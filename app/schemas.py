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
    # Known settlement source, set by detail enrichment overlay (the list
    # endpoint never provides one; judges fall back to rules-text detection)
    settlement_source: str | None = None
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


class ResearchSource(BaseModel):
    """One place evidence should come from (or came from)."""

    name: str
    url: str | None = None
    source_type: str = "web"  # settlement_source|stats_provider|official|news|web
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    # External-evidence provenance (MVP-004E): human-readable title,
    # credibility band, and fetch freshness (ISO timestamp string, JSON-safe)
    title: str | None = None
    credibility: Literal["official", "high", "medium", "unknown"] = "unknown"
    fetched_at: str | None = None


class ResearchFact(BaseModel):
    """One piece of evidence with provenance and confidence."""

    fact: str
    confidence: float = Field(ge=0.0, le=1.0)
    source_name: str | None = None


class ResearchPacket(BaseModel):
    """Structured evidence packet for one market. Contains research inputs
    and facts only — no probability forecasts, no trade recommendations."""

    domain: str
    source_queries: list[str] = []
    sources: list[ResearchSource] = []
    key_facts: list[ResearchFact] = []
    missing_info: list[str] = []
    research_completeness_score: float = Field(ge=0.0, le=1.0)
    research_risk: Literal["low", "medium", "high"]
    # Raw collector output persisted for audit; never serialized in API responses
    raw_response: dict | None = Field(default=None, exclude=True, repr=False)


class ResearchPacketOut(ResearchPacket):
    """A persisted research packet, as returned by the API (raw_response
    stays DB-only)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    market_ticker: str
    scanner_run_id: int | None = None
    enrichment_id: int | None = None
    resolution_assessment_id: int | None = None
    collector_name: str
    collector_version: str
    created_at: datetime


class ForecastCase(BaseModel):
    """One side of the argument (bull = case for YES, bear = case for NO)."""

    thesis: str
    points: list[str] = []


class ForecastAssumption(BaseModel):
    assumption: str
    criticality: Literal["low", "medium", "high"] = "medium"


class ForecastChangeTrigger(BaseModel):
    trigger: str
    direction: Literal["increases_probability", "decreases_probability", "unclear"] = "unclear"


class MarketForecast(BaseModel):
    """Structured probability forecast for one market. Probabilities and
    reasoning artifacts only — no EV, no sizing, no trade recommendations."""

    estimated_probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_depth: Literal["template_only", "source_backed", "mixed"]
    forecast_risk: Literal["low", "medium", "high"]
    forecast_summary: str
    bull_case: ForecastCase
    bear_case: ForecastCase
    skeptic_notes: list[str] = []
    key_assumptions: list[ForecastAssumption] = []
    missing_info: list[str] = []
    what_would_change_mind: list[ForecastChangeTrigger] = []
    calibration_tags: list[str] = []
    # Raw forecaster output persisted for audit; never serialized in API responses
    raw_response: dict | None = Field(default=None, exclude=True, repr=False)


class MarketForecastOut(MarketForecast):
    """A persisted forecast, as returned by the API (raw_response stays DB-only)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    market_ticker: str
    scanner_run_id: int | None = None
    research_packet_id: int | None = None
    resolution_assessment_id: int | None = None
    forecaster_name: str
    forecaster_version: str
    model_name: str | None = None
    prompt_version: str
    created_at: datetime


class MarketOutcome(BaseModel):
    """Settlement state for one market as observed via read-only endpoints."""

    outcome_status: Literal["open", "closed", "settled", "canceled", "unknown"]
    resolved_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    winning_side: Literal["yes", "no", "void", "unknown"] | None = None
    settlement_price: float | None = None
    close_time: datetime | None = None
    settled_time: datetime | None = None
    source: str = "kalshi_rest"
    # Raw API payload persisted for audit; never serialized in API responses
    raw_payload: dict | None = Field(default=None, exclude=True, repr=False)


class MarketOutcomeOut(MarketOutcome):
    model_config = ConfigDict(from_attributes=True)

    id: int
    market_ticker: str
    created_at: datetime


class ForecastScore(BaseModel):
    """Calibration score of one forecast against one outcome state."""

    brier_score: float | None = None
    log_loss: float | None = None
    absolute_error: float | None = None
    was_resolved: bool = False
    score_status: Literal["scored", "pending_outcome", "unscorable"]
    score_notes: str | None = None
    score_tags: list[str] = []


class ForecastScoreOut(ForecastScore):
    model_config = ConfigDict(from_attributes=True)

    id: int
    forecast_id: int
    market_ticker: str
    outcome_id: int | None = None
    created_at: datetime


class ComparisonMetric(BaseModel):
    """Scored-forecast metrics for one forecaster side within one slice."""

    count_scored: int = 0
    mean_brier: float | None = None
    mean_log_loss: float | None = None
    mean_absolute_error: float | None = None


class ForecasterSideSummary(BaseModel):
    forecaster: str
    scored: ComparisonMetric
    coverage: int = 0  # latest-scored rows considered, any status
    pending: int = 0
    unscorable: int = 0


class ForecasterPairComparison(BaseModel):
    """Same-market pairing: baseline vs challenger scored against the SAME
    ticker and outcome. Stronger evidence than unpaired aggregates."""

    pair_count: int = 0
    wins: int = 0  # challenger had strictly lower Brier
    losses: int = 0
    ties: int = 0
    win_rate_by_market: float | None = None
    mean_delta_brier: float | None = None  # challenger - baseline; < 0 favors challenger
    mean_delta_log_loss: float | None = None
    mean_delta_absolute_error: float | None = None
    sample_label: str = "insufficient_sample"


class ForecasterCohortComparison(BaseModel):
    """Unpaired side-by-side within one cohort (market type, signal type,
    confidence bucket, ...). Less reliable than paired comparison."""

    cohort: str
    baseline: ComparisonMetric
    challenger: ComparisonMetric
    delta_brier: float | None = None  # challenger - baseline; < 0 favors challenger
    delta_log_loss: float | None = None
    delta_absolute_error: float | None = None
    sample_label: str = "insufficient_sample"
    paired: bool = False


class ForecasterComparisonSummary(BaseModel):
    """Champion/challenger comparison. Read-only measurement — carries no EV
    and no trade semantics; exists to gate whether a challenger forecaster
    demonstrably beats the market-anchored baseline."""

    baseline_forecaster: str
    challenger_forecaster: str
    filters: dict = {}
    comparison_basis: Literal["paired", "unpaired"] = "unpaired"
    baseline: ForecasterSideSummary
    challenger: ForecasterSideSummary
    delta_brier: float | None = None  # challenger - baseline; < 0 favors challenger
    delta_log_loss: float | None = None
    delta_absolute_error: float | None = None
    paired: ForecasterPairComparison | None = None
    sample_label: str = "insufficient_sample"
    warning: str | None = None
    interpretation: str = (
        "delta_brier < 0 and delta_log_loss < 0 favor the challenger; paired "
        "comparisons are stronger than unpaired; do not infer edge below a "
        "useful sample size."
    )
    by_market_type: list[ForecasterCohortComparison] = []
    by_signal_type: list[ForecasterCohortComparison] = []
    by_confidence_bucket: list[ForecasterCohortComparison] = []
    by_evidence_depth: list[ForecasterCohortComparison] = []
    by_forecast_risk: list[ForecasterCohortComparison] = []
    by_domain: list[ForecasterCohortComparison] = []
    by_game_stage: list[ForecasterCohortComparison] = []


class CohortStats(BaseModel):
    count: int
    mean_brier: float | None = None
    mean_log_loss: float | None = None
    mean_absolute_error: float | None = None


class CalibrationSummary(BaseModel):
    """Aggregate calibration over the latest score per forecast. Read-only
    scoring output — carries no EV or trade metrics."""

    total_scores: int = 0
    resolved: int = 0
    pending_outcome: int = 0
    unscorable: int = 0
    overall: CohortStats | None = None
    by_evidence_depth: dict[str, CohortStats] = {}
    by_forecast_risk: dict[str, CohortStats] = {}
    by_forecaster: dict[str, CohortStats] = {}
    by_domain: dict[str, CohortStats] = {}
    by_tag: dict[str, CohortStats] = {}


SignalStatus = Literal[
    "new",
    "reviewed",
    "dismissed",
    "promoted_to_research",
    "research_refreshed",
    "forecast_refreshed",
    # review label only — no paper trading exists anywhere in this codebase
    "paper_candidate_pending",
]


class RefreshedPacketSummary(BaseModel):
    """Compact view of a signal's refreshed research packet (no raw payloads)."""

    packet_id: int
    collector_name: str
    collector_version: str
    domain: str
    research_completeness_score: float
    evidence_depth: str  # computed via forecasting.determine_evidence_depth


class OpportunitySignalOut(BaseModel):
    """A persisted opportunity signal (informational only; raw payload stays
    DB-only)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    market_ticker: str
    signal_type: str
    signal_status: SignalStatus
    observed_at: datetime
    old_midpoint: float | None = None
    new_midpoint: float | None = None
    price_change: float | None = None
    spread: int | None = None
    liquidity_proxy: int | None = None
    latest_forecast_id: int | None = None
    latest_forecast_probability: float | None = None
    reason: str = ""
    evidence: dict | None = None
    promoted_at: datetime | None = None
    processed_at: datetime | None = None
    refreshed_research_packet_id: int | None = None
    refreshed_forecast_id: int | None = None
    processing_error_type: str | None = None
    processing_error_message: str | None = None
    created_at: datetime
    # Populated by processing/detail endpoints (not an ORM column):
    # collector/evidence-depth/completeness of the refreshed packet
    refreshed_packet: RefreshedPacketSummary | None = None


class SignalStatusUpdate(BaseModel):
    signal_status: SignalStatus


class RefreshedSignalSummary(BaseModel):
    signal_id: int
    market_ticker: str
    signal_type: str
    refreshed_forecast_id: int
    refreshed_probability: float
    refreshed_confidence: float
    processed_at: datetime | None = None


class CollectorStats(BaseModel):
    count: int = 0
    mean_completeness: float | None = None
    by_evidence_depth: dict[str, int] = {}


class ResearchCanaryReport(BaseModel):
    """External-research canary metrics over persisted research packets."""

    total_packets: int = 0
    by_collector: dict[str, CollectorStats] = {}
    by_domain: dict[str, int] = {}
    # external collector ran but produced template-only content (fetch failed,
    # game not found, ticker unparseable, ...)
    external_fallbacks: int = 0
    # forecast counts by forecaster identity (template_baseline vs
    # baseball_evidence, ...) so calibration cohorts can be compared
    forecasts_by_forecaster: dict[str, int] = {}


class SignalReport(BaseModel):
    """Aggregate signal-workflow view. Informational only — no EV, no trade
    metrics."""

    total: int = 0
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    promoted_awaiting_processing: int = 0
    processed_with_errors: int = 0
    recent_refreshed: list[RefreshedSignalSummary] = []
    research_canary: ResearchCanaryReport | None = None


class WatcherRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    markets_checked: int = 0
    ticks_recorded: int = 0
    signals_created: int = 0
    error_type: str | None = None
    error_message: str | None = None
    created_at: datetime


class PipelineStageRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    stage_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    items_attempted: int = 0
    items_succeeded: int = 0
    items_failed: int = 0
    summary: dict | None = None
    error_type: str | None = None
    error_message: str | None = None
    created_at: datetime


class PipelineRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_type: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    config: dict | None = None
    summary: dict | None = None
    error_type: str | None = None
    error_message: str | None = None
    created_at: datetime


class PipelineRunDetailOut(PipelineRunOut):
    stages: list[PipelineStageRunOut] = []


class MarketDetailEnrichmentOut(BaseModel):
    """A persisted detail enrichment, without the large raw_* payloads
    (those stay DB-only for audit)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    market_ticker: str
    scanner_run_id: int | None = None
    event_ticker: str | None = None
    series_ticker: str | None = None
    title: str | None = None
    subtitle: str | None = None
    rules_text: str | None = None
    settlement_source: str | None = None
    category: str | None = None
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
    # Latest persisted forecast; populated only when include_forecast=true
    # (a GET never creates forecasts)
    forecast: MarketForecastOut | None = None


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


# --- Crypto Arena (CRYPTO-001) — read-only surveillance outputs. Raw
# provider payloads stay DB-only (audit), mirroring raw_response elsewhere.


class CryptoTokenOut(BaseModel):
    """A persisted crypto token (observation record only)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    chain: str
    token_address: str
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    token_metadata: dict | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime


class CryptoPairOut(BaseModel):
    """A persisted DEX pair (observation record only)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    chain: str
    pair_address: str
    base_token_address: str
    quote_token_address: str | None = None
    dex_id: str | None = None
    url: str | None = None
    pair_created_at: datetime | None = None
    pair_metadata: dict | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime


class CryptoSignalOut(BaseModel):
    """A persisted crypto signal (informational only; raw payload stays
    DB-only). No EV, no sizing, no trade directives."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    chain: str
    token_address: str
    pair_address: str | None = None
    signal_type: str
    signal_status: str
    observed_at: datetime
    reason: str = ""
    evidence: dict | None = None
    created_at: datetime


class CryptoRunSummary(BaseModel):
    """One crypto scan pass (audit summary)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    tokens_checked: int = 0
    pairs_checked: int = 0
    ticks_recorded: int = 0
    signals_created: int = 0
    error_type: str | None = None
    error_message: str | None = None


class CryptoReport(BaseModel):
    """Aggregate crypto surveillance view. Informational only."""

    totals: dict[str, int]
    signals_by_type: dict[str, int]
    signals_by_status: dict[str, int]
    risk_by_level: dict[str, int]
    recent_signals: list[CryptoSignalOut]
    recent_tokens: list[CryptoTokenOut]
    latest_run: CryptoRunSummary | None = None
    provider_errors: list[CryptoRunSummary] = []
