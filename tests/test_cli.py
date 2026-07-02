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
