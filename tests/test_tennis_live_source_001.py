"""TENNIS-LIVE-SOURCE-001 tests: read-only tennis provider/source validation.

Ticker/market classification, player mapping, template-provider fallback
(zero fetches, honest provider_gap), source-backed mapping via a fake
provider, provider no-match fallback, fetch-failure stale warnings, the
scoreboard fetch bound, report rendering, no persistence, no network, no
forbidden vocabulary. In-memory SQLite; no real provider is ever queried.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import Market, MarketPriceTick
from app.services import tennis_live_source as tls
from app.services.tennis_live_source import (
    MAP_GAP,
    MAP_NO_MATCH,
    MAP_NOT_WINNER,
    MAP_SOURCE_BACKED,
    MAP_UNPARSEABLE,
    TennisLiveSourceReportService,
    classify_tennis_market,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]
TICKER = "KXATPMATCH-26JUL09SINALC"


# --- classification ------------------------------------------------------------------


class TestClassification:
    def test_match_winner(self):
        assert classify_tennis_market("KXATPMATCH-26JUL09SINALC") == "match_winner"
        assert classify_tennis_market("KXWTAWINNER-26JUL09SWIGAU") == "match_winner"

    def test_set_winner(self):
        assert classify_tennis_market("KXATPSETWIN-26JUL09SINALC-1") == "set_winner"

    def test_prop(self):
        assert classify_tennis_market("KXATPTOTALGAMES-26JUL09SINALC-22") == "prop"

    def test_unknown(self):
        assert classify_tennis_market("KXATPFOO-26JUL09SINALC") == "unknown"
        assert classify_tennis_market("KXMLBTOTAL-26JUL09AAA-7") == "unknown"
        assert classify_tennis_market("") == "unknown"


# --- fake provider -------------------------------------------------------------------


def espn_event(a="SIN", b="ALC", status="In Progress"):
    return {
        "competitions": [{
            "competitors": [
                {"athlete": {"abbreviation": a}},
                {"athlete": {"abbreviation": b}},
            ],
        }],
        "status": {"type": {"description": status, "state": "in"}},
    }


class FakeFetcher:
    source_name = "fake.provider.test"

    def __init__(self, scoreboards=None):
        self.scoreboards = scoreboards or {}
        self.fetch_calls = []

    def scoreboard_url(self, tour, date):
        return f"fake://{tour}/{date}"

    def match_details_url(self, tour, event_id):
        return f"fake://{tour}/{event_id}"

    async def fetch_scoreboard(self, tour, date):
        self.fetch_calls.append((tour, date))
        return self.scoreboards.get((tour, date))

    async def fetch_match_details(self, tour, event_id):
        return None


# --- fixtures ------------------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def seed_market(session, ticker=TICKER, *, status="active", seen_minutes_ago=10,
                title="Sinner vs Alcaraz winner?"):
    session.add(Market(
        ticker=ticker, title=title, status=status,
        last_seen_at=NOW - timedelta(minutes=seen_minutes_ago),
    ))
    session.commit()


def seed_tick(session, ticker=TICKER, minutes_ago=3, mid=0.55):
    at = NOW - timedelta(minutes=minutes_ago)
    session.add(MarketPriceTick(
        market_ticker=ticker, observed_at=at,
        yes_bid=int(mid * 100) - 1, yes_ask=int(mid * 100) + 1, midpoint=mid,
        spread=2, volume_24h=10, liquidity_proxy=100_000, created_at=at,
    ))
    session.commit()


def build(session, fetcher=None, **kw):
    service = TennisLiveSourceReportService(fetcher=fetcher, use_settings_fetcher=False)
    return asyncio.run(service.build(session, **kw))


def example(report, ticker):
    return next(c for c in report["examples"] if c.market_ticker == ticker)


# --- template provider (no fetcher) ---------------------------------------------------


class TestTemplateProvider:
    def test_provider_gap_without_any_fetch(self, session):
        seed_market(session)
        r = build(session)
        assert r["provider"] == "template (none)"
        c = example(r, TICKER)
        assert c.mapping_status == MAP_GAP
        assert c.players_mapped is True
        assert c.player_a == "SIN" and c.player_b == "ALC"
        assert any("provider_gap" in w for w in r["warnings"])
        assert r["scoreboards_fetched"] == 0
        assert r["source_backed_count"] == 0

    def test_structural_validation_still_reported(self, session):
        seed_market(session)
        seed_market(session, ticker="KXWTAODDSHAPE!", title="odd")
        r = build(session)
        assert r["total_tennis_markets"] == 2
        assert r["unparseable_ticker_count"] == 1
        assert example(r, "KXWTAODDSHAPE!").mapping_status == MAP_UNPARSEABLE


# --- provider-backed mapping ----------------------------------------------------------


class TestProviderMapping:
    def test_source_backed_when_event_matches(self, session):
        seed_market(session)
        seed_tick(session, minutes_ago=3)
        fetcher = FakeFetcher({("atp", "2026-07-09"): {"events": [espn_event()]}})
        r = build(session, fetcher=fetcher)
        c = example(r, TICKER)
        assert c.mapping_status == MAP_SOURCE_BACKED
        assert c.event_status == "In Progress"
        assert c.fetched_at is not None
        # seeded 3m before module import; generous upper bound so a slow,
        # loaded suite run cannot flake this
        assert 120 <= c.market_quote_age_s <= 900
        assert c.score_to_market_lag_s == c.market_quote_age_s
        assert r["provider_match_rate"] == 1.0
        assert r["source_backed_count"] == 1
        assert fetcher.fetch_calls == [("atp", "2026-07-09")]

    def test_provider_no_match_falls_back_honestly(self, session):
        seed_market(session)
        fetcher = FakeFetcher(
            {("atp", "2026-07-09"): {"events": [espn_event("DJO", "MED")]}}
        )
        r = build(session, fetcher=fetcher)
        c = example(r, TICKER)
        assert c.mapping_status == MAP_NO_MATCH
        assert any("does not cover" in n for n in c.notes)
        assert r["provider_match_rate"] == 0.0

    def test_fetch_failure_is_stale_provider_warning(self, session):
        seed_market(session)
        fetcher = FakeFetcher({})   # returns None for every scoreboard
        r = build(session, fetcher=fetcher)
        assert example(r, TICKER).mapping_status == MAP_GAP
        assert any("stale_provider" in w for w in r["warnings"])
        assert r["scoreboards_fetched"] == 0

    def test_scoreboard_fetched_once_per_tour_date(self, session):
        seed_market(session, ticker="KXATPMATCH-26JUL09SINALC")
        seed_market(session, ticker="KXATPMATCH-26JUL09DJOMED",
                    title="Djokovic vs Medvedev winner?")
        fetcher = FakeFetcher({("atp", "2026-07-09"): {"events": [
            espn_event(), espn_event("DJO", "MED"),
        ]}})
        r = build(session, fetcher=fetcher)
        assert len(fetcher.fetch_calls) == 1
        assert r["source_backed_count"] == 2

    def test_fetch_bound_respected(self, session):
        for i in range(tls.MAX_SCOREBOARD_FETCHES + 2):
            day = 10 + i
            seed_market(session, ticker=f"KXATPMATCH-26JUL{day}SINALC")
        fetcher = FakeFetcher({})
        r = build(session, fetcher=fetcher)
        assert len(fetcher.fetch_calls) == tls.MAX_SCOREBOARD_FETCHES
        assert any("fetch bound" in n for c in r["examples"] for n in c.notes)

    def test_non_winner_markets_not_fetched(self, session):
        seed_market(session, ticker="KXATPSETWIN-26JUL09SINALC-1", title="set")
        fetcher = FakeFetcher({("atp", "2026-07-09"): {"events": [espn_event()]}})
        r = build(session, fetcher=fetcher)
        assert fetcher.fetch_calls == []
        assert example(r, "KXATPSETWIN-26JUL09SINALC-1").mapping_status == MAP_NOT_WINNER


# --- aggregates / candidacy ------------------------------------------------------------


class TestAggregates:
    def test_live_candidacy_by_recency_and_status(self, session):
        seed_market(session, ticker="KXATPMATCH-26JUL09SINALC", seen_minutes_ago=10)
        seed_market(session, ticker="KXATPMATCH-26JUL09DJOMED",
                    seen_minutes_ago=60 * 48, title="old")
        seed_market(session, ticker="KXATPMATCH-26JUL09RUNFRI",
                    status="settled", seen_minutes_ago=5, title="settled")
        r = build(session, hours=24)
        assert r["total_tennis_markets"] == 3
        assert r["live_candidates"] == 1
        assert r["match_winner_candidates"] == 3

    def test_empty_db_warns(self, session):
        r = build(session)
        assert r["total_tennis_markets"] == 0
        assert any("insufficient_data" in w for w in r["warnings"])


# --- CLI ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_parses(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "tennis_live_source_report", fake)
        rc = cli.main(["tennis-live-source-report", "--top", "3", "--hours", "12"])
        assert rc == 0
        assert captured == {"top": 3, "hours": 12}

    def test_cli_renders_template_mode(self, session, capsys, monkeypatch):
        # force the template path regardless of local env/config
        monkeypatch.setattr(tls, "get_tennis_fetcher", lambda *a, **k: None)
        seed_market(session)
        n = asyncio.run(cli.tennis_live_source_report(session=session))
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "provider_gap" in out
        assert "no probability" in out

    def test_cli_empty(self, session, capsys, monkeypatch):
        monkeypatch.setattr(tls, "get_tennis_fetcher", lambda *a, **k: None)
        n = asyncio.run(cli.tennis_live_source_report(session=session))
        assert n == 0
        assert "total_tennis_markets=0" in capsys.readouterr().out


# --- safety --------------------------------------------------------------------------


class TestSafety:
    def test_persists_nothing(self, session):
        seed_market(session)
        seed_tick(session)
        import sqlalchemy

        tables = ("markets", "market_price_ticks")
        before = {
            t: session.execute(sqlalchemy.text(f"select count(*) from {t}")).scalar()
            for t in tables
        }
        build(session, fetcher=FakeFetcher(
            {("atp", "2026-07-09"): {"events": [espn_event()]}}
        ))
        session.commit()
        after = {
            t: session.execute(sqlalchemy.text(f"select count(*) from {t}")).scalar()
            for t in tables
        }
        assert before == after

    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "tennis_live_source.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "jupiter", "recommend",
                    "execute_trade", "execution", "buy", "sell"):
            assert bad not in code, bad

    def test_no_direct_network_imports(self):
        src = (REPO / "app" / "services" / "tennis_live_source.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src

    def test_note_language(self, session):
        r = build(session)
        assert "never advice" in r["note"]
        assert "not EV" in r["note"]
