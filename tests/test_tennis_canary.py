"""Tennis evidence canary (TENNIS-001) tests: ticker/market-spec parsing,
provider selection, collector (source-backed + honest fallback), forecaster
(conservative capped adjustment + confidence caps + calibration tags), and
integration through SignalProcessingService / ForecastingService. Everything
mocked — no live network. Read-only measurement; no trade semantics."""

import copy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.config import get_settings
from app.db import Base
from app.models import MarketForecastRecord, MarketResearchPacket
from app.services.forecasting import (
    ForecastingService,
    ForecastInput,
    determine_evidence_depth,
)
from app.services.research import DOMAIN_SPORTS_TENNIS, create_research_packet
from app.services.signal_workflow import SignalPromotionService
from app.services.resolution import (
    RuleBasedResolutionJudge,
    latest_assessment_for,
    persist_assessment,
)
from app.services.tennis_forecasting import (
    MAX_PRIOR_SHIFT,
    TennisEvidenceAwareForecaster,
    parse_tennis_market_spec,
)
from app.services.tennis_research import (
    EspnTennisApiFetcher,
    TennisExternalResearchCollector,
    get_tennis_fetcher,
    parse_tennis_ticker,
)
from tests.conftest import make_market
from tests.test_signal_workflow import make_processor, seed_market, seed_signal

# matchup DJOKALCZ -> player A DJOK, player B ALCZ; suffix DJOK = subject player A
WINNER_TICKER = "KXATPMATCH-25MAY26DJOKALCZ-DJOK"
MOCK_SOURCE = "mock.tennis.provider"
JUDGE = RuleBasedResolutionJudge(min_clarity_score=0.70, prompt_version="v1")


def scoreboard(sets_a=(6, 3), sets_b=(4, 2), state="in", a_winner=False, b_winner=False,
               detail="2nd Set"):
    return {
        "events": [
            {
                "id": "401234",
                "season": {"displayName": "Roland Garros 2025"},
                "competitions": [
                    {
                        "surface": {"name": "Clay"},
                        "situation": {"server": 0},
                        "competitors": [
                            {
                                "order": 0,
                                "athlete": {"displayName": "N. Djokovic", "abbreviation": "DJOK"},
                                "linescores": [{"value": v} for v in sets_a],
                                "winner": a_winner, "rank": 1, "seed": 1,
                            },
                            {
                                "order": 1,
                                "athlete": {"displayName": "C. Alcaraz", "abbreviation": "ALCZ"},
                                "linescores": [{"value": v} for v in sets_b],
                                "winner": b_winner, "rank": 2, "seed": 2,
                            },
                        ],
                    }
                ],
                "status": {
                    "type": {
                        "name": "STATUS_IN_PROGRESS" if state == "in" else "STATUS_FINAL",
                        "state": state,
                        "description": "In Progress" if state == "in" else "Final",
                        "detail": detail,
                    }
                },
            }
        ]
    }


class MockTennisFetcher:
    source_name = MOCK_SOURCE

    def __init__(self, board=None, details=None):
        self.board = board if board is not None else scoreboard()
        self.details = details
        self.calls: list[str] = []

    def scoreboard_url(self, tour, date):
        return f"mock://scoreboard/{tour}/{date}"

    def match_details_url(self, tour, event_id):
        return f"mock://details/{tour}/{event_id}"

    async def fetch_scoreboard(self, tour, date):
        self.calls.append(f"scoreboard:{tour}:{date}")
        return self.board

    async def fetch_match_details(self, tour, event_id):
        self.calls.append(f"details:{tour}:{event_id}")
        return self.details


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def tennis_market(ticker=WINNER_TICKER, bid=48, ask=52, title="Djokovic vs Alcaraz: Djokovic wins?"):
    return make_market(
        ticker=ticker, title=title, yes_bid=bid, yes_ask=ask,
        rules_primary="Resolves YES if Djokovic wins per the official ATP result.",
        settlement_source="ATP Tour (https://www.atptour.com)",
    )


def collector(fetcher=None):
    return TennisExternalResearchCollector(fetcher=fetcher or MockTennisFetcher())


# --- parser -----------------------------------------------------------------

class TestParser:
    def test_parses_supported_winner_ticker(self):
        ctx = parse_tennis_ticker(WINNER_TICKER)
        assert ctx is not None
        assert ctx.tour == "atp" and ctx.market_type == "winner"
        assert ctx.date == "2025-05-26"

    def test_market_spec_identifies_subject_player(self):
        spec = parse_tennis_market_spec(WINNER_TICKER)
        assert spec.market_type == "winner"
        assert spec.player == "DJOK" and spec.player_is_a is True

    def test_rejects_unknown_and_prop_shapes(self):
        assert parse_tennis_ticker("KXNBA-FOO") is None          # not tennis
        assert parse_tennis_ticker("garbage") is None
        # a non-winner tennis series is 'unknown' market_type
        spec = parse_tennis_market_spec("KXATPACES-25MAY26DJOKALCZ-DJOK")
        assert spec.market_type == "unknown"


