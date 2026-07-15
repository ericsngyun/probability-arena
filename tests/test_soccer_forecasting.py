"""Soccer evidence-aware forecaster (SOCCER-002) tests: market spec parsing,
evidence extraction, forecast behavior across market types and match phases,
gating, integration with signal processing, and edge-precheck acceptance.
Everything mocked — no live network. Forecasts are measurement inputs only."""

import copy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import Base
from app.models import MarketForecastRecord, MarketResearchPacket
from app.services.forecasting import ForecastingService, ForecastInput
from app.services.research import DOMAIN_SPORTS_SOCCER, create_research_packet
from app.services.resolution import (
    RuleBasedResolutionJudge,
    latest_assessment_for,
    persist_assessment,
)
from app.services.signal_workflow import SignalPromotionService
from app.services.soccer_forecasting import (
    MAX_PRIOR_SHIFT,
    SoccerEvidenceAwareForecaster,
    extract_soccer_evidence,
    parse_soccer_market_spec,
)
from app.services.soccer_research import SoccerExternalResearchCollector
from tests.conftest import make_market
from tests.test_signal_workflow import make_processor, seed_market, seed_signal
from tests.test_soccer_canary import MockSoccerFetcher, seed_soccer_market

WINNER_TICKER = "KXWCGAME-26JUN141800USAWAL-USA"
TOTAL_TICKER = "KXWCTOTAL-26JUN14USAWAL-3"
JUDGE = RuleBasedResolutionJudge(min_clarity_score=0.70, prompt_version="v1")


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def scoreboard_with(
    home=2, away=1, clock="74'", period=2, state="in", status="In Progress",
    shootout_home=None, shootout_away=None, red_card=False,
):
    from tests.test_soccer_canary import SCOREBOARD

    board = copy.deepcopy(SCOREBOARD)
    event = board["events"][0]
    competitors = event["competitions"][0]["competitors"]
    competitors[0]["score"] = str(home)
    competitors[1]["score"] = str(away)
    if shootout_home is not None:
        competitors[0]["shootoutScore"] = shootout_home
        competitors[1]["shootoutScore"] = shootout_away
    if not red_card:
        event["competitions"][0]["details"] = []
    event["status"] = {
        "displayClock": clock,
        "period": period,
        "type": {"name": "STATUS_IN_PROGRESS" if state == "in" else "STATUS_FULL_TIME",
                 "state": state, "description": status},
    }
    return board


def soccer_market(ticker=WINNER_TICKER, bid=48, ask=52, title="USA vs Wales: USA wins?"):
    return make_market(
        ticker=ticker,
        title=title,
        yes_bid=bid,
        yes_ask=ask,
        rules_primary="Resolves YES per the official FIFA match report.",
        settlement_source="FIFA (https://www.fifa.com)",
    )


async def make_input(session, market_data, scoreboard=None) -> ForecastInput:
    """Persist market + settlement enrichment + researchable assessment +
    source-backed soccer packet (mirrors the live processing path)."""
    from app.models import MarketDetailEnrichment

    market_row = seed_market(session, market_data.ticker)
    session.add(
        MarketDetailEnrichment(
            market_ticker=market_data.ticker,
            title=market_data.title,
            rules_text=market_data.rules_primary,
            settlement_source=market_data.settlement_source
            or "FIFA (https://www.fifa.com)",
            raw_market_detail={},
        )
    )
    session.commit()
    assessment = await JUDGE.assess(market_data)
    persist_assessment(session, market_data.ticker, assessment, JUDGE)
    collector = SoccerExternalResearchCollector(
        fetcher=MockSoccerFetcher(scoreboard=scoreboard or scoreboard_with())
    )
    packet = await create_research_packet(session, market_row, collector=collector)
    return ForecastInput(
        market=market_data,
        packet=packet,
        resolution=latest_assessment_for(session, market_data.ticker),
    )


def forecaster() -> SoccerEvidenceAwareForecaster:
    return SoccerEvidenceAwareForecaster()


