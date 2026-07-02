import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import Market, MarketResearchPacket
from app.services.enrichment import MarketDetailEnrichmentService
from app.services.research import (
    DOMAIN_GENERAL,
    DOMAIN_MACRO,
    DOMAIN_SPORTS_BASEBALL,
    DOMAIN_SPORTS_TENNIS,
    MockResearchCollector,
    TemplateResearchCollector,
    classify_domain,
    create_research_packet,
    get_collector,
    market_data_from_row,
)
from app.services.resolution import MockResolutionJudge, RuleBasedResolutionJudge, persist_assessment
from app.schemas import ResolutionAssessment
from tests.conftest import make_market
from tests.test_enrichment import FakeDetailAdapter

COLLECTOR = TemplateResearchCollector(name="template", version="v1")

BASEBALL = make_market(
    ticker="KXMLBHRR-26JUL02-TEST-1",
    title="Player records 2+ hits + runs + RBIs?",
    rules_primary="If the player records 2+ total hits + runs + RBIs, resolves Yes.",
    settlement_source="ESPN (https://www.espn.com); the Governing League (https://www.mlb.com/)",
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class TestDomainClassification:
    def test_baseball_by_ticker(self):
        assert classify_domain(make_market(ticker="KXMLBHRR-1")) == DOMAIN_SPORTS_BASEBALL

    def test_baseball_by_title_keyword(self):
        market = make_market(ticker="XX-1", title="Home run leader in tonight's game?")
        assert classify_domain(market) == DOMAIN_SPORTS_BASEBALL

    def test_tennis_by_ticker_and_source(self):
        assert classify_domain(make_market(ticker="KXATPMATCH-1")) == DOMAIN_SPORTS_TENNIS
        by_source = make_market(
            ticker="XX-2", title="Match winner?", settlement_source="ATP (https://www.atptour.com/)"
        )
        assert classify_domain(by_source) == DOMAIN_SPORTS_TENNIS

    def test_macro_by_ticker_and_keywords(self):
        assert classify_domain(make_market(ticker="KXFED-27APR-T4.00")) == DOMAIN_MACRO
        assert (
            classify_domain(make_market(ticker="XX-3", title="CPI above 3.0% in June?"))
            == DOMAIN_MACRO
        )

    def test_soccer_weather_politics_crypto(self):
        assert classify_domain(make_market(ticker="KXWCADVANCE-1")) == "sports_soccer"
        assert classify_domain(make_market(ticker="XX-4", title="High temperature above 90F?")) == "weather"
        assert classify_domain(make_market(ticker="XX-5", title="Will the senate confirm?")) == "politics"
        assert classify_domain(make_market(ticker="KXBTC-1")) == "crypto"

    def test_general_fallback(self):
        market = make_market(ticker="XX-6", title="Will the ceremony happen?")
        assert classify_domain(market) == DOMAIN_GENERAL

    def test_classification_is_deterministic(self):
        market = make_market(ticker="KXMLB-1", title="tennis themed baseball night")
        assert classify_domain(market) == classify_domain(market) == DOMAIN_SPORTS_BASEBALL


class TestTemplateCollector:
    async def test_domain_appropriate_queries(self):
        packet = await COLLECTOR.collect(BASEBALL, DOMAIN_SPORTS_BASEBALL, "researchable")
        assert packet.domain == DOMAIN_SPORTS_BASEBALL
        joined = " ".join(packet.source_queries)
        assert "lineup" in joined and "pitcher" in joined
        assert any(BASEBALL.title in query for query in packet.source_queries)
        assert "{title}" not in joined  # templates fully substituted

    async def test_settlement_source_becomes_high_confidence_fact_and_sources(self):
        packet = await COLLECTOR.collect(BASEBALL, DOMAIN_SPORTS_BASEBALL, "researchable")
        source_fact = next(f for f in packet.key_facts if "settles via" in f.fact.lower())
        assert source_fact.confidence >= 0.9
        settlement_sources = [s for s in packet.sources if s.source_type == "settlement_source"]
        assert [s.name for s in settlement_sources] == ["ESPN", "the Governing League"]
        assert settlement_sources[0].url == "https://www.espn.com"

    async def test_missing_info_populated_for_sports(self):
        packet = await COLLECTOR.collect(BASEBALL, DOMAIN_SPORTS_BASEBALL, "researchable")
        assert packet.missing_info
        assert any("lineup" in gap for gap in packet.missing_info)

    async def test_missing_settlement_source_is_flagged(self):
        market = BASEBALL.model_copy(update={"settlement_source": None})
        packet = await COLLECTOR.collect(market, DOMAIN_SPORTS_BASEBALL, "researchable")
        assert "settlement source unresolved" in packet.missing_info

    async def test_completeness_score_is_deterministic_and_ordered(self):
        full = await COLLECTOR.collect(BASEBALL, DOMAIN_SPORTS_BASEBALL, "researchable")
        again = await COLLECTOR.collect(BASEBALL, DOMAIN_SPORTS_BASEBALL, "researchable")
        assert full.model_dump() == again.model_dump()

        bare = await COLLECTOR.collect(
            make_market(ticker="XX-7", title="Something?", close_time=None),
            DOMAIN_GENERAL,
            None,
        )
        assert full.research_completeness_score > bare.research_completeness_score
        assert full.research_risk == "low"
        assert bare.research_risk in ("medium", "high")

    async def test_avoid_resolution_forces_high_risk(self):
        packet = await COLLECTOR.collect(BASEBALL, DOMAIN_SPORTS_BASEBALL, "avoid")
        assert packet.research_risk == "high"


class TestCreateResearchPacket:
    async def _seed_market(self, session, market_data=BASEBALL) -> Market:
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

    async def test_persists_with_audit_linkage(self, session):
        market = await self._seed_market(session)
        enrichment = await MarketDetailEnrichmentService(adapter=FakeDetailAdapter()).enrich_ticker(
            session, market.ticker
        )
        judge = RuleBasedResolutionJudge(min_clarity_score=0.70)
        assessment_row = persist_assessment(
            session,
            market.ticker,
            await judge.assess(market_data_from_row(market)),
            judge,
        )

        packet_row = await create_research_packet(
            session, market, collector=COLLECTOR, scanner_run_id=None
        )

        loaded = session.execute(select(MarketResearchPacket)).scalar_one()
        assert loaded.id == packet_row.id
        assert loaded.market_ticker == market.ticker
        assert loaded.enrichment_id == enrichment.id
        assert loaded.resolution_assessment_id == assessment_row.id
        assert loaded.collector_name == "template"
        assert loaded.collector_version == "v1"
        assert loaded.domain == DOMAIN_SPORTS_BASEBALL
        assert loaded.source_queries
        assert loaded.sources[0]["source_type"] == "settlement_source"
        assert any("settles via" in f["fact"].lower() for f in loaded.key_facts)
        assert loaded.missing_info
        assert 0.0 <= loaded.research_completeness_score <= 1.0
        assert loaded.research_risk in ("low", "medium", "high")
        assert loaded.raw_response is not None

    async def test_works_without_enrichment_or_assessment(self, session):
        market = await self._seed_market(
            session, make_market(ticker="BARE-MKT", title="Bare market?", rules_primary=None)
        )
        row = await create_research_packet(session, market, collector=COLLECTOR)
        assert row.enrichment_id is None
        assert row.resolution_assessment_id is None
        assert "settlement source unresolved" in row.missing_info

    async def test_avoid_resolution_marks_packet_high_risk(self, session):
        market = await self._seed_market(session)
        judge = MockResolutionJudge(
            ResolutionAssessment(
                clarity_score=0.1,
                resolution_risk="high",
                tradeability="avoid",
                rejection_reasons=["clarity_below_min"],
            )
        )
        persist_assessment(session, market.ticker, await judge.assess(BASEBALL), judge)

        # Even with a collector that reports low risk, the service forces high
        optimistic = MockResearchCollector()
        optimistic.packet = optimistic.packet.model_copy(update={"research_risk": "low"})
        row = await create_research_packet(session, market, collector=optimistic)
        assert row.research_risk == "high"


def test_get_collector_defaults_to_template():
    collector = get_collector()
    assert isinstance(collector, TemplateResearchCollector)
    assert collector.name == "template"
    assert collector.version == "v1"


class TestCliCollectResearch:
    async def test_collects_for_top_candidates_with_summary(self, session, capsys):
        from tests.test_cli import FakeAdapter

        run = await cli.scan(
            limit=2,
            adapter=FakeAdapter(
                [make_market(ticker="KXMLB-AAA"), make_market(ticker="KXATP-BBB")]
            ),
            session=session,
        )

        collector = MockResearchCollector()
        collected = await cli.collect_research(limit=10, collector=collector, session=session)

        assert collected == 2
        assert sorted(collector.collected_tickers) == ["KXATP-BBB", "KXMLB-AAA"]
        rows = session.execute(select(MarketResearchPacket)).scalars().all()
        assert len(rows) == 2
        assert all(row.scanner_run_id == run.id for row in rows)
        assert all(row.collector_name == "mock" for row in rows)

        output = capsys.readouterr().out
        assert f"collecting research for 2 candidates from scan run {run.id}" in output
        assert "domains: general=2" in output
        assert "risk: medium=2" in output

    async def test_no_scan_is_a_noop_with_message(self, session, capsys):
        collected = await cli.collect_research(limit=5, session=session)
        assert collected == 0
        assert "no successful scan found" in capsys.readouterr().out

    async def test_default_does_not_trigger_enrichment_or_assessment(self, session):
        from app.models import MarketDetailEnrichment, MarketResolutionAssessment
        from tests.test_cli import FakeAdapter

        await cli.scan(
            limit=1, adapter=FakeAdapter([make_market(ticker="KXMLB-CCC")]), session=session
        )
        await cli.collect_research(limit=5, collector=MockResearchCollector(), session=session)

        assert session.execute(select(MarketDetailEnrichment)).scalars().all() == []
        assert session.execute(select(MarketResolutionAssessment)).scalars().all() == []

    async def test_prepare_flag_creates_missing_upstream_rows(self, session, monkeypatch):
        from app.models import MarketDetailEnrichment, MarketResolutionAssessment
        from app.services import enrichment as enrichment_module
        from tests.test_cli import FakeAdapter

        monkeypatch.setattr(enrichment_module, "KalshiRestAdapter", FakeDetailAdapter)
        await cli.scan(
            limit=1, adapter=FakeAdapter([make_market(ticker="KXMLB-DDD")]), session=session
        )
        await cli.collect_research(
            limit=5, collector=MockResearchCollector(), session=session, prepare=True
        )

        assert len(session.execute(select(MarketDetailEnrichment)).scalars().all()) == 1
        assert len(session.execute(select(MarketResolutionAssessment)).scalars().all()) == 1
        packet = session.execute(select(MarketResearchPacket)).scalar_one()
        assert packet.enrichment_id is not None
        assert packet.resolution_assessment_id is not None

    def test_main_wires_collect_research_command(self, monkeypatch):
        captured = {}

        async def fake_collect(limit=10, collector=None, session=None, prepare=False):
            captured.update(limit=limit, prepare=prepare)
            return 4

        monkeypatch.setattr(cli, "collect_research", fake_collect)
        assert cli.main(["collect-research", "--limit", "10", "--prepare"]) == 0
        assert captured == {"limit": 10, "prepare": True}