class TestProviderSelection:
    def test_template_provider_yields_no_fetcher(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "tennis_research_provider", "template")
        assert get_tennis_fetcher() is None

    def test_espn_provider_yields_fetcher(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "tennis_research_provider", "espn")
        assert isinstance(get_tennis_fetcher(), EspnTennisApiFetcher)

    def test_unknown_provider_falls_back_to_none(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "tennis_research_provider", "mystery")
        assert get_tennis_fetcher() is None


# --- collector --------------------------------------------------------------

class TestCollector:
    async def test_source_backed_packet_with_mocked_evidence(self, session):
        packet = await collector().collect(tennis_market(), DOMAIN_SPORTS_TENNIS, "researchable")
        assert packet.research_completeness_score > 0.65  # above template ceiling
        external = [f for f in packet.key_facts if f.source_name == MOCK_SOURCE]
        assert len(external) >= 3
        assert any("Match state:" in f.fact for f in external)
        assert packet.raw_response["fallback"] is False
        assert packet.raw_response["extracted"]["sets"] == {"a": 1, "b": 0}
        assert packet.raw_response["extracted"]["games"] == {"a": 3, "b": 2}

    async def test_sources_carry_provenance(self, session):
        packet = await collector().collect(tennis_market(), DOMAIN_SPORTS_TENNIS, "researchable")
        ext = [s for s in packet.sources if s.credibility == "high"]
        assert ext and all(s.url and s.title and s.fetched_at for s in ext)

    async def test_persisted_packet_is_source_backed(self, session):
        market_row = seed_market(session, WINNER_TICKER)
        packet_row = await create_research_packet(session, market_row, collector=collector())
        assert determine_evidence_depth(packet_row) == "source_backed"

    async def test_no_fetcher_falls_back_template(self, session):
        c = TennisExternalResearchCollector(fetcher=None, settings=get_settings())
        packet = await c.collect(tennis_market(), DOMAIN_SPORTS_TENNIS, "researchable")
        assert packet.raw_response["fallback"] is True
        assert packet.research_completeness_score <= 0.65

    async def test_unparseable_ticker_falls_back(self, session):
        packet = await collector().collect(
            tennis_market(ticker="KXNBA-FOO"), DOMAIN_SPORTS_TENNIS, "researchable"
        )
        assert packet.raw_response["fallback"] is True

    async def test_fetch_failure_falls_back(self, session):
        class Dead(MockTennisFetcher):
            async def fetch_scoreboard(self, tour, date):
                return None

        packet = await collector(Dead()).collect(tennis_market(), DOMAIN_SPORTS_TENNIS, "researchable")
        assert packet.raw_response == {"fallback": True, "reason": "scoreboard unavailable"}


# --- forecaster -------------------------------------------------------------

async def make_input(session, market_data, board=None) -> ForecastInput:
    from app.models import MarketDetailEnrichment

    market_row = seed_market(session, market_data.ticker)
    session.add(
        MarketDetailEnrichment(
            market_ticker=market_data.ticker, title=market_data.title,
            rules_text=market_data.rules_primary,
            settlement_source=market_data.settlement_source or "ATP Tour (https://www.atptour.com)",
            raw_market_detail={},
        )
    )
    session.commit()
    assessment = await JUDGE.assess(market_data)
    persist_assessment(session, market_data.ticker, assessment, JUDGE)
    c = TennisExternalResearchCollector(fetcher=MockTennisFetcher(board=board or scoreboard()))
    packet = await create_research_packet(session, market_row, collector=c)
    return ForecastInput(
        market=market_data, packet=packet,
        resolution=latest_assessment_for(session, market_data.ticker),
    )