class TestMarketSpecParsing:
    def test_winner_with_team_suffix(self):
        spec = parse_soccer_market_spec(WINNER_TICKER)
        assert spec.market_type == "winner"
        assert spec.team == "USA" and spec.team_is_home is False  # first-listed

    def test_total_with_line(self):
        spec = parse_soccer_market_spec(TOTAL_TICKER)
        assert spec.market_type == "total"
        assert spec.threshold == 2.5  # integer line -> x.5 semantics

    def test_advance_market(self):
        spec = parse_soccer_market_spec("KXWCADVANCE-26JUN14USAWAL-WAL")
        assert spec.market_type == "advance"
        assert spec.team == "WAL" and spec.team_is_home is True

    def test_spread_market(self):
        spec = parse_soccer_market_spec("KXWCSPREAD-26JUN14USAWAL-USA2")
        assert spec.market_type == "spread"
        assert spec.threshold == 1.5 and spec.team == "USA"

    def test_player_goal_is_its_own_type(self):
        spec = parse_soccer_market_spec("KXWCGOAL-26JUL03ARGCPV-CPVWSEMED17-1")
        assert spec.market_type == "player_goal"

    def test_unknown_shapes(self):
        assert parse_soccer_market_spec("KXWCGAME-26JUN14USAWAL").market_type == "unknown"
        assert parse_soccer_market_spec("NOT-A-SOCCER-TICKER").market_type == "unknown"
        assert parse_soccer_market_spec("KXWCGAME-26JUN14USAWAL-BRA").market_type == "unknown"


class TestEvidenceExtraction:
    async def test_extracts_live_state(self, session):
        inp = await make_input(
            session, soccer_market(), scoreboard_with(home=2, away=1, clock="74'", period=2)
        )
        evidence = extract_soccer_evidence(inp.packet)
        assert evidence.home_goals == 2 and evidence.away_goals == 1
        assert evidence.minute == 74 and evidence.period == 2
        assert evidence.is_live and not evidence.is_final
        assert not evidence.extra_time
        assert evidence.lineups_confirmed
        assert evidence.stats_available
        assert evidence.progress == pytest.approx(74 / 90)

    async def test_extra_time_and_red_cards(self, session):
        inp = await make_input(
            session,
            soccer_market(),
            scoreboard_with(home=1, away=1, clock="97'", period=3, red_card=True),
        )
        evidence = extract_soccer_evidence(inp.packet)
        assert evidence.extra_time
        assert evidence.red_cards >= 1


