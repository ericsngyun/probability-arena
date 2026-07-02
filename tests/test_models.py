from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Market, MarketSnapshot, OrderbookSnapshot, ScannerRun
from app.schemas import MarketData
from app.services.ranking import rank_markets
from app.services.scanner import persist_scan
from tests.conftest import make_market


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_schema_creates_all_tables(session):
    tables = set(Base.metadata.tables)
    assert tables == {
        "markets",
        "market_snapshots",
        "orderbook_snapshots",
        "scanner_runs",
        "market_eligibility_assessments",
        "market_resolution_assessments",
    }


def test_market_snapshot_roundtrip(session):
    market = Market(ticker="TEST-1", title="Test", status="active")
    session.add(market)
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        yes_bid=48,
        yes_ask=52,
        volume_24h=100,
        score=0.7,
        score_components={"spread": 0.96, "liquidity": 0.4},
    )
    session.add(snapshot)
    session.commit()

    loaded = session.execute(select(MarketSnapshot)).scalar_one()
    assert loaded.market.ticker == "TEST-1"
    assert loaded.score_components["spread"] == 0.96


def test_orderbook_snapshot_stores_levels(session):
    market = Market(ticker="TEST-2", title="Test", status="active")
    session.add(market)
    session.flush()

    session.add(
        OrderbookSnapshot(
            market_id=market.id,
            source="ws",
            yes_levels=[[49, 100], [48, 250]],
            no_levels=[[50, 80]],
        )
    )
    session.commit()

    loaded = session.execute(select(OrderbookSnapshot)).scalar_one()
    assert loaded.yes_levels[0] == [49, 100]
    assert loaded.market.ticker == "TEST-2"


def test_persist_scan_writes_run_markets_and_snapshots(session):
    ranked = rank_markets([make_market(ticker="AAA"), make_market(ticker="BBB")])
    run = persist_scan(session, ranked)

    assert run.status == "ok"
    assert run.markets_ranked == 2
    assert run.finished_at is not None
    assert session.execute(select(Market)).scalars().all().__len__() == 2
    snapshots = session.execute(select(MarketSnapshot)).scalars().all()
    assert len(snapshots) == 2
    assert all(s.scanner_run_id == run.id for s in snapshots)
    assert all(s.score is not None for s in snapshots)


def test_persist_scan_records_audit_fields(session):
    started_at = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    run = persist_scan(session, rank_markets([make_market()]), source="cli", started_at=started_at)

    assert run.source == "cli"
    assert run.duration_ms is not None and run.duration_ms >= 0
    # SQLite returns naive datetimes; compare in UTC wall-clock terms
    assert run.started_at.replace(tzinfo=timezone.utc) == started_at
    assert run.error_type is None and run.error_message is None


def test_persist_scan_defaults_to_api_source(session):
    run = persist_scan(session, rank_markets([make_market()]))
    assert run.source == "api"


def test_persist_scan_stores_raw_payload(session):
    raw = {"ticker": "AAA", "yes_bid": 48, "unmapped_field": "kept for debugging"}
    ranked = rank_markets([make_market(ticker="AAA", raw=raw)])
    persist_scan(session, ranked)

    snapshot = session.execute(select(MarketSnapshot)).scalar_one()
    assert snapshot.raw_payload == raw


def test_persist_scan_upserts_existing_market(session):
    first = rank_markets([make_market(ticker="AAA", title="Old title")])
    persist_scan(session, first)
    second = rank_markets([make_market(ticker="AAA", title="New title")])
    persist_scan(session, second)

    markets = session.execute(select(Market)).scalars().all()
    assert len(markets) == 1
    assert markets[0].title == "New title"
    assert len(session.execute(select(ScannerRun)).scalars().all()) == 2
    assert len(session.execute(select(MarketSnapshot)).scalars().all()) == 2
