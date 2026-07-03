import copy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.config import get_settings
from app.db import Base
from app.models import MarketForecastRecord, MarketResearchPacket
from app.services.baseball_forecasting import (
    MAX_PRIOR_SHIFT,
    BaseballEvidenceAwareForecaster,
    extract_baseball_evidence,
    parse_market_spec,
)
from app.services.baseball_research import BaseballExternalResearchCollector
from app.services.forecasting import ForecastingService, ForecastInput
from app.services.research import DOMAIN_SPORTS_BASEBALL, create_research_packet
from app.services.resolution import RuleBasedResolutionJudge, latest_assessment_for, persist_assessment
from app.services.signal_workflow import SignalPromotionService
from tests.conftest import make_market
from tests.test_baseball_canary import (
    LIVE_FEED,
    SCHEDULE,
    MockBaseballFetcher,
    seed_baseball_market,
)
from tests.test_signal_workflow import make_processor, seed_market, seed_signal

TOTAL_TICKER = "KXMLBTOTAL-26JUL021915STLATL-18"
SPREAD_TICKER = "KXMLBSPREAD-26JUL021915STLATL-ATL3"
JUDGE = RuleBasedResolutionJudge(min_clarity_score=0.70, prompt_version="v1")


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def feed_with(inning=6, half="Top", outs=2, away=4, home=3, state="Live"):
    feed = copy.deepcopy(LIVE_FEED)
    feed["gameData"]["status"]["abstractGameState"] = state
    feed["liveData"]["linescore"].update(
        currentInning=inning, inningState=half, outs=outs
    )
    feed["liveData"]["linescore"]["teams"] = {"away": {"runs": away}, "home": {"runs": home}}
    return feed


def total_market(ticker=TOTAL_TICKER, bid=48, ask=52):
    return make_market(
        ticker=ticker,
        title="St. Louis vs Atlanta Total Runs?",
        yes_bid=bid,
        yes_ask=ask,
        rules_primary="Resolves YES if total runs meet the line per the official box score.",
        settlement_source="MLB (https://www.mlb.com)",
    )


async def make_input(session, market_data, feed=None) -> ForecastInput:
    """Persist market + researchable assessment + source-backed packet."""
    market_row = seed_market(session, market_data.ticker)
    assessment = await JUDGE.assess(market_data)
    persist_assessment(session, market_data.ticker, assessment, JUDGE)
    collector = BaseballExternalResearchCollector(
        fetcher=MockBaseballFetcher(feed=feed or feed_with())
    )
    packet = await create_research_packet(session, market_row, collector=collector)
    return ForecastInput(
        market=market_data,
        packet=packet,
        resolution=latest_assessment_for(session, market_data.ticker),
    )


class TestMarketSpecParsing:
    def test_total_market(self):
        spec = parse_market_spec(TOTAL_TICKER)
        assert spec.market_type == "total"
        assert spec.threshold == 17.5

    def test_spread_market_with_side_detection(self):
        spec = parse_market_spec(SPREAD_TICKER)
        assert spec.market_type == "spread"
        assert spec.threshold == 2.5
        assert spec.team == "ATL"
        assert spec.team_is_home is True

        away = parse_market_spec("KXMLBSPREAD-26JUL021915STLATL-STL2")
        assert away.team_is_home is False

    def test_winner_market(self):
        spec = parse_market_spec("KXMLBGAME-26JUL021915STLATL-ATL")
        assert spec.market_type == "winner"
        assert spec.team == "ATL" and spec.team_is_home is True

    def test_player_props_and_f5_are_unknown(self):
        assert parse_market_spec("KXMLBHRR-26JUL021915STLATL-STLX1-2").market_type == "unknown"
        assert parse_market_spec("KXMLBF5-26JUL021940TBKC-TB").market_type == "unknown"
        assert parse_market_spec("KXMLBHIT-26JUL022140LAASEA-LAAJLOWE3-2").market_type == "unknown"


class TestEvidenceExtraction:
    async def test_extracts_state_from_packet(self, session):
        inp = await make_input(session, total_market(), feed=feed_with(inning=8, half="Bottom", outs=1, away=6, home=5))
        evidence = extract_baseball_evidence(inp.packet)
        assert evidence.away_runs == 6 and evidence.home_runs == 5
        assert evidence.inning == 8 and evidence.inning_half == "Bottom"
        assert evidence.outs == 1
        assert evidence.runners_on == 2  # first/third from LIVE_FEED offense
        assert evidence.is_live
        assert evidence.lineups_confirmed and evidence.probable_pitchers


