"""TENNIS-CANDIDATE-ORDER-001 tests: informative-books-first capture ordering.

Active/two-sided/high-volume/moving books rank first, source-backed boost,
deterministic tie-break, reason labels, match-winner precedence preserved,
scan and tape paths consume the ranking, no persistence changes, no network,
no forbidden vocabulary. In-memory SQLite; fake adapters only.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import Market, MarketPriceTick, TennisTapeLink, TennisTapeRun
from app.schemas import MarketData
from app.services.tennis_watcher import (
    REASON_ACTIVE,
    REASON_FALLBACK,
    REASON_HIGH_VOLUME,
    REASON_RECENT_MOVE,
    REASON_SOURCE_BACKED,
    REASON_TWO_SIDED,
    TennisTickWatcher,
    rank_tennis_candidates,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def seed_market(session, ticker, minutes_ago=10):
    m = Market(
        ticker=ticker, title="m", status="active",
        last_seen_at=NOW - timedelta(minutes=minutes_ago),
        close_time=NOW + timedelta(hours=8),
    )
    session.add(m)
    session.commit()
    return m


def seed_tick(session, ticker, *, minutes_ago=5, mid=0.5, bid=49, ask=51,
              volume=100):
    at = NOW - timedelta(minutes=minutes_ago)
    session.add(MarketPriceTick(
        market_ticker=ticker, observed_at=at, yes_bid=bid, yes_ask=ask,
        midpoint=mid, spread=(ask - bid) if bid is not None and ask is not None else None,
        volume_24h=volume, liquidity_proxy=1000, created_at=at,
    ))
    session.commit()


def rank(session, markets):
    return rank_tennis_candidates(session, markets)


class TestRanking:
    def test_active_two_sided_high_volume_first(self, session):
        quiet = seed_market(session, "KXITFMATCH-26JUL10AAAAAA-AAA")
        hot = seed_market(session, "KXITFMATCH-26JUL10ZZZZZZ-ZZZ")
        seed_tick(session, hot.ticker, minutes_ago=3, volume=500_000)
        out = rank(session, [quiet, hot])
        assert out[0].ticker == hot.ticker          # beats alphabetical order
        assert REASON_ACTIVE in out[0].reasons
        assert REASON_TWO_SIDED in out[0].reasons
        assert REASON_HIGH_VOLUME in out[0].reasons
        assert out[1].reasons == [REASON_FALLBACK]

    def test_recent_move_ranks_above_static(self, session):
        static = seed_market(session, "KXITFMATCH-26JUL10AAAAAA-AAA")
        mover = seed_market(session, "KXITFMATCH-26JUL10ZZZZZZ-ZZZ")
        for t, mids in ((static.ticker, [0.5, 0.5]), (mover.ticker, [0.5, 0.56])):
            seed_tick(session, t, minutes_ago=20, mid=mids[0], volume=100)
            seed_tick(session, t, minutes_ago=3, mid=mids[1], volume=100)
        out = rank(session, [static, mover])
        assert out[0].ticker == mover.ticker
        assert REASON_RECENT_MOVE in out[0].reasons
        assert REASON_RECENT_MOVE not in out[1].reasons

    def test_stale_tick_not_active(self, session):
        stale = seed_market(session, "KXITFMATCH-26JUL10AAAAAA-AAA")
        seed_tick(session, stale.ticker, minutes_ago=45, volume=999_999)
        out = rank(session, [stale])
        assert REASON_ACTIVE not in out[0].reasons
        # still credited for volume/two-sided from its latest (in-window) tick
        assert REASON_HIGH_VOLUME in out[0].reasons

    def test_source_backed_boost(self, session):
        plain = seed_market(session, "KXITFMATCH-26JUL10AAAAAA-AAA")
        backed = seed_market(session, "KXITFMATCH-26JUL10ZZZZZZ-ZZZ")
        run = TennisTapeRun(status="ok", started_at=NOW, created_at=NOW)
        session.add(run)
        session.flush()
        session.add(TennisTapeLink(
            tape_run_id=run.id, market_ticker=backed.ticker,
            link_label="source_backed_link", created_at=NOW,
        ))
        session.commit()
        out = rank(session, [plain, backed])
        assert out[0].ticker == backed.ticker
        assert REASON_SOURCE_BACKED in out[0].reasons

    def test_match_winner_outranks_everything(self, session):
        prop = seed_market(session, "KXITFTOTALGAMES-26JUL10AAAAAA-22")
        seed_tick(session, prop.ticker, minutes_ago=2, volume=1_000_000)
        winner = seed_market(session, "KXITFMATCH-26JUL10ZZZZZZ-ZZZ")
        out = rank(session, [prop, winner])
        assert out[0].ticker == winner.ticker

    def test_deterministic_tie_break_by_ticker(self, session):
        b = seed_market(session, "KXITFMATCH-26JUL10BBBBBB-BBB")
        a = seed_market(session, "KXITFMATCH-26JUL10AAAAAA-AAA")
        out1 = rank(session, [b, a])
        out2 = rank(session, [a, b])
        assert [c.ticker for c in out1] == [c.ticker for c in out2] == [
            a.ticker, b.ticker,
        ]


class FakeAdapter:
    def __init__(self):
        self.calls = []

    async def fetch_markets_by_tickers(self, tickers):
        self.calls.append(list(tickers))
        return [MarketData(ticker=t, title="t", status="active",
                           yes_bid=44, yes_ask=46) for t in tickers]


class TestConsumers:
    def test_scan_targets_use_ranking_and_report_reasons(self, session):
        quiet = seed_market(session, "KXITFMATCH-26JUL10AAAAAA-AAA")
        hot = seed_market(session, "KXITFMATCH-26JUL10ZZZZZZ-ZZZ")
        seed_tick(session, hot.ticker, minutes_ago=2, volume=200_000)
        adapter = FakeAdapter()
        watcher = TennisTickWatcher(adapter=adapter, settings=Settings(_env_file=None))
        r = asyncio.run(watcher.scan_once(session, limit=1, dry_run=True))
        assert adapter.calls == [[hot.ticker]]     # hot book got the only slot
        assert r["top_ordering"][0]["ticker"] == hot.ticker
        assert REASON_HIGH_VOLUME in r["top_ordering"][0]["reasons"]

    def test_tape_capture_uses_ranking(self, session):
        from tests.test_tennis_tape_001 import FakeScoreFetcher
        from app.services.tennis_tape import TennisTapeRecorder

        quiet = seed_market(session, "KXITFMATCH-26JUL10AAAAAA-AAA")
        hot = seed_market(session, "KXITFMATCH-26JUL10ZZZZZZ-ZZZ")
        seed_tick(session, hot.ticker, minutes_ago=2, volume=200_000)
        adapter = FakeAdapter()
        recorder = TennisTapeRecorder(
            score_fetcher=FakeScoreFetcher(), market_adapter=adapter,
        )
        r = asyncio.run(recorder.capture_once(session, limit=1, dry_run=True))
        assert adapter.calls == [[hot.ticker]]
        assert r["top_ordering"][0]["ticker"] == hot.ticker

    def test_ranking_persists_nothing(self, session):
        m = seed_market(session, "KXITFMATCH-26JUL10AAAAAA-AAA")
        seed_tick(session, m.ticker)
        before = session.execute(text("select count(*) from market_price_ticks")).scalar()
        rank(session, [m])
        session.commit()
        after = session.execute(text("select count(*) from market_price_ticks")).scalar()
        assert before == after


class TestSafety:
    def test_no_forbidden_vocab_in_ranking_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "tennis_watcher.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "recommend_trade", "execution",
                    "buy", "sell", "markov"):
            assert bad not in code, bad
