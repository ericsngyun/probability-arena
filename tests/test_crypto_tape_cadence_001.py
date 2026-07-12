"""CRYPTO-TAPE-CADENCE-001 tests: bounded manual crypto tape session helper.

Covers: duration cap (36h), interval clamp (15-120 min), capture-count bound
(<=144), dry-run persists nothing while still showing the planned schedule +
expected capture count (exactly one dry probe, zero sleeps), bounded repeated
capture calls with sleeps between, abort on abnormal pass status, abort on
MarketOps degradation, session summary math (totals + horizon maturity +
provider_gap trend), CLI rendering, no network calls, and no forbidden
trading/execution vocabulary. In-memory SQLite; no network anywhere.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from app import cli
from app.models import MarketOpsRun
from app.services.crypto_tape import (
    SESSION_INTERVAL_MAX_MINUTES,
    SESSION_INTERVAL_MIN_MINUTES,
    SESSION_MAX_CAPTURES,
    SESSION_MAX_DURATION_HOURS,
    CryptoLifecycleTapeRecorder,
    CryptoTapeConfig,
    run_tape_session,
    summarize_tape_session,
)
from tests.test_crypto_tape_001 import (
    NOW,
    seed_full_token,
    session,  # fixture reuse
    tape_counts,
)

REPO = Path(__file__).resolve().parents[1]


def recorder() -> CryptoLifecycleTapeRecorder:
    return CryptoLifecycleTapeRecorder(config=CryptoTapeConfig(chain="solana"))


class FakeSleeper:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float):
        self.calls.append(seconds)


class FakeRecorder:
    """Counts run_once calls; returns canned pass summaries."""

    def __init__(self, results=None):
        self.calls = 0
        self.results = results

    def run_once(self, session, limit=None, hours=None, dry_run=False):
        result = (
            self.results[self.calls] if self.results else {
                "status": "dry_run" if dry_run else "ok",
                "tokens_considered": 10,
                "external_calls": 0,
                "survival_label_mix": {"provider_gap": 8 - min(self.calls, 8)},
                "tape_run_id": None if dry_run else self.calls + 1,
            }
        )
        self.calls += 1
        return result


# --- caps and clamps ---------------------------------------------------------------


class TestCaps:
    async def test_duration_capped_at_36_hours(self, session):
        sleeper = FakeSleeper()
        fake = FakeRecorder(results=[{"status": "error"}])  # abort immediately
        r = await run_tape_session(
            session, recorder=fake, duration_hours=100, interval_min=60,
            sleeper=sleeper,
        )
        assert r["duration_hours"] == SESSION_MAX_DURATION_HOURS == 36

    async def test_interval_clamped_low_and_high(self, session):
        fake = FakeRecorder(results=[{"status": "error"}])
        low = await run_tape_session(
            session, recorder=fake, duration_hours=1, interval_min=1,
            sleeper=FakeSleeper(),
        )
        assert low["interval_min"] == SESSION_INTERVAL_MIN_MINUTES == 15
        fake2 = FakeRecorder(results=[{"status": "error"}])
        high = await run_tape_session(
            session, recorder=fake2, duration_hours=6, interval_min=999,
            sleeper=FakeSleeper(),
        )
        assert high["interval_min"] == SESSION_INTERVAL_MAX_MINUTES == 120

    async def test_capture_count_hard_capped(self, session):
        fake = FakeRecorder(results=[{"status": "error"}])
        r = await run_tape_session(
            session, recorder=fake, duration_hours=36, interval_min=15,
            sleeper=FakeSleeper(),
        )
        assert r["captures_planned"] == SESSION_MAX_CAPTURES == 144
        assert len(r["planned_schedule_min"]) == 144
        assert r["planned_schedule_min"][:3] == [0, 15, 30]


# --- dry run -----------------------------------------------------------------------


class TestDryRun:
    async def test_dry_run_persists_nothing_and_shows_schedule(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        sleeper = FakeSleeper()
        r = await run_tape_session(
            session, recorder=recorder(), duration_hours=2, interval_min=30,
            dry_run=True, sleeper=sleeper,
        )
        assert r["status"] == "dry_run"
        assert r["captures_planned"] == 4          # 120 min / 30 min
        assert r["planned_schedule_min"] == [0, 30, 60, 90]
        assert r["captures_run"] == 1              # exactly one dry probe
        assert r["capture_statuses"] == ["dry_run"]
        assert r["probe"]["external_calls"] == 0
        assert sleeper.calls == []                 # never sleeps in dry-run
        assert all(count == 0 for count in tape_counts(session).values())
        assert r["session_summary"]["available"] is False

    async def test_dry_run_probe_call_count_is_one(self, session):
        fake = FakeRecorder()
        await run_tape_session(
            session, recorder=fake, duration_hours=36, interval_min=15,
            dry_run=True, sleeper=FakeSleeper(),
        )
        assert fake.calls == 1


# --- real session ------------------------------------------------------------------


class TestSession:
    async def test_bounded_captures_with_sleeps_between(self, session):
        fake = FakeRecorder()
        sleeper = FakeSleeper()
        r = await run_tape_session(
            session, recorder=fake, duration_hours=1, interval_min=20,
            sleeper=sleeper,
        )
        assert r["captures_planned"] == 3          # 60 // 20
        assert fake.calls == 3                     # bounded, no extra calls
        assert sleeper.calls == [1200.0, 1200.0]   # planned-1 sleeps of 20min
        assert r["status"] == "ok"
        assert r["capture_statuses"] == ["ok", "ok", "ok"]

    async def test_provider_gap_trend_math(self, session):
        fake = FakeRecorder()  # gap true-count decreases per capture (8,7,6)
        r = await run_tape_session(
            session, recorder=fake, duration_hours=1, interval_min=20,
            sleeper=FakeSleeper(),
        )
        trend = r["provider_gap_trend"]
        assert trend["first_capture_gap_share"] == 0.8
        assert trend["last_capture_gap_share"] == 0.6
        assert trend["direction"] == "improving"

    async def test_abort_on_abnormal_capture_status(self, session):
        fake = FakeRecorder(results=[
            {"status": "ok", "tokens_considered": 5, "external_calls": 0,
             "survival_label_mix": {}, "tape_run_id": 1},
            {"status": "error", "tokens_considered": 0, "external_calls": 0,
             "survival_label_mix": {}, "tape_run_id": None},
        ])
        sleeper = FakeSleeper()
        r = await run_tape_session(
            session, recorder=fake, duration_hours=2, interval_min=15,
            sleeper=sleeper,
        )
        assert r["status"] == "aborted"
        assert r["abort_reason"] == "capture 2 status=error"
        assert r["captures_run"] == 2
        assert fake.calls == 2                     # loop stopped
        assert len(sleeper.calls) == 1             # only the sleep before capture 2

    async def test_abort_on_marketops_degradation(self, session):
        session.add(MarketOpsRun(status="error"))
        session.flush()
        fake = FakeRecorder()
        sleeper = FakeSleeper()
        r = await run_tape_session(
            session, recorder=fake, duration_hours=2, interval_min=15,
            sleeper=sleeper,
        )
        assert r["status"] == "aborted"
        assert r["abort_reason"] == "latest MarketOps run errored"
        assert fake.calls == 1                     # aborted after first capture
        assert sleeper.calls == []

    async def test_real_session_end_to_end_with_summary_math(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        r = await run_tape_session(
            session, recorder=recorder(), duration_hours=1, interval_min=30,
            sleeper=FakeSleeper(),
        )
        assert r["status"] == "ok"
        assert r["captures_run"] == 2
        assert len(r["tape_run_ids"]) == 2
        s = r["session_summary"]
        assert s["available"] is True
        assert s["runs"] == 2
        # one token: birth created on first pass only; snapshot/actor per pass
        assert s["totals"]["birth_events"] == 1
        assert s["totals"]["snapshots"] == 2
        assert s["totals"]["actor_observations"] == 2
        assert s["totals"]["outcomes_updated"] == 2
        # single 2h-old token: outcome exists, horizons still unknown (honest)
        assert s["outcomes_tracked"] == 1
        assert s["horizon_maturity"]["survived_24h"]["unknown"] == 1
        assert s["db_impact_rows"] == 2 + 1 + 2 + 2 + 2

    def test_summarize_empty_run_ids(self, session):
        s = summarize_tape_session(session, [])
        assert s["available"] is False


# --- CLI ---------------------------------------------------------------------------


class TestCLI:
    async def test_session_cli_renders_dry_run(self, session, capsys):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        n = await cli.crypto_tape_session(
            duration_hours=2, interval_min=30, dry_run=True, session=session
        )
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "status=dry_run" in out
        assert "planned schedule: +0m, +30m, +60m, +90m" in out
        assert "external_calls=0" in out
        assert "dry-run session" in out

    def test_main_wires_command(self):
        import argparse

        holder = {}
        original = argparse.ArgumentParser.parse_args

        def fake_parse(self, *a, **k):
            holder["parser"] = self
            raise SystemExit(0)

        argparse.ArgumentParser.parse_args = fake_parse
        try:
            with pytest.raises(SystemExit):
                cli.main([])
        finally:
            argparse.ArgumentParser.parse_args = original
        actions = holder["parser"]._subparsers._group_actions[0].choices
        assert "crypto-tape-session" in actions


# --- safety ------------------------------------------------------------------------


class TestSafety:
    def test_no_timer_or_daemon_vocabulary_in_session_code(self):
        # the session helper must stay a bounded one-invocation tool
        src = (REPO / "app" / "services" / "crypto_tape.py").read_text()
        for term in ("systemd", "while True", "daemonize"):
            assert term not in src

    async def test_no_network_even_with_broken_httpx(self, session, monkeypatch):
        import httpx

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("tape session must not construct an HTTP client")

        monkeypatch.setattr(httpx, "AsyncClient", explode)
        monkeypatch.setattr(httpx, "Client", explode)
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        result = await run_tape_session(
            session, recorder=recorder(), duration_hours=1, interval_min=60,
            sleeper=FakeSleeper(),
        )
        assert result["status"] == "ok"
