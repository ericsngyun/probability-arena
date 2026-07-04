import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.config import get_settings
from app.db import Base
from app.models import MarketResearchPacket
from app.schemas import ResearchPacketOut
from app.services.baseball_research import build_research_canary_report
from app.services.forecasting import determine_evidence_depth
from app.services.research import DOMAIN_SPORTS_SOCCER, create_research_packet
from app.services.signal_workflow import SignalPromotionService
from app.services.soccer_research import (
    FALLBACK_PREFIX,
    EspnSoccerApiFetcher,
    SoccerExternalResearchCollector,
    get_soccer_fetcher,
    parse_soccer_ticker,
)
from tests.conftest import make_market
from tests.test_signal_workflow import make_processor, seed_market, seed_signal

WC_TICKER = "KXWCGAME-26JUN141800USAWAL"
MOCK_SOURCE = "mock.soccer.provider"

SCOREBOARD = {
    "events": [
        {
            "id": "633838",
            "competitions": [
                {
                    "competitors": [
                        {
                            "homeAway": "home",
                            "score": "2",
                            "team": {"abbreviation": "USA", "displayName": "United States"},
                            "statistics": [
                                {"name": "possessionPct", "displayValue": "55.3"},
                                {"name": "totalShots", "displayValue": "12"},
                            ],
                        },
                        {
                            "homeAway": "away",
                            "score": "1",
                            "team": {"abbreviation": "WAL", "displayName": "Wales"},
                            "statistics": [
                                {"name": "possessionPct", "displayValue": "44.7"},
                                {"name": "totalShots", "displayValue": "8"},
                            ],
                        },
                    ],
                    "details": [
                        {
                            "redCard": True,
                            "team": {"displayName": "Wales"},
                            "clock": {"displayValue": "62'"},
                        }
                    ],
                }
            ],
            "status": {
                "displayClock": "74'",
                "period": 2,
                "type": {
                    "name": "STATUS_IN_PROGRESS",
                    "state": "in",
                    "description": "In Progress",
                },
            },
        }
    ]
}

MATCH_DETAILS = {
    "rosters": [
        {"roster": [{"starter": True, "athlete": {"displayName": "Player A"}}]},
        {"roster": [{"starter": True, "athlete": {"displayName": "Player B"}}]},
    ]
}


class MockSoccerFetcher:
    """Canned scoreboard/details payloads; no network."""

    source_name = MOCK_SOURCE

    def __init__(self, scoreboard=SCOREBOARD, details=MATCH_DETAILS):
        self.scoreboard = scoreboard
        self.details = details
        self.calls: list[str] = []

    def scoreboard_url(self, league, date):
        return f"mock://scoreboard/{league}/{date}"

    def match_details_url(self, league, event_id):
        return f"mock://details/{league}/{event_id}"

    async def fetch_scoreboard(self, league, date):
        self.calls.append(f"scoreboard:{league}:{date}")
        return self.scoreboard

    async def fetch_match_details(self, league, event_id):
        self.calls.append(f"details:{league}:{event_id}")
        return self.details


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def soccer_market(**overrides):
    return make_market(
        ticker=WC_TICKER,
        title="USA vs Wales: Who wins?",
        rules_primary="Resolves YES if the United States wins per the official FIFA match report.",
        settlement_source="FIFA (https://www.fifa.com)",
        **overrides,
    )


def collector(fetcher=None):
    return SoccerExternalResearchCollector(fetcher=fetcher or MockSoccerFetcher())


