"""POLY-001 tests: read-only Polymarket market-data observer. Adapter parses
mocked Gamma market catalog + CLOB order book, degrades gracefully on provider
failure, the scan persists market/orderbook/domain rows + an audit run, the
scheduled flag gates the scheduled path (manual always allowed), the windowed
report + domain report build, retention prunes the churn tables (domain
inventory protected), and NO trading/auth/wallet/signing surface exists.
No live network; in-memory SQLite."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

import app.config as config_module
from app import cli
from app.adapters.polymarket import (
    PolymarketAdapter,
    PolymarketMarketData,
    PolymarketOrderbook,
    _parse_market,
    _parse_orderbook,
)
from app.config import Settings
from app.db import Base
from app.models import (
    PolymarketDomainInventorySnapshot,
    PolymarketMarket,
    PolymarketOrderbookSnapshot,
    PolymarketScoutRun,
)
from app.services.polymarket import (
    PolymarketConfig,
    PolymarketDomainReportService,
    PolymarketReportService,
    PolymarketScoutRunner,
    PolymarketScoutService,
)
from app.services.retention import PROTECTED_TABLES, RetentionConfig, RetentionService

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]

GAMMA_MARKET = {
    "id": "540817",
    "conditionId": "0xcond",
    "question": "New Rihanna Album before GTA VI?",
    "slug": "rihanna-gta",
    "description": "resolves yes if ...",
    "active": True,
    "closed": False,
    "archived": False,
    "restricted": False,
    "enableOrderBook": True,
    "acceptingOrders": True,
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.4", "0.6"]',
    "clobTokenIds": '["111", "222"]',
    "bestBid": "0.39",
    "bestAsk": "0.41",
    "lastTradePrice": "0.40",
    "spread": "0.02",
    "liquidityNum": 14693.05,
    "volume24hr": 5000.0,
    "volumeNum": 100000.0,
    "startDate": "2025-05-02T15:48:10.582Z",
    "endDate": "2026-07-31T12:00:00Z",
    "events": [{"title": "What happens before GTA VI", "slug": "gta", "ticker": "GTA"}],
}

CLOB_BOOK = {
    "asset_id": "111",
    "market": "0xhash",
    "bids": [{"price": "0.39", "size": "100"}, {"price": "0.38", "size": "200"}],
    "asks": [{"price": "0.41", "size": "150"}, {"price": "0.42", "size": "50"}],
    "tick_size": "0.01",
    "min_order_size": "5",
    "last_trade_price": "0.40",
}


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def market_data(market_id, *, category="sports", liq=1000.0, vol=500.0, two_sided=True,
                enable_ob=True, active=True, tokens=("t1", "t2")):
    return PolymarketMarketData(
        market_id=market_id,
        condition_id="c-" + market_id,
        question="Q " + market_id,
        slug="s-" + market_id,
        category=category,
        description="d",
        active=active,
        closed=False,
        archived=False,
        restricted=False,
        enable_order_book=enable_ob,
        accepting_orders=True,
        outcomes=["Yes", "No"],
        outcome_prices=[0.4, 0.6],
        clob_token_ids=list(tokens),
        best_bid=0.39 if two_sided else None,
        best_ask=0.41 if two_sided else None,
        last_trade_price=0.40,
        spread=0.02 if two_sided else None,
        liquidity_usd=liq,
        volume_24h_usd=vol,
        volume_total_usd=vol * 10,
        start_date=NOW,
        end_date=NOW + timedelta(days=30),
    )


def orderbook(token_id):
    return PolymarketOrderbook(
        token_id=token_id, market="m", best_bid=0.39, best_ask=0.41, mid=0.40,
        spread=0.02, bid_depth=300.0, ask_depth=200.0, total_depth=500.0,
        num_bids=2, num_asks=2, liquidity_proxy=200.0, tick_size=0.01,
    )


class FakeAdapter:
    def __init__(self, markets=None, books=None, fail_markets=False):
        self._markets = markets or []
        self._books = books or {}
        self._fail_markets = fail_markets
        self.orderbook_calls = []

    async def fetch_markets(self, limit=50, active=True, closed=False):
        return [] if self._fail_markets else list(self._markets)[:limit]

    async def fetch_orderbook(self, token_id):
        self.orderbook_calls.append(token_id)
        return self._books.get(token_id)


def service_with(adapter, **cfg):
    base = dict(market_limit=50, orderbook_limit=20, provider_version="v1")
    base.update(cfg)
    return PolymarketScoutService(adapter=adapter, config=PolymarketConfig(**base))


def use_settings(monkeypatch, **kw):
    s = Settings(_env_file=None, **kw)
    monkeypatch.setattr(config_module, "get_settings", lambda: s)
    return s


# --- adapter parsing --------------------------------------------------------


class TestAdapterParsing:
    def test_parse_market_handles_json_string_fields(self):
        m = _parse_market(GAMMA_MARKET)
        assert m is not None
        assert m.market_id == "540817"
        assert m.outcomes == ["Yes", "No"]
        assert m.outcome_prices == [0.4, 0.6]
        assert m.clob_token_ids == ["111", "222"]
        assert m.enable_order_book is True
        assert m.two_sided is True  # best_bid + best_ask present
        assert m.category == "What happens before GTA VI"
        assert m.liquidity_usd == pytest.approx(14693.05)
        assert m.volume_24h_usd == pytest.approx(5000.0)

    def test_parse_market_missing_id_returns_none(self):
        assert _parse_market({"question": "no id"}) is None
        assert _parse_market("garbage") is None

    def test_parse_market_tolerates_bad_json_lists(self):
        m = _parse_market({"id": "1", "outcomes": "not-json", "clobTokenIds": None})
        assert m is not None
        assert m.outcomes == []
        assert m.clob_token_ids == []

    def test_parse_orderbook_reduces_to_proxies(self):
        ob = _parse_orderbook(CLOB_BOOK, "111")
        assert ob is not None
        assert ob.best_bid == 0.39
        assert ob.best_ask == 0.41
        assert ob.mid == pytest.approx(0.40)
        assert ob.spread == pytest.approx(0.02)
        assert ob.bid_depth == 300.0
        assert ob.ask_depth == 200.0
        assert ob.total_depth == 500.0
        assert ob.liquidity_proxy == pytest.approx(200.0)
        assert ob.num_bids == 2 and ob.num_asks == 2

    def test_parse_orderbook_empty_book(self):
        ob = _parse_orderbook({"asset_id": "x", "bids": [], "asks": []}, "x")
        assert ob is not None
        assert ob.best_bid is None and ob.best_ask is None
        assert ob.total_depth == 0.0 and ob.liquidity_proxy is None


class TestAdapterGracefulFailure:
    def test_fetch_markets_returns_empty_on_non_list(self, monkeypatch):
        adapter = PolymarketAdapter(Settings(_env_file=None))

        async def fake_get(base, path, params=None):
            return {"error": "boom"}  # not a list

        monkeypatch.setattr(adapter, "_get", fake_get)
        assert asyncio.run(adapter.fetch_markets()) == []

    def test_fetch_orderbook_returns_none_on_failure(self, monkeypatch):
        adapter = PolymarketAdapter(Settings(_env_file=None))

        async def fake_get(base, path, params=None):
            return None  # provider failure/timeout

        monkeypatch.setattr(adapter, "_get", fake_get)
        assert asyncio.run(adapter.fetch_orderbook("111")) is None


# --- scan_once --------------------------------------------------------------


class TestScanOnce:
    def test_persists_markets_orderbooks_domains(self, session):
        adapter = FakeAdapter(
            markets=[market_data("A", category="sports", tokens=("t1", "t2")),
                     market_data("B", category="politics", tokens=("t3",))],
            books={"t1": orderbook("t1"), "t2": orderbook("t2"), "t3": orderbook("t3")},
        )
        run = asyncio.run(service_with(adapter).scan_once(session))
        assert run.status == "ok"
        assert run.markets_seen == 2
        assert run.markets_persisted == 2
        assert run.orderbooks_fetched == 3
        assert run.orderbook_errors == 0
        assert run.domains_seen == 2
        assert session.query(PolymarketMarket).count() == 2
        assert session.query(PolymarketOrderbookSnapshot).count() == 3
        assert session.query(PolymarketDomainInventorySnapshot).count() == 2

    def test_orderbook_budget_is_bounded(self, session):
        # 3 markets × 2 tokens = 6 candidates, but limit is 2
        adapter = FakeAdapter(
            markets=[market_data(x, tokens=("a" + x, "b" + x)) for x in ("A", "B", "C")],
            books={},  # every book fetch returns None → counts as error
        )
        run = asyncio.run(service_with(adapter, orderbook_limit=2).scan_once(session))
        assert run.status == "ok"
        assert len(adapter.orderbook_calls) == 2  # bounded
        assert run.orderbook_errors == 2
        assert run.orderbooks_fetched == 0

    def test_provider_outage_is_ok_not_error(self, session):
        adapter = FakeAdapter(fail_markets=True)
        run = asyncio.run(service_with(adapter).scan_once(session))
        assert run.status == "ok"  # graceful: nothing observed, not a crash
        assert run.markets_seen == 0
        assert session.query(PolymarketMarket).count() == 0

    def test_skips_orderbooks_when_disabled(self, session):
        adapter = FakeAdapter(
            markets=[market_data("A", enable_ob=False, tokens=("t1",))],
            books={"t1": orderbook("t1")},
        )
        run = asyncio.run(service_with(adapter).scan_once(session))
        assert run.orderbooks_fetched == 0
        assert adapter.orderbook_calls == []

    def test_runner_never_raises(self, session):
        adapter = FakeAdapter(markets=[market_data("A", tokens=())])
        runner = PolymarketScoutRunner(scout=service_with(adapter))
        run = asyncio.run(runner.run_cycle(session))
        assert run is not None and run.status == "ok"


# --- flag gate (scheduled vs manual) ----------------------------------------


class TestScheduledGate:
    def test_scheduled_skips_when_flag_off(self, monkeypatch, session):
        use_settings(monkeypatch, enable_polymarket_scout=False)
        called = {"n": 0}

        class Runner:
            async def run_cycle(self, session, limit=None):
                called["n"] += 1

        rc = asyncio.run(cli.polymarket_scan_once(scheduled=True, runner=Runner(), session=session))
        assert rc == 0
        assert called["n"] == 0  # never ran

    def test_manual_always_allowed_when_flag_off(self, monkeypatch, session):
        use_settings(monkeypatch, enable_polymarket_scout=False)
        adapter = FakeAdapter(markets=[market_data("A", tokens=())])
        runner = PolymarketScoutRunner(scout=service_with(adapter))
        rc = asyncio.run(cli.polymarket_scan_once(scheduled=False, runner=runner, session=session))
        assert rc >= 0
        assert session.query(PolymarketScoutRun).count() == 1


# --- reports ----------------------------------------------------------------


class TestReports:
    def _seed(self, session):
        adapter = FakeAdapter(
            markets=[market_data("A", category="sports", liq=9000, vol=8000, tokens=("t1",)),
                     market_data("B", category="politics", liq=100, vol=50,
                                 two_sided=False, tokens=())],
            books={"t1": orderbook("t1")},
        )
        return asyncio.run(service_with(adapter).scan_once(session))

    def test_report_builds(self, session):
        self._seed(session)
        r = PolymarketReportService().build(session, hours=24)
        assert r.markets_seen == 2
        assert r.active_markets == 2
        assert r.categories == 2
        assert r.two_sided_markets == 1
        assert r.orderbook_enabled_markets == 2
        assert r.orderbook_snapshots_in_window == 1
        assert r.spread_p50 is not None
        assert r.top_volume_markets[0]["market_id"] == "A"  # highest volume first
        assert r.row_counts["polymarket_markets"] == 2
        assert "no EV" in r.note.lower() or "not a recommendation" in r.note.lower()
        assert "arbitrage" in r.cross_venue_note.lower()

    def test_domain_report_builds(self, session):
        self._seed(session)
        r = PolymarketDomainReportService().build(session)
        assert r.total_domains == 2
        doms = {d["domain"] for d in r.domains}
        assert doms == {"sports", "politics"}
        # most markets first
        assert r.domains[0]["market_count"] >= r.domains[-1]["market_count"]


# --- retention --------------------------------------------------------------


class TestRetention:
    def test_prunes_churn_tables_protects_domain_inventory(self, session):
        old = NOW - timedelta(days=30)
        recent = NOW
        # old churn rows
        session.add(PolymarketScoutRun(status="ok", started_at=old, created_at=old))
        session.add(PolymarketMarket(market_id="old", observed_at=old, created_at=old))
        session.add(PolymarketOrderbookSnapshot(token_id="old", observed_at=old, created_at=old))
        # recent churn rows (kept)
        session.add(PolymarketMarket(market_id="new", observed_at=recent, created_at=recent))
        # a still-running old run (never pruned)
        session.add(PolymarketScoutRun(status="running", started_at=old, created_at=old))
        # domain inventory is protected/coverage history (kept even when old)
        session.add(PolymarketDomainInventorySnapshot(domain="sports", observed_at=old, created_at=old))
        session.commit()

        svc = RetentionService(RetentionConfig(polymarket_days=14))
        counts = svc.prune(session, dry_run=False)

        assert counts["polymarket_markets"] == 1
        assert counts["polymarket_orderbook_snapshots"] == 1
        assert counts["polymarket_scout_runs"] == 1  # the finished one only
        assert session.query(PolymarketMarket).count() == 1  # recent kept
        assert session.query(PolymarketScoutRun).count() == 1  # running kept
        assert session.query(PolymarketDomainInventorySnapshot).count() == 1  # protected

    def test_domain_inventory_in_protected_tables(self):
        assert "polymarket_domain_inventory_snapshots" in PROTECTED_TABLES


# --- safety: no trading / auth / wallet / signing surface -------------------


class TestSafetySurface:
    # Implementation-SIGNATURE tokens (underscores/parens) that must never
    # appear as code. Bare disclaimer words like "wallet"/"swap"/"signing" are
    # ALLOWED in boundary docstrings, so they are deliberately not listed here.
    FORBIDDEN = [
        "private_key", "keypair", "sign_transaction", "send_transaction",
        "place_order", "post_order", "create_order", "cancel_order", "submit_order",
        "jupiter", ".swap(", "expected_value", "paper_trad",
        "position_siz", "trade_recommend", "kelly",
    ]

    def _read(self, rel):
        return (REPO / rel).read_text().lower()

    def test_adapter_has_no_forbidden_surface(self):
        src = self._read("app/adapters/polymarket.py")
        for term in self.FORBIDDEN:
            assert term not in src, f"forbidden term {term!r} in adapter"

    def test_service_has_no_forbidden_surface(self):
        src = self._read("app/services/polymarket.py")
        for term in self.FORBIDDEN:
            assert term not in src, f"forbidden term {term!r} in service"

    def test_adapter_uses_only_public_no_auth_endpoints(self):
        src = self._read("app/adapters/polymarket.py")
        # no auth headers / api keys anywhere
        assert "authorization" not in src
        assert "api_key" not in src and "api-key" not in src
        # only the two public read hosts
        assert "gamma-api.polymarket.com" in src
        assert "clob.polymarket.com" in src


def test_migration_0020_round_trips():
    """The 0020 migration applies and reverses cleanly (up then down)."""
    from alembic import command
    from alembic.config import Config

    engine = create_engine("sqlite://")
    url = "sqlite://"
    # Use a file-less in-memory DB via alembic requires a real url; use a temp file.
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        db = f"sqlite:///{d}/t.db"
        cfg = Config()
        cfg.set_main_option("script_location", str(REPO / "alembic"))
        cfg.set_main_option("sqlalchemy.url", db)
        command.upgrade(cfg, "0020")
        insp = inspect(create_engine(db))
        tables = set(insp.get_table_names())
        assert {
            "polymarket_scout_runs",
            "polymarket_markets",
            "polymarket_orderbook_snapshots",
            "polymarket_domain_inventory_snapshots",
        } <= tables
        command.downgrade(cfg, "0019")
        insp2 = inspect(create_engine(db))
        remaining = set(insp2.get_table_names())
        assert "polymarket_markets" not in remaining
    engine.dispose()
