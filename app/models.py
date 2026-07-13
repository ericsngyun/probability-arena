from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# JSON on SQLite (tests), JSONB on Postgres
RawJSON = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Market(Base):
    """A Kalshi market we have observed. One row per ticker; mutable metadata."""

    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    event_ticker: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiration_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rules_primary: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    snapshots: Mapped[list["MarketSnapshot"]] = relationship(back_populates="market")
    orderbook_snapshots: Mapped[list["OrderbookSnapshot"]] = relationship(back_populates="market")


class MarketSnapshot(Base):
    """Point-in-time top-of-book and activity stats for a market. Prices in cents (0-100)."""

    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    scanner_run_id: Mapped[int | None] = mapped_column(ForeignKey("scanner_runs.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    yes_bid: Mapped[int | None] = mapped_column(Integer)
    yes_ask: Mapped[int | None] = mapped_column(Integer)
    no_bid: Mapped[int | None] = mapped_column(Integer)
    no_ask: Mapped[int | None] = mapped_column(Integer)
    last_price: Mapped[int | None] = mapped_column(Integer)
    volume: Mapped[int] = mapped_column(Integer, default=0)
    volume_24h: Mapped[int] = mapped_column(Integer, default=0)
    open_interest: Mapped[int] = mapped_column(Integer, default=0)
    liquidity: Mapped[int] = mapped_column(Integer, default=0)

    score: Mapped[float | None] = mapped_column(Float)
    score_components: Mapped[dict | None] = mapped_column(JSON)
    # Raw Kalshi market object as fetched, for debugging normalization issues
    raw_payload: Mapped[dict | None] = mapped_column(RawJSON)

    market: Mapped[Market] = relationship(back_populates="snapshots")
    scanner_run: Mapped["ScannerRun | None"] = relationship(back_populates="snapshots")

    __table_args__ = (Index("ix_market_snapshots_market_captured", "market_id", "captured_at"),)


class OrderbookSnapshot(Base):
    """Full orderbook depth captured from the WebSocket feed (or REST backfill)."""

    __tablename__ = "orderbook_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(16), default="ws")
    # {"yes": [[price_cents, qty], ...], "no": [[price_cents, qty], ...]}
    yes_levels: Mapped[list | None] = mapped_column(JSON)
    no_levels: Mapped[list | None] = mapped_column(JSON)

    market: Mapped[Market] = relationship(back_populates="orderbook_snapshots")

    __table_args__ = (Index("ix_orderbook_snapshots_market_captured", "market_id", "captured_at"),)


class MarketEligibilityAssessment(Base):
    """Audit record of the eligibility gate for one market in one scan."""

    __tablename__ = "market_eligibility_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    scanner_run_id: Mapped[int | None] = mapped_column(ForeignKey("scanner_runs.id"), index=True)
    is_eligible: Mapped[bool] = mapped_column(default=False)
    rejection_reasons: Mapped[list | None] = mapped_column(RawJSON)
    warnings: Mapped[list | None] = mapped_column(RawJSON)
    has_two_sided_quote: Mapped[bool] = mapped_column(default=False)
    yes_bid: Mapped[int | None] = mapped_column(Integer)
    yes_ask: Mapped[int | None] = mapped_column(Integer)
    spread: Mapped[int | None] = mapped_column(Integer)
    liquidity: Mapped[int] = mapped_column(Integer, default=0)
    volume_24h: Mapped[int] = mapped_column(Integer, default=0)
    expiration_days: Mapped[float | None] = mapped_column(Float)
    market_type_flags: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    scanner_run: Mapped["ScannerRun | None"] = relationship(back_populates="eligibility_assessments")


class MarketDetailEnrichment(Base):
    """Richest available Kalshi metadata for one market at one point in time,
    fetched from the detail/event/series endpoints (the list endpoint omits
    settlement sources and secondary rules). Raw payloads kept for audit.

    scanner_run_id is null when enriched ad hoc (POST endpoint)."""

    __tablename__ = "market_detail_enrichments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    scanner_run_id: Mapped[int | None] = mapped_column(ForeignKey("scanner_runs.id"), index=True)
    event_ticker: Mapped[str | None] = mapped_column(String(64))
    series_ticker: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(Text)
    subtitle: Mapped[str | None] = mapped_column(Text)
    rules_text: Mapped[str | None] = mapped_column(Text)
    settlement_source: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    raw_market_detail: Mapped[dict] = mapped_column(RawJSON)
    raw_event_detail: Mapped[dict | None] = mapped_column(RawJSON)
    raw_series_detail: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    scanner_run: Mapped["ScannerRun | None"] = relationship(
        back_populates="detail_enrichments"
    )


class MarketResolutionAssessment(Base):
    """Audit record of one resolution-criteria assessment for one market.

    scanner_run_id is null when the assessment was made ad hoc (POST endpoint)
    rather than as part of a scan-driven batch.
    """

    __tablename__ = "market_resolution_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    scanner_run_id: Mapped[int | None] = mapped_column(ForeignKey("scanner_runs.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(16), default="v1")
    clarity_score: Mapped[float] = mapped_column(Float)
    resolution_risk: Mapped[str] = mapped_column(String(16))  # low|medium|high|unknown
    tradeability: Mapped[str] = mapped_column(String(32))  # researchable|avoid|needs_manual_review
    settlement_source: Mapped[str | None] = mapped_column(Text)
    resolution_summary: Mapped[str] = mapped_column(Text, default="")
    ambiguity_flags: Mapped[list | None] = mapped_column(RawJSON)
    rejection_reasons: Mapped[list | None] = mapped_column(RawJSON)
    llm_confidence: Mapped[float | None] = mapped_column(Float)
    raw_response: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    scanner_run: Mapped["ScannerRun | None"] = relationship(
        back_populates="resolution_assessments"
    )


class MarketResearchPacket(Base):
    """Structured evidence packet for one market: queries, sources, facts,
    gaps. Research inputs only — no forecasts, no trade recommendations.

    scanner_run_id / enrichment_id / resolution_assessment_id link the packet
    to the pipeline rows it was built from (null when built ad hoc or when
    the upstream row didn't exist yet)."""

    __tablename__ = "market_research_packets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    scanner_run_id: Mapped[int | None] = mapped_column(ForeignKey("scanner_runs.id"), index=True)
    enrichment_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_detail_enrichments.id")
    )
    resolution_assessment_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_resolution_assessments.id")
    )
    collector_name: Mapped[str] = mapped_column(String(64))
    collector_version: Mapped[str] = mapped_column(String(16), default="v1")
    domain: Mapped[str] = mapped_column(String(32), index=True)
    source_queries: Mapped[list | None] = mapped_column(RawJSON)
    sources: Mapped[list | None] = mapped_column(RawJSON)
    key_facts: Mapped[list | None] = mapped_column(RawJSON)
    missing_info: Mapped[list | None] = mapped_column(RawJSON)
    research_completeness_score: Mapped[float] = mapped_column(Float)
    research_risk: Mapped[str] = mapped_column(String(16))  # low|medium|high
    raw_response: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    scanner_run: Mapped["ScannerRun | None"] = relationship(back_populates="research_packets")


