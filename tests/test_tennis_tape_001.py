"""TENNIS-TAPE-001 tests: read-only synchronized tennis tape recorder.

Migration up/down/up round trip, pure link labeling (source_backed / fuzzy /
unresolved / provider_no_match / incompatible), dry-run persists nothing,
capture persists ONLY tape rows (no ticks, no signals, no watcher rows),
score-snapshot dedup, hard score-call cap, provider-gap skip with zero
fetches, key redaction, report rendering, no forbidden vocabulary. Fake
provider + fake market adapter — no real network request is ever made.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import cli
from app.db import Base, run_migrations
from app.models import Market
from app.schemas import MarketData
from app.services import tennis_tape as tt
from app.services.tennis_tape import (
    LINK_FUZZY,
    LINK_INCOMPATIBLE,
    LINK_NO_MATCH,
    LINK_SOURCE_BACKED,
    LINK_UNRESOLVED,
    STATUS_DRY_RUN,
    STATUS_OK,
    STATUS_PROVIDER_GAP,
    TennisTapeRecorder,
    build_tape_report,
    link_candidate,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]
TICKER = "KXATPCHALLENGERMATCH-26JUL10CASAMB-CAS"
DATE = "2026-07-10"


def fixture(first="Hugo Casanova", second="Thiago Ambrosio", key=111,
            status="Set 2", **extra):
    f = {
        "event_key": key,
        "event_first_player": first,
        "event_second_player": second,
        "event_status": status,
        "event_type_type": "Challenger Men Singles",
        "tournament_name": "Test Challenger",
        "event_final_result": "0 - 0",
        "event_game_result": "30 - 15",
        "event_serve": "First Player",
        "scores": [{"score_first": "4", "score_second": "2", "score_set": "1"}],
        "pointbypoint": [{"huge": "array"}],
    }
    f.update(extra)
    return f


class FakeScoreFetcher:
    source_name = "fake-provider.test"
    has_key = True

    def __init__(self, fixtures_by_date=None, fail_dates=(), livescore=None):
        self.fixtures_by_date = fixtures_by_date or {}
        self.fail_dates = set(fail_dates)
        self.livescore = livescore or []
        self.calls = []
        self.livescore_calls = 0

    async def _get(self, params):
        if params.get("method") == "get_livescore":
            self.livescore_calls += 1
            return {"result": self.livescore}
        date = params.get("date_start")
        self.calls.append(date)
        if date in self.fail_dates:
            return None
        return {"result": self.fixtures_by_date.get(date, [])}


class FakeMarketAdapter:
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


def seed_market(session, ticker=TICKER, title="Casanova vs Ambrosio winner?"):
    session.add(Market(
        ticker=ticker, title=title, status="active",
        last_seen_at=NOW - timedelta(minutes=10),
        close_time=NOW + timedelta(hours=8),
    ))
    session.commit()


def recorder(fetcher=None, adapter=None):
    return TennisTapeRecorder(
        score_fetcher=fetcher if fetcher is not None else FakeScoreFetcher(),
        market_adapter=adapter or FakeMarketAdapter(),
    )


def capture(session, rec, **kw):
    return asyncio.run(rec.capture_once(session, **kw))


def counts(session):
    return {
        t: session.execute(text(f"select count(*) from {t}")).scalar()
        for t in ("tennis_tape_runs", "tennis_tape_score_snapshots",
                  "tennis_tape_market_snapshots", "tennis_tape_links",
                  "market_price_ticks", "opportunity_signals", "watcher_runs")
    }


# --- migration -----------------------------------------------------------------------


def _config(url: str) -> Config:
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _tables(url: str) -> set:
    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(text(
            "select name from sqlite_master where type='table'"
        )).fetchall()
    return {r[0] for r in rows}


class TestMigration0025:
    def test_up_down_up_round_trip(self, tmp_path):
        url = f"sqlite:///{tmp_path}/tape.db"
        run_migrations(url)
        tape_tables = {
            "tennis_tape_runs", "tennis_tape_score_snapshots",
            "tennis_tape_market_snapshots", "tennis_tape_links",
        }
        assert tape_tables <= _tables(url)
        command.downgrade(_config(url), "0024")
        assert not (tape_tables & _tables(url))
        command.upgrade(_config(url), "head")
        assert tape_tables <= _tables(url)


# --- pure linking --------------------------------------------------------------------


class TestLinking:
    def test_source_backed_both_codes_and_date(self):
        out = link_candidate(TICKER, {DATE: [fixture()]})
        assert out.label == LINK_SOURCE_BACKED
        assert out.fixture["event_key"] == 111

    def test_provider_no_match(self):
        out = link_candidate(TICKER, {DATE: [fixture("Ana Diaz", "Mia Solis")]})
        assert out.label == LINK_NO_MATCH

    def test_fuzzy_single_one_sided(self):
        out = link_candidate(
            TICKER, {DATE: [fixture(second="Someone Else")]}
        )
        assert out.label == LINK_FUZZY
        assert "one-sided" in out.basis

    def test_fuzzy_when_ambiguous_exact(self):
        out = link_candidate(
            TICKER, {DATE: [fixture(key=1), fixture(key=2)]}
        )
        assert out.label == LINK_FUZZY
        assert "ambiguous" in out.basis

    def test_unresolved_date_not_fetched(self):
        out = link_candidate(TICKER, {})
        assert out.label == LINK_UNRESOLVED
        assert "cap" in out.basis

    def test_incompatible_market_type(self):
        out = link_candidate("KXATPSETWIN-26JUL10CASAMB-1", {DATE: [fixture()]})
        assert out.label == LINK_INCOMPATIBLE

    def test_adjacent_date_link_matoch_regression(self):
        # measured live 2026-07-10: Kalshi KXITFMATCH-26JUL09MATOCH traded
        # actively while the provider listed Matsuda vs Ochi under 2026-07-10
        out = link_candidate(
            "KXITFMATCH-26JUL09MATOCH-MAT",
            {"2026-07-09": [fixture("Ana Diaz", "Mia Solis")],
             "2026-07-10": [fixture("K. Matsuda", "M. Ochi", key=222)]},
        )
        assert out.label == LINK_SOURCE_BACKED
        assert "adjacent date (2026-07-10)" in out.basis
        assert out.fixture["event_key"] == 222

    def test_exact_date_wins_over_adjacent(self):
        out = link_candidate(
            TICKER,
            {DATE: [fixture(key=1)], "2026-07-11": [fixture(key=2)]},
        )
        assert out.fixture["event_key"] == 1
        assert out.basis == "both player codes + date"

    def test_no_cross_date_fuzzy(self):
        # a one-sided match on an ADJACENT date must not produce fuzzy
        out = link_candidate(
            TICKER,
            {DATE: [], "2026-07-11": [fixture(second="Someone Else")]},
        )
        assert out.label == LINK_NO_MATCH


# --- capture -------------------------------------------------------------------------


class TestCapture:
    def test_dry_run_persists_nothing(self, session):
        seed_market(session)
        r = capture(session, recorder(
            FakeScoreFetcher({DATE: [fixture()]})
        ), dry_run=True)
        assert r["status"] == STATUS_DRY_RUN
        assert r["links"] == {LINK_SOURCE_BACKED: 1}
        assert all(v == 0 for v in counts(session).values())

    def test_capture_persists_only_tape_rows(self, session):
        seed_market(session)
        r = capture(session, recorder(FakeScoreFetcher({DATE: [fixture()]})))
        assert r["status"] == STATUS_OK
        c = counts(session)
        assert c["tennis_tape_runs"] == 1
        assert c["tennis_tape_score_snapshots"] == 1
        assert c["tennis_tape_market_snapshots"] == 1
        assert c["tennis_tape_links"] == 1
        # never ticks, signals, or watcher rows
        assert c["market_price_ticks"] == 0
        assert c["opportunity_signals"] == 0
        assert c["watcher_runs"] == 0

    def test_score_snapshot_fields_and_bulk_stripped(self, session):
        seed_market(session)
        capture(session, recorder(FakeScoreFetcher({DATE: [fixture()]})))
        row = session.execute(text(
            "select match_status, match_state, serving, provider_event_id, "
            "raw_payload from tennis_tape_score_snapshots"
        )).one()
        assert row[0] == "Set 2"
        assert row[1] == "in"
        assert row[2] == "First Player"
        assert row[3] == "111"
        assert "pointbypoint" not in (row[4] or "")

    def test_two_markets_same_match_share_score_snapshot(self, session):
        seed_market(session, ticker="KXATPCHALLENGERMATCH-26JUL10CASAMB-CAS")
        seed_market(session, ticker="KXATPCHALLENGERMATCH-26JUL10CASAMB-AMB",
                    title="other side")
        r = capture(session, recorder(FakeScoreFetcher({DATE: [fixture()]})))
        assert r["links"] == {LINK_SOURCE_BACKED: 2}
        c = counts(session)
        assert c["tennis_tape_score_snapshots"] == 1     # deduped by event
        assert c["tennis_tape_market_snapshots"] == 2
        assert c["tennis_tape_links"] == 2

    def test_link_delta_recorded(self, session):
        seed_market(session)
        capture(session, recorder(FakeScoreFetcher({DATE: [fixture()]})))
        delta = session.execute(text(
            "select score_to_market_delta_s from tennis_tape_links"
        )).scalar()
        assert delta is not None

    def test_provider_gap_skip_fetches_nothing(self, session):
        seed_market(session)
        fetcher = FakeScoreFetcher()
        fetcher.has_key = False
        adapter = FakeMarketAdapter()
        r = capture(session, recorder(fetcher, adapter))
        assert r["status"] == STATUS_PROVIDER_GAP
        assert fetcher.calls == []
        assert adapter.calls == []
        assert all(v == 0 for v in counts(session).values())

    def test_score_call_hard_cap(self, session):
        for day in range(10, 10 + tt.MAX_SCORE_CALLS + 4):
            seed_market(session, ticker=f"KXATPCHALLENGERMATCH-26JUL{day}CASAMB-CAS",
                        title=f"d{day}")
        fetcher = FakeScoreFetcher()
        r = capture(session, recorder(fetcher), dry_run=True)
        assert len(fetcher.calls) == tt.MAX_SCORE_CALLS      # fixture calls capped
        assert fetcher.livescore_calls == 1                  # exactly one overlay
        assert r["score_calls"] == tt.MAX_SCORE_CALLS + tt.LIVESCORE_CALLS
        # candidates beyond the cap (and its +/-1-day reach) stay unresolved
        assert r["links"].get(LINK_UNRESOLVED, 0) >= 2

    def test_failed_score_fetch_leaves_unresolved(self, session):
        seed_market(session)
        r = capture(session, recorder(
            FakeScoreFetcher(fail_dates={DATE})
        ), dry_run=True)
        assert r["links"] == {LINK_UNRESOLVED: 1}

    def test_no_targets(self, session):
        r = capture(session, recorder())
        assert r["status"] == "no_targets"

    def test_livescore_overlay_replaces_stale_fixture_state(self, session):
        # fixtures endpoint lags in-play state (measured live); the livescore
        # row for the same event_key must win
        seed_market(session)
        stale = fixture(status="", key=111)
        stale["event_live"] = "0"
        live = fixture(status="Set 2", key=111)
        live["event_live"] = "1"
        live["event_date"] = DATE
        r = capture(session, recorder(FakeScoreFetcher(
            {DATE: [stale]}, livescore=[live],
        )))
        assert r["links"] == {LINK_SOURCE_BACKED: 1}
        state = session.execute(text(
            "select match_state, match_status from tennis_tape_score_snapshots"
        )).one()
        assert tuple(state) == ("in", "Set 2")

    def test_livescore_only_event_links_via_own_date_bucket(self, session):
        # a live match whose date bucket was never fetched still links,
        # because the livescore row creates its own bucket
        seed_market(session, ticker="KXITFMATCH-26JUL09MATOCH-MAT",
                    title="Matsuda vs Ochi winner?")
        live = fixture("K. Matsuda", "M. Ochi", key=333, status="Set 1")
        live["event_date"] = "2026-07-10"
        r = capture(session, recorder(FakeScoreFetcher(
            {"2026-07-09": []}, livescore=[live],
        )))
        assert r["links"] == {LINK_SOURCE_BACKED: 1}


# --- report --------------------------------------------------------------------------


class TestReport:
    def test_report_after_capture(self, session):
        seed_market(session)
        capture(session, recorder(FakeScoreFetcher({DATE: [fixture()]})))
        r = build_tape_report(session, hours=24)
        assert r["tape_runs"] == 1
        assert r["links_total"] == 1
        assert r["source_backed_rate"] == 1.0
        assert r["quote_coverage"] == 1.0
        assert r["in_play_score_snapshots"] == 1
        assert r["mean_score_to_market_delta_s"] is not None
        assert r["linked_examples"][0]["ticker"] == TICKER
        assert "measurement only" in r["disclaimer"]

    def test_empty_report(self, session):
        r = build_tape_report(session)
        assert r["tape_runs"] == 0
        assert r["source_backed_rate"] is None
        assert r["provider_gaps"]


# --- CLI ---------------------------------------------------------------------------


class TestCLI:
    def test_capture_cli_parses(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "tennis_tape_capture_once", fake)
        rc = cli.main([
            "tennis-tape-capture-once", "--limit", "30", "--hours", "12", "--dry-run",
        ])
        assert rc == 0
        assert captured == {"limit": 30, "hours": 12, "dry_run": True}

    def test_report_cli_renders(self, session, capsys):
        seed_market(session)
        capture(session, recorder(FakeScoreFetcher({DATE: [fixture()]})))
        n = asyncio.run(cli.tennis_tape_report(session=session))
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "source_backed_rate=" in out
        assert "measurement only" in out

    def test_capture_cli_renders_and_redacts(self, session, capsys, monkeypatch):
        import app.services.tennis_tape as tape_mod

        seed_market(session)
        monkeypatch.setattr(
            tape_mod, "TennisTapeRecorder",
            lambda: recorder(
                FakeScoreFetcher({DATE: [fixture()]}), FakeMarketAdapter()
            ),
        )
        n = asyncio.run(cli.tennis_tape_capture_once(session=session, dry_run=True))
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "APIkey" not in out and "api_key" not in out


# --- safety --------------------------------------------------------------------------


class TestSafety:
    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "tennis_tape.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "jupiter", "recommend",
                    "execute_trade", "execution", "buy", "sell", "markov",
                    "odds"):
            assert bad not in code, bad

    def test_no_direct_network_imports(self):
        src = (REPO / "app" / "services" / "tennis_tape.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src

    def test_note_language(self, session):
        r = build_tape_report(session)
        assert "never advice" in r["note"]
        assert "not EV" in r["note"]
