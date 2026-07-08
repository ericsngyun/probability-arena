"""POLY-002 tests: read-only Kalshi<->Polymarket cross-venue observation.
Title/outcome normalization, compatible-winner match, incompatible-outcome and
resolution rejection, low-confidence/unresolved, midpoint-difference measurement,
match-once + report, migration up/down, and — critically — NO forbidden
arbitrage/trade/EV/side/size labels or fields. No live network; in-memory SQLite."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (
    CrossVenueMarketCandidate,
    Market,
    MarketSnapshot,
    PolymarketMarket,
)
from app.services.cross_venue import (
    LABEL_COMPARABLE,
    LABEL_INCOMPATIBLE_OUTCOME,
    LABEL_INCOMPATIBLE_RESOLUTION,
    LABEL_LOW_CONFIDENCE,
    LABEL_UNRESOLVED,
    CrossVenueMatchingService,
    CrossVenueReportService,
    coarse_domain,
    normalize_outcome,
    normalize_title,
    outcomes_compatible,
)

NOW = datetime.now(timezone.utc)
REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def kalshi(session, ticker, title, *, bid=20, ask=24, liq=5000, days=25, category="Sports"):
    m = Market(ticker=ticker, event_ticker=ticker.split("-")[0], title=title, category=category,
               status="active", close_time=NOW + timedelta(days=days))
    session.add(m)
    session.flush()
    session.add(MarketSnapshot(market_id=m.id, yes_bid=bid, yes_ask=ask, liquidity=liq,
                               volume_24h=liq, captured_at=NOW))
    return m


def poly(session, mid, question, *, bb=0.19, ba=0.21, liq=9000, days=25, category="World Cup Winner"):
    session.add(PolymarketMarket(
        market_id=mid, condition_id="0x" + mid, question=question, category=category,
        best_bid=bb, best_ask=ba, spread=round(ba - bb, 4), liquidity_usd=liq,
        outcomes=["Yes", "No"], outcome_prices=[round((bb + ba) / 2, 4), round(1 - (bb + ba) / 2, 4)],
        clob_token_ids=["t" + mid], end_date=NOW + timedelta(days=days), active=True, observed_at=NOW,
    ))


# --- normalizer -------------------------------------------------------------


def test_normalize_title():
    assert normalize_title("Will Egypt WIN the 2026 FIFA World Cup?") == "will egypt win the 2026 fifa world cup"
    assert normalize_title("Yankees vs. Red Sox") == "yankees vs red sox"


def test_normalize_outcome():
    assert normalize_outcome("Will France win the World Cup") == "winner"
    assert normalize_outcome("Total runs Over 8.5") == "over_under"
    assert normalize_outcome("Team to Advance") == "advance"
    assert normalize_outcome("Presidential Election Winner") == "candidate_winner"


def test_outcomes_compatible():
    assert outcomes_compatible("winner", "yes_no") is True     # both yes-ish
    assert outcomes_compatible("over_under", "winner") is False


def test_coarse_domain():
    assert coarse_domain("Will France win the FIFA World Cup") == "sports"
    assert coarse_domain("Presidential Election Winner") == "politics"
    assert coarse_domain("Random thing") == "other"


# --- matching ---------------------------------------------------------------


class TestMatching:
    def test_compatible_winner_market_matches(self, session):
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup", bid=20, ask=24)
        poly(session, "PMF", "Will France win the 2026 FIFA World Cup?", bb=0.19, ba=0.21)
        session.commit()
        run = CrossVenueMatchingService().match_once(session)
        cands = session.query(CrossVenueMarketCandidate).all()
        assert len(cands) == 1
        c = cands[0]
        assert c.match_label == LABEL_COMPARABLE
        assert c.kalshi_ticker == "KXWCWIN-FRA" and c.polymarket_market_id == "PMF"

    def test_midpoint_difference_measured(self, session):
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup", bid=20, ask=24)  # mid 0.22
        poly(session, "PMF", "Will France win the 2026 FIFA World Cup?", bb=0.19, ba=0.21)         # mid 0.20
        session.commit()
        CrossVenueMatchingService().match_once(session)
        c = session.query(CrossVenueMarketCandidate).one()
        assert c.kalshi_midpoint == pytest.approx(0.22)
        assert c.polymarket_midpoint == pytest.approx(0.20)
        assert c.observed_difference == pytest.approx(0.02)  # 0.22 - 0.20

    def test_incompatible_outcome_rejected(self, session):
        # same teams, but Kalshi over/under vs Polymarket winner -> incompatible_outcome
        kalshi(session, "KXMLB-TOT", "Yankees Red Sox total runs Over 8.5", bid=48, ask=52)
        poly(session, "PMY", "Will Yankees Red Sox winner be Yankees", bb=0.5, ba=0.54,
             category="MLB")
        session.commit()
        CrossVenueMatchingService().match_once(session)
        c = session.query(CrossVenueMarketCandidate).one()
        assert c.match_label == LABEL_INCOMPATIBLE_OUTCOME
        assert c.observed_difference is None  # not meaningful across incompatible outcomes

    def test_incompatible_resolution_rejected(self, session):
        kalshi(session, "KXWCWIN-EGY", "Egypt to win the FIFA World Cup", days=20)
        poly(session, "PME", "Will Egypt win the FIFA World Cup?", days=400)  # ~1yr apart
        session.commit()
        CrossVenueMatchingService().match_once(session)
        c = session.query(CrossVenueMarketCandidate).one()
        assert c.match_label == LABEL_INCOMPATIBLE_RESOLUTION

    def test_low_or_unresolved_for_weak_similarity(self, session):
        kalshi(session, "KXWCWIN-BRA", "Brazil to win the FIFA World Cup championship trophy final", days=25)
        poly(session, "PMB", "Will Brazil win", days=25)  # sparse overlap
        session.commit()
        CrossVenueMatchingService().match_once(session)
        c = session.query(CrossVenueMarketCandidate).one()
        assert c.match_label in (LABEL_LOW_CONFIDENCE, LABEL_UNRESOLVED)

    def test_no_plausible_match_not_persisted(self, session):
        kalshi(session, "KXPRES-X", "Will Smith win the Presidential Election", category="Politics", days=120)
        poly(session, "PMX", "Will Bitcoin hit 200k in 2026?", category="Crypto", days=180)
        session.commit()
        CrossVenueMatchingService().match_once(session)
        assert session.query(CrossVenueMarketCandidate).count() == 0  # no noise


# --- report -----------------------------------------------------------------


def test_report_builds(session):
    kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup")
    kalshi(session, "KXWCWIN-ARG", "Argentina to win the 2026 FIFA World Cup", bid=30, ask=34)
    poly(session, "PMF", "Will France win the 2026 FIFA World Cup?")
    poly(session, "PMA", "Will Argentina win the 2026 FIFA World Cup?", bb=0.28, ba=0.30)
    session.commit()
    CrossVenueMatchingService().match_once(session)
    r = CrossVenueReportService().build(session)
    assert r.candidates == 2
    assert r.by_label.get(LABEL_COMPARABLE) == 2
    assert r.midpoint_difference["n"] == 2
    assert r.midpoint_difference["abs_p50"] is not None
    assert "not arbitrage" in r.note.lower() or "not ev" in r.note.lower()


# --- forbidden labels / fields ----------------------------------------------


def test_no_forbidden_labels():
    from app.services.cross_venue import MATCH_LABELS

    forbidden = ("arbitrage", "arb", "trade_candidate", "buy", "sell", "bet", "ev", "position")
    for lbl in MATCH_LABELS:
        assert not any(f == lbl or f in lbl.split("_") for f in forbidden)


def test_no_forbidden_fields_on_model():
    cols = set(CrossVenueMarketCandidate.__table__.columns.keys())
    for bad in ("side", "size", "ev", "expected_value", "action", "recommendation",
                "order", "wallet", "arbitrage", "arb", "profit", "dollars"):
        assert bad not in cols, f"forbidden column {bad!r}"


def test_no_forbidden_vocab_in_serialized_output(session):
    kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup")
    poly(session, "PMF", "Will France win the 2026 FIFA World Cup?")
    session.commit()
    CrossVenueMatchingService().match_once(session)
    c = session.query(CrossVenueMarketCandidate).one()
    blob = " ".join([c.match_label] + [str(x) for x in (c.match_reasons or [])]
                    + [str(x) for x in (c.mismatch_reasons or [])]).lower()
    for term in ("arbitrage", " arb", "buy", "sell", " bet", "trade_candidate",
                 "expected_value", "position_siz"):
        assert term not in blob


# --- migration up/down ------------------------------------------------------


def test_migration_0021_round_trips():
    from alembic import command
    from alembic.config import Config
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        db = f"sqlite:///{d}/t.db"
        cfg = Config()
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", db)
        command.upgrade(cfg, "0021")
        tables = set(inspect(create_engine(db)).get_table_names())
        assert {"cross_venue_observation_runs", "cross_venue_market_candidates"} <= tables
        command.downgrade(cfg, "0020")
        remaining = set(inspect(create_engine(db)).get_table_names())
        assert "cross_venue_market_candidates" not in remaining