class MarketForecastRecord(Base):
    """One structured probability forecast for one market. Probabilities and
    reasoning artifacts only — this table (and this codebase) carries no EV,
    sizing, or trade-recommendation fields by design.

    Links back to the research packet and resolution assessment consumed."""

    __tablename__ = "market_forecasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    scanner_run_id: Mapped[int | None] = mapped_column(ForeignKey("scanner_runs.id"), index=True)
    research_packet_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_research_packets.id")
    )
    resolution_assessment_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_resolution_assessments.id")
    )
    forecaster_name: Mapped[str] = mapped_column(String(64))
    forecaster_version: Mapped[str] = mapped_column(String(16), default="v1")
    model_name: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(16), default="v1")
    estimated_probability: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    evidence_depth: Mapped[str] = mapped_column(String(16))  # template_only|source_backed|mixed
    forecast_risk: Mapped[str] = mapped_column(String(16))  # low|medium|high
    forecast_summary: Mapped[str] = mapped_column(Text, default="")
    bull_case: Mapped[dict | None] = mapped_column(RawJSON)
    bear_case: Mapped[dict | None] = mapped_column(RawJSON)
    skeptic_notes: Mapped[list | None] = mapped_column(RawJSON)
    key_assumptions: Mapped[list | None] = mapped_column(RawJSON)
    missing_info: Mapped[list | None] = mapped_column(RawJSON)
    what_would_change_mind: Mapped[list | None] = mapped_column(RawJSON)
    calibration_tags: Mapped[list | None] = mapped_column(RawJSON)
    raw_response: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    scanner_run: Mapped["ScannerRun | None"] = relationship(back_populates="forecasts")


class MarketOutcomeRecord(Base):
    """Latest known outcome/settlement state for one market, synced read-only
    from the Kalshi detail endpoint. One row per ticker, updated in place as
    the market moves open -> closed -> settled."""

    __tablename__ = "market_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_ticker: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    outcome_status: Mapped[str] = mapped_column(String(16))  # open|closed|settled|canceled|unknown
    resolved_probability: Mapped[float | None] = mapped_column(Float)  # 1.0 yes / 0.0 no / null
    winning_side: Mapped[str | None] = mapped_column(String(8))  # yes|no|void|unknown
    settlement_price: Mapped[float | None] = mapped_column(Float)  # dollars per contract
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32), default="kalshi_rest")
    raw_payload: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ForecastScoreRecord(Base):
    """Calibration score of one forecast against one outcome state. Append-
    only: re-scoring after an outcome change creates a new row, preserving
    the audit trail. Read-only scoring — no EV, no trade metrics."""

    __tablename__ = "forecast_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    forecast_id: Mapped[int] = mapped_column(ForeignKey("market_forecasts.id"), index=True)
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    outcome_id: Mapped[int | None] = mapped_column(ForeignKey("market_outcomes.id"))
    brier_score: Mapped[float | None] = mapped_column(Float)
    log_loss: Mapped[float | None] = mapped_column(Float)
    absolute_error: Mapped[float | None] = mapped_column(Float)
    was_resolved: Mapped[bool] = mapped_column(default=False)
    score_status: Mapped[str] = mapped_column(String(16), index=True)  # scored|pending_outcome|unscorable
    score_notes: Mapped[str | None] = mapped_column(Text)
    score_tags: Mapped[list | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MarketPriceTick(Base):
    """One observed quote snapshot from the real-time watcher. Midpoint is in
    dollars (0..1); bid/ask/spread in integer cents; liquidity_proxy in cents
    of resting top-of-book notional."""

    __tablename__ = "market_price_ticks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    yes_bid: Mapped[int | None] = mapped_column(Integer)
    yes_ask: Mapped[int | None] = mapped_column(Integer)
    midpoint: Mapped[float | None] = mapped_column(Float)
    spread: Mapped[int | None] = mapped_column(Integer)
    volume_24h: Mapped[int] = mapped_column(Integer, default=0)
    liquidity_proxy: Mapped[int] = mapped_column(Integer, default=0)
    raw_payload: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_market_price_ticks_ticker_observed", "market_ticker", "observed_at"),
    )


class MarketPriceTickBucket(Base):
    """OPS-012: one fixed-interval AGGREGATE of raw market_price_ticks for a
    ticker — OHLC midpoint, open/close bid/ask, spread and liquidity ranges, and
    the tick count. A storage/telemetry SUMMARY so raw ticks need not be kept
    forever: it carries no side, size, EV, dollar, action, recommendation,
    order, wallet, or execution field and is never a trading signal. Raw ticks
    are unchanged; aggregation never deletes them (only the retention service,
    explicitly invoked, prunes raw ticks per its own unchanged window)."""

    __tablename__ = "market_price_tick_buckets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    domain: Mapped[str | None] = mapped_column(String(32), index=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bucket_seconds: Mapped[int] = mapped_column(Integer, default=300)
    # midpoint OHLC (dollars 0..1, like the raw tick); None when no tick in the
    # bucket carried a midpoint — never fabricated
    open_mid: Mapped[float | None] = mapped_column(Float)
    high_mid: Mapped[float | None] = mapped_column(Float)
    low_mid: Mapped[float | None] = mapped_column(Float)
    close_mid: Mapped[float | None] = mapped_column(Float)
    # first/last observed quotes (integer cents, like the raw tick)
    open_bid: Mapped[int | None] = mapped_column(Integer)
    close_bid: Mapped[int | None] = mapped_column(Integer)
    open_ask: Mapped[int | None] = mapped_column(Integer)
    close_ask: Mapped[int | None] = mapped_column(Integer)
    # spread/liquidity ranges over the bucket
    spread_min: Mapped[int | None] = mapped_column(Integer)
    spread_max: Mapped[int | None] = mapped_column(Integer)
    spread_avg: Mapped[float | None] = mapped_column(Float)
    liquidity_min: Mapped[int | None] = mapped_column(Integer)
    liquidity_max: Mapped[int | None] = mapped_column(Integer)
    liquidity_avg: Mapped[float | None] = mapped_column(Float)
    tick_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "market_ticker", "bucket_start", "bucket_seconds",
            name="uq_tick_bucket_ticker_start_seconds",
        ),
        Index("ix_tick_bucket_start", "bucket_start"),
    )


