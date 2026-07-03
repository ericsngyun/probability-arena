import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import cli
from app.config import get_settings
from app.db import Base
from app.models import MarketResearchPacket
from app.services.baseball_research import (
    FALLBACK_PREFIX,
    BaseballExternalResearchCollector,
    build_research_canary_report,
    parse_mlb_ticker,
)
from app.services.forecasting import determine_evidence_depth
from app.services.research import DOMAIN_SPORTS_BASEBALL, create_research_packet
from app.services.signal_workflow import SignalPromotionService
from tests.conftest import make_market
from tests.test_signal_workflow import make_processor, seed_market, seed_signal

MLB_TICKER = "KXMLBTOTAL-26JUL021915STLATL-18"

SCHEDULE = {
    "dates": [
        {
            "games": [
                {
                    "gamePk": 776543,
                    "teams": {
                        "away": {"team": {"abbreviation": "STL", "name": "St. Louis Cardinals"}},
                        "home": {"team": {"abbreviation": "ATL", "name": "Atlanta Braves"}},
                    },
                }
            ]
        }
    ]
}

LIVE_FEED = {
    "gameData": {
        "status": {"abstractGameState": "Live", "detailedState": "In Progress"},
        "teams": {"away": {"name": "St. Louis Cardinals"}, "home": {"name": "Atlanta Braves"}},
        "probablePitchers": {
            "away": {"fullName": "Sonny Gray"},
            "home": {"fullName": "Chris Sale"},
        },
        "weather": {"condition": "Partly Cloudy", "temp": "84", "wind": "8 mph, L To R"},
        "venue": {"name": "Truist Park"},
    },
    "liveData": {
        "linescore": {
            "currentInning": 6,
            "inningState": "Top",
            "outs": 2,
            "offense": {"first": {"id": 1}, "third": {"id": 2}},
            "teams": {"away": {"runs": 4}, "home": {"runs": 3}},
        },
        "boxscore": {
            "teams": {
                "away": {"battingOrder": [1, 2, 3]},
                "home": {"battingOrder": [4, 5, 6]},
            }
        },
    },
}


class MockBaseballFetcher:
    """Canned MLB Stats API payloads; no network."""

    def __init__(self, schedule=SCHEDULE, feed=LIVE_FEED):
        self.schedule = schedule
        self.feed = feed
        self.calls: list[str] = []

    def schedule_url(self, date):
        return f"mock://schedule/{date}"

    def live_feed_url(self, game_pk):
        return f"mock://feed/{game_pk}"

    async def fetch_schedule(self, date):
        self.calls.append(f"schedule:{date}")
        return self.schedule

    async def fetch_live_feed(self, game_pk):
        self.calls.append(f"feed:{game_pk}")
        return self.feed


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def baseball_market(**overrides):
    return make_market(
        ticker=MLB_TICKER,
        title="St. Louis vs Atlanta Total Runs?",
        rules_primary="Resolves YES if total runs exceed 8.5 per the official box score.",
        settlement_source="ESPN (https://www.espn.com); MLB (https://www.mlb.com)",
        **overrides,
    )


def collector(fetcher=None):
    return BaseballExternalResearchCollector(fetcher=fetcher or MockBaseballFetcher())


class TestTickerParsing:
    def test_parses_standard_mlb_ticker(self):
        context = parse_mlb_ticker(MLB_TICKER)
        assert context.date == "2026-07-02"
        assert context.matchup == "STLATL"

    def test_parses_two_letter_team_codes(self):
        context = parse_mlb_ticker("KXMLBHR-26JUL021940TBKC-TBYDIAZ2-1")
        assert context.date == "2026-07-02"
        assert context.matchup == "TBKC"

    def test_non_mlb_ticker_returns_none(self):
        assert parse_mlb_ticker("KXATPMATCH-26JUL02FOO-BAR") is None
        assert parse_mlb_ticker("KXFED-27APR-T4.00") is None