class TestTickerParsing:
    def test_parses_world_cup_ticker_with_time_block(self):
        context = parse_soccer_ticker(WC_TICKER)
        assert context.date == "2026-06-14"
        assert context.matchup == "USAWAL"
        assert context.team_a == "USA" and context.team_b == "WAL"
        assert context.league == "fifa.world"
        assert context.market_type == "winner"
        assert context.line is None

    def test_parses_total_ticker_with_line(self):
        context = parse_soccer_ticker("KXWCTOTAL-26JUN14USAWAL-2.5")
        assert context.market_type == "total"
        assert context.line == 2.5

    def test_parses_other_league_prefixes(self):
        assert parse_soccer_ticker("KXUCLGAME-26MAY30PSGINT").league == "uefa.champions"
        assert parse_soccer_ticker("KXEPLMATCH-26AUG15ARSCHE").league == "eng.1"
        assert parse_soccer_ticker("KXMLSGAME-26JUL04LAFCSEA").league == "usa.1"

    def test_odd_length_matchup_keeps_raw_string(self):
        context = parse_soccer_ticker("KXWCGAME-26JUN14RSANEDX")
        assert context.matchup == "RSANEDX"
        assert context.team_a is None and context.team_b is None

    def test_unknown_shapes_return_none(self):
        assert parse_soccer_ticker("KXWC-WEIRD-FORMAT") is None
        assert parse_soccer_ticker("KXNBA-26JUN14LALBOS") is None
        assert parse_soccer_ticker("KXMLBTOTAL-26JUL021915STLATL-18") is None
        assert parse_soccer_ticker("KXFED-27APR-T4.00") is None

    def test_non_numeric_suffix_degrades_to_no_line(self):
        context = parse_soccer_ticker("KXWCGAME-26JUN14USAWAL-USA")
        assert context is not None
        assert context.line is None


class TestProviderSelection:
    def test_template_provider_returns_no_fetcher(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "soccer_research_provider", "template")
        assert get_soccer_fetcher() is None

    def test_espn_provider_returns_read_only_fetcher(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "soccer_research_provider", "espn")
        fetcher = get_soccer_fetcher()
        assert isinstance(fetcher, EspnSoccerApiFetcher)
        assert fetcher.timeout == get_settings().soccer_research_timeout_seconds

    def test_unknown_provider_falls_back_to_no_fetcher(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "soccer_research_provider", "bogus")
        assert get_soccer_fetcher() is None

    def test_espn_urls_are_read_only_gets(self):
        fetcher = EspnSoccerApiFetcher()
        assert (
            fetcher.scoreboard_url("fifa.world", "2026-06-14")
            == "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates=20260614"
        )
        assert (
            fetcher.match_details_url("fifa.world", "633838")
            == "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event=633838"
        )


