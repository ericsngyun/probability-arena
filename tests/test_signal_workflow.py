from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import (
    Market,
    MarketDetailEnrichment,
    MarketForecastRecord,
    MarketResearchPacket,
    MarketResolutionAssessment,
    OpportunitySignal,
)
from app.services.forecasting import TemplateBaselineForecaster
from app.services.research import TemplateResearchCollector
from app.services.resolution import RuleBasedResolutionJudge
from app.services.signal_workflow import (
    PROMOTION_PRIORITY,
    PromotionNotAllowedError,
    SignalNotFoundError,
    SignalProcessingService,
    SignalPromotionService,
    build_signal_report,
)
from tests.test_enrichment import FakeDetailAdapter


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def seed_signal(session, ticker="SW-1", signal_type="price_move_threshold", status="new"):
    now = datetime.now(timezone.utc)
    row = OpportunitySignal(
        market_ticker=ticker,
        signal_type=signal_type,
        signal_status=status,
        observed_at=now,
        reason="seeded",
        created_at=now,
    )
    session.add(row)
    session.commit()
    return row


def seed_market(session, ticker="SW-1") -> Market:
    row = Market(
        ticker=ticker,
        title=f"{ticker} market?",
        status="active",
        rules_primary="Resolves YES if the thing happens.",
    )
    session.add(row)
    session.commit()
    return row


def make_processor(**overrides) -> SignalProcessingService:
    defaults = dict(
        enrichment_adapter=FakeDetailAdapter(),
        judge=RuleBasedResolutionJudge(min_clarity_score=0.70, prompt_version="v1"),
        collector=TemplateResearchCollector(name="template", version="v1"),
        forecaster=TemplateBaselineForecaster(),
    )
    defaults.update(overrides)
    return SignalProcessingService(**defaults)


class TestPromotion:
    def test_promote_one(self, session):
        signal = seed_signal(session)
        promoted = SignalPromotionService().promote(session, signal.id)
        assert promoted.signal_status == "promoted_to_research"
        assert promoted.promoted_at is not None

    def test_duplicate_promotion_is_idempotent(self, session):
        signal = seed_signal(session)
        service = SignalPromotionService()
        first = service.promote(session, signal.id)
        first_promoted_at = first.promoted_at
        second = service.promote(session, signal.id)
        assert second.signal_status == "promoted_to_research"
        assert second.promoted_at == first_promoted_at  # unchanged

    @pytest.mark.parametrize("status", ["dismissed", "reviewed", "forecast_refreshed"])
    def test_non_new_signals_cannot_be_promoted(self, session, status):
        signal = seed_signal(session, status=status)
        with pytest.raises(PromotionNotAllowedError):
            SignalPromotionService().promote(session, signal.id)
        assert signal.signal_status == status

    def test_unknown_signal_raises(self, session):
        with pytest.raises(SignalNotFoundError):
            SignalPromotionService().promote(session, 999)

    def test_promote_top_prioritizes_types_and_dedupes_tickers(self, session):
        # seeded in reverse priority order; ids ascending
        seed_signal(session, ticker="T-LIQ", signal_type="liquidity_appeared")
        seed_signal(session, ticker="T-SPREAD", signal_type="spread_tightened")
        seed_signal(session, ticker="T-CROSS", signal_type="price_crossed_latest_forecast")
        seed_signal(session, ticker="T-MOVE", signal_type="price_move_threshold")
        seed_signal(session, ticker="T-MOVE", signal_type="newly_two_sided")  # same ticker, lower prio
        seed_signal(session, ticker="T-DISMISSED", signal_type="price_move_threshold", status="dismissed")

        promoted = SignalPromotionService().promote_top(session, limit=3)
        assert [s.signal_type for s in promoted] == [
            "price_move_threshold",
            "price_crossed_latest_forecast",
            "spread_tightened",
        ]
        assert [s.market_ticker for s in promoted] == ["T-MOVE", "T-CROSS", "T-SPREAD"]
        # same-ticker lower-priority signal stayed new; dismissed untouched
        remaining = {
            s.signal_type: s.signal_status
            for s in session.execute(select(OpportunitySignal)).scalars()
            if s.signal_status == "new"
        }
        assert "newly_two_sided" in remaining
        assert "liquidity_appeared" in remaining

    def test_priority_constant_matches_spec_order(self):
        assert PROMOTION_PRIORITY == (
            "price_move_threshold",
            "price_crossed_latest_forecast",
            "spread_tightened",
            "liquidity_appeared",
            "newly_two_sided",
        )

    def test_list_recent_newest_first_with_status_filter(self, session):
        first = seed_signal(session, ticker="A")
        second = seed_signal(session, ticker="B", status="dismissed")
        service = SignalPromotionService()
        assert [s.id for s in service.list_recent(session)] == [second.id, first.id]
        assert [s.id for s in service.list_recent(session, signal_status="new")] == [first.id]


