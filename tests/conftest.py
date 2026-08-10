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


@pytest.fixture(scope="session", autouse=True)
def _isolate_crypto_tape_overlap_lock(tmp_path_factory):
    """CRYPTO-COVERAGE-REPAIR-001 NEW-M4/NEW-M1: `_resolve_lock_dir` derives
    the reconciliation overlap flock's directory from `settings.database_url`.
    On any host where `.env` sets a real sqlite DATABASE_URL (production,
    EVO, some CI configurations), a Settings object built without an
    explicit `database_url` override — which is how every crypto-tape test's
    `_settings()`-style helper builds one — inherits that real path. Without
    this, running the suite there would take the SAME
    `.crypto-tape-reconcile-{chain}.lock` file the production
    `probability-arena-crypto-reconcile.timer` uses: the real timer could get
    `skipped_overlap`-failed by the test suite, and the suite could be
    blocked by the real timer for its duration. Force every crypto-tape
    overlap lock any test takes into an isolated tmp dir, unconditionally —
    tests that pass their own explicit `lock_dir` (via `CryptoTapeConfig`)
    are unaffected, since `_resolve_lock_dir` is only reached when no
    explicit `lock_dir` was given.

    NEW-M1 fix: this used to be FUNCTION-scoped, which pytest sets up only
    among other function-scoped fixtures — a module- or session-scoped
    fixture belonging to some other test module is instantiated BEFORE
    function-scoped fixtures and would see the real, unpatched
    `_resolve_lock_dir` (reproduced with a faithful defeat-test copy: a
    module-scoped fixture bypassed the patch and leaked a lock file into the
    fake prod dir). pytest sets up fixtures in strict scope order —
    session-scoped before package/module/class/function-scoped — regardless
    of the dependency graph, so making this fixture session-scoped guarantees
    it patches BEFORE any narrower-scoped fixture anywhere in the suite can
    run. The suite runs sequentially (no xdist), so one shared session-wide
    lock dir is safe — each test's flock is released when the test's own
    `with` block exits, before the next test starts."""
    import app.services.crypto_tape as tape_mod

    lock_dir = tmp_path_factory.mktemp("crypto-tape-overlap-lock")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tape_mod, "_resolve_lock_dir", lambda settings=None: lock_dir)
        yield


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