class TestForecasterBehavior:
    async def test_source_backed_winner_market_adjusts_from_prior(self, session):
        inp = await make_input(
            session, soccer_market(bid=48, ask=52),
            scoreboard_with(home=1, away=2, clock="80'"),  # USA (away slot) leads
        )
        forecast = await forecaster().forecast(inp)
        # USA is first-listed (away in ESPN payload); leading 2-1 late -> above prior
        assert forecast.estimated_probability > 0.50
        assert abs(forecast.estimated_probability - 0.50) <= MAX_PRIOR_SHIFT + 1e-9
        assert forecast.confidence >= 0.60  # passes the edge-precheck gate
        assert "soccer_evidence_v1" in forecast.calibration_tags
        assert "market_type_winner" in forecast.calibration_tags
        assert "late_match" in forecast.calibration_tags
        assert "live_match_state" in forecast.calibration_tags
        assert "evidence_adjusted" in forecast.calibration_tags

    async def test_deterministic_for_identical_input(self, session):
        inp = await make_input(session, soccer_market(), scoreboard_with())
        first = await forecaster().forecast(inp)
        second = await forecaster().forecast(inp)
        assert first.estimated_probability == second.estimated_probability
        assert first.confidence == second.confidence
        assert first.calibration_tags == second.calibration_tags

    async def test_late_total_adjusts_more_than_early(self, session):
        early_inp = await make_input(
            session,
            soccer_market(ticker=TOTAL_TICKER, title="Over 3 goals?"),
            scoreboard_with(home=0, away=0, clock="15'", period=1),
        )
        early = await forecaster().forecast(early_inp)
        late_inp = await make_input(
            session,
            soccer_market(ticker="KXWCTOTAL-26JUN15USAWAL-3", title="Over 3 goals?"),
            scoreboard_with(home=0, away=0, clock="85'", period=2),
        )
        late = await forecaster().forecast(late_inp)
        # 0-0 late is much worse for an over-2.5 market than 0-0 early
        assert late.estimated_probability < early.estimated_probability
        assert "early_match" in early.calibration_tags
        assert "late_match" in late.calibration_tags

    async def test_level_late_winner_market_decays_below_prior(self, session):
        inp = await make_input(
            session, soccer_market(bid=48, ask=52),
            scoreboard_with(home=1, away=1, clock="85'"),
        )
        forecast = await forecaster().forecast(inp)
        assert forecast.estimated_probability < 0.50  # draws resolve NO

    async def test_red_card_context_conservative(self, session):
        clean_inp = await make_input(
            session, soccer_market(), scoreboard_with(home=1, away=2, clock="70'")
        )
        clean = await forecaster().forecast(clean_inp)
        red_inp = await make_input(
            session,
            soccer_market(ticker="KXWCGAME-26JUN151800USAWAL-USA"),
            scoreboard_with(home=1, away=2, clock="70'", red_card=True),
        )
        red = await forecaster().forecast(red_inp)
        assert "red_card_context" in red.calibration_tags
        assert red.confidence < clean.confidence  # reduced, never boosted
        assert any("Red card" in note for note in red.skeptic_notes)
        # estimate itself is not inflated by the red card
        assert red.estimated_probability == pytest.approx(
            clean.estimated_probability, abs=0.02
        )

    async def test_shootout_high_uncertainty_for_winner(self, session):
        inp = await make_input(
            session,
            soccer_market(),
            scoreboard_with(
                home=1, away=1, clock="120'", period=4, state="in",
                status="Shootout", shootout_home=3, shootout_away=2,
            ),
        )
        forecast = await forecaster().forecast(inp)
        assert "penalty_context" in forecast.calibration_tags
        assert forecast.confidence <= 0.50  # shootout cap for non-advance markets
        assert forecast.forecast_risk == "high"

    async def test_shootout_advance_market_uses_pens_margin(self, session):
        inp = await make_input(
            session,
            soccer_market(
                ticker="KXWCADVANCE-26JUN141800USAWAL-WAL", title="Wales to advance?"
            ),
            scoreboard_with(
                home=1, away=1, clock="120'", period=4, state="in",
                status="Shootout", shootout_home=1, shootout_away=3,
            ),
        )
        forecast = await forecaster().forecast(inp)
        # WAL is second-listed (home slot); pens 1-3 against -> below prior
        assert forecast.estimated_probability < 0.50
        assert "market_type_advance" in forecast.calibration_tags
        assert "penalty_context" in forecast.calibration_tags

    async def test_player_goal_falls_back_conservatively(self, session):
        inp = await make_input(
            session,
            soccer_market(
                ticker="KXWCGOAL-26JUN141800USAWAL-USAPPLAYER9-1",
                title="Player to score?",
            ),
            scoreboard_with(home=3, away=0, clock="80'"),
        )
        forecast = await forecaster().forecast(inp)
        # team-level data must not price a player: template fallback
        assert "market_type_player_goal" in forecast.calibration_tags
        assert any("player" in note.lower() for note in forecast.skeptic_notes)

    async def test_unknown_market_type_falls_back(self, session):
        inp = await make_input(
            session,
            soccer_market(ticker="KXWCCORNERS-26JUN14USAWAL-XYZ9", title="Corners?"),
        )
        forecast = await forecaster().forecast(inp)
        assert "market_type_unknown" in forecast.calibration_tags
        assert any("not recognized" in note for note in forecast.skeptic_notes)

    async def test_confidence_cap_enforced(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "soccer_forecast_max_confidence", 0.55)
        inp = await make_input(session, soccer_market(), scoreboard_with(clock="85'"))
        forecast = await SoccerEvidenceAwareForecaster().forecast(inp)
        assert forecast.confidence <= 0.55

    async def test_missing_state_caps_confidence(self, session):
        board = scoreboard_with()
        for competitor in board["events"][0]["competitions"][0]["competitors"]:
            competitor.pop("score", None)
        board["events"][0]["status"]["type"]["state"] = "pre"
        inp = await make_input(session, soccer_market(), board)
        forecast = await forecaster().forecast(inp)
        assert forecast.confidence <= 0.50
        assert forecast.forecast_risk == "high"


