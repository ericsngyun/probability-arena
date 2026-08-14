from datetime import datetime, timedelta, timezone

import pytest

from app.schemas import MarketData

# LOW fix (fourth re-review, CRYPTO-COVERAGE-REPAIR-001): the
# `_isolate_crypto_tape_overlap_lock` fixture below patches
# `_resolve_lock_dir` at SESSION scope, but that patch only takes effect
# once pytest sets the fixture up — it does NOT protect any module-level /
# import-time code. If a future test module calls `CryptoTapeConfig.
# from_settings()` (or anything else that reaches `_resolve_lock_dir`) at
# IMPORT time (e.g. as a module-level constant or a `@pytest.fixture`
# default-arg evaluated at collection), it will resolve against the real,
# unpatched settings before this fixture ever runs — producing a real path
# under whatever DATABASE_URL `.env` points at (measured: resolves to
# `<fakeprod>/data`) instead of the test-isolated tmp dir. Nothing in the
# suite does this today (verified: no module does DB/tape work at import
# time), but do not add any.


@pytest.fixture(autouse=True)
def _isolate_sqlite_telemetry(tmp_path):
    """SQLITE-LOCK-TELEMETRY-001A: every test writes telemetry (if any) to a
    per-test temp dir, never to the real ~/probability-arena-telemetry/ —
    instrumented writers (tick aggregation, backup) are exercised by many
    pre-existing tests. Also resets the process-wide sink singleton.

    MEDIUM-1 (GATE7-SPARSE-TELEMETRY-001 review): this used to take the TEST's
    `monkeypatch` fixture. pytest hands every fixture of a test the SAME
    `MonkeyPatch` instance, so any test calling `monkeypatch.undo()` — seven
    call sites across four modules do — reverted THIS isolation too, and the
    next `get_sink()` resolved to the operator's REAL
    ~/probability-arena-telemetry/sqlite-writes.jsonl. Reproduced by the
    reviewer: after `undo()` the resolved path was the real sink, which exists
    on this host and is a live operator artifact.

    THE FIX IS STRUCTURAL, NOT A SWEEP OF THE SEVEN CALL SITES. A private
    `MonkeyPatch` records its patches on ITSELF, so no test-visible `undo()`
    can reach them — the same instrument `_isolate_crypto_tape_overlap_lock`
    below already uses. Rewriting the seven `undo()` calls would fix seven
    instances and leave the class open to the eighth; this closes the class.
    The env var (not the resolved `telemetry_dir` function) stays the patch
    point deliberately: `SQLITE_TELEMETRY_DIR` is inherited by SUBPROCESSES,
    and at least one test asserts the no-SQLite mandate out of process.

    The teardown assertion is the second half of the fix: if some future
    fixture ordering or an explicit `os.environ` write ever un-isolates a test
    anyway, that test FAILS rather than silently appending to the operator's
    file."""
    import app.telemetry.sink as _telemetry_sink

    isolated = tmp_path / "telemetry-isolated"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SQLITE_TELEMETRY_DIR", str(isolated))
        _telemetry_sink._sink = None
        try:
            yield
        finally:
            resolved = _telemetry_sink.telemetry_dir()
            _telemetry_sink._sink = None
    if resolved != isolated:
        raise AssertionError(
            "SQLite telemetry isolation was lost during this test: "
            f"telemetry_dir() resolved to {resolved}, not {isolated}. "
            "Anything this test emitted may have landed in the operator's "
            "real sink."
        )


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