class TickAggregationRun(Base):
    """OPS-013: one tick-aggregation pass (audit spine). Evidence for the
    raw-retention-readiness gates — a retention reduction may only be STAGED
    once scheduled runs are demonstrably clean. Counters only; storage
    plumbing, never a trading surface."""

    __tablename__ = "tick_aggregation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error
    scheduled: Mapped[bool] = mapped_column(default=False)  # timer-invoked (--scheduled)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    window_hours: Mapped[int] = mapped_column(Integer, default=0)
    subwindow_hours: Mapped[int] = mapped_column(Integer, default=1)
    bucket_seconds: Mapped[int] = mapped_column(Integer, default=300)
    rows_read: Mapped[int] = mapped_column(Integer, default=0)
    buckets_written: Mapped[int] = mapped_column(Integer, default=0)
    buckets_inserted: Mapped[int] = mapped_column(Integer, default=0)
    buckets_updated: Mapped[int] = mapped_column(Integer, default=0)
    failed_windows: Mapped[dict | None] = mapped_column(RawJSON)     # ISO starts of failed commits
    oversized_windows: Mapped[dict | None] = mapped_column(RawJSON)  # skipped-oversized (loud)
    truncated: Mapped[bool] = mapped_column(default=False)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OpportunitySignal(Base):
    """Informational-only opportunity signal detected by the watcher.
    Signals record what moved and why, for later human/research review —
    they carry no EV, no sizing, and no trade directives."""

    __tablename__ = "opportunity_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    signal_type: Mapped[str] = mapped_column(String(48), index=True)
    # new|reviewed|dismissed|promoted_to_research|research_refreshed|
    # forecast_refreshed|paper_candidate_pending (a review label only —
    # no paper trading exists)
    signal_status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    old_midpoint: Mapped[float | None] = mapped_column(Float)
    new_midpoint: Mapped[float | None] = mapped_column(Float)
    price_change: Mapped[float | None] = mapped_column(Float)
    spread: Mapped[int | None] = mapped_column(Integer)
    liquidity_proxy: Mapped[int | None] = mapped_column(Integer)
    latest_forecast_id: Mapped[int | None] = mapped_column(ForeignKey("market_forecasts.id"))
    latest_forecast_probability: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict | None] = mapped_column(RawJSON)
    raw_payload: Mapped[dict | None] = mapped_column(RawJSON)
    # Signal workflow (OPS-004): promotion + refresh audit trail
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refreshed_research_packet_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_research_packets.id")
    )
    refreshed_forecast_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_forecasts.id")
    )
    processing_error_type: Mapped[str | None] = mapped_column(String(128))
    processing_error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WatcherRun(Base):
    """One polling pass of the real-time watcher."""

    __tablename__ = "watcher_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    markets_checked: Mapped[int] = mapped_column(Integer, default=0)
    ticks_recorded: Mapped[int] = mapped_column(Integer, default=0)
    signals_created: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PipelineRun(Base):
    """One execution of the baseline measurement pipeline. The read-only
    loop's audit spine: config in, per-stage children, final status/summary.

    A status='running' row doubles as the overlap lock."""

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_type: Mapped[str] = mapped_column(String(32), default="baseline", index=True)
    # running|completed|completed_with_errors|failed|skipped|dry_run
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    config: Mapped[dict | None] = mapped_column(RawJSON)
    summary: Mapped[dict | None] = mapped_column(RawJSON)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    stages: Mapped[list["PipelineStageRun"]] = relationship(
        back_populates="pipeline_run", order_by="PipelineStageRun.id"
    )


class PipelineStageRun(Base):
    """One stage of one pipeline run: timing, item counts, and error capture."""

    __tablename__ = "pipeline_stage_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline_run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_runs.id"), index=True)
    stage_name: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))  # running|completed|failed|skipped
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    items_attempted: Mapped[int] = mapped_column(Integer, default=0)
    items_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict | None] = mapped_column(RawJSON)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    pipeline_run: Mapped[PipelineRun] = relationship(back_populates="stages")


class ScannerRun(Base):
    """One execution of the market scanner: fetch -> rank -> persist."""

    __tablename__ = "scanner_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error
    source: Mapped[str] = mapped_column(String(16), default="api")  # api|cli
    markets_fetched: Mapped[int] = mapped_column(Integer, default=0)
    markets_ranked: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)

    snapshots: Mapped[list[MarketSnapshot]] = relationship(back_populates="scanner_run")
    eligibility_assessments: Mapped[list[MarketEligibilityAssessment]] = relationship(
        back_populates="scanner_run"
    )
    resolution_assessments: Mapped[list[MarketResolutionAssessment]] = relationship(
        back_populates="scanner_run"
    )
    detail_enrichments: Mapped[list[MarketDetailEnrichment]] = relationship(
        back_populates="scanner_run"
    )
    research_packets: Mapped[list[MarketResearchPacket]] = relationship(
        back_populates="scanner_run"
    )
    forecasts: Mapped[list[MarketForecastRecord]] = relationship(back_populates="scanner_run")


# --- Crypto Arena (CRYPTO-001) — read-only Solana memecoin surveillance ---
# These tables observe and audit public DEX data only. No wallet, key, swap,
# transaction, order, or execution fields exist anywhere in this lane.


class CryptoToken(Base):
    """One observed token on a chain (upserted by discovery)."""

    __tablename__ = "crypto_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(256))
    decimals: Mapped[int | None] = mapped_column(Integer)
    token_metadata: Mapped[dict | None] = mapped_column("metadata", RawJSON)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_crypto_tokens_chain_address", "chain", "token_address", unique=True),
    )


class CryptoPair(Base):
    """One observed DEX pair/pool for a token (upserted by discovery)."""

    __tablename__ = "crypto_pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    pair_address: Mapped[str] = mapped_column(String(128), index=True)
    base_token_address: Mapped[str] = mapped_column(String(128), index=True)
    quote_token_address: Mapped[str | None] = mapped_column(String(128))
    dex_id: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(String(512))
    pair_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pair_metadata: Mapped[dict | None] = mapped_column("metadata", RawJSON)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_crypto_pairs_chain_address", "chain", "pair_address", unique=True),
    )


class CryptoTokenDiscoveryEvent(Base):
    """Audit record of HOW a token surfaced (profile, boost, pair search)."""

    __tablename__ = "crypto_token_discovery_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    pair_address: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(64))  # e.g. dexscreener
    event_type: Mapped[str] = mapped_column(String(48), index=True)  # profile|boost|pair_seen
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_payload: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CryptoTokenRiskAssessment(Base):
    """One risk read for a token: a raw provider read (CRYPTO-001 mock) or a
    CRYPTO-002 risk-engine evaluation with normalized sub-scores. Risk
    intelligence only — a score is an avoid/flag verdict for review, never a
    trade recommendation."""

    __tablename__ = "crypto_token_risk_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    risk_score: Mapped[float | None] = mapped_column(Float)
    risk_level: Mapped[str | None] = mapped_column(String(16))  # low|medium|high|severe|unknown
    flags: Mapped[dict | None] = mapped_column(RawJSON)
    raw_payload: Mapped[dict | None] = mapped_column(RawJSON)
    # CRYPTO-002 normalized engine fields (nullable: CRYPTO-001 rows lack them)
    liquidity_risk_score: Mapped[float | None] = mapped_column(Float)
    holder_risk_score: Mapped[float | None] = mapped_column(Float)
    authority_risk_score: Mapped[float | None] = mapped_column(Float)
    market_structure_risk_score: Mapped[float | None] = mapped_column(Float)
    manipulation_risk_score: Mapped[float | None] = mapped_column(Float)
    provider_risk_score: Mapped[float | None] = mapped_column(Float)
    composite_risk_score: Mapped[float | None] = mapped_column(Float)
    composite_risk_level: Mapped[str | None] = mapped_column(String(16))
    risk_reasons: Mapped[list | None] = mapped_column(RawJSON)
    provider_names: Mapped[list | None] = mapped_column(RawJSON)
    heuristic_version: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CryptoPriceTick(Base):
    """One observed price/liquidity/volume snapshot for a token pair."""

    __tablename__ = "crypto_price_ticks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    pair_address: Mapped[str | None] = mapped_column(String(128), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    price_usd: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    volume_5m_usd: Mapped[float | None] = mapped_column(Float)
    volume_1h_usd: Mapped[float | None] = mapped_column(Float)
    volume_24h_usd: Mapped[float | None] = mapped_column(Float)
    price_change_5m: Mapped[float | None] = mapped_column(Float)  # percent
    price_change_1h: Mapped[float | None] = mapped_column(Float)  # percent
    market_cap: Mapped[float | None] = mapped_column(Float)
    fdv: Mapped[float | None] = mapped_column(Float)
    raw_payload: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_crypto_price_ticks_pair_observed", "pair_address", "observed_at"),
    )


