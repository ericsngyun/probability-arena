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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


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


class ScannerRun(Base):
    """One execution of the market scanner: fetch -> rank -> persist."""

    __tablename__ = "scanner_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error
    markets_fetched: Mapped[int] = mapped_column(Integer, default=0)
    markets_ranked: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    snapshots: Mapped[list[MarketSnapshot]] = relationship(back_populates="scanner_run")
