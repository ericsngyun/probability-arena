import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import cli
from app.adapters.kalshi import parse_market_outcome
from app.db import Base
from app.models import Market, MarketOutcomeRecord
from app.services.outcomes import OutcomeService, OutcomeSyncError, latest_outcome_for
from tests.conftest import make_market


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class TestParseMarketOutcome:
    def test_yes_win(self):
        outcome = parse_market_outcome(
            {
                "ticker": "T-1",
                "status": "settled",
                "result": "yes",
                "settlement_value_dollars": "1.0000",
                "close_time": "2026-07-01T20:00:00Z",
                "settled_time": "2026-07-01T21:00:00Z",
            }
        )
        assert outcome.outcome_status == "settled"
        assert outcome.winning_side == "yes"
        assert outcome.resolved_probability == 1.0
        assert outcome.settlement_price == 1.0
        assert outcome.close_time is not None and outcome.settled_time is not None

    def test_no_win_with_legacy_cent_settlement(self):
        outcome = parse_market_outcome(
            {"status": "finalized", "result": "no", "settlement_value": 0}
        )
        assert outcome.outcome_status == "settled"
        assert outcome.winning_side == "no"
        assert outcome.resolved_probability == 0.0
        assert outcome.settlement_price == 0.0

    def test_open_market(self):
        outcome = parse_market_outcome({"status": "active", "result": ""})
        assert outcome.outcome_status == "open"
        assert outcome.winning_side is None
        assert outcome.resolved_probability is None

    def test_closed_but_unsettled(self):
        assert parse_market_outcome({"status": "closed"}).outcome_status == "closed"

    def test_canceled_market(self):
        by_status = parse_market_outcome({"status": "canceled", "result": ""})
        by_result = parse_market_outcome({"status": "settled", "result": "void"})
        for outcome in (by_status, by_result):
            assert outcome.outcome_status == "canceled"
            assert outcome.winning_side == "void"
            assert outcome.resolved_probability is None

    def test_settled_without_result_is_unknown_side(self):
        outcome = parse_market_outcome({"status": "settled", "result": ""})
        assert outcome.outcome_status == "settled"
        assert outcome.winning_side == "unknown"
        assert outcome.resolved_probability is None

    def test_missing_and_drifted_fields(self):
        empty = parse_market_outcome({})
        assert empty.outcome_status == "unknown"
        drifted = parse_market_outcome(
            {"status": "some_new_status", "settlement_value_dollars": "not-a-number"}
        )
        assert drifted.outcome_status == "unknown"
        assert drifted.settlement_price is None
        assert empty.raw_payload == {}


class FakeOutcomeAdapter:
    def __init__(self, details: dict[str, dict | None]):
        self.details = details
        self.calls: list[str] = []

    async def get_market_detail(self, ticker):
        self.calls.append(ticker)
        return self.details.get(ticker)


def seed_market(session, ticker: str) -> Market:
    row = Market(ticker=ticker, title=ticker, status="active")
    session.add(row)
    session.commit()
    return row


class TestOutcomeService:
    async def test_sync_persists_outcome(self, session):
        seed_market(session, "T-YES")
        adapter = FakeOutcomeAdapter({"T-YES": {"status": "settled", "result": "yes"}})
        row = await OutcomeService(adapter=adapter).sync_ticker(session, "T-YES")

        loaded = session.execute(select(MarketOutcomeRecord)).scalar_one()
        assert loaded.id == row.id
        assert loaded.market_ticker == "T-YES"
        assert loaded.outcome_status == "settled"
        assert loaded.resolved_probability == 1.0
        assert loaded.source == "kalshi_rest"
        assert loaded.raw_payload == {"status": "settled", "result": "yes"}

    async def test_sync_upserts_single_row_per_ticker(self, session):
        seed_market(session, "T-UP")
        adapter = FakeOutcomeAdapter({"T-UP": {"status": "active", "result": ""}})
        service = OutcomeService(adapter=adapter)
        first = await service.sync_ticker(session, "T-UP")
        assert first.outcome_status == "open"

        adapter.details["T-UP"] = {"status": "settled", "result": "no"}
        second = await service.sync_ticker(session, "T-UP")

        rows = session.execute(select(MarketOutcomeRecord)).scalars().all()
        assert len(rows) == 1
        assert second.id == first.id
        assert rows[0].outcome_status == "settled"
        assert rows[0].winning_side == "no"

    async def test_sync_missing_detail_raises(self, session):
        seed_market(session, "T-GONE")
        service = OutcomeService(adapter=FakeOutcomeAdapter({}))
        with pytest.raises(OutcomeSyncError):
            await service.sync_ticker(session, "T-GONE")

    async def test_sync_known_markets_prioritizes_forecasted_and_skips_failures(self, session):
        from app.services.forecasting import ForecastingService, TemplateBaselineForecaster
        from app.services.research import TemplateResearchCollector, create_research_packet

        forecasted = seed_market(session, "T-FORECASTED")
        seed_market(session, "T-PLAIN")
        seed_market(session, "T-FAILS")
        await create_research_packet(
            session, forecasted, collector=TemplateResearchCollector(name="template", version="v1")
        )
        await ForecastingService(forecaster=TemplateBaselineForecaster()).forecast_market(
            session, forecasted
        )

        adapter = FakeOutcomeAdapter(
            {
                "T-FORECASTED": {"status": "settled", "result": "yes"},
                "T-PLAIN": {"status": "active"},
                # T-FAILS returns None -> skipped
            }
        )
        synced = await OutcomeService(adapter=adapter).sync_known_markets(session, limit=10)

        assert adapter.calls[0] == "T-FORECASTED"  # forecasted tickers first
        assert {row.market_ticker for row in synced} == {"T-FORECASTED", "T-PLAIN"}
        assert latest_outcome_for(session, "T-FAILS") is None


class TestCliSyncOutcomes:
    async def test_cli_syncs_and_prints_summary(self, session, capsys):
        seed_market(session, "T-A")
        seed_market(session, "T-B")
        adapter = FakeOutcomeAdapter(
            {
                "T-A": {"status": "settled", "result": "yes"},
                "T-B": {"status": "active"},
            }
        )
        count = await cli.sync_outcomes(limit=10, adapter=adapter, session=session)
        assert count == 2
        output = capsys.readouterr().out
        assert "synced 2 outcomes" in output
        assert "open=1" in output and "settled=1" in output

    def test_main_wires_sync_outcomes(self, monkeypatch):
        captured = {}

        async def fake_sync(limit=100, adapter=None, session=None):
            captured["limit"] = limit
            return 3

        monkeypatch.setattr(cli, "sync_outcomes", fake_sync)
        assert cli.main(["sync-outcomes", "--limit", "100"]) == 0
        assert captured["limit"] == 100
