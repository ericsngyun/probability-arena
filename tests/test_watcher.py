import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import MarketPriceTick, OpportunitySignal, WatcherRun
from app.services.watcher import (
    SIGNAL_LIQUIDITY_APPEARED,
    SIGNAL_NEWLY_TWO_SIDED,
    SIGNAL_PRICE_CROSSED_FORECAST,
    SIGNAL_PRICE_MOVE,
    SIGNAL_SPREAD_TIGHTENED,
    RealtimeWatcher,
    WatcherConfig,
)
from tests.conftest import make_market


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class FrameAdapter:
    """Returns the next 'frame' of market states on each poll."""

    def __init__(self, frames: list[list]):
        self.frames = frames
        self.polls = 0

    def _next(self):
        frame = self.frames[min(self.polls, len(self.frames) - 1)]
        self.polls += 1
        return frame

    async def fetch_active_markets(self, max_markets=None):
        return self._next()[: max_markets or 1000]

    async def fetch_markets_by_tickers(self, tickers):
        return [m for m in self._next() if m.ticker in set(tickers)]


CFG = WatcherConfig(
    market_limit=100,
    price_move_threshold=0.07,
    max_spread_cents=15,
    min_liquidity_proxy=100,
    signal_cooldown_seconds=900,
)


def market(ticker="W-1", bid=48, ask=52, liquidity=100000, **kw):
    return make_market(ticker=ticker, yes_bid=bid, yes_ask=ask, liquidity=liquidity, **kw)


async def run_frames(session, frames, cfg=CFG, limit=100):
    watcher = RealtimeWatcher(adapter=FrameAdapter(frames), config=cfg)
    runs = []
    for _ in frames:
        runs.append(await watcher.watch_once(session, limit=limit))
    return runs


def all_signals(session):
    return session.execute(
        select(OpportunitySignal).order_by(OpportunitySignal.id)
    ).scalars().all()


class TestTicksAndRuns:
    async def test_ticks_persist_with_derived_fields(self, session):
        runs = await run_frames(session, [[market(bid=48, ask=52, liquidity=26900)]])
        tick = session.execute(select(MarketPriceTick)).scalar_one()
        assert tick.market_ticker == "W-1"
        assert tick.yes_bid == 48 and tick.yes_ask == 52
        assert tick.midpoint == 0.5
        assert tick.spread == 4
        assert tick.liquidity_proxy == 26900
        assert tick.raw_payload is None  # make_market has no raw payload
        assert runs[0].status == "ok"
        assert runs[0].markets_checked == 1
        assert runs[0].ticks_recorded == 1
        assert runs[0].signals_created == 0  # first observation -> no signals

    async def test_error_recorded_on_run(self, session):
        class ExplodingAdapter:
            async def fetch_active_markets(self, max_markets=None):
                raise RuntimeError("kalshi down")

            async def fetch_markets_by_tickers(self, tickers):
                raise RuntimeError("kalshi down")

        watcher = RealtimeWatcher(adapter=ExplodingAdapter(), config=CFG)
        with pytest.raises(RuntimeError):
            await watcher.watch_once(session)
        run = session.execute(select(WatcherRun)).scalar_one()
        assert run.status == "error"
        assert run.error_type == "RuntimeError"
        assert "kalshi down" in run.error_message