class TestCollector:
    async def test_produces_source_backed_packet(self, session):
        fetcher = MockBaseballFetcher()
        packet = await collector(fetcher).collect(
            baseball_market(), DOMAIN_SPORTS_BASEBALL, "researchable"
        )

        assert packet.research_completeness_score > 0.65
        assert fetcher.calls == ["schedule:2026-07-02", "feed:776543"]
        facts = [f.fact for f in packet.key_facts]
        assert any("Game state" in f and "4" in f and "3" in f for f in facts)
        assert any("Top 6, 2 out(s)" in f and "first/third" in f for f in facts)
        assert any("Sonny Gray" in f and "Chris Sale" in f for f in facts)
        assert any("Confirmed lineups" in f for f in facts)
        assert any("Truist Park" in f and "84" in f for f in facts)
        external = [f for f in packet.key_facts if f.source_name == "statsapi.mlb.com"]
        assert len(external) >= 4
        assert packet.raw_response["fallback"] is False
        assert packet.raw_response["game_pk"] == 776543

    async def test_sources_carry_url_title_credibility_freshness(self, session):
        packet = await collector().collect(
            baseball_market(), DOMAIN_SPORTS_BASEBALL, "researchable"
        )
        external_sources = [s for s in packet.sources if s.credibility == "official"]
        assert len(external_sources) == 2
        for source in external_sources:
            assert source.url.startswith("mock://")
            assert source.title
            assert source.fetched_at  # ISO string
        # settlement/template sources retained after externals, capped at max
        assert len(packet.sources) <= get_settings().baseball_research_max_sources

    async def test_filled_gaps_removed_from_missing_info(self, session):
        packet = await collector().collect(
            baseball_market(), DOMAIN_SPORTS_BASEBALL, "researchable"
        )
        assert "confirmed starting lineup" not in packet.missing_info
        assert "probable pitcher matchup and handedness splits" not in packet.missing_info
        assert "ballpark and weather conditions" not in packet.missing_info
        # form is not fetched -> still a gap
        assert any("form" in gap for gap in packet.missing_info)

    async def test_persisted_packet_is_source_backed_downstream(self, session):
        market_row = seed_market(session, MLB_TICKER)
        packet_row = await create_research_packet(
            session, market_row, collector=collector(), scanner_run_id=None
        )
        assert packet_row.collector_name == "baseball-external"
        assert packet_row.collector_version == "v1"
        assert determine_evidence_depth(packet_row) == "source_backed"
        assert packet_row.raw_response["fallback"] is False
        # provenance persisted in JSON
        official = [s for s in packet_row.sources if s.get("credibility") == "official"]
        assert official and all(s.get("url") and s.get("fetched_at") for s in official)

    async def test_no_matching_game_falls_back_to_template(self, session):
        fetcher = MockBaseballFetcher(schedule={"dates": []})
        packet = await collector(fetcher).collect(
            baseball_market(), DOMAIN_SPORTS_BASEBALL, "researchable"
        )
        assert packet.research_completeness_score <= 0.65
        assert any(gap.startswith(FALLBACK_PREFIX) for gap in packet.missing_info)
        assert packet.raw_response["fallback"] is True
        assert all(f.source_name != "statsapi.mlb.com" for f in packet.key_facts)

    async def test_fetch_failures_fall_back_safely(self, session):
        class DeadFetcher(MockBaseballFetcher):
            async def fetch_schedule(self, date):
                return None

        packet = await collector(DeadFetcher()).collect(
            baseball_market(), DOMAIN_SPORTS_BASEBALL, "researchable"
        )
        assert packet.raw_response == {"fallback": True, "reason": "MLB schedule unavailable"}

    async def test_unparseable_ticker_falls_back(self, session):
        market = baseball_market().model_copy(update={"ticker": "KXMLB-WEIRD-FORMAT"})
        packet = await collector().collect(market, DOMAIN_SPORTS_BASEBALL, "researchable")
        assert packet.raw_response["fallback"] is True
        assert "ticker not parseable" in packet.raw_response["reason"]

    async def test_fallback_depth_stays_template_only(self, session):
        market_row = seed_market(session, MLB_TICKER)
        fetcher = MockBaseballFetcher(schedule={"dates": []})
        packet_row = await create_research_packet(
            session, market_row, collector=collector(fetcher), scanner_run_id=None
        )
        assert packet_row.collector_name == "baseball-external"
        assert determine_evidence_depth(packet_row) == "template_only"  # honest