class TestForecasterBehavior:
    async def test_deterministic_for_identical_input(self, session):
        inp = await make_input(session, total_market())
        forecaster = BaseballEvidenceAwareForecaster()
        first = await forecaster.forecast(inp)
        second = await forecaster.forecast(inp)
        first_dump = first.model_dump()
        second_dump = second.model_dump()
        # raw_response carries a generated_at timestamp; everything else equal
        assert first.raw_response.pop("generated_at") and second.raw_response.pop("generated_at")
        assert first.raw_response == second.raw_response
        assert first_dump == second_dump  # raw_response excluded from dumps

    async def test_total_market_adjusts_away_from_midpoint(self, session):
        # 7 runs at 61% progress vs an 17.5 line -> strongly under -> below prior
        inp = await make_input(session, total_market(), feed=feed_with(inning=6, away=4, home=3))
        forecast = await BaseballEvidenceAwareForecaster().forecast(inp)
        assert forecast.estimated_probability < 0.5  # prior was 0.50
        assert "evidence_adjusted" in forecast.calibration_tags
        assert "baseball_evidence_v1" in forecast.calibration_tags
        assert "market_type_total" in forecast.calibration_tags
        assert "live_game_state" in forecast.calibration_tags
        assert forecast.raw_response["prior"] == 0.5
        assert forecast.raw_response["evidence_estimate"] is not None

    async def test_late_game_adjusts_more_than_early_game(self, session):
        # same matchup/line/score; only game progress differs
        early = await make_input(
            session,
            total_market("KXMLBTOTAL-26JUL021915STLATL-18"),
            feed=feed_with(inning=2, half="Top", away=1, home=0),
        )
        late = await make_input(
            session,
            total_market("KXMLBTOTAL-26JUL022200STLATL-18"),
            feed=feed_with(inning=8, half="Bottom", away=1, home=0),
        )
        forecaster = BaseballEvidenceAwareForecaster()
        early_forecast = await forecaster.forecast(early)
        late_forecast = await forecaster.forecast(late)

        assert "early_game" in early_forecast.calibration_tags
        assert "late_game" in late_forecast.calibration_tags
        # same score/line: late game is farther below the prior than early
        assert late_forecast.estimated_probability < early_forecast.estimated_probability
        # and both are shift-capped relative to the 0.50 prior
        for forecast in (early_forecast, late_forecast):
            assert abs(forecast.estimated_probability - 0.5) <= MAX_PRIOR_SHIFT + 1e-9

    async def test_line_already_reached_pushes_near_certainty(self, session):
        inp = await make_input(
            session, total_market(), feed=feed_with(inning=7, away=10, home=9)  # 19 > 17.5
        )
        forecast = await BaseballEvidenceAwareForecaster().forecast(inp)
        assert forecast.raw_response["evidence_estimate"] == 0.97
        assert forecast.estimated_probability > 0.5

    async def test_spread_market_uses_margin_for_team(self, session):
        market = total_market(SPREAD_TICKER)
        # ATL (home) leads 8-1 in the 8th; needs to win by >2.5
        inp = await make_input(
            session, market, feed=feed_with(inning=8, half="Bottom", away=1, home=8)
        )
        forecast = await BaseballEvidenceAwareForecaster().forecast(inp)
        assert forecast.estimated_probability > 0.5
        assert "market_type_spread" in forecast.calibration_tags

    async def test_unknown_market_type_falls_back_with_note_and_tag(self, session):
        market = total_market("KXMLBHRR-26JUL021915STLATL-STLX1-2")
        inp = await make_input(session, market)
        forecast = await BaseballEvidenceAwareForecaster().forecast(inp)
        assert "template_baseline_v1" in forecast.calibration_tags  # template output
        assert "market_type_unknown" in forecast.calibration_tags
        assert any("market type not recognized" in n for n in forecast.skeptic_notes)

    async def test_confidence_cap_enforced(self, session):
        inp = await make_input(session, total_market(), feed=feed_with(inning=9, half="Bottom"))
        forecast = await BaseballEvidenceAwareForecaster().forecast(inp)
        assert forecast.confidence <= get_settings().baseball_forecast_max_confidence

    async def test_pregame_makes_no_adjustment_and_caps_confidence(self, session):
        pregame = copy.deepcopy(LIVE_FEED)
        pregame["gameData"]["status"]["abstractGameState"] = "Preview"
        pregame["liveData"] = {}
        inp = await make_input(session, total_market(), feed=pregame)
        forecast = await BaseballEvidenceAwareForecaster().forecast(inp)
        assert forecast.estimated_probability == 0.5  # prior kept
        assert "anchored_to_market_mid" in forecast.calibration_tags
        assert forecast.confidence <= 0.5
        assert forecast.forecast_risk == "high"


