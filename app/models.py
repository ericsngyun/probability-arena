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
