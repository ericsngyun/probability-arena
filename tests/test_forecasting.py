import json
import re

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import Market, MarketForecastRecord
from app.schemas import MarketForecast
from app.services.forecasting import (
    EVIDENCE_MIXED,
    EVIDENCE_SOURCE_BACKED,
    EVIDENCE_TEMPLATE_ONLY,
    FALLBACK_NOTE,
    ForecastingService,
    ForecastInput,
    LLMForecaster,
    MissingResearchPacketError,
    MockForecaster,
    TemplateBaselineForecaster,
    determine_evidence_depth,
    get_forecaster,
    is_critical_info_missing,
)
from app.services.research import (
    TemplateResearchCollector,
    classify_domain,
    create_research_packet,
    market_data_from_row,
)
from app.services.resolution import RuleBasedResolutionJudge, persist_assessment
from tests.conftest import make_market

FORECASTER = TemplateBaselineForecaster()
JUDGE = RuleBasedResolutionJudge(min_clarity_score=0.70, prompt_version="v1")
COLLECTOR = TemplateResearchCollector(name="template", version="v1")

BASEBALL = make_market(
    ticker="KXMLBHRR-FORECAST-1",
    title="Player records 2+ hits + runs + RBIs?",
    rules_primary="If the player records 2+ total hits + runs + RBIs, resolves Yes.",
    settlement_source="ESPN (https://www.espn.com)",
)

