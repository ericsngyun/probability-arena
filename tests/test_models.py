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
        "market_detail_enrichments",
        "market_research_packets",
        "market_forecasts",
        "market_outcomes",
        "forecast_scores",
        "pipeline_runs",
        "pipeline_stage_runs",
        "market_price_ticks",
        "market_price_tick_buckets",  # OPS-012 aggregated telemetry summaries
        "tick_aggregation_runs",  # OPS-013 aggregation audit spine
        "opportunity_signals",
        "watcher_runs",
        # Crypto Arena (CRYPTO-001) — read-only surveillance lane
        "crypto_tokens",
        "crypto_pairs",
        "crypto_token_discovery_events",
        "crypto_token_risk_assessments",
        "crypto_price_ticks",
        "crypto_opportunity_signals",
        "crypto_watcher_runs",
        # MarketOps Autopilot (OPS-006) — coordination audit
        "marketops_runs",
        "marketops_alerts",
        # Edge precheck (MVP-005A) — probability-gap measurement only
        "edge_precheck_snapshots",
        # Frontier evaluation (EVAL-001) — evaluation audit only
        "frontier_eval_runs",
        # MEME-NEWS-001 — read-only meme/news scout + domain expansion
        "meme_scout_runs",
        "meme_attention_snapshots",
        "meme_catalyst_events",
        "domain_scout_runs",
        "domain_market_inventory_snapshots",
        # POLY-001 — read-only Polymarket market-data observer (second venue)
        "polymarket_scout_runs",
        "polymarket_markets",
        "polymarket_orderbook_snapshots",
        "polymarket_domain_inventory_snapshots",
        # TENNIS-TAPE-001 — read-only synchronized tennis tape (measurement only)
        "tennis_tape_runs",
        "tennis_tape_score_snapshots",
        "tennis_tape_market_snapshots",
        "tennis_tape_links",
        # POLY-002 — read-only Kalshi<->Polymarket cross-venue observation
        "cross_venue_observation_runs",
        "cross_venue_market_candidates",
        # CRYPTO-TAPE-001 — read-only memecoin lifecycle tape (research only)
        "crypto_token_lifecycle_runs",
        "crypto_token_birth_events",
        "crypto_token_lifecycle_snapshots",
        "crypto_token_actor_observations",
        "crypto_token_survival_outcomes",
        # CRYPTO-HORIZON-OBS-001 — bounded read-only horizon-observation lane
        "crypto_horizon_cohorts",
        "crypto_horizon_cohort_members",
        "crypto_horizon_observations",
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