class TestCollector:
    async def test_produces_source_backed_packet(self, session):
        fetcher = MockSoccerFetcher()
        packet = await collector(fetcher).collect(
            soccer_market(), DOMAIN_SPORTS_SOCCER, "researchable"
        )

        assert packet.research_completeness_score > 0.65
        assert fetcher.calls == [
            "scoreboard:fifa.world:2026-06-14",
            "details:fifa.world:633838",
        ]
        facts = [f.fact for f in packet.key_facts]
        assert any("Match state" in f and "2" in f and "1" in f for f in facts)
        assert any("clock 74', period 2" in f for f in facts)
        assert any("Red cards" in f and "Wales" in f and "62'" in f for f in facts)
        assert any("Confirmed lineups" in f for f in facts)
        assert any("possessionPct 55.3–44.7" in f and "totalShots 12–8" in f for f in facts)
        external = [f for f in packet.key_facts if f.source_name == MOCK_SOURCE]
        assert len(external) >= 4
        assert packet.raw_response["fallback"] is False
        assert packet.raw_response["event_id"] == "633838"
        assert packet.raw_response["extracted"]["market_type"] == "winner"

    async def test_sources_carry_url_title_credibility_freshness(self, session):
        packet = await collector().collect(soccer_market(), DOMAIN_SPORTS_SOCCER, "researchable")
        external_sources = [s for s in packet.sources if s.credibility == "high"]
        assert len(external_sources) == 2
        for source in external_sources:
            assert source.url.startswith("mock://")
            assert source.title
            assert source.source_type == "stats_provider"
            assert source.fetched_at  # ISO string
        assert len(packet.sources) <= get_settings().soccer_research_max_sources

    async def test_shootout_state_reported_when_present(self, session):
        scoreboard = {
            "events": [
                {
                    "id": "9",
                    "competitions": [
                        {
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "score": "1",
                                    "shootoutScore": 4,
                                    "team": {"abbreviation": "USA", "displayName": "United States"},
                                },
                                {
                                    "homeAway": "away",
                                    "score": "1",
                                    "shootoutScore": 3,
                                    "team": {"abbreviation": "WAL", "displayName": "Wales"},
                                },
                            ]
                        }
                    ],
                    "status": {
                        "type": {
                            "name": "STATUS_FINAL_PEN",
                            "state": "post",
                            "description": "Final PEN",
                        }
                    },
                }
            ]
        }
        packet = await collector(MockSoccerFetcher(scoreboard=scoreboard, details=None)).collect(
            soccer_market(), DOMAIN_SPORTS_SOCCER, "researchable"
        )
        facts = [f.fact for f in packet.key_facts]
        assert any("Penalty shootout" in f and "4" in f and "3" in f for f in facts)
        assert packet.raw_response["extracted"]["shootout"] == {"home": 4, "away": 3}

    async def test_filled_gaps_removed_missing_info_stays_honest(self, session):
        packet = await collector().collect(soccer_market(), DOMAIN_SPORTS_SOCCER, "researchable")
        # lineups were fetched -> gap closed
        assert "confirmed lineups" not in packet.missing_info
        # pre-match team news / recent form are NOT fetched -> still gaps
        assert any("team news" in gap for gap in packet.missing_info)
        assert any("recent form" in gap for gap in packet.missing_info)

    async def test_persisted_packet_is_source_backed_downstream(self, session):
        market_row = seed_market(session, WC_TICKER)
        packet_row = await create_research_packet(
            session, market_row, collector=collector(), scanner_run_id=None
        )
        assert packet_row.collector_name == "soccer-external"
        assert packet_row.collector_version == "v1"
        assert determine_evidence_depth(packet_row) == "source_backed"
        assert packet_row.raw_response["fallback"] is False
        provenance = [s for s in packet_row.sources if s.get("credibility") == "high"]
        assert provenance and all(s.get("url") and s.get("fetched_at") for s in provenance)

    async def test_no_matching_event_falls_back_to_template(self, session):
        fetcher = MockSoccerFetcher(scoreboard={"events": []})
        packet = await collector(fetcher).collect(
            soccer_market(), DOMAIN_SPORTS_SOCCER, "researchable"
        )
        assert packet.research_completeness_score <= 0.65
        assert any(gap.startswith(FALLBACK_PREFIX) for gap in packet.missing_info)
        assert packet.raw_response["fallback"] is True
        assert all(f.source_name != MOCK_SOURCE for f in packet.key_facts)

    async def test_fetch_failures_fall_back_safely(self, session):
        class DeadFetcher(MockSoccerFetcher):
            async def fetch_scoreboard(self, league, date):
                return None

        packet = await collector(DeadFetcher()).collect(
            soccer_market(), DOMAIN_SPORTS_SOCCER, "researchable"
        )
        assert packet.raw_response == {"fallback": True, "reason": "scoreboard unavailable"}

    async def test_unparseable_ticker_falls_back(self, session):
        market = soccer_market().model_copy(update={"ticker": "KXWC-WEIRD-FORMAT"})
        packet = await collector().collect(market, DOMAIN_SPORTS_SOCCER, "researchable")
        assert packet.raw_response["fallback"] is True
        assert "ticker not parseable" in packet.raw_response["reason"]

    async def test_no_fetcher_configured_falls_back(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "soccer_research_provider", "template")
        packet = await SoccerExternalResearchCollector(fetcher=None).collect(
            soccer_market(), DOMAIN_SPORTS_SOCCER, "researchable"
        )
        assert packet.raw_response["fallback"] is True
        assert "template" in packet.raw_response["reason"]

    async def test_fallback_depth_stays_template_only(self, session):
        market_row = seed_market(session, WC_TICKER)
        fetcher = MockSoccerFetcher(scoreboard={"events": []})
        packet_row = await create_research_packet(
            session, market_row, collector=collector(fetcher), scanner_run_id=None
        )
        assert packet_row.collector_name == "soccer-external"
        assert determine_evidence_depth(packet_row) == "template_only"  # honest

    async def test_api_serialization_excludes_raw_response(self, session):
        market_row = seed_market(session, WC_TICKER)
        packet_row = await create_research_packet(
            session, market_row, collector=collector(), scanner_run_id=None
        )
        assert packet_row.raw_response is not None  # persisted for audit
        out = ResearchPacketOut.model_validate(packet_row)
        assert "raw_response" not in out.model_dump()
        assert "raw_response" not in out.model_dump_json()