def _enable_evidence_forecasting(monkeypatch, value=True):
    monkeypatch.setattr(get_settings(), "enable_baseball_evidence_forecasting", value)


async def forecast_via_service(session, market_data, feed=None):
    from sqlalchemy import select

    from app.models import Market

    await make_input(session, market_data, feed=feed)
    market = session.execute(
        select(Market).where(Market.ticker == market_data.ticker)
    ).scalar_one()
    return await ForecastingService().forecast_market(session, market)


class TestServiceSelection:
    async def test_flag_false_uses_template(self, session, monkeypatch):
        _enable_evidence_forecasting(monkeypatch, False)
        row = await forecast_via_service(session, total_market())
        assert row.forecaster_name == "template_baseline"

    async def test_flag_true_source_backed_uses_evidence_forecaster(self, session, monkeypatch):
        _enable_evidence_forecasting(monkeypatch)
        row = await forecast_via_service(session, total_market())
        assert row.forecaster_name == "baseball_evidence"
        assert row.forecaster_version == "v1"
        assert row.model_name is None
        assert "baseball_evidence_v1" in row.calibration_tags
        assert row.evidence_depth == "source_backed"

    async def test_template_only_packet_falls_back(self, session, monkeypatch):
        _enable_evidence_forecasting(monkeypatch)
        from app.services.research import TemplateResearchCollector

        market_data = total_market("KXMLBTOTAL-26JUL021940TBKC-10")
        market_row = seed_market(session, market_data.ticker)
        assessment = await JUDGE.assess(market_data)
        persist_assessment(session, market_data.ticker, assessment, JUDGE)
        await create_research_packet(
            session, market_row, collector=TemplateResearchCollector(name="template", version="v1")
        )
        row = await ForecastingService().forecast_market(session, market_row)
        assert row.forecaster_name == "template_baseline"

    async def test_non_baseball_packet_falls_back(self, session, monkeypatch):
        _enable_evidence_forecasting(monkeypatch)
        from app.services.research import TemplateResearchCollector

        market_data = make_market(
            ticker="KXATPMATCH-26JUL02FOO-BAR", title="Tennis winner?",
            rules_primary="Resolves YES per ATP (https://www.atptour.com).",
        )
        market_row = seed_market(session, market_data.ticker)
        assessment = await JUDGE.assess(market_data)
        persist_assessment(session, market_data.ticker, assessment, JUDGE)
        await create_research_packet(
            session, market_row, collector=TemplateResearchCollector(name="template", version="v1")
        )
        row = await ForecastingService().forecast_market(session, market_row)
        assert row.forecaster_name == "template_baseline"

    async def test_injected_forecaster_always_wins(self, session, monkeypatch):
        _enable_evidence_forecasting(monkeypatch)
        from sqlalchemy import select

        from app.models import Market
        from app.services.forecasting import TemplateBaselineForecaster

        market_data = total_market("KXMLBTOTAL-26JUL022300STLATL-12")
        await make_input(session, market_data)
        market = session.execute(
            select(Market).where(Market.ticker == market_data.ticker)
        ).scalar_one()
        service = ForecastingService(forecaster=TemplateBaselineForecaster())
        row = await service.forecast_market(session, market)
        assert row.forecaster_name == "template_baseline"


class TestSignalIntegration:
    async def test_process_promoted_links_evidence_aware_forecast(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_baseball_external_research", True)
        _enable_evidence_forecasting(monkeypatch)

        seed_baseball_market(session)  # KXMLBTOTAL-26JUL021915STLATL-18 with rules
        signal = seed_signal(session, ticker=TOTAL_TICKER)
        SignalPromotionService().promote(session, signal.id)

        processor = make_processor(
            collector=None, forecaster=None, baseball_fetcher=MockBaseballFetcher(feed=feed_with())
        )
        processed = await processor.process(session, signal)

        assert processed.signal_status == "forecast_refreshed"
        packet = session.get(MarketResearchPacket, processed.refreshed_research_packet_id)
        assert packet.collector_name == "baseball-external"
        forecast = session.get(MarketForecastRecord, processed.refreshed_forecast_id)
        assert forecast.forecaster_name == "baseball_evidence"
        assert "baseball_evidence_v1" in forecast.calibration_tags
        assert forecast.research_packet_id == packet.id

    async def test_canary_report_shows_forecaster_breakdown(self, session, capsys, monkeypatch):
        _enable_evidence_forecasting(monkeypatch)
        await forecast_via_service(session, total_market())
        total = await cli.research_canary_report(session=session)
        assert total >= 1
        output = capsys.readouterr().out
        assert "forecasts by forecaster: baseball_evidence=1" in output