class CryptoOpportunitySignal(Base):
    """Informational-only crypto signal (surveillance/risk telemetry). Like
    opportunity_signals, this is a review record — no EV, no sizing, no trade
    directives, no execution semantics."""

    __tablename__ = "crypto_opportunity_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    pair_address: Mapped[str | None] = mapped_column(String(128))
    signal_type: Mapped[str] = mapped_column(String(48), index=True)
    signal_status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict | None] = mapped_column(RawJSON)
    raw_payload: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CryptoWatcherRun(Base):
    """One crypto discovery/scan pass (audit spine for the crypto lane)."""

    __tablename__ = "crypto_watcher_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    tokens_checked: Mapped[int] = mapped_column(Integer, default=0)
    pairs_checked: Mapped[int] = mapped_column(Integer, default=0)
    ticks_recorded: Mapped[int] = mapped_column(Integer, default=0)
    signals_created: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- MarketOps Autopilot (OPS-006) — read-only coordination audit ---
# One run row per autopilot cycle; local DB alerts only. No EV, trade,
# order, wallet, or execution fields exist.


class MarketOpsRun(Base):
    """One MarketOps Autopilot cycle: which stages ran, what they touched,
    and what went wrong. Coordination audit only."""

    __tablename__ = "marketops_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|partial|error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    config: Mapped[dict | None] = mapped_column(RawJSON)
    summary: Mapped[dict | None] = mapped_column(RawJSON)
    signals_seen: Mapped[int] = mapped_column(Integer, default=0)
    signals_promoted: Mapped[int] = mapped_column(Integer, default=0)
    signals_processed: Mapped[int] = mapped_column(Integer, default=0)
    crypto_tokens_seen: Mapped[int] = mapped_column(Integer, default=0)
    crypto_signals_created: Mapped[int] = mapped_column(Integer, default=0)
    outcomes_synced: Mapped[int] = mapped_column(Integer, default=0)
    forecasts_scored: Mapped[int] = mapped_column(Integer, default=0)
    alerts_created: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MarketOpsAlert(Base):
    """Local DB alert raised by the autopilot (no external delivery in
    OPS-006). Informational operator telemetry only."""

    __tablename__ = "marketops_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")  # info|warning|critical
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open|resolved
    title: Mapped[str] = mapped_column(String(256))
    message: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EdgePrecheckSnapshot(Base):
    """One probability-gap MEASUREMENT (MVP-005A): forecast probability vs
    market midpoint with validity checks. Append-only audit rows.

    Hard boundary: no dollar EV, no side, no direction, no size, no order or
    execution semantics exist here — by design there is no column where they
    could live. 'paper_candidate_later' is a review label for a possible
    future, separately-gated MVP-005B; it triggers no behavior."""

    __tablename__ = "edge_precheck_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity_signals.id"))
    forecast_id: Mapped[int] = mapped_column(ForeignKey("market_forecasts.id"), index=True)
    market_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_price_ticks.id")  # watcher quote used as the price source
    )
    resolution_assessment_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_resolution_assessments.id")
    )
    forecaster_name: Mapped[str] = mapped_column(String(64), index=True)
    evidence_depth: Mapped[str] = mapped_column(String(16))
    forecast_probability: Mapped[float] = mapped_column(Float)
    forecast_confidence: Mapped[float] = mapped_column(Float)
    forecast_risk: Mapped[str | None] = mapped_column(String(16))
    market_midpoint: Mapped[float | None] = mapped_column(Float)
    yes_bid: Mapped[int | None] = mapped_column(Integer)
    yes_ask: Mapped[int | None] = mapped_column(Integer)
    spread_cents: Mapped[int | None] = mapped_column(Integer)
    liquidity_proxy_cents: Mapped[int | None] = mapped_column(Integer)
    probability_gap: Mapped[float | None] = mapped_column(Float)  # signed
    abs_probability_gap: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(40), index=True)
    invalidation_reasons: Mapped[list | None] = mapped_column(RawJSON)
    forecast_age_seconds: Mapped[int | None] = mapped_column(Integer)
    market_snapshot_age_seconds: Mapped[int | None] = mapped_column(Integer)
    persistence_count: Mapped[int] = mapped_column(Integer, default=1)
    thresholds: Mapped[dict | None] = mapped_column(RawJSON)
    tags: Mapped[list | None] = mapped_column(RawJSON)
    raw_context: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FrontierEvalRun(Base):
    """One persisted frontier-evaluation run (EVAL-001). Evaluation audit
    only: summarizes measurement quality over a time window. No EV, trade,
    order, or execution semantics exist in this system."""

    __tablename__ = "frontier_eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict | None] = mapped_column(RawJSON)
    warnings: Mapped[list | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- MEME-NEWS-001: read-only meme/news scout + domain expansion -------------
# Discovery, attention scoring, catalyst abstraction, and market-domain
# inventory. All read-only intelligence: no EV, no trade advice, no sizing, no
# orders, no wallets/keys/swaps/signing/execution. An attention_score is an
# interest/velocity signal for human review — never a buy/trade/EV signal.


class MemeScoutRun(Base):
    """One meme-scout pass (audit spine for the attention/catalyst lane)."""

    __tablename__ = "meme_scout_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    profiles_seen: Mapped[int] = mapped_column(Integer, default=0)
    boosts_seen: Mapped[int] = mapped_column(Integer, default=0)
    tokens_scored: Mapped[int] = mapped_column(Integer, default=0)
    catalysts_created: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemeAttentionSnapshot(Base):
    """A read-only attention/velocity snapshot for one token. attention_score
    is an interest signal for human review — NOT a buy/trade/EV/alpha score,
    carries no action and no position."""

    __tablename__ = "meme_attention_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("meme_scout_runs.id"), index=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    pair_address: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(256))
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_age_seconds: Mapped[int | None] = mapped_column(Integer)
    # trajectories (current + growth vs previous tick)
    price_usd: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    volume_5m_usd: Mapped[float | None] = mapped_column(Float)
    volume_1h_usd: Mapped[float | None] = mapped_column(Float)
    volume_24h_usd: Mapped[float | None] = mapped_column(Float)
    price_change_5m: Mapped[float | None] = mapped_column(Float)
    price_change_1h: Mapped[float | None] = mapped_column(Float)
    liquidity_growth: Mapped[float | None] = mapped_column(Float)  # fraction vs previous
    volume_growth: Mapped[float | None] = mapped_column(Float)
    boost_amount: Mapped[float | None] = mapped_column(Float)
    boost_velocity: Mapped[float | None] = mapped_column(Float)  # boost delta / hour
    # metadata / catalyst presence
    profile_completeness: Mapped[float | None] = mapped_column(Float)  # 0..1
    has_social: Mapped[bool] = mapped_column(default=False)
    social_links_count: Mapped[int] = mapped_column(Integer, default=0)
    # risk overlay (read from existing risk assessments)
    risk_level: Mapped[str | None] = mapped_column(String(16))
    risk_score: Mapped[float | None] = mapped_column(Float)
    provider_confidence: Mapped[float | None] = mapped_column(Float)  # 0..1
    # the score (read-only interest signal; never advice)
    attention_score: Mapped[float | None] = mapped_column(Float)  # 0..1
    score_components: Mapped[dict | None] = mapped_column(RawJSON)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_meme_attention_token_observed", "token_address", "observed_at"),
    )


