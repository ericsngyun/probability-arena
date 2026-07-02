import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import MarketDetailEnrichment
from app.services.enrichment import (
    EnrichmentError,
    MarketDetailEnrichmentService,
    apply_latest_enrichment,
    latest_enrichment_for,
)
from app.services.resolution import (
    FLAG_UNCLEAR_SETTLEMENT_SOURCE,
    RuleBasedResolutionJudge,
)
from tests.conftest import make_market

MARKET_DETAIL = {
    "ticker": "KXMLBHRR-TEST-1",
    "event_ticker": "KXMLBHRR-EVENT",
    "title": "Player records 2+ hits + runs + RBIs?",
    "yes_sub_title": "2+ combined",
    "rules_primary": "If the player records 2+ total hits + runs + RBIs, the market resolves Yes.",
    "rules_secondary": "Stats are counted at the conclusion of the game.",
}
EVENT_DETAIL = {
    "event_ticker": "KXMLBHRR-EVENT",
    "series_ticker": "KXMLBHRR",
    "title": "Hits Runs RBIs",
    "sub_title": "CIN vs MIL",
    "category": "Sports",
    "settlement_sources": [{"name": "Backup League Feed", "url": ""}],
}
SERIES_DETAIL = {
    "ticker": "KXMLBHRR",
    "title": "Pro Baseball Hits Runs RBIs",
    "category": "Sports",
    "settlement_sources": [
        {"name": "ESPN", "url": "https://www.espn.com"},
        {"name": "the Governing League", "url": "https://www.mlb.com/"},
    ],
}


class FakeDetailAdapter:
    def __init__(self, market=MARKET_DETAIL, event=EVENT_DETAIL, series=SERIES_DETAIL):
        self.market = market
        self.event = event
        self.series = series
        self.calls: list[tuple[str, str]] = []

    async def get_market_detail(self, ticker):
        self.calls.append(("market", ticker))
        return self.market

    async def get_event_detail(self, event_ticker):
        self.calls.append(("event", event_ticker))
        return self.event

    async def get_series_detail(self, series_ticker):
        self.calls.append(("series", series_ticker))
        return self.series


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class TestEnrichTicker:
    async def test_persists_normalized_fields_and_raw_payloads(self, session):
        service = MarketDetailEnrichmentService(adapter=FakeDetailAdapter())
        row = await service.enrich_ticker(session, "KXMLBHRR-TEST-1", scanner_run_id=None)

        loaded = session.execute(select(MarketDetailEnrichment)).scalar_one()
        assert loaded.id == row.id
        assert loaded.market_ticker == "KXMLBHRR-TEST-1"
        assert loaded.event_ticker == "KXMLBHRR-EVENT"
        assert loaded.series_ticker == "KXMLBHRR"  # resolved via event detail
        assert loaded.title == "Player records 2+ hits + runs + RBIs?"
        assert loaded.subtitle == "2+ combined"
        assert "resolves Yes" in loaded.rules_text
        assert "conclusion of the game" in loaded.rules_text  # secondary appended
        # Series sources win over event sources
        assert loaded.settlement_source == (
            "ESPN (https://www.espn.com); the Governing League (https://www.mlb.com/)"
        )
        assert loaded.category == "Sports"
        assert loaded.raw_market_detail == MARKET_DETAIL
        assert loaded.raw_event_detail == EVENT_DETAIL
        assert loaded.raw_series_detail == SERIES_DETAIL

    async def test_missing_event_and_series_are_tolerated(self, session):
        adapter = FakeDetailAdapter(
            market={"ticker": "BARE-1", "rules_primary": "Resolves YES if X."},
            event=None,
            series=None,
        )
        service = MarketDetailEnrichmentService(adapter=adapter)
        row = await service.enrich_ticker(session, "BARE-1")

        assert row.event_ticker is None
        assert row.series_ticker is None
        assert row.settlement_source is None
        assert row.raw_event_detail is None
        # no event ticker -> event/series endpoints never called
        assert [c[0] for c in adapter.calls] == ["market"]

    async def test_event_sources_used_when_series_absent(self, session):
        adapter = FakeDetailAdapter(series=None)
        service = MarketDetailEnrichmentService(adapter=adapter)
        row = await service.enrich_ticker(session, "KXMLBHRR-TEST-1")
        assert row.settlement_source == "Backup League Feed"

    async def test_missing_market_detail_raises(self, session):
        adapter = FakeDetailAdapter(market=None)
        service = MarketDetailEnrichmentService(adapter=adapter)
        with pytest.raises(EnrichmentError):
            await service.enrich_ticker(session, "GONE-1")


