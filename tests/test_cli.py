import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import MarketSnapshot, ScannerRun
from tests.conftest import make_market


class FakeAdapter:
    def __init__(self, markets):
        self.markets = markets
        self.requested_max = None

    async def fetch_active_markets(self, max_markets=None):
        self.requested_max = max_markets
        return self.markets[: max_markets or len(self.markets)]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


async def test_scan_persists_run_with_cli_source(session, capsys):
    adapter = FakeAdapter([make_market(ticker="AAA"), make_market(ticker="BBB")])
    run = await cli.scan(limit=2, adapter=adapter, session=session)

    assert run.status == "ok"
    assert run.source == "cli"
    assert adapter.requested_max == 2
    snapshots = session.execute(select(MarketSnapshot)).scalars().all()
    assert len(snapshots) == 2

    output = capsys.readouterr().out
    assert f"scan run={run.id} status=ok source=cli" in output
    assert "AAA" in output and "BBB" in output


async def test_scan_limit_caps_markets(session):
    adapter = FakeAdapter([make_market(ticker=f"MKT-{i}") for i in range(5)])
    run = await cli.scan(limit=3, adapter=adapter, session=session)
    assert run.markets_ranked == 3


async def test_scan_records_error_run_and_raises(session):
    class ExplodingAdapter:
        async def fetch_active_markets(self, max_markets=None):
            raise RuntimeError("kalshi unreachable")

    with pytest.raises(RuntimeError):
        await cli.scan(limit=10, adapter=ExplodingAdapter(), session=session)

    run = session.execute(select(ScannerRun)).scalar_one()
    assert run.status == "error"
    assert run.source == "cli"
    assert run.error_type == "RuntimeError"
    assert "kalshi unreachable" in run.error_message


class TestAssessResolution:
    async def test_assesses_top_candidates_from_latest_scan(self, session, capsys):
        from app.models import MarketResolutionAssessment
        from app.services.resolution import MockResolutionJudge

        adapter = FakeAdapter([make_market(ticker="AAA"), make_market(ticker="BBB")])
        scan_run = await cli.scan(limit=2, adapter=adapter, session=session)

        judge = MockResolutionJudge()
        assessed = await cli.assess_resolution(limit=10, judge=judge, session=session)

        assert assessed == 2
        assert sorted(judge.assessed_tickers) == ["AAA", "BBB"]
        rows = session.execute(select(MarketResolutionAssessment)).scalars().all()
        assert len(rows) == 2
        assert all(row.scanner_run_id == scan_run.id for row in rows)
        assert all(row.model_name == "mock" for row in rows)

        output = capsys.readouterr().out
        assert f"assessing 2 candidates from scan run {scan_run.id} judge=mock" in output
        assert "tradeability=researchable" in output

    async def test_limit_caps_assessed_candidates(self, session):
        from app.services.resolution import MockResolutionJudge

        adapter = FakeAdapter([make_market(ticker=f"MKT-{i}") for i in range(5)])
        await cli.scan(limit=5, adapter=adapter, session=session)

        judge = MockResolutionJudge()
        assessed = await cli.assess_resolution(limit=3, judge=judge, session=session)
        assert assessed == 3

    async def test_skips_ineligible_markets(self, session):
        from app.services.resolution import MockResolutionJudge

        eligible = make_market(ticker="ELIGIBLE")
        rejected = make_market(ticker="REJECTED", yes_bid=None, yes_ask=None, liquidity=0)
        await cli.scan(limit=10, adapter=FakeAdapter([eligible, rejected]), session=session)

        judge = MockResolutionJudge()
        await cli.assess_resolution(limit=10, judge=judge, session=session)
        assert judge.assessed_tickers == ["ELIGIBLE"]


def test_main_wires_assess_resolution_command(monkeypatch):
    captured = {}

    async def fake_assess(limit=20, judge=None, session=None):
        captured["limit"] = limit
        return 5

    monkeypatch.setattr(cli, "assess_resolution", fake_assess)
    exit_code = cli.main(["assess-resolution", "--limit", "20"])
    assert exit_code == 0
    assert captured["limit"] == 20


def test_main_wires_scan_command_with_limit(monkeypatch):
    captured = {}

    async def fake_scan(limit=None, adapter=None, session=None):
        captured["limit"] = limit
        return ScannerRun(status="ok")

    monkeypatch.setattr(cli, "scan", fake_scan)
    exit_code = cli.main(["scan", "--limit", "100"])
    assert exit_code == 0
    assert captured["limit"] == 100


def test_main_requires_a_command():
    with pytest.raises(SystemExit):
        cli.main([])