class MemeCatalystEvent(Base):
    """Generic catalyst-event abstraction. Today only read-only public sources
    already in scope (dexscreener profiles/boosts/paid-boost metadata) populate
    it; the schema is source-agnostic so RSS/X/Discord/Telegram can be added
    later ONLY if explicitly configured. A catalyst is an informational event,
    never a trade trigger."""

    __tablename__ = "meme_catalyst_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("meme_scout_runs.id"), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # dexscreener|rss|x|discord|telegram
    subject_type: Mapped[str] = mapped_column(String(24), index=True)  # token|pair|news
    subject_ref: Mapped[str] = mapped_column(String(256), index=True)  # token_address|url
    catalyst_type: Mapped[str] = mapped_column(String(48), index=True)  # profile_seen|boost|boost_increase|paid_order|social_present
    magnitude: Mapped[float | None] = mapped_column(Float)  # e.g. boost amount/delta
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    detail: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DomainScoutRun(Base):
    """One market-domain inventory pass (audit spine for domain expansion)."""

    __tablename__ = "domain_scout_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    markets_scanned: Mapped[int] = mapped_column(Integer, default=0)
    domains_seen: Mapped[int] = mapped_column(Integer, default=0)
    series_seen: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DomainMarketInventorySnapshot(Base):
    """Read-only inventory of one domain/series cluster of probability markets.
    Coverage + candidate-priority intelligence for future canary planning —
    it adds NO forecaster, changes NO promotion logic, and is never advice."""

    __tablename__ = "domain_market_inventory_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("domain_scout_runs.id"), index=True)
    domain: Mapped[str] = mapped_column(String(48), index=True)
    series_prefix: Mapped[str | None] = mapped_column(String(48), index=True)
    market_count: Mapped[int] = mapped_column(Integer, default=0)
    active_count: Mapped[int] = mapped_column(Integer, default=0)
    two_sided_count: Mapped[int] = mapped_column(Integer, default=0)
    two_sided_rate: Mapped[float | None] = mapped_column(Float)
    volume_proxy_cents: Mapped[int | None] = mapped_column(Integer)
    liquidity_proxy_cents: Mapped[int | None] = mapped_column(Integer)
    resolution_clarity_proxy: Mapped[float | None] = mapped_column(Float)  # 0..1
    has_evidence_forecaster: Mapped[bool] = mapped_column(default=False)
    data_source_notes: Mapped[str | None] = mapped_column(String(256))
    canary_priority: Mapped[float | None] = mapped_column(Float)  # 0..1
    priority_components: Mapped[dict | None] = mapped_column(RawJSON)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- POLY-001: read-only Polymarket market-data observer (second venue) ------
# Read-only market-DATA telemetry only. Prices are informational quotes, order
# books are microstructure snapshots — NOT EV, advice, sizes, or trade
# triggers. No column here holds an order, position, wallet, key, or execution.


class PolymarketScoutRun(Base):
    """One Polymarket observer pass (audit spine)."""

    __tablename__ = "polymarket_scout_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    markets_seen: Mapped[int] = mapped_column(Integer, default=0)
    markets_persisted: Mapped[int] = mapped_column(Integer, default=0)
    orderbooks_fetched: Mapped[int] = mapped_column(Integer, default=0)
    orderbook_errors: Mapped[int] = mapped_column(Integer, default=0)
    domains_seen: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str | None] = mapped_column(String(32))
    provider_version: Mapped[str | None] = mapped_column(String(16))
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    # POLY-COVERAGE-001 scan provenance: HOW this read-only sample was obtained.
    # Coverage counters for the audit spine — never EV/advice/trading state.
    scan_mode: Mapped[str | None] = mapped_column(String(32))  # catalog|targeted|catalog+targeted
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    market_fetch_errors: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_dropped: Mapped[int] = mapped_column(Integer, default=0)
    queries_used: Mapped[dict | None] = mapped_column(RawJSON)  # e.g. ["world cup","mlb"]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PolymarketMarket(Base):
    """One read-only Polymarket market-catalog snapshot. Metadata + price/
    liquidity/volume proxies for human review — never EV, advice, or a trade
    trigger."""

    __tablename__ = "polymarket_markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("polymarket_scout_runs.id"), index=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    condition_id: Mapped[str | None] = mapped_column(String(128))
    question: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(String(256))
    category: Mapped[str | None] = mapped_column(String(64), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    # status flags
    active: Mapped[bool] = mapped_column(default=False)
    closed: Mapped[bool] = mapped_column(default=False)
    archived: Mapped[bool] = mapped_column(default=False)
    restricted: Mapped[bool] = mapped_column(default=False)
    enable_order_book: Mapped[bool] = mapped_column(default=False)
    accepting_orders: Mapped[bool] = mapped_column(default=False)
    # outcomes / tokens
    outcomes: Mapped[dict | None] = mapped_column(RawJSON)  # e.g. ["Yes","No"]
    outcome_prices: Mapped[dict | None] = mapped_column(RawJSON)
    clob_token_ids: Mapped[dict | None] = mapped_column(RawJSON)
    num_outcomes: Mapped[int] = mapped_column(Integer, default=0)
    # microstructure proxies (informational quotes only)
    best_bid: Mapped[float | None] = mapped_column(Float)
    best_ask: Mapped[float | None] = mapped_column(Float)
    last_trade_price: Mapped[float | None] = mapped_column(Float)
    spread: Mapped[float | None] = mapped_column(Float)
    two_sided: Mapped[bool] = mapped_column(default=False)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    volume_24h_usd: Mapped[float | None] = mapped_column(Float)
    volume_total_usd: Mapped[float | None] = mapped_column(Float)
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_polymarket_market_observed", "market_id", "observed_at"),
    )