class TestProcessing:
    async def test_process_refreshes_full_chain_and_links(self, session):
        seed_market(session)
        signal = seed_signal(session)
        SignalPromotionService().promote(session, signal.id)

        processed = await make_processor().process(session, signal)

        assert processed.signal_status == "forecast_refreshed"
        assert processed.processed_at is not None
        assert processed.processing_error_type is None

        enrichment = session.execute(select(MarketDetailEnrichment)).scalar_one()
        assessment = session.execute(select(MarketResolutionAssessment)).scalar_one()
        packet = session.execute(select(MarketResearchPacket)).scalar_one()
        forecast = session.execute(select(MarketForecastRecord)).scalar_one()
        assert enrichment.market_ticker == "SW-1"
        assert assessment.market_ticker == "SW-1"
        assert processed.refreshed_research_packet_id == packet.id
        assert processed.refreshed_forecast_id == forecast.id
        # forecast consumed the freshly created packet
        assert forecast.research_packet_id == packet.id
        # enriched settlement source flowed into the assessment
        assert assessment.settlement_source is not None

    async def test_failure_is_captured_on_signal(self, session):
        seed_market(session)
        signal = seed_signal(session)
        SignalPromotionService().promote(session, signal.id)

        class ExplodingAdapter(FakeDetailAdapter):
            async def get_market_detail(self, ticker):
                raise RuntimeError("detail endpoint down")

        processed = await make_processor(enrichment_adapter=ExplodingAdapter()).process(
            session, signal
        )
        assert processed.signal_status == "promoted_to_research"  # stage never advanced
        assert processed.processing_error_type == "RuntimeError"
        assert "detail endpoint down" in processed.processing_error_message
        assert processed.processed_at is not None
        assert processed.refreshed_forecast_id is None

    async def test_missing_market_metadata_is_captured(self, session):
        signal = seed_signal(session, ticker="NEVER-SCANNED")
        SignalPromotionService().promote(session, signal.id)
        processed = await make_processor().process(session, signal)
        assert processed.processing_error_type == "LookupError"
        assert "run a scan first" in processed.processing_error_message

    async def test_process_promoted_fifo_and_skips_errored(self, session):
        seed_market(session, "SW-1")
        seed_market(session, "SW-2")
        first = seed_signal(session, ticker="SW-1")
        second = seed_signal(session, ticker="SW-2")
        service = SignalPromotionService()
        service.promote(session, first.id)
        service.promote(session, second.id)
        # mark the first as previously failed
        first.processing_error_type = "RuntimeError"
        session.commit()

        processed = await make_processor().process_promoted(session, limit=5)
        assert [s.id for s in processed] == [second.id]
        assert processed[0].signal_status == "forecast_refreshed"

    async def test_process_promoted_ignores_unpromoted(self, session):
        seed_market(session)
        seed_signal(session)  # still new
        processed = await make_processor().process_promoted(session, limit=5)
        assert processed == []


class TestReport:
    async def test_report_counts_and_recent_refreshed(self, session):
        seed_market(session, "SW-1")
        refreshed = seed_signal(session, ticker="SW-1")
        seed_signal(session, ticker="SW-2", signal_type="spread_tightened")  # stays new
        errored = seed_signal(session, ticker="SW-3")
        service = SignalPromotionService()
        service.promote(session, refreshed.id)
        service.promote(session, errored.id)
        errored.processing_error_type = "RuntimeError"
        session.commit()
        await make_processor().process(session, refreshed)

        report = build_signal_report(session)
        assert report.total == 3
        assert report.by_status["forecast_refreshed"] == 1
        assert report.by_status["new"] == 1
        assert report.by_type["price_move_threshold"] == 2
        assert report.promoted_awaiting_processing == 0  # errored one is excluded
        assert report.processed_with_errors == 1
        assert len(report.recent_refreshed) == 1
        item = report.recent_refreshed[0]
        assert item.market_ticker == "SW-1"
        assert item.refreshed_forecast_id is not None
        assert 0.0 <= item.refreshed_probability <= 1.0


class TestCli:
    async def test_signals_recent_cli(self, session, capsys):
        seed_signal(session, ticker="CLI-1")
        count = await cli.signals_recent(limit=10, session=session)
        assert count == 1
        assert "CLI-1" in capsys.readouterr().out

    async def test_promote_signals_cli(self, session, capsys):
        seed_signal(session, ticker="CLI-2")
        count = await cli.promote_signals(limit=5, session=session)
        assert count == 1
        assert "promoted 1 signal(s)" in capsys.readouterr().out

    async def test_process_promoted_signals_cli(self, session, capsys):
        seed_market(session, "CLI-3")
        signal = seed_signal(session, ticker="CLI-3")
        SignalPromotionService().promote(session, signal.id)

        count = await cli.process_promoted_signals(limit=5, services=make_processor(), session=session)
        assert count == 1
        output = capsys.readouterr().out
        assert "forecast_refreshed" in output
        assert "packet=" in output and "forecast=" in output

    async def test_signal_report_cli(self, session, capsys):
        seed_signal(session, ticker="CLI-4")
        total = await cli.signal_report(session=session)
        assert total == 1
        output = capsys.readouterr().out
        assert "signals: total=1" in output
        assert "by type: price_move_threshold=1" in output

    def test_main_wires_signal_commands(self, monkeypatch):
        captured = {}

        async def fake_recent(limit=20, signal_status=None, session=None):
            captured["recent"] = (limit, signal_status)
            return 1

        async def fake_promote(limit=5, session=None):
            captured["promote"] = limit
            return 1

        async def fake_process(limit=5, services=None, session=None):
            captured["process"] = limit
            return 1

        async def fake_report(session=None):
            captured["report"] = True
            return 1

        monkeypatch.setattr(cli, "signals_recent", fake_recent)
        monkeypatch.setattr(cli, "promote_signals", fake_promote)
        monkeypatch.setattr(cli, "process_promoted_signals", fake_process)
        monkeypatch.setattr(cli, "signal_report", fake_report)
        assert cli.main(["signals-recent", "--limit", "20"]) == 0
        assert cli.main(["promote-signals", "--limit", "5"]) == 0
        assert cli.main(["process-promoted-signals", "--limit", "5"]) == 0
        assert cli.main(["signal-report"]) == 0
        assert captured == {"recent": (20, None), "promote": 5, "process": 5, "report": True}