class TestDetectors:
    async def test_price_move_threshold(self, session):
        await run_frames(
            session,
            [[market(bid=48, ask=52)], [market(bid=58, ask=62)]],  # mid 0.50 -> 0.60
        )
        signals = all_signals(session)
        assert [s.signal_type for s in signals] == [SIGNAL_PRICE_MOVE]
        signal = signals[0]
        assert signal.old_midpoint == 0.5 and signal.new_midpoint == 0.6
        assert signal.price_change == pytest.approx(0.1)
        assert signal.signal_status == "new"
        assert signal.evidence["threshold"] == 0.07
        assert "Midpoint moved +0.10" in signal.reason

    async def test_small_move_does_not_fire(self, session):
        await run_frames(session, [[market(bid=48, ask=52)], [market(bid=52, ask=56)]])  # Δ0.04
        assert all_signals(session) == []

    async def test_spread_tightened(self, session):
        await run_frames(
            session,
            [[market(bid=30, ask=70)], [market(bid=45, ask=55)]],  # 40c -> 10c
        )
        types = [s.signal_type for s in all_signals(session)]
        assert SIGNAL_SPREAD_TIGHTENED in types

    async def test_newly_two_sided(self, session):
        await run_frames(
            session,
            [[market(bid=None, ask=None)], [market(bid=48, ask=52)]],
        )
        types = [s.signal_type for s in all_signals(session)]
        assert SIGNAL_NEWLY_TWO_SIDED in types

    async def test_liquidity_appeared(self, session):
        await run_frames(
            session,
            [[market(liquidity=0)], [market(liquidity=5000)]],
        )
        types = [s.signal_type for s in all_signals(session)]
        assert SIGNAL_LIQUIDITY_APPEARED in types

    async def test_price_crossed_latest_forecast(self, session):
        from tests.test_calibration import seed_forecast

        forecast = await seed_forecast(session, "W-1", probability=0.55)
        await run_frames(
            session,
            [[market(bid=48, ask=52)], [market(bid=58, ask=62)]],  # 0.50 -> 0.60 crosses 0.55
        )
        crossed = [s for s in all_signals(session) if s.signal_type == SIGNAL_PRICE_CROSSED_FORECAST]
        assert len(crossed) == 1
        assert crossed[0].latest_forecast_id == forecast.id
        assert crossed[0].latest_forecast_probability == 0.55
        assert crossed[0].evidence["forecast_probability"] == 0.55

    async def test_no_forecast_no_crossing_signal(self, session):
        await run_frames(session, [[market(bid=48, ask=52)], [market(bid=58, ask=62)]])
        types = [s.signal_type for s in all_signals(session)]
        assert SIGNAL_PRICE_CROSSED_FORECAST not in types
        assert SIGNAL_PRICE_MOVE in types  # move still fires


class TestCooldown:
    async def test_cooldown_dedupes_repeated_signals(self, session):
        # Oscillating midpoint: both transitions exceed the threshold, but the
        # second is inside the cooldown window -> suppressed.
        frames = [
            [market(bid=48, ask=52)],
            [market(bid=58, ask=62)],
            [market(bid=48, ask=52)],
        ]
        runs = await run_frames(session, frames)
        moves = [s for s in all_signals(session) if s.signal_type == SIGNAL_PRICE_MOVE]
        assert len(moves) == 1
        assert runs[1].signals_created == 1
        assert runs[2].signals_created == 0

    async def test_zero_cooldown_allows_repeats(self, session):
        cfg = WatcherConfig(
            price_move_threshold=0.07, max_spread_cents=15,
            min_liquidity_proxy=100, signal_cooldown_seconds=0,
        )
        frames = [
            [market(bid=48, ask=52)],
            [market(bid=58, ask=62)],
            [market(bid=48, ask=52)],
        ]
        await run_frames(session, frames, cfg=cfg)
        moves = [s for s in all_signals(session) if s.signal_type == SIGNAL_PRICE_MOVE]
        assert len(moves) == 2

    async def test_cooldown_is_per_type(self, session):
        # A different signal type is not suppressed by a price-move cooldown
        frames = [
            [market(bid=48, ask=52, liquidity=0)],
            [market(bid=58, ask=62, liquidity=0)],       # price move fires
            [market(bid=58, ask=62, liquidity=5000)],    # liquidity fires despite cooldown window
        ]
        await run_frames(session, frames)
        types = [s.signal_type for s in all_signals(session)]
        assert types.count(SIGNAL_PRICE_MOVE) == 1
        assert types.count(SIGNAL_LIQUIDITY_APPEARED) == 1


