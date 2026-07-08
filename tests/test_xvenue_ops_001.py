"""XVENUE-OPS-001 tests: recency-aware Kalshi loading for cross-venue matching.

The default `cross-venue-match-once` used to load active Kalshi markets in rowid
order — on a long-running DB that is the OLDEST-inserted (stale) rows, which do
not overlap a freshly-scanned Polymarket sample. These tests pin the fix:
most-recently-seen first, an optional recency window, domain / market-type
sample filters, and a transparent sample-composition report. This is read-only
usability/coverage only — it changes WHICH persisted rows are considered, never
the matcher's labels, gates, or precision. No forbidden vocabulary/fields; no
live network; in-memory SQLite.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import CrossVenueMarketCandidate, Market, MarketSnapshot, PolymarketMarket
from app.services.cross_venue import (
    LABEL_COMPARABLE,
    CrossVenueMatchingService,
    MatchSampleComposition,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def kalshi(session, ticker, title, *, bid=20, ask=24, liq=5000, days=5,
           category="Sports", status="active", last_seen=None, snapshot=True):
    m = Market(ticker=ticker, event_ticker=ticker.split("-")[0], title=title,
               category=category, status=status, close_time=NOW + timedelta(days=days),
               last_seen_at=last_seen or NOW, first_seen_at=last_seen or NOW)
    session.add(m)
    session.flush()
    if snapshot:
        session.add(MarketSnapshot(market_id=m.id, yes_bid=bid, yes_ask=ask, liquidity=liq,
                                   volume_24h=liq, captured_at=last_seen or NOW))
    return m


def poly(session, mid, question, *, bb=0.19, ba=0.21, days=5, category="World Cup Winner"):
    session.add(PolymarketMarket(
        market_id=mid, condition_id="0x" + mid, question=question, category=category,
        best_bid=bb, best_ask=ba, spread=round(ba - bb, 4), liquidity_usd=9000,
        outcomes=["Yes", "No"], outcome_prices=[round((bb + ba) / 2, 4), round(1 - (bb + ba) / 2, 4)],
        clob_token_ids=["t" + mid], end_date=NOW + timedelta(days=days), active=True, observed_at=NOW,
    ))


def sample(run) -> MatchSampleComposition:
    return run._sample


# --- recency-aware loading ---------------------------------------------------


class TestRecencyLoading:
    def test_default_load_prefers_recent_over_rowid_order(self, session):
        """Insert a STALE market first (lower rowid) and a FRESH one second. The
        old code returned the stale one first (rowid); recency must return fresh
        first, so with limit=1 only the fresh market is considered."""
        kalshi(session, "KXOLD-1", "Old stale market",
               last_seen=NOW - timedelta(days=6))
        kalshi(session, "KXNEW-1", "New fresh market", last_seen=NOW)
        session.commit()

        views, mode, _ = CrossVenueMatchingService()._load_kalshi(session, limit=1)
        assert [v.ticker for v in views] == ["KXNEW-1"]
        assert mode == "recent_active"

    def test_full_recency_ordering(self, session):
        kalshi(session, "KXA", "A", last_seen=NOW - timedelta(hours=1))
        kalshi(session, "KXB", "B", last_seen=NOW - timedelta(hours=50))
        kalshi(session, "KXC", "C", last_seen=NOW - timedelta(hours=10))
        session.commit()

        views, _, _ = CrossVenueMatchingService()._load_kalshi(session, limit=10)
        assert [v.ticker for v in views] == ["KXA", "KXC", "KXB"]

    def test_recent_hours_window_drops_stale_and_counts_them(self, session):
        kalshi(session, "KXFRESH", "fresh", last_seen=NOW - timedelta(hours=2))
        kalshi(session, "KXSTALE1", "stale one", last_seen=NOW - timedelta(hours=100))
        kalshi(session, "KXSTALE2", "stale two", last_seen=NOW - timedelta(hours=200))
        session.commit()

        views, mode, stale = CrossVenueMatchingService()._load_kalshi(
            session, limit=10, recent_hours=48)
        assert [v.ticker for v in views] == ["KXFRESH"]
        assert stale == 2
        assert mode == "recent_active"

    def test_recent_hours_window_empty_falls_back_to_active(self, session):
        """If the window excludes everything, fall back to active-by-recency so
        the command still returns a sample."""
        kalshi(session, "KXONLYSTALE", "stale", last_seen=NOW - timedelta(days=30))
        session.commit()

        views, mode, _ = CrossVenueMatchingService()._load_kalshi(
            session, limit=10, recent_hours=1)
        assert [v.ticker for v in views] == ["KXONLYSTALE"]
        assert mode == "recent_active_no_window"

    def test_no_active_markets_falls_back_to_any_status(self, session):
        kalshi(session, "KXCLOSED", "closed market", status="finalized",
               last_seen=NOW - timedelta(hours=3))
        session.commit()

        views, mode, _ = CrossVenueMatchingService()._load_kalshi(session, limit=10)
        assert [v.ticker for v in views] == ["KXCLOSED"]
        assert mode == "any_status_recent"

    def test_limit_is_bounded(self, session):
        for i in range(6):
            kalshi(session, f"KX{i}", f"m{i}", last_seen=NOW - timedelta(hours=i))
        session.commit()

        views, _, _ = CrossVenueMatchingService()._load_kalshi(session, limit=3)
        assert len(views) == 3
        assert [v.ticker for v in views] == ["KX0", "KX1", "KX2"]  # freshest 3


# --- sample composition + representative default -----------------------------


class TestSampleComposition:
    def test_default_run_surfaces_recent_overlap(self, session):
        # a STALE non-overlapping market (older rowid) + a FRESH matching one
        kalshi(session, "KXMLB-OLD", "Old baseball total over 8.5",
               last_seen=NOW - timedelta(days=7), category="MLB")
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup",
               bid=20, ask=24, last_seen=NOW)
        poly(session, "PMF", "Will France win the 2026 FIFA World Cup?", bb=0.19, ba=0.21)
        session.commit()

        # limit=1: recency must pick the fresh France market, producing a comparable
        run = CrossVenueMatchingService().match_once(session, kalshi_limit=1, polymarket_limit=50)
        assert run.comparable_count == 1
        assert session.query(CrossVenueMarketCandidate).one().match_label == LABEL_COMPARABLE

    def test_composition_reports_counts_and_breakdowns(self, session):
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup", last_seen=NOW)
        kalshi(session, "KXPRES-X", "Presidential election winner 2028",
               category="Politics", last_seen=NOW)
        poly(session, "PMF", "Will France win the 2026 FIFA World Cup?")
        session.commit()

        run = CrossVenueMatchingService().match_once(session)
        s = sample(run)
        assert s.kalshi_loaded == 2 and s.kalshi_considered == 2
        assert s.polymarket_loaded == 1
        assert "sports" in s.kalshi_by_domain and "politics" in s.kalshi_by_domain
        assert "winner" in s.kalshi_by_market_type
        assert s.kalshi_load_mode == "recent_active"

    def test_composition_reports_domain_overlap(self, session):
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup", last_seen=NOW)
        poly(session, "PMF", "Will France win the 2026 FIFA World Cup?")
        session.commit()

        s = sample(CrossVenueMatchingService().match_once(session))
        assert "sports" in s.overlap_domains

    def test_low_overlap_flagged_when_no_shared_domain(self, session):
        kalshi(session, "KXPRES-X", "Presidential election winner", category="Politics",
               last_seen=NOW)
        poly(session, "PMB", "Will Bitcoin hit 200k in 2026?", category="Crypto")
        session.commit()

        s = sample(CrossVenueMatchingService().match_once(session))
        assert s.low_overlap is True

    def test_low_overlap_flagged_when_zero_candidates(self, session):
        # shared domain but titles too dissimilar to surface any candidate
        kalshi(session, "KXWC-ZZ", "Xylophone quux", last_seen=NOW)
        poly(session, "PMB", "Will France win the World Cup?")
        session.commit()

        s = sample(CrossVenueMatchingService().match_once(session))
        assert s.low_overlap is True

    def test_no_snapshot_market_counted_unavailable(self, session):
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup",
               last_seen=NOW, snapshot=False)
        poly(session, "PMF", "Will France win the 2026 FIFA World Cup?")
        session.commit()

        s = sample(CrossVenueMatchingService().match_once(session))
        assert s.kalshi_without_snapshot == 1


# --- sample filters ----------------------------------------------------------


class TestSampleFilters:
    def _seed(self, session):
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup", last_seen=NOW)
        kalshi(session, "KXPRES-X", "Presidential election winner 2028",
               category="Politics", last_seen=NOW)
        poly(session, "PMF", "Will France win the 2026 FIFA World Cup?")
        poly(session, "PMP", "Presidential election winner 2028", category="Presidential Election")
        session.commit()

    def test_domain_filter_narrows_both_sides(self, session):
        self._seed(session)
        run = CrossVenueMatchingService().match_once(session, domain="sports")
        s = sample(run)
        assert set(s.kalshi_by_domain) == {"sports"}
        assert set(s.polymarket_by_domain) <= {"sports"}
        assert s.domain_filter == "sports"

    def test_market_type_filter_narrows_sample(self, session):
        self._seed(session)
        run = CrossVenueMatchingService().match_once(session, market_type="winner")
        s = sample(run)
        assert set(s.kalshi_by_market_type) == {"winner"}
        assert s.market_type_filter == "winner"

    def test_recent_hours_filter_flows_through_match_once(self, session):
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup", last_seen=NOW)
        kalshi(session, "KXWC-STALE", "Old world cup thing",
               last_seen=NOW - timedelta(days=10))
        poly(session, "PMF", "Will France win the 2026 FIFA World Cup?")
        session.commit()

        s = sample(CrossVenueMatchingService().match_once(session, recent_hours=48))
        assert s.recent_hours == 48
        assert s.kalshi_stale_skipped == 1
        assert s.kalshi_considered == 1


# --- precision unchanged -----------------------------------------------------


class TestPrecisionUnchanged:
    def test_filters_do_not_force_or_relax_matches(self, session):
        """Narrowing to a domain must not turn an incompatible pair comparable —
        the matcher and its gates are untouched. High title overlap here isolates
        the scope gate (game vs tournament future) as the sole rejection."""
        kalshi(session, "KXWCGAME-1", "Will France win the World Cup match vs Morocco?",
               last_seen=NOW)                                        # scope=game ("vs")
        poly(session, "PMW", "Will France win the World Cup?")       # scope=tournament_future
        session.commit()

        run = CrossVenueMatchingService().match_once(session, domain="sports")
        c = session.query(CrossVenueMarketCandidate).one()           # candidate IS recorded
        assert c.match_label != LABEL_COMPARABLE                     # scope gate still fires
        assert "market_type_mismatch" in " ".join(c.mismatch_reasons or [])
        assert c.observed_difference is None

    def test_recency_changes_selection_not_labels(self, session):
        """A comparable pair stays comparable regardless of recency ordering."""
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup",
               bid=20, ask=24, last_seen=NOW)
        poly(session, "PMF", "Will France win the 2026 FIFA World Cup?", bb=0.19, ba=0.21)
        session.commit()

        c = session.query(CrossVenueMarketCandidate)
        CrossVenueMatchingService().match_once(session)
        row = c.one()
        assert row.match_label == LABEL_COMPARABLE
        assert row.observed_difference == pytest.approx(0.02)


# --- CLI ---------------------------------------------------------------------


class TestCLI:
    def test_cli_parses_new_options(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 3

        monkeypatch.setattr(cli, "cross_venue_match_once", fake)
        rc = cli.main([
            "cross-venue-match-once", "--kalshi-limit", "8000", "--polymarket-limit", "600",
            "--recent-hours", "48", "--domain", "sports", "--market-type", "winner",
        ])
        assert rc == 0
        assert captured["kalshi_limit"] == 8000
        assert captured["polymarket_limit"] == 600
        assert captured["recent_hours"] == 48
        assert captured["domain"] == "sports"
        assert captured["market_type"] == "winner"

    def test_cli_defaults(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 0

        monkeypatch.setattr(cli, "cross_venue_match_once", fake)
        cli.main(["cross-venue-match-once"])
        assert captured["recent_hours"] is None
        assert captured["domain"] is None
        assert captured["market_type"] is None
        # XVENUE-OPS-001 raised the defaults so the no-arg run is representative
        # without magic limits (still bounded).
        assert captured["kalshi_limit"] == 4000
        assert captured["polymarket_limit"] == 500

    def test_cli_prints_sample_composition(self, session, capsys):
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup", last_seen=NOW)
        poly(session, "PMF", "Will France win the 2026 FIFA World Cup?")
        session.commit()

        import asyncio

        asyncio.run(cli.cross_venue_match_once(session=session))
        out = capsys.readouterr().out
        assert "sample:" in out
        assert "kalshi by domain" in out
        assert "domain overlap" in out

    def test_cli_low_overlap_note_is_coverage_language_not_signal(self, session, capsys):
        kalshi(session, "KXPRES-X", "Presidential election", category="Politics", last_seen=NOW)
        poly(session, "PMB", "Will Bitcoin hit 200k?", category="Crypto")
        session.commit()

        import asyncio

        asyncio.run(cli.cross_venue_match_once(session=session))
        out = capsys.readouterr().out
        # scope the vocabulary check to the note itself — the standard run line
        # legitimately says "not arbitrage, not EV" as a disclaimer.
        note = next(ln for ln in out.splitlines() if "low sample overlap" in ln.lower())
        for bad in ("arbitrage", "opportunity", "edge", "profit", "buy", "sell"):
            assert bad not in note.lower()


# --- safety ------------------------------------------------------------------


class TestSafety:
    def test_composition_has_no_forbidden_fields(self):
        fields = set(MatchSampleComposition.__annotations__)
        for bad in ("ev", "expected_value", "side", "size", "profit", "edge",
                    "arbitrage", "arb", "opportunity", "order", "wallet",
                    "observed_difference", "recommendation"):
            assert bad not in fields

    def test_new_code_has_no_forbidden_vocab(self):
        """Forbidden vocabulary must not appear in EXECUTABLE identifiers/operators.
        String literals (disclaimers legitimately say "NOT arbitrage") and comments
        are stripped via tokenize so only real code surface is checked."""
        import io
        import tokenize

        src = (REPO / "app" / "services" / "cross_venue.py").read_text()
        code_tokens = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                continue
            code_tokens.append(tok.string.lower())
        code = " ".join(code_tokens)
        for bad in ("arbitrage", "opportunity", "expected_value", "paper_trad",
                    "place_order", "wallet", "private_key", "kelly", "position_siz"):
            assert bad not in code, f"forbidden vocab {bad!r} in executable code"

    def test_no_live_network_in_matcher(self):
        src = (REPO / "app" / "services" / "cross_venue.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src