class PolymarketOrderbookSnapshot(Base):
    """One read-only CLOB order-book snapshot for a token id, reduced to
    spread/depth/liquidity proxies. Reading the book only — no order can be
    placed, sized, or signed from this row."""

    __tablename__ = "polymarket_orderbook_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("polymarket_scout_runs.id"), index=True)
    market_id: Mapped[str | None] = mapped_column(String(128), index=True)
    token_id: Mapped[str] = mapped_column(String(128), index=True)
    outcome: Mapped[str | None] = mapped_column(String(64))
    best_bid: Mapped[float | None] = mapped_column(Float)
    best_ask: Mapped[float | None] = mapped_column(Float)
    mid: Mapped[float | None] = mapped_column(Float)
    spread: Mapped[float | None] = mapped_column(Float)
    bid_depth: Mapped[float | None] = mapped_column(Float)
    ask_depth: Mapped[float | None] = mapped_column(Float)
    total_depth: Mapped[float | None] = mapped_column(Float)
    num_bids: Mapped[int] = mapped_column(Integer, default=0)
    num_asks: Mapped[int] = mapped_column(Integer, default=0)
    liquidity_proxy: Mapped[float | None] = mapped_column(Float)
    tick_size: Mapped[float | None] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_polymarket_book_token_observed", "token_id", "observed_at"),
    )


class PolymarketDomainInventorySnapshot(Base):
    """Read-only inventory of one Polymarket category/domain cluster: market
    counts, two-sided/orderbook availability, and liquidity/volume/spread
    proxies. Coverage intelligence for human review — adds no forecaster,
    changes no logic, and is never advice."""

    __tablename__ = "polymarket_domain_inventory_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("polymarket_scout_runs.id"), index=True)
    domain: Mapped[str] = mapped_column(String(64), index=True)
    market_count: Mapped[int] = mapped_column(Integer, default=0)
    active_count: Mapped[int] = mapped_column(Integer, default=0)
    two_sided_count: Mapped[int] = mapped_column(Integer, default=0)
    orderbook_enabled_count: Mapped[int] = mapped_column(Integer, default=0)
    two_sided_rate: Mapped[float | None] = mapped_column(Float)
    total_liquidity_usd: Mapped[float | None] = mapped_column(Float)
    total_volume_24h_usd: Mapped[float | None] = mapped_column(Float)
    avg_spread: Mapped[float | None] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- POLY-002: read-only Kalshi <-> Polymarket cross-venue observation --------
# OBSERVATION only: identify comparable markets and measure observable
# differences (midpoints/spreads/liquidity). No side, size, EV, dollar, profit,
# action, recommendation, arbitrage/arb label, order, wallet, or execution field
# exists here by construction — a difference is a measurement, never a signal.


class CrossVenueObservationRun(Base):
    """One cross-venue matching/observation pass (audit spine)."""

    __tablename__ = "cross_venue_observation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    kalshi_markets_considered: Mapped[int] = mapped_column(Integer, default=0)
    polymarket_markets_considered: Mapped[int] = mapped_column(Integer, default=0)
    candidates_created: Mapped[int] = mapped_column(Integer, default=0)
    comparable_count: Mapped[int] = mapped_column(Integer, default=0)
    unresolved_count: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CrossVenueMarketCandidate(Base):
    """One observed Kalshi<->Polymarket candidate pairing. `match_label` is a
    semantic-comparability verdict for human review; `observed_difference` is a
    measured midpoint gap. NEITHER is a trade signal, an arbitrage claim, or an
    action — no side/size/EV/dollar/order/wallet field exists."""

    __tablename__ = "cross_venue_market_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("cross_venue_observation_runs.id"), index=True)
    kalshi_ticker: Mapped[str | None] = mapped_column(String(64), index=True)
    kalshi_event_ticker: Mapped[str | None] = mapped_column(String(64))
    polymarket_market_id: Mapped[str | None] = mapped_column(String(128), index=True)
    polymarket_token_id: Mapped[str | None] = mapped_column(String(128))
    polymarket_condition_id: Mapped[str | None] = mapped_column(String(128))
    domain: Mapped[str | None] = mapped_column(String(64), index=True)
    event_title_normalized: Mapped[str | None] = mapped_column(Text)
    outcome_normalized: Mapped[str | None] = mapped_column(String(64))
    resolution_time_kalshi: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_time_polymarket: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    match_confidence: Mapped[float | None] = mapped_column(Float)  # 0..1
    match_label: Mapped[str] = mapped_column(String(32), index=True)
    match_reasons: Mapped[dict | None] = mapped_column(RawJSON)
    mismatch_reasons: Mapped[dict | None] = mapped_column(RawJSON)
    # measurement-only microstructure (probability scale 0..1; never dollars)
    kalshi_midpoint: Mapped[float | None] = mapped_column(Float)
    polymarket_midpoint: Mapped[float | None] = mapped_column(Float)
    midpoint_difference: Mapped[float | None] = mapped_column(Float)
    kalshi_spread: Mapped[float | None] = mapped_column(Float)
    polymarket_spread: Mapped[float | None] = mapped_column(Float)
    kalshi_liquidity_proxy: Mapped[float | None] = mapped_column(Float)
    polymarket_liquidity_proxy: Mapped[float | None] = mapped_column(Float)
    observed_difference: Mapped[float | None] = mapped_column(Float)  # headline measured gap
    observation_confidence: Mapped[float | None] = mapped_column(Float)  # data freshness/completeness
    raw_context: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_cross_venue_run_label", "run_id", "match_label"),
    )