class TestUniverse:
    async def test_prefers_scan_candidates_when_available(self, session):
        from tests.test_cli import FakeAdapter as ScanFakeAdapter

        await cli.scan(
            limit=2,
            adapter=ScanFakeAdapter([make_market(ticker="W-1"), make_market(ticker="OTHER")]),
            session=session,
        )
        frames = [[market(ticker="W-1"), market(ticker="NOT-IN-SCAN")]]
        adapter = FrameAdapter(frames)
        watcher = RealtimeWatcher(adapter=adapter, config=CFG)
        run = await watcher.watch_once(session, limit=10)
        # fetch_markets_by_tickers filtered to scan universe
        tickers = {t.market_ticker for t in session.execute(select(MarketPriceTick)).scalars()}
        assert "NOT-IN-SCAN" not in tickers
        assert run.markets_checked == 1


class TestCli:
    async def test_watch_once_prints_summary_and_signals(self, session, capsys):
        adapter = FrameAdapter([[market(bid=48, ask=52)], [market(bid=58, ask=62)]])
        watcher_run = await cli.watch_once(limit=10, adapter=adapter, session=session)
        assert watcher_run.status == "ok"
        second = await cli.watch_once(limit=10, adapter=adapter, session=session)
        assert second.signals_created == 1

        output = capsys.readouterr().out
        assert "watcher run=" in output
        assert "[price_move_threshold] W-1:" in output

    async def test_watch_loop_requires_enable_flag(self, session, capsys, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "enable_realtime_watcher", False)
        iterations = await cli.watch_loop(interval=0, limit=10, session=session)
        assert iterations == 0
        assert "ENABLE_REALTIME_WATCHER=false" in capsys.readouterr().out

    async def test_watch_loop_runs_iterations_and_stops(self, session, capsys, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "enable_realtime_watcher", True)
        adapter = FrameAdapter([[market(bid=48, ask=52)], [market(bid=58, ask=62)]])
        iterations = await cli.watch_loop(
            interval=0, limit=10, adapter=adapter, session=session, max_iterations=2
        )
        assert iterations == 2
        output = capsys.readouterr().out
        assert "watcher loop started" in output
        assert "watcher loop stopped after 2 iteration(s)" in output
        assert len(session.execute(select(WatcherRun)).scalars().all()) == 2

    async def test_watch_loop_survives_pass_errors(self, session, capsys, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "enable_realtime_watcher", True)

        class FlakyAdapter(FrameAdapter):
            async def fetch_active_markets(self, max_markets=None):
                if self.polls == 0:
                    self.polls += 1
                    raise RuntimeError("transient")
                return await super().fetch_active_markets(max_markets)

        adapter = FlakyAdapter([[market(bid=48, ask=52)]])
        iterations = await cli.watch_loop(
            interval=0, limit=10, adapter=adapter, session=session, max_iterations=2
        )
        assert iterations == 2
        assert "watcher pass failed: RuntimeError: transient" in capsys.readouterr().out

    def test_main_wires_watch_commands(self, monkeypatch):
        captured = {}

        class FakeRun:
            status = "ok"

        async def fake_once(limit=None, adapter=None, session=None):
            captured["once_limit"] = limit
            return FakeRun()

        async def fake_loop(interval=None, limit=None, adapter=None, session=None, max_iterations=None):
            captured.update(interval=interval, loop_limit=limit)
            return 1

        monkeypatch.setattr(cli, "watch_once", fake_once)
        monkeypatch.setattr(cli, "watch_loop", fake_loop)
        assert cli.main(["watch-once", "--limit", "100"]) == 0
        assert cli.main(["watch-loop", "--interval", "60", "--limit", "100"]) == 0
        assert captured == {"once_limit": 100, "interval": 60, "loop_limit": 100}