def _enable_canary(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_soccer_external_research", True)


def seed_soccer_market(session):
    from app.models import Market

    row = Market(
        ticker=WC_TICKER,
        title="USA vs Wales: Who wins?",
        status="active",
        rules_primary="Resolves YES if the United States wins per the official FIFA match report.",
    )
    session.add(row)
    session.commit()
    return row


class TestProcessingIntegration:
    async def test_promoted_soccer_signal_uses_external_collector_when_enabled(
        self, session, monkeypatch
    ):
        _enable_canary(monkeypatch)
        seed_soccer_market(session)
        signal = seed_signal(session, ticker=WC_TICKER)
        SignalPromotionService().promote(session, signal.id)

        processor = make_processor(collector=None, soccer_fetcher=MockSoccerFetcher())
        processed = await processor.process(session, signal)

        assert processed.signal_status == "forecast_refreshed"
        packet = session.get(MarketResearchPacket, processed.refreshed_research_packet_id)
        assert packet.collector_name == "soccer-external"
        assert determine_evidence_depth(packet) == "source_backed"
        assert packet.research_completeness_score > 0.65
        from app.models import MarketForecastRecord

        forecast = session.get(MarketForecastRecord, processed.refreshed_forecast_id)
        assert forecast.evidence_depth == "source_backed"

    async def test_flag_false_falls_back_to_template(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_soccer_external_research", False)
        seed_soccer_market(session)
        signal = seed_signal(session, ticker=WC_TICKER)
        SignalPromotionService().promote(session, signal.id)

        processor = make_processor(collector=None, soccer_fetcher=MockSoccerFetcher())
        processed = await processor.process(session, signal)
        packet = session.get(MarketResearchPacket, processed.refreshed_research_packet_id)
        assert packet.collector_name == "template"

    async def test_non_soccer_domain_falls_back_to_template(self, session, monkeypatch):
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

        processor = make_processor(collector=None, soccer_fetcher=MockSoccerFetcher())
        processed = await processor.process(session, signal)
        packet = session.get(MarketResearchPacket, processed.refreshed_research_packet_id)
        assert packet.collector_name == "template"

    async def test_flag_on_but_template_provider_falls_back_honestly(
        self, session, monkeypatch
    ):
        """Flag flipped but SOCCER_RESEARCH_PROVIDER=template: the soccer
        collector is selected (observable in reports) but produces an honest
        template-depth packet with the reason recorded."""
        _enable_canary(monkeypatch)
        monkeypatch.setattr(get_settings(), "soccer_research_provider", "template")
        seed_soccer_market(session)
        signal = seed_signal(session, ticker=WC_TICKER)
        SignalPromotionService().promote(session, signal.id)

        processor = make_processor(collector=None)  # no injected fetcher
        processed = await processor.process(session, signal)
        packet = session.get(MarketResearchPacket, processed.refreshed_research_packet_id)
        assert packet.collector_name == "soccer-external"
        assert determine_evidence_depth(packet) == "template_only"
        assert packet.raw_response["fallback"] is True

    async def test_injected_collector_always_wins(self, session, monkeypatch):
        _enable_canary(monkeypatch)
        seed_soccer_market(session)
        signal = seed_signal(session, ticker=WC_TICKER)
        SignalPromotionService().promote(session, signal.id)

        processor = make_processor()  # explicit template collector injected
        processed = await processor.process(session, signal)
        packet = session.get(MarketResearchPacket, processed.refreshed_research_packet_id)
        assert packet.collector_name == "template"

    async def test_baseball_canary_unaffected(self, session, monkeypatch):
        """Soccer flag on must not hijack baseball signals (and vice versa)."""
        _enable_canary(monkeypatch)
        monkeypatch.setattr(get_settings(), "enable_baseball_external_research", False)
        from app.models import Market

        session.add(
            Market(
                ticker="KXMLBTOTAL-26JUL021915STLATL-18",
                title="St. Louis vs Atlanta Total Runs?",
                status="active",
                rules_primary="Resolves YES if total runs exceed 8.5 per the official box score.",
            )
        )
        session.commit()
        signal = seed_signal(session, ticker="KXMLBTOTAL-26JUL021915STLATL-18")
        SignalPromotionService().promote(session, signal.id)

        processor = make_processor(collector=None, soccer_fetcher=MockSoccerFetcher())
        processed = await processor.process(session, signal)
        packet = session.get(MarketResearchPacket, processed.refreshed_research_packet_id)
        assert packet.collector_name == "template"


class TestCanaryReport:
    async def test_report_counts_soccer_collector_and_fallbacks(self, session):
        market_row = seed_market(session, WC_TICKER)
        await create_research_packet(session, market_row, collector=collector())  # source_backed
        await create_research_packet(
            session, market_row, collector=collector(MockSoccerFetcher(scoreboard={"events": []}))
        )  # fallback
        from app.services.research import TemplateResearchCollector

        await create_research_packet(
            session, market_row, collector=TemplateResearchCollector(name="template", version="v1")
        )

        report = build_research_canary_report(session)
        assert report.total_packets == 3
        assert report.by_collector["soccer-external"].count == 2
        assert report.by_collector["soccer-external"].by_evidence_depth == {
            "source_backed": 1,
            "template_only": 1,
        }
        assert report.by_domain["sports_soccer"] == 3
        assert report.external_fallbacks == 1

    async def test_cli_research_canary_report_includes_soccer(self, session, capsys):
        market_row = seed_market(session, WC_TICKER)
        await create_research_packet(session, market_row, collector=collector())
        total = await cli.research_canary_report(session=session)
        assert total == 1
        output = capsys.readouterr().out
        assert "research canary: packets=1 external_fallbacks=0" in output
        assert "collector=soccer-external" in output
        assert "source_backed=1" in output
        assert "sports_soccer=1" in output

    async def test_cli_process_prints_soccer_research_info(self, session, capsys, monkeypatch):
        _enable_canary(monkeypatch)
        seed_soccer_market(session)
        signal = seed_signal(session, ticker=WC_TICKER)
        SignalPromotionService().promote(session, signal.id)

        processor = make_processor(collector=None, soccer_fetcher=MockSoccerFetcher())
        await cli.process_promoted_signals(limit=5, services=processor, session=session)
        output = capsys.readouterr().out
        assert "research=soccer-external/source_backed" in output
        assert "completeness=" in output

    async def test_cli_signal_report_includes_canary_metrics(self, session, capsys):
        market_row = seed_market(session, WC_TICKER)
        await create_research_packet(session, market_row, collector=collector())
        total = await cli.signal_report(session=session)
        assert total == 0  # no signals seeded; report still renders
        # research canary metrics ride along in the underlying report object
        from app.services.signal_workflow import build_signal_report

        report = build_signal_report(session)
        assert report.research_canary.by_collector["soccer-external"].count == 1
