"""LIVE-MARKET-001 tests: read-only live market/state observation.

Quote-quality and volatility bucket math, freshness/latency diagnostics,
missing-provider fallback (template_only + provider_gap, never fabricated),
stale-provider warnings, tennis state extraction from persisted research-packet
facts, player parsing, report rendering, no persistence, no network, no
forbidden vocabulary. In-memory SQLite; nothing live is touched.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import Market, MarketPriceTick, MarketResearchPacket
from app.services.live_market_state import (
    CALM,
    INSUFFICIENT,
    STATUS_MARKET_ONLY,
    STATUS_STALE,
    STATUS_STATE_BACKED,
    VOLATILE,
    LiveMarketStateReportService,
    TickPoint,
    classify_volatility,
    extract_tennis_state,
    parse_players,
    quote_instability,
    quote_quality,
    window_move,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]
TENNIS_TICKER = "KXATPMATCH-26JUL09SINALC-SIN"


# --- pure diagnostics -----------------------------------------------------------------


class TestQuoteQuality:
    def test_buckets(self):
        assert quote_quality(49, 51, 2, 100) == "tight"
        assert quote_quality(48, 52, 4, 100) == "moderate"
        assert quote_quality(45, 55, 10, 100) == "wide"
        assert quote_quality(None, 51, 2, 100) == "missing_quotes"
        assert quote_quality(49, None, None, None) == "missing_quotes"

    def test_spread_derived_from_touches_when_missing(self):
        assert quote_quality(45, 55, None, 100) == "wide"


def points(*pairs):
    """pairs: (minutes_ago, mid[, spread])"""
    return [
        TickPoint(
            at=NOW - timedelta(minutes=m), mid=mid,
            spread=(p[2] if len(p) > 2 else 1), liquidity=1000,
        )
        for p in pairs
        for m, mid in [(p[0], p[1])]
    ]


class TestVolatilityMath:
    def test_window_move(self):
        pts = points((9, 0.50), (4, 0.52), (1, 0.55))
        assert window_move(pts, NOW, 5) == pytest.approx(0.03)   # 0.52 -> 0.55
        assert window_move(pts, NOW, 10) == pytest.approx(0.05)  # 0.50 -> 0.55

    def test_window_move_insufficient_ticks(self):
        assert window_move(points((1, 0.55)), NOW, 5) is None
        assert window_move([], NOW, 5) is None

    def test_quote_instability(self):
        pts = points((8, 0.50), (6, 0.50), (4, 0.52), (2, 0.52), (1, 0.55))
        # 2 changes over 4 consecutive pairs
        assert quote_instability(pts, NOW, 10) == pytest.approx(0.5)

    def test_classify_volatile_by_move(self):
        label, reason = classify_volatility(None, 0.04, 0.02, 0.04, 0)
        assert label == VOLATILE
        assert "move_5m" in reason

    def test_classify_volatile_by_spread_widening(self):
        label, reason = classify_volatility(None, 0.0, 0.0, 0.0, 3)
        assert label == VOLATILE
        assert "spread widened" in reason

    def test_classify_calm(self):
        label, _ = classify_volatility(0.0, 0.01, 0.02, 0.02, 0)
        assert label == CALM

    def test_classify_insufficient(self):
        label, _ = classify_volatility(None, None, None, None, None)
        assert label == INSUFFICIENT


class TestTennisParsing:
    def test_parse_players_variants(self):
        assert parse_players("Will Sinner beat Alcaraz?") == ("Sinner", "Alcaraz")
        assert parse_players("Sinner vs Alcaraz winner?") == ("Sinner", "Alcaraz")
        assert parse_players("Sinner to beat Alcaraz") == ("Sinner", "Alcaraz")

    def test_parse_players_unknown_shape(self):
        assert parse_players("Total games over 22.5?") == (None, None)
        assert parse_players(None) == (None, None)

    def test_extract_state_from_packet_fact(self):
        facts = [{
            "fact": "Match state: Sinner vs Alcaraz — sets 1-0 "
                    "(games [6, 3] vs [4, 2]); In Progress",
            "confidence": 0.95, "source_name": "site.api.espn.com",
        }]
        state = extract_tennis_state(facts)
        assert state["set_score"] == "1-0"
        assert state["game_score"] == "3-2"
        assert state["match_status"] == "In Progress"
        assert state["point_score"] is None   # never fabricated

    def test_extract_state_empty(self):
        state = extract_tennis_state(None)
        assert all(v is None for v in state.values())


# --- end-to-end over DB ---------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def seed_market(session, ticker=TENNIS_TICKER, title="Will Sinner beat Alcaraz?"):
    session.add(Market(ticker=ticker, title=title, status="active"))
    session.commit()


def seed_ticks(session, ticker=TENNIS_TICKER, *, mids, minutes, spread=1):
    for mid, m in zip(mids, minutes):
        at = NOW - timedelta(minutes=m)
        session.add(MarketPriceTick(
            market_ticker=ticker, observed_at=at,
            yes_bid=int(mid * 100) - 1, yes_ask=int(mid * 100) + 1,
            midpoint=mid, spread=spread, volume_24h=10,
            liquidity_proxy=500_000, created_at=at,
        ))
    session.commit()


def seed_packet(session, ticker=TENNIS_TICKER, *, collector="tennis-external",
                minutes_ago=5, facts=None):
    session.add(MarketResearchPacket(
        market_ticker=ticker, collector_name=collector, collector_version="v1",
        domain="sports_tennis",
        key_facts=facts if facts is not None else [{
            "fact": "Match state: Sinner vs Alcaraz — sets 1-0 "
                    "(games [6, 3] vs [4, 2]); In Progress",
            "confidence": 0.95, "source_name": "site.api.espn.com",
        }],
        missing_info=[], research_completeness_score=0.8, research_risk="low",
        created_at=NOW - timedelta(minutes=minutes_ago),
    ))
    session.commit()


def build(session, **kw):
    return LiveMarketStateReportService().build(session, **kw)


class TestEndToEnd:
    def test_source_backed_state_when_packet_exists(self, session):
        seed_market(session)
        seed_ticks(session, mids=[0.50, 0.52, 0.55], minutes=[9, 4, 1])
        seed_packet(session, minutes_ago=5)
        r = build(session)
        assert r["live_candidates"] == 1
        o = r["observations"][0]
        assert o.tennis["source"] == "source_backed"
        assert o.tennis["set_score"] == "1-0"
        assert o.tennis["player_a"] == "Sinner"
        assert o.live_observation_status == STATUS_STATE_BACKED
        assert o.score_to_market_lag_s is not None
        assert r["state_backed_count"] == 1
        assert r["provider_gaps"] == []

    def test_missing_provider_falls_back_to_template_only(self, session):
        seed_market(session)
        seed_ticks(session, mids=[0.50, 0.52], minutes=[4, 1])
        r = build(session)
        o = r["observations"][0]
        assert o.tennis["source"] == "template_only"
        assert "provider_gap" in o.tennis["provenance"]["note"]
        assert o.tennis["set_score"] is None            # never fabricated
        assert "set_score" in o.tennis["missing_info"]
        assert any("provider_gap" in g for g in r["provider_gaps"])
        assert o.live_observation_status == STATUS_MARKET_ONLY

    def test_template_packet_is_not_source_backed(self, session):
        seed_market(session)
        seed_ticks(session, mids=[0.50, 0.52], minutes=[4, 1])
        seed_packet(session, collector="template", facts=[])
        r = build(session)
        assert r["observations"][0].tennis["source"] == "template_only"

    def test_stale_provider_warning(self, session):
        seed_market(session)
        seed_ticks(session, mids=[0.50, 0.52], minutes=[25, 20])   # >15m old
        r = build(session)
        o = r["observations"][0]
        assert o.live_observation_status == STATUS_STALE
        assert any("stale_provider" in w for w in o.warnings)
        assert any("stale_provider" in w for w in r["warnings"])

    def test_market_freshness_measured(self, session):
        seed_market(session)
        seed_ticks(session, mids=[0.50, 0.52], minutes=[4, 1])
        o = build(session)["observations"][0]
        # seeded 1m before module import; generous upper bound so a slow,
        # loaded suite run cannot flake this
        assert 30 <= o.market_freshness_s <= 600
        assert o.quote_quality == "tight"

    def test_volatile_market_surfaces_in_examples(self, session):
        seed_market(session)
        seed_ticks(session, mids=[0.50, 0.58], minutes=[4, 1])   # +0.08 in 5m
        r = build(session)
        assert r["observations"][0].volatility_label == VOLATILE
        assert r["volatile_examples"][0]["ticker"] == TENNIS_TICKER

    def test_empty_domain_reports_insufficient(self, session):
        r = build(session)
        assert r["live_candidates"] == 0
        assert any("insufficient_live_data" in w for w in r["warnings"])

    def test_domain_filter_excludes_other_domains(self, session):
        seed_market(session, ticker="KXMLBTOTAL-26JUL09AAA-7", title="Total?")
        seed_ticks(session, ticker="KXMLBTOTAL-26JUL09AAA-7",
                   mids=[0.50, 0.52], minutes=[4, 1])
        r = build(session, domain="sports_tennis")
        assert r["live_candidates"] == 0
        r2 = build(session, domain="sports_baseball")
        assert r2["live_candidates"] == 1
        assert r2["observations"][0].tennis is None   # scaffold is tennis-only


# --- CLI ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_parses(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "live_market_state_report", fake)
        rc = cli.main([
            "live-market-state-report", "--domain", "sports_tennis",
            "--top", "3", "--hours", "2",
        ])
        assert rc == 0
        assert captured == {"domain": "sports_tennis", "top": 3, "hours": 2}

    def test_cli_renders(self, session, capsys):
        seed_market(session)
        seed_ticks(session, mids=[0.50, 0.52], minutes=[4, 1])
        n = asyncio.run(cli.live_market_state_report(session=session))
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "tennis[template_only]" in out
        assert "not EV" in out
        assert "diagnostic labels, not signals" in out or "observation" in out

    def test_cli_empty(self, session, capsys):
        n = asyncio.run(cli.live_market_state_report(session=session))
        assert n == 0
        assert "live_candidates=0" in capsys.readouterr().out


# --- safety --------------------------------------------------------------------------


class TestSafety:
    def test_persists_nothing(self, session):
        seed_market(session)
        seed_ticks(session, mids=[0.50, 0.52], minutes=[4, 1])
        seed_packet(session)
        import sqlalchemy

        tables = ("markets", "market_price_ticks", "market_research_packets")
        before = {
            t: session.execute(sqlalchemy.text(f"select count(*) from {t}")).scalar()
            for t in tables
        }
        build(session)
        session.commit()
        after = {
            t: session.execute(sqlalchemy.text(f"select count(*) from {t}")).scalar()
            for t in tables
        }
        assert before == after

    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "live_market_state.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "jupiter", "recommend",
                    "execute_trade", "execution", "place_order", "buy", "sell"):
            assert bad not in code, bad

    def test_no_live_network(self):
        src = (REPO / "app" / "services" / "live_market_state.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket", "espn_api"):
            assert net not in src

    def test_note_language(self, session):
        r = build(session)
        assert "never advice" in r["note"]
        assert "not EV" in r["note"] or "Not EV" in r["note"]