class TestEnrichTopCandidates:
    async def test_enriches_only_eligible_candidates_of_run(self, session):
        from tests.test_cli import FakeAdapter

        eligible = make_market(ticker="ELIGIBLE")
        rejected = make_market(ticker="REJECTED", yes_bid=None, yes_ask=None, liquidity=0)
        run = await cli.scan(limit=10, adapter=FakeAdapter([eligible, rejected]), session=session)

        detail_adapter = FakeDetailAdapter()
        service = MarketDetailEnrichmentService(adapter=detail_adapter)
        enriched = await service.enrich_top_candidates(session, run_id=run.id, limit=10)

        assert [row.market_ticker for row in enriched] == ["ELIGIBLE"]
        assert enriched[0].scanner_run_id == run.id
        assert ("market", "ELIGIBLE") in detail_adapter.calls
        assert all(call[1] != "REJECTED" for call in detail_adapter.calls if call[0] == "market")

    async def test_fetch_failures_are_skipped_not_fatal(self, session):
        from tests.test_cli import FakeAdapter

        run = await cli.scan(
            limit=10,
            adapter=FakeAdapter([make_market(ticker="AAA"), make_market(ticker="BBB")]),
            session=session,
        )

        class FlakyAdapter(FakeDetailAdapter):
            async def get_market_detail(self, ticker):
                if ticker == "AAA":
                    return None
                return dict(MARKET_DETAIL, ticker=ticker)

        service = MarketDetailEnrichmentService(adapter=FlakyAdapter())
        enriched = await service.enrich_top_candidates(session, run_id=run.id, limit=10)
        assert [row.market_ticker for row in enriched] == ["BBB"]


class TestResolutionUsesEnrichment:
    JUDGE = RuleBasedResolutionJudge(min_clarity_score=0.70, prompt_version="v1")

    async def test_enriched_source_and_rules_beat_list_level(self, session):
        market_data = make_market(
            ticker="KXMLBHRR-TEST-1",
            rules_primary="If the player records 2+ hits the market resolves Yes.",  # no source
        )
        before = await self.JUDGE.assess(market_data)
        assert FLAG_UNCLEAR_SETTLEMENT_SOURCE in before.ambiguity_flags

        service = MarketDetailEnrichmentService(adapter=FakeDetailAdapter())
        await service.enrich_ticker(session, "KXMLBHRR-TEST-1")

        enriched_data = apply_latest_enrichment(session, market_data)
        assert enriched_data.settlement_source is not None
        assert "conclusion of the game" in enriched_data.rules_primary

        after = await self.JUDGE.assess(enriched_data)
        assert FLAG_UNCLEAR_SETTLEMENT_SOURCE not in after.ambiguity_flags
        assert after.clarity_score > before.clarity_score
        assert after.settlement_source.startswith("ESPN")

    async def test_fallback_without_enrichment_is_unchanged(self, session):
        market_data = make_market(ticker="NO-ENRICHMENT", rules_primary="Resolves YES if X happens.")
        assert latest_enrichment_for(session, "NO-ENRICHMENT") is None
        assert apply_latest_enrichment(session, market_data) == market_data

    async def test_enrichment_without_rules_keeps_list_rules(self, session):
        adapter = FakeDetailAdapter(
            market={"ticker": "T-1", "event_ticker": "KXMLBHRR-EVENT"},  # no rules fields
        )
        await MarketDetailEnrichmentService(adapter=adapter).enrich_ticker(session, "T-1")

        market_data = make_market(ticker="T-1", rules_primary="List-level rules text stays.")
        merged = apply_latest_enrichment(session, market_data)
        assert merged.rules_primary == "List-level rules text stays."
        assert merged.settlement_source is not None  # series sources still apply


class TestCliEnrichDetails:
    async def test_cli_enriches_top_candidates(self, session, capsys):
        from tests.test_cli import FakeAdapter

        scan_adapter = FakeAdapter([make_market(ticker="AAA"), make_market(ticker="BBB")])
        run = await cli.scan(limit=2, adapter=scan_adapter, session=session)

        enriched_count = await cli.enrich_details(
            limit=10, adapter=FakeDetailAdapter(), session=session
        )
        assert enriched_count == 2

        rows = session.execute(select(MarketDetailEnrichment)).scalars().all()
        assert len(rows) == 2
        assert all(row.scanner_run_id == run.id for row in rows)

        output = capsys.readouterr().out
        assert f"enriched 2 candidates from scan run {run.id}" in output
        assert "series=KXMLBHRR" in output
        assert "source=ESPN" in output

    def test_main_wires_enrich_details_command(self, monkeypatch):
        captured = {}

        async def fake_enrich(limit=20, adapter=None, session=None):
            captured["limit"] = limit
            return 3

        monkeypatch.setattr(cli, "enrich_details", fake_enrich)
        assert cli.main(["enrich-details", "--limit", "20"]) == 0
        assert captured["limit"] == 20
