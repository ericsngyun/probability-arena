"""POLY-PRECISION-001 tests: cross-venue matcher precision.

Covers the two POLY-002 defects surfaced by POLY-COVERAGE-001 — a Polymarket
midpoint read from `outcome_prices[0]` regardless of which proposition that
outcome names, and "O/U 2.5" normalizing to yes_no because punctuation was
stripped before over/under detection — plus the market-type, threshold, entity
and sport gates, side-uncertainty handling, and the large-difference review flag.

Fixtures use the REAL shapes observed on the live venues:
  * Polymarket `outcomes` is ["Yes","No"] ~74% of the time and a pair of TEAM
    names ~26% of the time; the market-level book quotes `outcomes[0]`.
  * Kalshi encodes the Yes side of a game market in the TICKER SUFFIX, so
    `KXMLBGAME-...-SD` and `...-LAD` share the title "San Diego vs Los Angeles D
    Winner?" and the title alone cannot say which side Yes is.

Everything is measurement/observation only: no arbitrage, EV, trade
recommendation, paper trading, sizing, orders, wallets, keys, signing, or
execution. No live network; in-memory SQLite.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import CrossVenueMarketCandidate, Market, MarketSnapshot, PolymarketMarket
from app.services.cross_venue import (
    HIGH_SEMANTIC_CONFIDENCE,
    LABEL_COMPARABLE,
    LABEL_INCOMPATIBLE_OUTCOME,
    LABEL_LOW_CONFIDENCE,
    LABEL_UNRESOLVED,
    LARGE_OBSERVED_DIFFERENCE,
    REASON_ENTITY_MISMATCH,
    REASON_LARGE_DIFFERENCE,
    REASON_MARKET_TYPE_MISMATCH,
    REASON_MIDPOINT_SIDE_UNCERTAIN,
    REASON_OUTCOME_SIDE_UNCERTAIN,
    REASON_SPORT_OR_GAME_MISMATCH,
    REASON_THRESHOLD_MISMATCH,
    CrossVenueMatchingService,
    KalshiView,
    PolyView,
    detect_sport,
    entity_tokens,
    extract_kalshi_yes_entity,
    extract_spread_line,
    extract_threshold,
    market_scope,
    normalize_outcome,
    outcomes_compatible,
    poly_yes_midpoint,
    resolve_poly_yes_index,
    scopes_compatible,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def kalshi(session, ticker, title, *, bid=20, ask=24, liq=5000, days=5, category=None):
    m = Market(ticker=ticker, event_ticker=ticker.split("-")[0], title=title, category=category,
               status="active", close_time=NOW + timedelta(days=days))
    session.add(m)
    session.flush()
    session.add(MarketSnapshot(market_id=m.id, yes_bid=bid, yes_ask=ask, liquidity=liq,
                               volume_24h=liq, captured_at=NOW))
    return m


def poly(session, mid, question, *, outcomes=("Yes", "No"), prices=None,
         bb=None, ba=None, liq=9000, days=5, category="World Cup"):
    session.add(PolymarketMarket(
        market_id=mid, condition_id="0x" + mid, question=question, category=category,
        active=True, best_bid=bb, best_ask=ba,
        spread=(round(ba - bb, 4) if (bb is not None and ba is not None) else None),
        liquidity_usd=liq, outcomes=list(outcomes),
        outcome_prices=list(prices) if prices else None,
        clob_token_ids=["t" + mid], end_date=NOW + timedelta(days=days), observed_at=NOW,
    ))


def one(session) -> CrossVenueMarketCandidate:
    CrossVenueMatchingService().match_once(session)
    return session.query(CrossVenueMarketCandidate).one()


def reasons(c) -> str:
    return " ".join(str(r) for r in (c.mismatch_reasons or []))


# --- bug 1: Polymarket midpoint side alignment --------------------------------


class TestMidpointSideAlignment:
    def test_yes_no_ordering_resolves_to_yes(self):
        assert resolve_poly_yes_index(["Yes", "No"], "Will X win?") == (0, None)

    def test_reversed_yes_no_ordering_resolves_to_yes(self):
        """["No","Yes"] must map the Yes side to index 1, not index 0."""
        assert resolve_poly_yes_index(["No", "Yes"], "Will X win?") == (1, None)

    def test_reversed_ordering_uses_book_complement(self):
        """The market-level book quotes outcomes[0]. With ["No","Yes"] the Yes
        probability is 1 - book_mid, NOT the book mid."""
        p = PolyView(market_id="p", condition_id=None, token_id=None, question="q",
                     domain="other", outcome_type="yes_no", tokens=frozenset(),
                     resolution_time=NOW, spread=None, liquidity_proxy=None,
                     outcomes=["No", "Yes"], outcome_prices=[0.9965, 0.0035],
                     best_bid=0.994, best_ask=0.999)
        assert p.probability_for_index(0) == pytest.approx(0.9965)   # P(No)
        assert p.probability_for_index(1) == pytest.approx(0.0035)   # P(Yes)

    def test_gpt_style_adjacent_markets_are_not_side_inverted(self):
        """The two live GPT-5.6 markets both carry outcomes ["Yes","No"] and
        coherent P(Yes) values (0.0035 by Jul 8, 0.975 by Jul 9 — monotone). A
        correct reader must report P(Yes), not flip one of them."""
        jul8 = PolyView(market_id="a", condition_id=None, token_id=None,
                        question="GPT-5.6 released by July 8, 2026?", domain="other",
                        outcome_type="yes_no", tokens=frozenset(), resolution_time=NOW,
                        spread=None, liquidity_proxy=None, outcomes=["Yes", "No"],
                        outcome_prices=[0.0035, 0.9965], best_bid=0.001, best_ask=0.006)
        jul9 = PolyView(market_id="b", condition_id=None, token_id=None,
                        question="GPT-5.6 released by July 9, 2026?", domain="other",
                        outcome_type="yes_no", tokens=frozenset(), resolution_time=NOW,
                        spread=None, liquidity_proxy=None, outcomes=["Yes", "No"],
                        outcome_prices=[0.975, 0.025], best_bid=0.971, best_ask=0.979)
        k = KalshiView(ticker="KXGPT-OPEN-26JUL08", event_ticker="KXGPT",
                       title="Will OpenAI release GPT-5.6 before Jul 8, 2026?",
                       domain="other", outcome_type="yes_no", tokens=frozenset(),
                       resolution_time=NOW, midpoint=0.67, spread=None,
                       liquidity_proxy=None, snapshot_age_seconds=10.0)

        assert poly_yes_midpoint(jul8, k)[0] == pytest.approx(0.0035)
        assert poly_yes_midpoint(jul9, k)[0] == pytest.approx(0.975)

    def test_team_named_outcomes_without_kalshi_yes_entity_are_uncertain(self, session):
        """Kalshi's KXMLBGAME title names both teams and no Yes side (the side is
        in the ticker suffix). Comparing P(Royals) to Kalshi P(Yes) is meaningless,
        so the midpoint and the difference must be ABSENT."""
        kalshi(session, "KXMLBGAME-26JUL071910K", "Kansas City vs New York M Winner?")
        poly(session, "2768596", "Kansas City Royals vs. New York Mets",
             outcomes=("Kansas City Royals", "New York Mets"), prices=[0.375, 0.625],
             bb=0.37, ba=0.38, category="MLB")
        session.commit()
        c = one(session)

        assert c.polymarket_midpoint is None
        assert c.observed_difference is None
        assert c.midpoint_difference is None
        assert REASON_OUTCOME_SIDE_UNCERTAIN in reasons(c)
        assert c.match_label == LABEL_UNRESOLVED

    def test_team_named_outcomes_align_when_kalshi_names_the_yes_entity(self, session):
        kalshi(session, "KXG-1", "Will Kansas City Royals win the game?", bid=40, ask=44)
        poly(session, "PM1", "Will Kansas City Royals win the game?",
             outcomes=("Kansas City Royals", "New York Mets"), prices=[0.375, 0.625],
             bb=0.37, ba=0.38, category="MLB")
        session.commit()
        c = one(session)

        assert c.polymarket_midpoint == pytest.approx(0.375)   # the Royals side
        assert REASON_OUTCOME_SIDE_UNCERTAIN not in reasons(c)

    def test_entity_outcomes_matching_both_sides_are_uncertain(self):
        idx, reason = resolve_poly_yes_index(
            ["Kansas City Royals", "Kansas City Chiefs"], "Will Kansas City win?")
        assert idx is None and reason == REASON_OUTCOME_SIDE_UNCERTAIN

    def test_over_under_outcomes_align_to_kalshi_direction(self):
        assert resolve_poly_yes_index(["Over", "Under"], "Will over 5.5 goals be scored?") == (0, None)
        assert resolve_poly_yes_index(["Under", "Over"], "Will over 5.5 goals be scored?") == (1, None)
        assert resolve_poly_yes_index(["Over", "Under"], "Total goals?")[1] == REASON_OUTCOME_SIDE_UNCERTAIN

    def test_missing_outcome_metadata_is_midpoint_side_uncertain(self):
        assert resolve_poly_yes_index([], "Will X win?") == (None, REASON_MIDPOINT_SIDE_UNCERTAIN)
        assert resolve_poly_yes_index(None, "Will X win?") == (None, REASON_MIDPOINT_SIDE_UNCERTAIN)

    def test_non_binary_outcomes_are_outcome_side_uncertain(self):
        idx, reason = resolve_poly_yes_index(["A", "B", "C"], "Will A win?")
        assert idx is None and reason == REASON_OUTCOME_SIDE_UNCERTAIN

    def test_side_resolved_but_no_price_is_midpoint_side_uncertain(self):
        p = PolyView(market_id="p", condition_id=None, token_id=None, question="q",
                     domain="other", outcome_type="yes_no", tokens=frozenset(),
                     resolution_time=NOW, spread=None, liquidity_proxy=None,
                     outcomes=["Yes", "No"], outcome_prices=[], best_bid=None, best_ask=None)
        k = KalshiView(ticker="K", event_ticker=None, title="Will X win?", domain="other",
                       outcome_type="yes_no", tokens=frozenset(), resolution_time=NOW,
                       midpoint=0.5, spread=None, liquidity_proxy=None, snapshot_age_seconds=1.0)
        assert poly_yes_midpoint(p, k) == (None, REASON_MIDPOINT_SIDE_UNCERTAIN)

    def test_kalshi_yes_entity_extraction(self):
        assert extract_kalshi_yes_entity(
            "Will Procyon Gaming win map 2 in the Procyon Gaming vs. Guara Esports match"
        ) == "Procyon Gaming"
        assert extract_kalshi_yes_entity("Portugal wins by more than 2.5 goals?") == "Portugal"
        # the real ambiguity: both teams named, Yes side lives in the ticker suffix
        assert extract_kalshi_yes_entity("San Diego vs Los Angeles D Winner?") is None


# --- bug 2: O/U normalization -------------------------------------------------


class TestOverUnderNormalization:
    @pytest.mark.parametrize("title", [
        "Switzerland vs. Colombia: O/U 2.5",
        "O/U 8.5 Total Corners",
        "Will over 5.5 goals be scored?",
        "Over 3.5 2H goals scored?",
        "Total runs Over 8.5",
        "under 2.5 goals",
        "Switzerland vs. Colombia: O / U 1.5",
    ])
    def test_over_under_variants_normalize_to_over_under(self, title):
        assert normalize_outcome(title) == "over_under"

    def test_ou_is_not_yes_no(self):
        """The original defect: normalize_title stripped the slash before the
        \\bo/?u\\b test could ever see it, so "O/U 2.5" fell through to yes_no."""
        assert normalize_outcome("O/U 2.5") != "yes_no"

    def test_word_boundaries_prevent_false_over_under(self):
        assert normalize_outcome("Overtime thriller") == "yes_no"
        assert normalize_outcome("Thunder win the title") != "over_under"

    def test_thresholds_parse(self):
        assert extract_threshold("O/U 2.5") == 2.5
        assert extract_threshold("Will over 5.5 goals be scored?") == 5.5
        assert extract_threshold("Total runs Over 8.5") == 8.5
        assert extract_threshold("Will France win?") is None

    def test_spread_line_parses_and_signs_do_not_false_positive(self):
        assert normalize_outcome("Spread: Colombia (-1.5)") == "spread"
        assert extract_spread_line("Spread: Colombia (-1.5)") == -1.5
        assert normalize_outcome("Portugal wins by more than 2.5 goals?") == "spread"
        assert extract_spread_line("Portugal wins by more than 2.5 goals?") == 2.5
        # a hyphen inside a token is never a handicap line
        assert normalize_outcome("GPT-5.6 released by July 8, 2026?") == "yes_no"
        assert normalize_outcome("Will France win on 2026-07-09?") == "winner"


# --- market-type / threshold / entity / sport gates ---------------------------


class TestCompatibilityGates:
    def test_outcome_type_compatibility_is_tightened(self):
        assert outcomes_compatible("winner", "winner") is True
        assert outcomes_compatible("winner", "candidate_winner") is True
        assert outcomes_compatible("yes_no", "winner") is False       # was True in POLY-002
        assert outcomes_compatible("over_under", "over_under") is True
        assert outcomes_compatible("over_under", "spread") is False
        assert outcomes_compatible("advance", "winner") is False

    def test_market_scope_detection(self):
        assert market_scope("Foster Griffin: 9+ strikeouts?") == "player_prop"
        assert market_scope("Switzerland vs Colombia Winner?") == "game"
        assert market_scope("France to win the 2026 FIFA World Cup") == "tournament_future"
        assert market_scope("GPT-5.6 released by July 8, 2026?") == "event"

    def test_scopes_compatible_treats_event_as_wildcard(self):
        assert scopes_compatible("game", "tournament_future") is False
        assert scopes_compatible("player_prop", "game") is False
        assert scopes_compatible("player_prop", "tournament_future") is False
        assert scopes_compatible("event", "tournament_future") is True   # unknown -> no rejection
        assert scopes_compatible("game", "game") is True

    def test_compatible_winner_match_still_works(self, session):
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup", bid=20, ask=24)
        poly(session, "PMF", "Will France win the 2026 FIFA World Cup?", bb=0.19, ba=0.21)
        session.commit()
        c = one(session)

        assert c.match_label == LABEL_COMPARABLE
        assert c.kalshi_midpoint == pytest.approx(0.22)
        assert c.polymarket_midpoint == pytest.approx(0.20)
        assert c.observed_difference == pytest.approx(0.02)

    def test_game_market_does_not_match_tournament_future(self, session):
        """A single game never resolves a tournament future, however well the
        titles overlap."""
        kalshi(session, "KXWCWIN-FRA", "Will France win the World Cup?")  # tournament_future
        poly(session, "PMW", "France vs Morocco: Will France win?",       # game
             outcomes=("France", "Morocco"), prices=[0.775, 0.225], bb=0.77, ba=0.78)
        session.commit()
        c = one(session)

        assert c.match_label == LABEL_INCOMPATIBLE_OUTCOME
        assert REASON_MARKET_TYPE_MISMATCH in reasons(c)
        assert c.observed_difference is None

    def test_player_prop_does_not_match_game_future(self, session):
        kalshi(session, "KXMLBKS-26JUL031845-WSH", "Foster Griffin: 9+ strikeouts?", category="MLB")
        poly(session, "PMG", "Foster Griffin vs. New York Mets",   # a game, not a prop
             outcomes=("Foster Griffin", "New York Mets"), prices=[0.4, 0.6],
             bb=0.39, ba=0.41, category="MLB")
        session.commit()
        c = one(session)

        assert c.match_label == LABEL_INCOMPATIBLE_OUTCOME
        assert REASON_MARKET_TYPE_MISMATCH in reasons(c)

    def test_threshold_mismatch_rejected(self, session):
        kalshi(session, "KXWCTOTAL-26JUL06PORESP-6", "Will over 5.5 goals be scored?")
        poly(session, "PMO", "Will over 2.5 goals be scored?",
             outcomes=("Over", "Under"), bb=0.5, ba=0.54)
        session.commit()
        c = one(session)

        assert c.match_label == LABEL_INCOMPATIBLE_OUTCOME
        assert REASON_THRESHOLD_MISMATCH in reasons(c)
        assert c.observed_difference is None

    def test_matching_threshold_over_under_can_be_comparable(self, session):
        kalshi(session, "KXWCTOTAL-26JUL06PORESP-6", "Will over 2.5 goals be scored?", bid=48, ask=52)
        poly(session, "PMO", "Will over 2.5 goals be scored?",
             outcomes=("Over", "Under"), bb=0.48, ba=0.52)
        session.commit()
        c = one(session)

        assert c.match_label == LABEL_COMPARABLE
        assert c.polymarket_midpoint == pytest.approx(0.50)

    def test_over_under_with_unparseable_line_never_reaches_comparable(self, session):
        kalshi(session, "KXWCTOTAL-X", "Will the total be over?", bid=48, ask=52)
        poly(session, "PMU", "Will the total be over?", outcomes=("Over", "Under"),
             bb=0.48, ba=0.52)
        session.commit()
        c = one(session)

        assert c.match_label != LABEL_COMPARABLE
        assert c.match_label in (LABEL_LOW_CONFIDENCE, LABEL_UNRESOLVED)

    def test_cs2_does_not_match_valorant(self, session):
        """The POLY-COVERAGE-001 false positive: token overlap on
        map/2/winner/esports paired Counter-Strike 2 with Valorant."""
        kalshi(session, "KXCS2MAP-26JUL031200PROGE-2-PRO",
               "Will Procyon Gaming win map 2 in the Procyon Gaming vs. Guara Esports match")
        poly(session, "2844884", "Valorant: Fire Flux Esports vs CGN Esports - Map 2 Winner",
             outcomes=("Fire Flux Esports", "CGN Esports"), prices=[0.5, 0.5],
             bb=0.49, ba=0.51, category="Valorant")
        session.commit()
        c = one(session)

        assert c.match_label == LABEL_INCOMPATIBLE_OUTCOME
        assert REASON_SPORT_OR_GAME_MISMATCH in reasons(c) or REASON_ENTITY_MISMATCH in reasons(c)
        assert c.observed_difference is None

    def test_sport_detection(self):
        assert detect_sport("x", ticker="KXCS2MAP-26JUL03") == "cs2"
        assert detect_sport("Valorant: Fire Flux vs CGN - Map 2 Winner") == "valorant"
        assert detect_sport("x", ticker="KXMLBGAME-1") == "baseball"
        assert detect_sport("x", ticker="KXITFMATCH-1") == "tennis"
        assert detect_sport("Nothing identifying here") is None

    def test_disjoint_entities_rejected(self, session):
        kalshi(session, "KXG-A", "Will Arsenal win the match?", category="Soccer")
        poly(session, "PMB", "Will Chelsea win the match?", bb=0.4, ba=0.44, category="Soccer")
        session.commit()
        c = one(session)

        assert c.match_label == LABEL_INCOMPATIBLE_OUTCOME
        assert REASON_ENTITY_MISMATCH in reasons(c)

    def test_generic_words_are_not_entity_evidence(self):
        """"Esports"/"Gaming"/"Map"/"Winner" must never be the sole shared entity."""
        k = entity_tokens("Will Procyon Gaming win map 2 in the Procyon Gaming vs. Guara Esports match")
        p = entity_tokens("Valorant: Fire Flux Esports vs CGN Esports - Map 2 Winner")
        assert not (k & p)
        assert "esports" not in k and "esports" not in p

    def test_all_caps_titles_yield_no_entities(self):
        assert entity_tokens("WILL FRANCE WIN THE WORLD CUP") == frozenset()

    def test_rejection_is_still_recorded_when_no_plausible_compatible_partner(self, session):
        """Preferring a compatible partner must not silently drop the audit row:
        an implausible compatible partner (below the similarity floor) would be
        filtered out, losing the record of the real near-match rejection."""
        kalshi(session, "KXCS2MAP-1",
               "Will Procyon Gaming win map 2 in the Procyon Gaming vs. Guara Esports match")
        poly(session, "PMV", "Valorant: Procyon Gaming vs Guara Esports - Map 2 Winner",
             outcomes=("Procyon Gaming", "Guara Esports"), prices=[0.5, 0.5],
             bb=0.49, ba=0.51, category="Valorant")
        session.commit()
        c = one(session)   # the rejection row exists rather than vanishing

        assert c.match_label == LABEL_INCOMPATIBLE_OUTCOME
        assert REASON_SPORT_OR_GAME_MISMATCH in reasons(c)

    def test_best_match_prefers_a_compatible_partner(self, session):
        """A high-overlap incompatible market must not shadow a real match."""
        kalshi(session, "KXVAL-1", "Valorant: Fire Flux Esports vs CGN Esports - Map 2 Winner",
               category="Valorant")
        kalshi(session, "KXCS2MAP-1",
               "Will Procyon Gaming win map 2 in the Procyon Gaming vs. Guara Esports match")
        poly(session, "PMV", "Will Fire Flux Esports win map 2 vs CGN Esports",
             outcomes=("Fire Flux Esports", "CGN Esports"), prices=[0.5, 0.5],
             bb=0.49, ba=0.51, category="Valorant")
        session.commit()
        c = one(session)
        assert c.kalshi_ticker == "KXVAL-1"

    def test_boolean_and_detailed_gates_agree(self):
        """`_is_hard_incompatible` is a perf twin of `_hard_incompatibilities`;
        they must never disagree."""
        svc = CrossVenueMatchingService()
        combos = [
            ("yes_no", "winner", "event", "game", "cs2", "valorant", 2.5, 5.5),
            ("over_under", "over_under", "game", "game", None, None, 2.5, 2.5),
            ("winner", "winner", "game", "game", "baseball", "baseball", None, None),
            ("spread", "spread", "event", "event", None, None, None, None),
        ]
        for p_out, k_out, p_scope, k_scope, p_sport, k_sport, p_thr, k_thr in combos:
            p = PolyView(market_id="p", condition_id=None, token_id=None, question="q",
                         domain="sports", outcome_type=p_out, tokens=frozenset(),
                         resolution_time=NOW, spread=None, liquidity_proxy=None,
                         scope=p_scope, sport=p_sport, entities=frozenset({"a"}),
                         threshold=p_thr, spread_line=p_thr)
            k = KalshiView(ticker="K", event_ticker=None, title="t", domain="sports",
                           outcome_type=k_out, tokens=frozenset(), resolution_time=NOW,
                           midpoint=0.5, spread=None, liquidity_proxy=None,
                           snapshot_age_seconds=1.0, scope=k_scope, sport=k_sport,
                           entities=frozenset({"a"}), threshold=k_thr, spread_line=k_thr)
            assert svc._is_hard_incompatible(p, k) == bool(svc._hard_incompatibilities(p, k))


# --- observed-difference sanity flag ------------------------------------------


class TestLargeDifferenceFlag:
    def test_large_difference_flagged_for_review_not_rejected(self, session):
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup", bid=88, ask=90)
        poly(session, "PMF", "Will France win the World Cup?", bb=0.19, ba=0.21)
        session.commit()
        c = one(session)

        assert c.match_confidence < HIGH_SEMANTIC_CONFIDENCE
        assert c.observed_difference == pytest.approx(0.69)      # measured, not rejected
        assert c.match_label == LABEL_COMPARABLE                 # not rejected on the gap alone
        assert REASON_LARGE_DIFFERENCE in reasons(c)

    def test_very_high_semantic_confidence_suppresses_the_flag(self, session):
        """Identical propositions (confidence 1.0) may genuinely disagree across
        venues; the flag exists to catch bad MATCHES, not venue disagreement."""
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup", bid=88, ask=90)
        poly(session, "PMF", "France to win the 2026 FIFA World Cup", bb=0.19, ba=0.21)
        session.commit()
        c = one(session)

        assert c.match_confidence >= HIGH_SEMANTIC_CONFIDENCE
        assert c.observed_difference == pytest.approx(0.69)
        assert REASON_LARGE_DIFFERENCE not in reasons(c)

    def test_small_difference_not_flagged(self, session):
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup", bid=20, ask=24)
        poly(session, "PMF", "Will France win the 2026 FIFA World Cup?", bb=0.19, ba=0.21)
        session.commit()
        c = one(session)

        assert abs(c.observed_difference) <= LARGE_OBSERVED_DIFFERENCE
        assert REASON_LARGE_DIFFERENCE not in reasons(c)

    def test_flag_threshold_and_confidence_gate_are_conservative(self):
        assert LARGE_OBSERVED_DIFFERENCE == 0.35
        assert HIGH_SEMANTIC_CONFIDENCE == 0.85

    def test_no_difference_no_flag_when_side_uncertain(self, session):
        kalshi(session, "KXMLBGAME-26JUL071910K", "Kansas City vs New York M Winner?", bid=86, ask=88)
        poly(session, "2768596", "Kansas City Royals vs. New York Mets",
             outcomes=("Kansas City Royals", "New York Mets"), prices=[0.375, 0.625],
             bb=0.37, ba=0.38, category="MLB")
        session.commit()
        c = one(session)

        # the old code produced observed_difference ~0.49 here
        assert c.observed_difference is None
        assert REASON_LARGE_DIFFERENCE not in reasons(c)

    def test_observation_confidence_counts_unaligned_side_as_missing(self, session):
        kalshi(session, "KXMLBGAME-26JUL071910K", "Kansas City vs New York M Winner?")
        poly(session, "2768596", "Kansas City Royals vs. New York Mets",
             outcomes=("Kansas City Royals", "New York Mets"), prices=[0.375, 0.625],
             bb=0.37, ba=0.38, category="MLB")
        session.commit()
        c = one(session)
        assert c.observation_confidence < 1.0


# --- safety -------------------------------------------------------------------


class TestSafety:
    def test_no_forbidden_reason_or_label_vocabulary(self):
        from app.services import cross_venue as cv

        names = [n for n in dir(cv) if n.startswith(("REASON_", "LABEL_", "SCOPE_"))]
        values = " ".join(str(getattr(cv, n)) for n in names).lower()
        for bad in ("arbitrage", "arb_", "expected_value", "profit", "pnl",
                    "recommend", "position_siz", "paper_trad", "order", "wallet",
                    "buy", "sell", "opportunity"):
            assert bad not in values, f"forbidden vocabulary {bad!r} in {values}"

    def test_large_difference_reason_is_review_language_not_opportunity(self):
        assert REASON_LARGE_DIFFERENCE == "large_observed_difference_requires_review"
        for bad in ("edge", "opportunity", "arb", "profit", "ev"):
            assert bad not in REASON_LARGE_DIFFERENCE.replace("review", "")

    def test_no_forbidden_identifiers_in_module(self):
        import ast

        src = (REPO / "app" / "services" / "cross_venue.py").read_text()
        tree = ast.parse(src)
        bad = ("wallet", "private_key", "keypair", "swap", "jupiter", "send_transaction",
               "place_order", "submit_order", "create_order", "expected_value",
               "paper_trad", "position_siz", "trade_recommend", "execute_trade")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                assert not any(b in node.name.lower() for b in bad), node.name

    def test_no_forbidden_vocab_in_serialized_output(self, session):
        kalshi(session, "KXWCWIN-FRA", "France to win the 2026 FIFA World Cup")
        poly(session, "PMF", "Will France win the 2026 FIFA World Cup?", bb=0.19, ba=0.21)
        session.commit()
        c = one(session)

        blob = " ".join([c.match_label] + [str(x) for x in (c.match_reasons or [])]
                        + [str(x) for x in (c.mismatch_reasons or [])]).lower()
        for term in ("arbitrage", " arb", "buy", "sell", " bet", "trade_candidate",
                     "expected_value", "position_siz", "profit", "opportunity"):
            assert term not in blob

    def test_candidate_model_has_no_forbidden_columns(self):
        cols = set(CrossVenueMarketCandidate.__table__.columns.keys())
        for bad in ("side", "size", "ev", "expected_value", "action", "recommendation",
                    "order", "wallet", "arbitrage", "arb", "profit", "dollars"):
            assert bad not in cols

    def test_no_live_network_used_by_matcher(self):
        src = (REPO / "app" / "services" / "cross_venue.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src, f"matcher must not perform network calls ({net})"