class TennisTapeRun(Base):
    """TENNIS-TAPE-001: one manual bounded tape capture pass. Counters and
    provenance only — measurement infrastructure, never a trading surface."""

    __tablename__ = "tennis_tape_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    provider_source: Mapped[str | None] = mapped_column(String(64))
    score_calls_made: Mapped[int] = mapped_column(Integer, default=0)
    market_fetches_made: Mapped[int] = mapped_column(Integer, default=0)
    candidates_considered: Mapped[int] = mapped_column(Integer, default=0)
    score_snapshots: Mapped[int] = mapped_column(Integer, default=0)
    market_snapshots: Mapped[int] = mapped_column(Integer, default=0)
    links_created: Mapped[int] = mapped_column(Integer, default=0)
    source_backed_links: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TennisTapeScoreSnapshot(Base):
    """One provider score/state observation on a tape. Score facts only."""

    __tablename__ = "tennis_tape_score_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tape_run_id: Mapped[int] = mapped_column(ForeignKey("tennis_tape_runs.id"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    provider_source: Mapped[str] = mapped_column(String(64))
    provider_event_id: Mapped[str | None] = mapped_column(String(64), index=True)
    event_date: Mapped[str | None] = mapped_column(String(10))
    event_type: Mapped[str | None] = mapped_column(String(64))
    tournament_name: Mapped[str | None] = mapped_column(String(128))
    player_a: Mapped[str | None] = mapped_column(String(128))
    player_b: Mapped[str | None] = mapped_column(String(128))
    match_status: Mapped[str | None] = mapped_column(String(64))
    match_state: Mapped[str | None] = mapped_column(String(16))
    final_result: Mapped[str | None] = mapped_column(String(32))
    game_result: Mapped[str | None] = mapped_column(String(32))
    serving: Mapped[str | None] = mapped_column(String(16))
    set_scores: Mapped[list | None] = mapped_column(RawJSON)
    missing_info: Mapped[list | None] = mapped_column(RawJSON)
    raw_payload: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TennisTapeMarketSnapshot(Base):
    """One Kalshi tennis market quote observation on a tape. Quotes only."""

    __tablename__ = "tennis_tape_market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tape_run_id: Mapped[int] = mapped_column(ForeignKey("tennis_tape_runs.id"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    market_title: Mapped[str | None] = mapped_column(Text)
    market_status: Mapped[str | None] = mapped_column(String(32))
    yes_bid: Mapped[int | None] = mapped_column(Integer)
    yes_ask: Mapped[int | None] = mapped_column(Integer)
    midpoint: Mapped[float | None] = mapped_column(Float)
    spread: Mapped[int | None] = mapped_column(Integer)
    liquidity_proxy: Mapped[int | None] = mapped_column(Integer)
    volume_24h: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TennisTapeLink(Base):
    """One market↔score alignment on a tape, with an honest confidence label
    (source_backed_link / fuzzy_candidate / unresolved / provider_no_match /
    incompatible_market_type). Alignment metadata only."""

    __tablename__ = "tennis_tape_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tape_run_id: Mapped[int] = mapped_column(ForeignKey("tennis_tape_runs.id"), index=True)
    score_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("tennis_tape_score_snapshots.id")
    )
    market_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("tennis_tape_market_snapshots.id")
    )
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(64))
    link_label: Mapped[str] = mapped_column(String(32), index=True)
    link_basis: Mapped[str | None] = mapped_column(String(128))
    player_a_code: Mapped[str | None] = mapped_column(String(8))
    player_b_code: Mapped[str | None] = mapped_column(String(8))
    event_date: Mapped[str | None] = mapped_column(String(10))
    score_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    market_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    score_to_market_delta_s: Mapped[float | None] = mapped_column(Float)
    missing_info: Mapped[list | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- CRYPTO-TAPE-001: read-only Solana memecoin lifecycle tape ---------------
# Replayable token lifecycle recording DERIVED from rows the existing lanes
# already persist (crypto ticks/pairs/discovery events/risk assessments +
# meme attention/catalysts). Research infrastructure only: observation and
# survival-label columns exist; no EV, side, size, recommendation, order,
# key, swap, signing, or execution column exists by construction.


class CryptoTokenLifecycleRun(Base):
    """One lifecycle-tape assembly pass (audit spine). Dry-run persists
    nothing, including this row."""

    __tablename__ = "crypto_token_lifecycle_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    window_hours: Mapped[int | None] = mapped_column(Integer)
    tokens_considered: Mapped[int] = mapped_column(Integer, default=0)
    birth_events_created: Mapped[int] = mapped_column(Integer, default=0)
    snapshots_created: Mapped[int] = mapped_column(Integer, default=0)
    actor_observations_created: Mapped[int] = mapped_column(Integer, default=0)
    outcomes_updated: Mapped[int] = mapped_column(Integer, default=0)
    provider_coverage: Mapped[dict | None] = mapped_column(RawJSON)
    config: Mapped[dict | None] = mapped_column(RawJSON)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CryptoTokenBirthEvent(Base):
    """First-evidence record for one token: how and when it surfaced, its
    launch/authority/social context, and raw-payload provenance. One row per
    (chain, token); fields the sources never provided stay NULL and are named
    in missing_info — nothing is fabricated."""

    __tablename__ = "crypto_token_birth_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crypto_token_lifecycle_runs.id"), index=True
    )
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(256))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    first_evidence_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    launch_source: Mapped[str | None] = mapped_column(String(64))  # e.g. dexscreener:profile
    first_pair_address: Mapped[str | None] = mapped_column(String(128))
    first_dex_id: Mapped[str | None] = mapped_column(String(64))
    pair_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    creator_address: Mapped[str | None] = mapped_column(String(128))  # public-chain address only
    mint_authority_enabled: Mapped[bool | None] = mapped_column()
    freeze_authority_enabled: Mapped[bool | None] = mapped_column()
    metadata_links: Mapped[dict | None] = mapped_column(RawJSON)  # description/urls/socials
    initial_price_usd: Mapped[float | None] = mapped_column(Float)
    initial_liquidity_usd: Mapped[float | None] = mapped_column(Float)
    initial_volume_24h_usd: Mapped[float | None] = mapped_column(Float)
    initial_market_cap: Mapped[float | None] = mapped_column(Float)
    initial_fdv: Mapped[float | None] = mapped_column(Float)
    bonding_curve_state: Mapped[str | None] = mapped_column(String(32))
    provenance: Mapped[dict | None] = mapped_column(RawJSON)  # source row ids per field
    missing_info: Mapped[list | None] = mapped_column(RawJSON)
    raw_payload: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_crypto_birth_chain_token", "chain", "token_address", unique=True),
    )


class CryptoTokenLifecycleSnapshot(Base):
    """One consolidated lifecycle observation per token per tape run: market
    state + holder structure + risk labels + social/catalyst context + source
    coverage, all read from already-persisted rows."""

    __tablename__ = "crypto_token_lifecycle_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crypto_token_lifecycle_runs.id"), index=True
    )
    birth_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("crypto_token_birth_events.id"), index=True
    )
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    token_age_seconds: Mapped[int | None] = mapped_column(Integer)
    # market state (from the best pair's latest persisted tick)
    price_usd: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    volume_5m_usd: Mapped[float | None] = mapped_column(Float)
    volume_1h_usd: Mapped[float | None] = mapped_column(Float)
    volume_24h_usd: Mapped[float | None] = mapped_column(Float)
    market_cap: Mapped[float | None] = mapped_column(Float)
    fdv: Mapped[float | None] = mapped_column(Float)
    # holder / concentration structure (from persisted risk assessments)
    holder_count: Mapped[int | None] = mapped_column(Integer)
    top10_holder_pct: Mapped[float | None] = mapped_column(Float)
    sniper_pct: Mapped[float | None] = mapped_column(Float)
    insider_pct: Mapped[float | None] = mapped_column(Float)
    bundler_pct: Mapped[float | None] = mapped_column(Float)
    creator_pct: Mapped[float | None] = mapped_column(Float)
    # risk labels (avoid/flag verdicts for review — never trade directions)
    risk_score: Mapped[float | None] = mapped_column(Float)
    risk_level: Mapped[str | None] = mapped_column(String(16))
    risk_reasons: Mapped[list | None] = mapped_column(RawJSON)
    # social / boost / catalyst context
    boost_amount: Mapped[float | None] = mapped_column(Float)
    attention_score: Mapped[float | None] = mapped_column(Float)
    has_social: Mapped[bool | None] = mapped_column()
    social_links_count: Mapped[int | None] = mapped_column(Integer)
    catalyst_count_24h: Mapped[int | None] = mapped_column(Integer)
    # quote / liquidity quality
    pair_count: Mapped[int | None] = mapped_column(Integer)
    best_pair_address: Mapped[str | None] = mapped_column(String(128))
    best_dex_id: Mapped[str | None] = mapped_column(String(64))
    volume_to_liquidity_24h: Mapped[float | None] = mapped_column(Float)
    single_venue: Mapped[bool | None] = mapped_column()
    # honesty about the derived sources
    source_tick_id: Mapped[int | None] = mapped_column(ForeignKey("crypto_price_ticks.id"))
    source_risk_assessment_id: Mapped[int | None] = mapped_column(
        ForeignKey("crypto_token_risk_assessments.id")
    )
    source_attention_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("meme_attention_snapshots.id")
    )
    source_tick_age_seconds: Mapped[int | None] = mapped_column(Integer)
    provider_coverage: Mapped[list | None] = mapped_column(RawJSON)
    missing_info: Mapped[list | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_crypto_lifecycle_snap_token_observed", "token_address", "observed_at"),
    )


