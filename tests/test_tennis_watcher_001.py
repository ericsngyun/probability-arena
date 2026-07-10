"""TENNIS-WATCHER-001 tests: read-only tennis tick capture and coverage.

Universe discovery, series-prefix bucketing, dry-run persists nothing, manual
scan persists ONLY market_price_ticks (no signals, no watcher_runs), the
scheduled guard no-ops when ENABLE_TENNIS_TICK_WATCHER=false (default),
coverage-report rendering, flag defaults, no external calls (fake adapter),
no forbidden vocabulary. In-memory SQLite; no real Kalshi request is made.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import cli
from app.config import Settings
from app.db import Base
from app.models import Market, MarketPriceTick
from app.schemas import MarketData
from app.services.tennis_watcher import (
    SCAN_DRY_RUN,
    SCAN_NO_TARGETS,
    SCAN_OK,
    SCAN_SKIPPED_FLAG,
    TennisTickWatcher,
    build_tennis_watch_report,
    discover_tennis_universe,
    series_bucket,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]
CH_TICKER = "KXATPCHALLENGERMATCH-26JUL10CASAMB-CAS"


class FakeAdapter:
    def __init__(self, quotes=None):
        self.quotes = quotes or {}
        self.calls = []

    async def fetch_markets_by_tickers(self, tickers):
        self.calls.append(list(tickers))
        out = []
        for t in tickers:
            q = self.quotes.get(t, {"yes_bid": 44, "yes_ask": 46})
            out.append(MarketData(ticker=t, title="t", status="active", **q))
        return out


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def seed_market(session, ticker=CH_TICKER, *, status="active", seen_minutes_ago=10,
                close_in_hours=8):
    session.add(Market(
        ticker=ticker, title="match", status=status,
        last_seen_at=NOW - timedelta(minutes=seen_minutes_ago),
        close_time=NOW + timedelta(hours=close_in_hours),
    ))
    session.commit()


def seed_tick(session, ticker=CH_TICKER, minutes_ago=5):
    at = NOW - timedelta(minutes=minutes_ago)
    session.add(MarketPriceTick(
        market_ticker=ticker, observed_at=at, yes_bid=44, yes_ask=46,
        midpoint=0.45, spread=2, volume_24h=10, liquidity_proxy=1000,
        created_at=at,
    ))
    session.commit()


def watcher(adapter=None, **flag_overrides):
    settings = Settings(_env_file=None, **flag_overrides)
    return TennisTickWatcher(adapter=adapter or FakeAdapter(), settings=settings)


def scan(session, w, **kw):
    return asyncio.run(w.scan_once(session, **kw))


# --- discovery / series ---------------------------------------------------------------


class TestDiscovery:
    def test_active_tennis_universe(self, session):
        seed_market(session)                                        # active recent
        seed_market(session, ticker="KXWTAMATCH-26JUL10SWIGAU-SWI")
        seed_market(session, ticker="KXATPMATCH-26JUL01OLDOLD-OLD",
                    seen_minutes_ago=60 * 72)                       # stale
        seed_market(session, ticker="KXATPMATCH-26JUL10SETSET-SET",
                    status="settled")                               # settled
        seed_market(session, ticker="KXATPMATCH-26JUL10EXPEXP-EXP",
                    close_in_hours=-1)                              # expired
        seed_market(session, ticker="KXMLBTOTAL-26JUL10AAA-7")      # wrong domain
        u = discover_tennis_universe(session, hours=24)
        assert {m.ticker for m in u.active} == {
            CH_TICKER, "KXWTAMATCH-26JUL10SWIGAU-SWI",
        }

    def test_tick_coverage_split(self, session):
        seed_market(session)
        seed_market(session, ticker="KXWTAMATCH-26JUL10SWIGAU-SWI")
        seed_tick(session)   # only the Challenger market has a tick
        u = discover_tennis_universe(session, hours=24)
        assert u.covered_tickers == {CH_TICKER}
        assert [m.ticker for m in u.uncovered] == ["KXWTAMATCH-26JUL10SWIGAU-SWI"]

    def test_series_bucket_most_specific_first(self):
        assert series_bucket(CH_TICKER) == "KXATPCHALLENGERMATCH"
        assert series_bucket("KXITFWMATCH-26JUL10AABB-AA") == "KXITFWMATCH"
        assert series_bucket("KXITFMATCH-26JUL10AABB-AA") == "KXITFMATCH"
        assert series_bucket("KXATPMATCH-26JUL10AABB-AA") == "KXATP"
        assert series_bucket("KXWTAWINNER-26JUL10AABB-AA") == "KXWTA"


# --- scan ----------------------------------------------------------------------------


class TestScan:
    def test_dry_run_persists_nothing(self, session):
        seed_market(session)
        adapter = FakeAdapter()
        r = scan(session, watcher(adapter), dry_run=True)
        assert r["status"] == SCAN_DRY_RUN
        assert r["targets"] == 1
        assert r["fetched"] == 1
        assert r["ticks_recorded"] == 0
        assert session.execute(
            text("select count(*) from market_price_ticks")
        ).scalar() == 0

    def test_manual_scan_persists_only_ticks(self, session):
        seed_market(session)
        seed_market(session, ticker="KXWTAMATCH-26JUL10SWIGAU-SWI")
        r = scan(session, watcher())
        assert r["status"] == SCAN_OK
        assert r["ticks_recorded"] == 2
        assert session.execute(
            text("select count(*) from market_price_ticks")
        ).scalar() == 2
        # no signal, no watcher-run, no market mutation side effects
        assert session.execute(
            text("select count(*) from opportunity_signals")
        ).scalar() == 0
        assert session.execute(
            text("select count(*) from watcher_runs")
        ).scalar() == 0

    def test_tick_shape_matches_watcher_convention(self, session):
        seed_market(session)
        scan(session, watcher(FakeAdapter({CH_TICKER: {"yes_bid": 40, "yes_ask": 44}})))
        tick = session.execute(
            text("select yes_bid, yes_ask, midpoint, spread from market_price_ticks")
        ).one()
        assert tuple(tick) == (40, 44, 0.42, 4)

    def test_scheduled_guard_noops_when_flag_false(self, session):
        seed_market(session)
        adapter = FakeAdapter()
        r = scan(session, watcher(adapter), scheduled=True)
        assert r["status"] == SCAN_SKIPPED_FLAG
        assert adapter.calls == []          # not even a fetch
        assert session.execute(
            text("select count(*) from market_price_ticks")
        ).scalar() == 0

    def test_scheduled_runs_when_flag_true(self, session):
        seed_market(session)
        r = scan(session, watcher(enable_tennis_tick_watcher=True), scheduled=True)
        assert r["status"] == SCAN_OK
        assert r["ticks_recorded"] == 1

    def test_limit_bounds_targets_match_winner_first(self, session):
        seed_market(session, ticker="KXATPSETWIN-26JUL10AABB-1")   # set_winner
        seed_market(session)                                        # match_winner
        adapter = FakeAdapter()
        r = scan(session, watcher(adapter), limit=1)
        assert r["targets"] == 1
        assert adapter.calls == [[CH_TICKER]]   # match-winner outranks set

    def test_no_targets(self, session):
        r = scan(session, watcher())
        assert r["status"] == SCAN_NO_TARGETS
        assert r["ticks_recorded"] == 0

    def test_flag_and_limit_defaults(self):
        s = Settings(_env_file=None)
        assert s.enable_tennis_tick_watcher is False
        assert s.tennis_tick_watch_limit == 200


# --- report --------------------------------------------------------------------------


class TestReport:
    def test_coverage_report(self, session):
        seed_market(session)
        seed_market(session, ticker="KXWTAMATCH-26JUL10SWIGAU-SWI")
        seed_tick(session)
        r = build_tennis_watch_report(session, hours=24)
        assert r["active_tennis_markets"] == 2
        assert r["match_winner_markets"] == 2
        assert r["tick_covered"] == 1
        assert r["uncovered"] == 1
        assert r["coverage_rate"] == 0.5
        assert r["latest_tick_age_s"] is not None
        assert r["quote_stats"]["two_sided_share"] == 1.0
        assert r["series_mix_active"] == {"KXATPCHALLENGERMATCH": 1, "KXWTA": 1}
        assert "no trading" in r["disclaimer"]
        assert r["flag_enable_tennis_tick_watcher"] is False

    def test_empty_report(self, session):
        r = build_tennis_watch_report(session)
        assert r["active_tennis_markets"] == 0
        assert r["coverage_rate"] is None
        assert r["latest_tick_age_s"] is None


# --- CLI ---------------------------------------------------------------------------


class TestCLI:
    def test_scan_cli_parses(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "tennis_watch_scan_once", fake)
        rc = cli.main([
            "tennis-watch-scan-once", "--limit", "50", "--hours", "12", "--dry-run",
        ])
        assert rc == 0
        assert captured == {"limit": 50, "hours": 12, "dry_run": True, "scheduled": False}

    def test_report_cli_parses(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 0

        monkeypatch.setattr(cli, "tennis_watch_report", fake)
        rc = cli.main(["tennis-watch-report", "--hours", "48"])
        assert rc == 0
        assert captured == {"hours": 48}

    def test_report_cli_renders(self, session, capsys):
        seed_market(session)
        seed_tick(session)
        n = asyncio.run(cli.tennis_watch_report(session=session))
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "coverage_rate=" in out
        assert "market observation only" in out


# --- safety --------------------------------------------------------------------------


class TestSafety:
    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "tennis_watcher.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "jupiter", "recommend",
                    "execute_trade", "execution", "buy", "sell"):
            assert bad not in code, bad

    def test_no_direct_network_imports(self):
        # quotes go through the existing read-only adapter only
        src = (REPO / "app" / "services" / "tennis_watcher.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src

    def test_no_signal_detection_capability(self):
        src = (REPO / "app" / "services" / "tennis_watcher.py").read_text()
        assert "OpportunitySignal" not in src
        assert "WatcherRun" not in src

    def test_note_language(self, session):
        r = build_tennis_watch_report(session)
        assert "never advice" in r["note"]
        assert "not EV" in r["note"]
