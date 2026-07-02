"""End-to-end tests of GET /markets/candidates via TestClient.

Lifespan is intentionally not entered (no context manager), so no migrations
run; the DB dependency is overridden with an in-memory SQLite session and the
Kalshi adapter is faked.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import MarketEligibilityAssessment
from app.services import cache
from app.services import scanner as scanner_module
from tests.conftest import make_market

GOOD = make_market(
    ticker="GOOD-MKT",
    title="Healthy two-sided market",
    close_time=datetime.now(timezone.utc) + timedelta(days=7),
)
PARLAY = make_market(
    ticker="KXMVECROSSCATEGORY-S2026-XYZ",
    title="yes Spain advances,yes Croatia advances,yes Argentina advances",
    yes_bid=None,
    yes_ask=None,
    liquidity=0,
    volume_24h=0,
    close_time=datetime.now(timezone.utc) + timedelta(days=18),
)


class FakeAdapter:
    def __init__(self, markets):
        self.markets = markets

    async def fetch_active_markets(self, max_markets=None):
        return self.markets[: max_markets or len(self.markets)]


@pytest.fixture
def client(monkeypatch):
    # TestClient serves requests on another thread; share the one in-memory connection
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(scanner_module, "KalshiRestAdapter", lambda: FakeAdapter([GOOD, PARLAY]))
    monkeypatch.setattr(cache, "get_cached", lambda key: None)
    monkeypatch.setattr(cache, "set_cached", lambda key, value, ttl: None)

    yield TestClient(app), session

    app.dependency_overrides.clear()
    session.close()


def test_rejected_markets_do_not_appear_by_default(client):
    test_client, _ = client
    body = test_client.get("/markets/candidates").json()

    tickers = [c["ticker"] for c in body["candidates"]]
    assert tickers == ["GOOD-MKT"]
    assert body["rejected"] == []
    assert body["markets_assessed"] == 2
    assert body["eligible_count"] == 1
    assert body["rejected_count"] == 1


def test_eligible_candidate_ranks_normally_with_eligibility_status(client):
    test_client, _ = client
    body = test_client.get("/markets/candidates").json()

    candidate = body["candidates"][0]
    assert candidate["is_eligible"] is True
    assert candidate["score"] > 0.0
    assert candidate["components"]["spread"] > 0.0


def test_include_rejected_exposes_reasons(client):
    test_client, _ = client
    body = test_client.get("/markets/candidates?include_rejected=true").json()

    assert [c["ticker"] for c in body["candidates"]] == ["GOOD-MKT"]
    assert len(body["rejected"]) == 1
    rejected = body["rejected"][0]
    assert rejected["ticker"] == "KXMVECROSSCATEGORY-S2026-XYZ"
    assert rejected["is_eligible"] is False
    assert rejected["score"] == 0.0
    assert "no_quotes" in rejected["rejection_reasons"]
    assert rejected["market_type_flags"]["multivariate"] is True
    assert "parlay_like_market" in rejected["warnings"]


def test_every_rejected_market_has_reasons(client):
    test_client, _ = client
    body = test_client.get("/markets/candidates?include_rejected=true").json()
    assert all(r["rejection_reasons"] for r in body["rejected"])


def test_scan_persists_eligibility_assessments_linked_to_run(client):
    test_client, session = client
    body = test_client.get("/markets/candidates").json()

    rows = session.execute(select(MarketEligibilityAssessment)).scalars().all()
    assert len(rows) == 2
    by_ticker = {row.market_ticker: row for row in rows}
    assert by_ticker["GOOD-MKT"].is_eligible is True
    assert by_ticker["GOOD-MKT"].rejection_reasons == []

    parlay = by_ticker["KXMVECROSSCATEGORY-S2026-XYZ"]
    assert parlay.is_eligible is False
    assert "no_quotes" in parlay.rejection_reasons
    assert parlay.has_two_sided_quote is False
    assert parlay.market_type_flags["multivariate"] is True
    assert all(row.scanner_run_id == body["scanner_run_id"] for row in rows)