class TestGating:
    async def test_template_only_packet_falls_back(self, session):
        from app.services.research import TemplateResearchCollector

        market_data = soccer_market()
        seed_market(session, market_data.ticker)
        assessment = await JUDGE.assess(market_data)
        persist_assessment(session, market_data.ticker, assessment, JUDGE)
        from app.models import Market
        from sqlalchemy import select

        market_row = session.execute(
            select(Market).where(Market.ticker == market_data.ticker)
        ).scalar_one()
        packet = await create_research_packet(
            session, market_row, collector=TemplateResearchCollector()
        )
        inp = ForecastInput(
            market=market_data,
            packet=packet,
            resolution=latest_assessment_for(session, market_data.ticker),
        )
        forecast = await forecaster().forecast(inp)
        assert any("not source_backed" in note for note in forecast.skeptic_notes)

    async def test_non_soccer_domain_falls_back(self, session):
        inp = await make_input(session, soccer_market(), scoreboard_with())
        inp.packet.domain = "sports_baseball"
        forecast = await forecaster().forecast(inp)
        assert any("not sports_soccer" in note for note in forecast.skeptic_notes)

    async def test_forecasting_service_selects_by_flags(self, session, monkeypatch):
        inp = await make_input(session, soccer_market(), scoreboard_with())
        service = ForecastingService()

        monkeypatch.setattr(get_settings(), "enable_soccer_evidence_forecasting", False)
        assert service._forecaster_for(inp).name != "soccer_evidence"

        monkeypatch.setattr(get_settings(), "enable_soccer_evidence_forecasting", True)
        selected = ForecastingService()._forecaster_for(inp)
        assert selected.name == "soccer_evidence"

        # explicit injection always wins
        from app.services.forecasting import TemplateBaselineForecaster

        injected = ForecastingService(forecaster=TemplateBaselineForecaster())
        assert injected._forecaster_for(inp).name != "soccer_evidence"

    async def test_baseball_gate_unaffected(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_soccer_evidence_forecasting", True)
        inp = await make_input(session, soccer_market(), scoreboard_with())
        inp.packet.domain = "sports_baseball"
        monkeypatch.setattr(get_settings(), "enable_baseball_evidence_forecasting", False)
        assert ForecastingService()._forecaster_for(inp).name == "template_baseline"


class TestIntegration:
    async def test_process_promoted_links_soccer_evidence_forecast(
        self, session, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "enable_soccer_external_research", True)
        monkeypatch.setattr(get_settings(), "enable_soccer_evidence_forecasting", True)
        from tests.test_soccer_canary import WC_TICKER

        seed_soccer_market(session)
        signal = seed_signal(session, ticker=WC_TICKER)
        SignalPromotionService().promote(session, signal.id)

        processor = make_processor(
            collector=None, forecaster=None, soccer_fetcher=MockSoccerFetcher()
        )
        processed = await processor.process(session, signal)
        assert processed.signal_status == "forecast_refreshed"
        forecast = session.get(MarketForecastRecord, processed.refreshed_forecast_id)
        # WC_TICKER is a plain GAME ticker without a team suffix -> the
        # forecaster runs but falls back on market-type recognition; the
        # forecaster identity persisted is still soccer_evidence
        assert forecast.forecaster_name == "soccer_evidence"

    async def test_edge_precheck_accepts_fresh_soccer_evidence_forecast(self, session):
        from datetime import datetime, timedelta, timezone

        from app.services.edge_precheck import EdgePrecheckConfig, EdgePrecheckService
        from tests.test_edge_precheck import seed_resolution, seed_tick

        now = datetime.now(timezone.utc)
        ticker = WINNER_TICKER
        seed_resolution(session, ticker=ticker)
        tick = seed_tick(
            session, ticker=ticker, midpoint=0.50, spread=4, liquidity=2_000
        )
        tick.observed_at = now - timedelta(seconds=30)
        tick.created_at = tick.observed_at
        session.commit()
        forecast_row = MarketForecastRecord(
            market_ticker=ticker,
            forecaster_name="soccer_evidence",
            forecaster_version="v1",
            prompt_version="v1",
            estimated_probability=0.61,
            confidence=0.65,  # what the soccer forecaster produces live
            evidence_depth="source_backed",
            forecast_risk="low",
            created_at=now - timedelta(seconds=60),
        )
        session.add(forecast_row)
        session.commit()

        snapshot = EdgePrecheckService(EdgePrecheckConfig()).precheck_forecast(
            session, forecast_row, now=now
        )
        assert snapshot.status == "watchlist"  # soccer forecasts now measurable
        assert snapshot.probability_gap == pytest.approx(0.11)
        assert snapshot.forecaster_name == "soccer_evidence"