class CryptoTokenActorObservation(Base):
    """Actor-structure observation for one token per tape run: creator /
    early-buyer / concentration-cohort structure as far as public-chain
    addresses already persisted by providers reveal it. Public addresses
    only; no deanonymization, no key material, ever."""

    __tablename__ = "crypto_token_actor_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crypto_token_lifecycle_runs.id"), index=True
    )
    birth_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("crypto_token_birth_events.id"), index=True
    )
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    creator_address: Mapped[str | None] = mapped_column(String(128))
    creator_holding_pct: Mapped[float | None] = mapped_column(Float)
    first_buyer_addresses: Mapped[list | None] = mapped_column(RawJSON)  # rarely available
    sniper_address_count: Mapped[int | None] = mapped_column(Integer)
    insider_address_count: Mapped[int | None] = mapped_column(Integer)
    bundler_address_count: Mapped[int | None] = mapped_column(Integer)
    # placeholders for later cross-token cohort analysis (no behavior today)
    repeated_cohort_ref: Mapped[str | None] = mapped_column(String(64))
    known_creator_cluster_ref: Mapped[str | None] = mapped_column(String(64))
    holder_distribution: Mapped[dict | None] = mapped_column(RawJSON)
    observation_sources: Mapped[list | None] = mapped_column(RawJSON)
    missing_info: Mapped[list | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CryptoTokenSurvivalOutcome(Base):
    """Deterministic survival labels for one birth event, recomputed per tape
    run until final. Labels are measured token behavior (liquidity/volume/risk
    trajectory) — never PnL, EV, a return, or a recommendation. NULL means
    'not yet measurable or source gap', never a guess."""

    __tablename__ = "crypto_token_survival_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    birth_event_id: Mapped[int] = mapped_column(
        ForeignKey("crypto_token_birth_events.id"), index=True, unique=True
    )
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    survived_15m: Mapped[bool | None] = mapped_column()
    survived_1h: Mapped[bool | None] = mapped_column()
    survived_6h: Mapped[bool | None] = mapped_column()
    survived_24h: Mapped[bool | None] = mapped_column()
    liquidity_removed: Mapped[bool | None] = mapped_column()
    dead_volume: Mapped[bool | None] = mapped_column()
    severe_risk: Mapped[bool | None] = mapped_column()
    graduated_or_migrated: Mapped[bool | None] = mapped_column()
    provider_gap: Mapped[bool | None] = mapped_column()
    final: Mapped[bool] = mapped_column(default=False, index=True)
    details: Mapped[dict | None] = mapped_column(RawJSON)  # per-horizon evidence
    last_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crypto_token_lifecycle_runs.id")
    )
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- CRYPTO-HORIZON-OBS-001: bounded read-only horizon-observation lane -------
# CRYPTO-COVERAGE-001 proved the 6h/24h maturation ceiling is UPSTREAM tick
# coverage (the background scout does not tick aged tokens near their long
# horizons). This lane fixes a small, STABLE research cohort and — on manual
# invocation only — fetches market/liquidity state via the existing read-only
# DexScreener adapter near each 15m/1h/6h/24h mark, persisting ordinary price
# ticks (so survival horizons can mature) plus an audit observation row.
# Manual only: no timer, no scheduled path, no autonomy. Observation/market
# data only — no EV, side, size, order, wallet, key, swap, signing, or
# execution column exists by construction.


class CryptoHorizonCohort(Base):
    """One fixed research cohort selected from persisted discoveries. Members
    are frozen at creation; provenance records how it was chosen."""

    __tablename__ = "crypto_horizon_cohorts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    member_limit: Mapped[int] = mapped_column(Integer)
    window_hours: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[dict | None] = mapped_column(RawJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CryptoHorizonCohortMember(Base):
    """One frozen member of a horizon cohort (its birth anchor is fixed)."""

    __tablename__ = "crypto_horizon_cohort_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cohort_id: Mapped[int] = mapped_column(
        ForeignKey("crypto_horizon_cohorts.id"), index=True
    )
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str | None] = mapped_column(String(64))
    birth_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("crypto_token_birth_events.id")
    )
    birth_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_evidence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_horizon_member_cohort_token", "cohort_id", "token_address", unique=True),
    )


class CryptoHorizonObservation(Base):
    """One horizon-observation attempt for a (cohort, token, horizon). Records
    the captured market/liquidity state (or the honest miss cause), and links
    to the ordinary crypto_price_tick it persisted. Unique per
    (cohort, token, horizon) so a horizon is never double-observed. Market
    observation only — never a trade signal."""

    __tablename__ = "crypto_horizon_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cohort_id: Mapped[int] = mapped_column(
        ForeignKey("crypto_horizon_cohorts.id"), index=True
    )
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("crypto_horizon_cohort_members.id"), index=True
    )
    chain: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str] = mapped_column(String(128), index=True)
    horizon: Mapped[str] = mapped_column(String(8), index=True)  # 15m|1h|6h|24h
    target_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), index=True)  # observed|token_inactive|provider_no_pair|no_liquidity_state|request_failed
    missing_cause: Mapped[str | None] = mapped_column(String(32))
    tick_id: Mapped[int | None] = mapped_column(ForeignKey("crypto_price_ticks.id"))
    price_usd: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    volume_24h_usd: Mapped[float | None] = mapped_column(Float)
    market_cap: Mapped[float | None] = mapped_column(Float)
    fdv: Mapped[float | None] = mapped_column(Float)
    pair_address: Mapped[str | None] = mapped_column(String(128))
    dex_id: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(64))
    raw_payload: Mapped[dict | None] = mapped_column(RawJSON)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_horizon_obs_cohort_token_horizon", "cohort_id", "token_address",
              "horizon", unique=True),
    )
