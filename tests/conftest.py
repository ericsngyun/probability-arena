from datetime import datetime, timedelta, timezone

import pytest

from app.schemas import MarketData


@pytest.fixture(autouse=True)
def _isolate_sqlite_telemetry(tmp_path, monkeypatch):
    """SQLITE-LOCK-TELEMETRY-001A: every test writes telemetry (if any) to a
    per-test temp dir, never to the real ~/probability-arena-telemetry/ —
    instrumented writers (tick aggregation, backup) are exercised by many
    pre-existing tests. Also resets the process-wide sink singleton."""
    import app.telemetry.sink as _telemetry_sink

    monkeypatch.setenv(
        "SQLITE_TELEMETRY_DIR", str(tmp_path / "telemetry-isolated"))
    _telemetry_sink._sink = None
    yield
    _telemetry_sink._sink = None


@pytest.fixture
def sample_kalshi_market() -> dict:
    """Raw market object shaped like GET /trade-api/v2/markets output."""
    return {
        "ticker": "FED-25DEC-T4.00",
        "event_ticker": "FED-25DEC",
        "market_type": "binary",
        "title": "Fed funds rate above 4.00% after December meeting?",
        "category": "Economics",
        "status": "active",
        "yes_bid": 43,
        "yes_ask": 45,
        "no_bid": 55,
        "no_ask": 57,
        "last_price": 44,
        "volume": 120000,
        "volume_24h": 8500,
        "open_interest": 45000,
        "liquidity": 250000,
        "close_time": "2025-12-10T19:00:00Z",
        "expiration_time": "2025-12-10T20:00:00Z",
        "rules_primary": "Resolves YES if the upper bound of the federal funds target range exceeds 4.00%.",
    }


@pytest.fixture
def sample_markets_payload(sample_kalshi_market) -> dict:
    quiet = dict(
        sample_kalshi_market,
        ticker="OSCAR-26-BESTPIC",
        event_ticker="OSCAR-26",
        title="Will the favorite win Best Picture?",
        yes_bid=0,
        yes_ask=0,
        volume_24h=0,
        liquidity=0,
    )
    return {"markets": [sample_kalshi_market, quiet], "cursor": ""}


def make_market(**overrides) -> MarketData:
    base = dict(
        ticker="TEST-MKT",
        title="Test market",
        status="active",
        yes_bid=48,
        yes_ask=52,
        volume_24h=1000,
        open_interest=5000,
        liquidity=100000,
        close_time=datetime.now(timezone.utc) + timedelta(days=7),
    )
    base.update(overrides)
    return MarketData(**base)