# Words that must never appear in forecast output (word-boundary matched)
FORBIDDEN_TERMS = re.compile(
    r"\b(buy|sell|bet|bets|betting|wager|stake|order|orders|trade|trades|trading|"
    r"position|sizing|kelly|edge|ev|expected value|paper)\b",
    re.IGNORECASE,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


async def seed_market(session, market_data=BASEBALL) -> Market:
    row = Market(
        ticker=market_data.ticker,
        title=market_data.title,
        status="active",
        close_time=market_data.close_time,
        rules_primary=market_data.rules_primary,
    )
    session.add(row)
    session.commit()
    return row


async def seed_pipeline(session, market_data=BASEBALL, assess=True):
    """Market + enrichment + resolution assessment + template research packet
    (the full recommended operator sequence)."""
    from app.services.enrichment import MarketDetailEnrichmentService
    from tests.test_enrichment import FakeDetailAdapter

    market = await seed_market(session, market_data)
    await MarketDetailEnrichmentService(adapter=FakeDetailAdapter()).enrich_ticker(
        session, market.ticker
    )
    if assess:
        assessment = await JUDGE.assess(market_data)
        persist_assessment(session, market.ticker, assessment, JUDGE)
    packet = await create_research_packet(session, market, collector=COLLECTOR)
    return market, packet


async def template_input(session, market_data=BASEBALL) -> ForecastInput:
    from app.services.resolution import latest_assessment_for

    market, packet = await seed_pipeline(session, market_data)
    return ForecastInput(
        market=market_data,
        packet=packet,
        resolution=latest_assessment_for(session, market.ticker),
    )


class TestEvidenceDepth:
    async def test_template_packet_is_template_only(self, session):
        inp = await template_input(session)
        assert determine_evidence_depth(inp.packet) == EVIDENCE_TEMPLATE_ONLY

    def test_external_facts_above_ceiling_is_source_backed(self):
        class PacketStub:
            research_completeness_score = 0.8
            key_facts = [{"fact": "lineup confirmed", "source_name": "espn.com"}]
            missing_info = []

        assert determine_evidence_depth(PacketStub()) == EVIDENCE_SOURCE_BACKED

    def test_external_facts_below_ceiling_is_mixed(self):
        class PacketStub:
            research_completeness_score = 0.5
            key_facts = [{"fact": "lineup confirmed", "source_name": "espn.com"}]
            missing_info = []

        assert determine_evidence_depth(PacketStub()) == EVIDENCE_MIXED


class TestTemplateBaselineForecaster:
    async def test_deterministic_for_identical_input(self, session):
        inp = await template_input(session)
        first = await FORECASTER.forecast(inp)
        second = await FORECASTER.forecast(inp)
        assert first.model_dump() == second.model_dump()

    async def test_anchors_to_market_mid_when_quoted(self, session):
        inp = await template_input(session)  # make_market: bid 48 / ask 52
        forecast = await FORECASTER.forecast(inp)
        assert forecast.estimated_probability == 0.5
        assert "anchored_to_market_mid" in forecast.calibration_tags

    async def test_unquoted_market_defaults_to_half(self, session):
        market_data = BASEBALL.model_copy(
            update={"ticker": "KXMLBHRR-FORECAST-2", "yes_bid": None, "yes_ask": None}
        )
        inp = await template_input(session, market_data)
        forecast = await FORECASTER.forecast(inp)
        assert forecast.estimated_probability == 0.5
        assert "uninformative_prior" in forecast.calibration_tags

    async def test_template_only_confidence_cap_enforced(self, session):
        inp = await template_input(session)
        forecast = await FORECASTER.forecast(inp)
        assert forecast.evidence_depth == EVIDENCE_TEMPLATE_ONLY
        assert forecast.confidence <= 0.55
        assert forecast.forecast_risk in ("medium", "high")

    async def test_missing_critical_info_cap_enforced(self, session):
        # No resolution assessment at all -> critical info missing
        market = await seed_market(
            session, BASEBALL.model_copy(update={"ticker": "KXMLBHRR-FORECAST-3"})
        )
        packet = await create_research_packet(session, market, collector=COLLECTOR)
        inp = ForecastInput(
            market=BASEBALL.model_copy(update={"ticker": market.ticker}),
            packet=packet,
            resolution=None,
        )
        assert is_critical_info_missing(packet, None)
        forecast = await FORECASTER.forecast(inp)
        assert forecast.confidence <= 0.50
        assert forecast.forecast_risk == "high"

    async def test_reasoning_fields_populated(self, session):
        inp = await template_input(session)
        forecast = await FORECASTER.forecast(inp)
        assert forecast.bull_case.thesis and forecast.bull_case.points
        assert forecast.bear_case.thesis and forecast.bear_case.points
        assert forecast.skeptic_notes
        assert forecast.key_assumptions
        assert forecast.missing_info
        assert forecast.what_would_change_mind
        assert forecast.forecast_summary

    async def test_no_trade_ev_or_sizing_language(self, session):
        inp = await template_input(session)
        forecast = await FORECASTER.forecast(inp)
        serialized = json.dumps(forecast.model_dump())
        match = FORBIDDEN_TERMS.search(serialized)
        assert match is None, f"forbidden term {match.group(0)!r} in forecast output"

    async def test_no_trade_fields_in_schema(self):
        field_names = set(MarketForecast.model_fields)
        forbidden_fields = {
            "expected_value", "ev", "edge", "position_size", "stake", "kelly_fraction",
            "recommended_side", "order", "trade",
        }
        assert not (field_names & forbidden_fields)


class TestForecastingService:
    async def test_persists_with_audit_linkage(self, session):
        from app.services.resolution import latest_assessment_for

        market, packet = await seed_pipeline(session)
        resolution = latest_assessment_for(session, market.ticker)

        service = ForecastingService(forecaster=TemplateBaselineForecaster())
        row = await service.forecast_market(session, market, scanner_run_id=None)

        loaded = session.execute(select(MarketForecastRecord)).scalar_one()
        assert loaded.id == row.id
        assert loaded.market_ticker == market.ticker
        assert loaded.research_packet_id == packet.id
        assert loaded.resolution_assessment_id == resolution.id
        assert loaded.forecaster_name == "template_baseline"
        assert loaded.forecaster_version == "v1"
        assert loaded.model_name is None
        assert loaded.prompt_version == "v1"
        assert 0.0 <= loaded.estimated_probability <= 1.0
        assert loaded.evidence_depth == EVIDENCE_TEMPLATE_ONLY
        assert loaded.bull_case["points"]
        assert loaded.bear_case["points"]
        assert loaded.skeptic_notes
        assert loaded.raw_response is not None

    async def test_missing_packet_raises(self, session):
        market = await seed_market(session)
        service = ForecastingService(forecaster=TemplateBaselineForecaster())
        with pytest.raises(MissingResearchPacketError):
            await service.forecast_market(session, market)

    async def test_caps_enforced_even_for_overconfident_forecaster(self, session):
        market, _ = await seed_pipeline(session)
        overconfident = MockForecaster()
        overconfident.canned = overconfident.canned.model_copy(
            update={"confidence": 0.99, "evidence_depth": EVIDENCE_SOURCE_BACKED}
        )
        service = ForecastingService(forecaster=overconfident)
        row = await service.forecast_market(session, market)
        # Service recomputes depth (template packet) and caps confidence
        assert row.evidence_depth == EVIDENCE_TEMPLATE_ONLY
        assert row.confidence <= 0.55


class TestLLMForecasterFallback:
    async def test_fallback_on_simulated_failure(self, session, monkeypatch):
        import anthropic

        def exploding_client(*args, **kwargs):
            raise RuntimeError("no credentials configured")

        monkeypatch.setattr(anthropic, "AsyncAnthropic", exploding_client)
        inp = await template_input(session)
        forecaster = LLMForecaster()
        forecast = await forecaster.forecast(inp)

        assert FALLBACK_NOTE in forecast.skeptic_notes
        assert "llm_error_fallback" in forecast.calibration_tags
        assert forecast.confidence <= 0.55  # baseline caps still hold
        assert forecast.evidence_depth == EVIDENCE_TEMPLATE_ONLY


def test_get_forecaster_defaults_to_template_baseline():
    forecaster = get_forecaster()
    assert isinstance(forecaster, TemplateBaselineForecaster)
    assert forecaster.name == "template_baseline"
    assert forecaster.version == "v1"
    assert forecaster.model_name is None


class TestCliForecast:
    async def test_forecasts_candidates_with_packets(self, session, capsys):
        from tests.test_cli import FakeAdapter

        run = await cli.scan(
            limit=2,
            adapter=FakeAdapter(
                [make_market(ticker="KXMLB-FA"), make_market(ticker="KXATP-FB")]
            ),
            session=session,
        )
        await cli.collect_research(limit=10, collector=None, session=session)  # template packets

        forecaster = MockForecaster()
        count = await cli.forecast(limit=10, forecaster=forecaster, session=session)

        assert count == 2
        assert sorted(forecaster.forecasted_tickers) == ["KXATP-FB", "KXMLB-FA"]
        rows = session.execute(select(MarketForecastRecord)).scalars().all()
        assert len(rows) == 2
        assert all(row.scanner_run_id == run.id for row in rows)
        assert all(row.forecaster_name == "mock" for row in rows)

        output = capsys.readouterr().out
        assert f"forecasting 2 candidates from scan run {run.id}" in output
        assert "evidence: template_only=2" in output
        assert "risk:" in output and "domains:" in output

    async def test_default_skips_markets_without_packets(self, session, capsys):
        from tests.test_cli import FakeAdapter

        await cli.scan(
            limit=1, adapter=FakeAdapter([make_market(ticker="KXMLB-FC")]), session=session
        )
        count = await cli.forecast(limit=5, forecaster=MockForecaster(), session=session)

        assert count == 0
        output = capsys.readouterr().out
        assert "skipping 1 candidates without research packets" in output
        assert session.execute(select(MarketForecastRecord)).scalars().all() == []

    async def test_prepare_creates_missing_packets(self, session):
        from app.models import MarketResearchPacket
        from tests.test_cli import FakeAdapter

        await cli.scan(
            limit=1, adapter=FakeAdapter([make_market(ticker="KXMLB-FD")]), session=session
        )
        count = await cli.forecast(
            limit=5, forecaster=MockForecaster(), session=session, prepare=True
        )

        assert count == 1
        assert len(session.execute(select(MarketResearchPacket)).scalars().all()) == 1
        forecast_row = session.execute(select(MarketForecastRecord)).scalar_one()
        assert forecast_row.research_packet_id is not None

    def test_main_wires_forecast_command(self, monkeypatch):
        captured = {}

        async def fake_forecast(limit=10, forecaster=None, session=None, prepare=False):
            captured.update(limit=limit, prepare=prepare)
            return 2

        monkeypatch.setattr(cli, "forecast", fake_forecast)
        assert cli.main(["forecast", "--limit", "10", "--prepare"]) == 0
        assert captured == {"limit": 10, "prepare": True}