class TestForecaster:
    async def test_source_backed_winner_adjusts_conservatively(self, session):
        inp = await make_input(session, tennis_market(bid=48, ask=52))  # subject leads 1-0 sets
        fc = await TennisEvidenceAwareForecaster().forecast(inp)
        assert fc.estimated_probability > 0.50  # leading -> above prior
        assert abs(fc.estimated_probability - 0.50) <= MAX_PRIOR_SHIFT + 1e-9  # tight cap
        assert "tennis_evidence_v1" in fc.calibration_tags
        assert "market_type_winner" in fc.calibration_tags
        assert "match_state" in fc.calibration_tags
        assert "source_backed" in fc.calibration_tags
        assert "evidence_adjusted" in fc.calibration_tags
        assert fc.confidence <= 0.65  # conservative cap
        assert fc.forecast_risk in ("low", "medium", "high")

    async def test_final_match_adjusts_up_within_cap(self, session):
        board = scoreboard(sets_a=(6, 6, 6), sets_b=(4, 3, 2), state="post", a_winner=True, detail="Final")
        inp = await make_input(session, tennis_market(), board=board)
        fc = await TennisEvidenceAwareForecaster().forecast(inp)
        assert fc.estimated_probability > 0.50  # winner known -> pushed up
        assert abs(fc.estimated_probability - 0.50) <= MAX_PRIOR_SHIFT + 1e-9  # still capped
        assert "evidence_adjusted" in fc.calibration_tags

    async def test_missing_evidence_caps_confidence(self, session):
        # pre-match: tournament/surface/rank facts exist (source_backed) but no
        # live set data -> estimate insufficient -> conf <= 0.50, high risk
        board = scoreboard(sets_a=(), sets_b=(), state="pre")
        inp = await make_input(session, tennis_market(), board=board)
        fc = await TennisEvidenceAwareForecaster().forecast(inp)
        assert fc.confidence <= 0.50
        assert fc.forecast_risk == "high"
        assert "evidence_insufficient" in fc.calibration_tags

    async def test_unknown_market_falls_back_to_template(self, session):
        # non-winner tennis series: collector falls back (v1 winner-only) so the
        # packet is template_only -> forecaster falls back with a skeptic note
        inp = await make_input(session, tennis_market(ticker="KXATPACES-25MAY26DJOKALCZ-DJOK"))
        fc = await TennisEvidenceAwareForecaster().forecast(inp)
        assert "Evidence-aware tennis forecasting not applied" in " ".join(fc.skeptic_notes)

    async def test_wrong_domain_falls_back(self, session):
        inp = await make_input(session, tennis_market())
        object.__setattr__(inp.packet, "domain", "sports_baseball")
        fc = await TennisEvidenceAwareForecaster().forecast(inp)
        assert "Evidence-aware tennis forecasting not applied" in " ".join(fc.skeptic_notes)


# --- integration ------------------------------------------------------------

class TestIntegration:
    async def test_both_flags_on_creates_tennis_evidence_forecast(self, session, monkeypatch):
        s = get_settings()
        monkeypatch.setattr(s, "enable_tennis_external_research", True)
        monkeypatch.setattr(s, "enable_tennis_evidence_forecasting", True)
        seed_market(session, WINNER_TICKER)
        sig = seed_signal(session, ticker=WINNER_TICKER)
        SignalPromotionService().promote(session, sig.id)
        processor = make_processor(
            collector=None, forecaster=None, tennis_fetcher=MockTennisFetcher()
        )
        await processor.process(session, sig)
        fc = session.query(MarketForecastRecord).filter_by(market_ticker=WINNER_TICKER).first()
        assert fc is not None and fc.forecaster_name == "tennis_evidence"
        assert fc.evidence_depth == "source_backed"

    async def test_flag_off_stays_template(self, session, monkeypatch):
        s = get_settings()
        monkeypatch.setattr(s, "enable_tennis_external_research", False)
        monkeypatch.setattr(s, "enable_tennis_evidence_forecasting", False)
        seed_market(session, WINNER_TICKER)
        sig = seed_signal(session, ticker=WINNER_TICKER)
        SignalPromotionService().promote(session, sig.id)
        processor = make_processor(collector=None, tennis_fetcher=MockTennisFetcher())
        await processor.process(session, sig)
        fc = session.query(MarketForecastRecord).filter_by(market_ticker=WINNER_TICKER).first()
        assert fc.forecaster_name != "tennis_evidence"

    async def test_injected_forecaster_still_wins(self, session, monkeypatch):
        monkeypatch.setattr(get_settings(), "enable_tennis_evidence_forecasting", True)
        inp = await make_input(session, tennis_market())
        from app.services.forecasting import TemplateBaselineForecaster
        svc = ForecastingService(forecaster=TemplateBaselineForecaster())
        chosen = svc._forecaster_for(inp)
        assert chosen.name != "tennis_evidence"  # explicit injection wins

    async def test_baseball_soccer_unaffected(self, session, monkeypatch):
        # tennis flags on must not change the baseball/soccer selection
        s = get_settings()
        monkeypatch.setattr(s, "enable_tennis_evidence_forecasting", True)
        monkeypatch.setattr(s, "enable_baseball_evidence_forecasting", True)
        inp = await make_input(session, tennis_market())
        object.__setattr__(inp.packet, "domain", "sports_baseball")
        chosen = ForecastingService()._forecaster_for(inp)
        assert chosen.name in ("baseball_evidence", "template_baseline")


def test_research_canary_report_includes_tennis(session):
    import asyncio
    market_row = seed_market(session, WINNER_TICKER)
    asyncio.run(create_research_packet(session, market_row, collector=collector()))
    from app.services.baseball_research import build_research_canary_report
    report = build_research_canary_report(session)
    assert "tennis-external" in report.by_collector