def _enable_canary(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_baseball_external_research", True)


def seed_baseball_market(session):
    from app.models import Market

    row = Market(
        ticker=MLB_TICKER,
        title="St. Louis vs Atlanta Total Runs?",
        status="active",
        rules_primary="Resolves YES if total runs exceed 8.5 per the official box score.",
    )
    session.add(row)
    session.commit()
    return row


class TestProcessingIntegration:
    async def test_promoted_baseball_signal_uses_external_collector_when_enabled(
        self, session, monkeypatch
    ):
        _enable_canary(monkeypatch)
        seed_baseball_market(session)
        signal = seed_signal(session, ticker=MLB_TICKER)
        SignalPromotionService().promote(session, signal.id)

        processor = make_processor(collector=None, baseball_fetcher=MockBaseballFetcher())
        processed = await processor.process(session, signal)

        assert processed.signal_status == "forecast_refreshed"
        packet = session.get(MarketResearchPacket, processed.refreshed_research_packet_id)
        assert packet.collector_name == "baseball-external"
        assert determine_evidence_depth(packet) == "source_backed"
        assert packet.research_completeness_score > 0.65
        # source-backed forecast gets the higher confidence ceiling available
        from app.models import MarketForecastRecord

        forecast = session.get(MarketForecastRecord, processed.refreshed_forecast_id)
        assert forecast.evidence_depth == "source_backed"

    async def test_flag_false_falls_back_to_template(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_baseball_external_research", False)
        seed_baseball_market(session)
        signal = seed_signal(session, ticker=MLB_TICKER)
        SignalPromotionService().promote(session, signal.id)

        processor = make_processor(collector=None, baseball_fetcher=MockBaseballFetcher())
        processed = await processor.process(session, signal)
        packet = session.get(MarketResearchPacket, processed.refreshed_research_packet_id)
        assert packet.collector_name == "template"

    async def test_non_baseball_domain_falls_back_to_template(self, session, monkeypatch):
        _enable_canary(monkeypatch)
        from app.models import Market

        session.add(
            Market(
                ticker="KXATPMATCH-26JUL02FOO-BAR",
                title="Tennis match winner?",
                status="active",
                rules_primary="Resolves YES if the player wins per ATP (https://www.atptour.com).",
            )
        )
        session.commit()
        signal = seed_signal(session, ticker="KXATPMATCH-26JUL02FOO-BAR")
        SignalPromotionService().promote(session, signal.id)

        processor = make_processor(collector=None, baseball_fetcher=MockBaseballFetcher())
        processed = await processor.process(session, signal)
        packet = session.get(MarketResearchPacket, processed.refreshed_research_packet_id)
        assert packet.collector_name == "template"

    async def test_non_researchable_resolution_falls_back_to_template(self, session, monkeypatch):
        _enable_canary(monkeypatch)
        from app.schemas import ResolutionAssessment
        from app.services.resolution import MockResolutionJudge

        seed_baseball_market(session)
        signal = seed_signal(session, ticker=MLB_TICKER)
        SignalPromotionService().promote(session, signal.id)

        manual_review_judge = MockResolutionJudge(
            ResolutionAssessment(
                clarity_score=0.5,
                resolution_risk="medium",
                tradeability="needs_manual_review",
            )
        )
        processor = make_processor(
            collector=None, judge=manual_review_judge, baseball_fetcher=MockBaseballFetcher()
        )
        processed = await processor.process(session, signal)
        packet = session.get(MarketResearchPacket, processed.refreshed_research_packet_id)
        assert packet.collector_name == "template"

    async def test_injected_collector_always_wins(self, session, monkeypatch):
        _enable_canary(monkeypatch)
        seed_baseball_market(session)
        signal = seed_signal(session, ticker=MLB_TICKER)
        SignalPromotionService().promote(session, signal.id)

        processor = make_processor()  # explicit template collector injected
        processed = await processor.process(session, signal)
        packet = session.get(MarketResearchPacket, processed.refreshed_research_packet_id)
        assert packet.collector_name == "template"


class TestCanaryReport:
    async def test_report_counts_collectors_depths_and_fallbacks(self, session):
        market_row = seed_market(session, MLB_TICKER)
        await create_research_packet(session, market_row, collector=collector())  # source_backed
        await create_research_packet(
            session, market_row, collector=collector(MockBaseballFetcher(schedule={"dates": []}))
        )  # fallback
        from app.services.research import TemplateResearchCollector

        await create_research_packet(
            session, market_row, collector=TemplateResearchCollector(name="template", version="v1")
        )

        report = build_research_canary_report(session)
        assert report.total_packets == 3
        assert report.by_collector["baseball-external"].count == 2
        assert report.by_collector["baseball-external"].by_evidence_depth == {
            "source_backed": 1,
            "template_only": 1,
        }
        assert report.by_collector["template"].count == 1
        assert report.by_domain["sports_baseball"] == 3
        assert report.external_fallbacks == 1

    async def test_cli_research_canary_report(self, session, capsys):
        market_row = seed_market(session, MLB_TICKER)
        await create_research_packet(session, market_row, collector=collector())
        total = await cli.research_canary_report(session=session)
        assert total == 1
        output = capsys.readouterr().out
        assert "research canary: packets=1 external_fallbacks=0" in output
        assert "collector=baseball-external" in output
        assert "source_backed=1" in output

    async def test_cli_process_prints_research_info(self, session, capsys, monkeypatch):
        _enable_canary(monkeypatch)
        seed_baseball_market(session)
        signal = seed_signal(session, ticker=MLB_TICKER)
        SignalPromotionService().promote(session, signal.id)

        processor = make_processor(collector=None, baseball_fetcher=MockBaseballFetcher())
        await cli.process_promoted_signals(limit=5, services=processor, session=session)
        output = capsys.readouterr().out
        assert "research=baseball-external/source_backed" in output
        assert "completeness=0.9" in output or "completeness=1.00" in output

    def test_main_wires_canary_report(self, monkeypatch):
        captured = {}

        async def fake_report(session=None):
            captured["ran"] = True
            return 0

        monkeypatch.setattr(cli, "research_canary_report", fake_report)
        assert cli.main(["research-canary-report"]) == 0
        assert captured == {"ran": True}