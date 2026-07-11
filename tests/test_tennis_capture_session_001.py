"""TENNIS-CAPTURE-SESSION-001 tests: bounded manual capture-session runner.

Duration/interval/capture caps, dry-run pass-through (persists nothing),
capture_once called a bounded number of times, abort on abnormal status and
on detectable MarketOps error, session summary aggregation (movers, quote
coverage, DB impact), CLI parse/render, no network (fake recorder), no
forbidden vocabulary.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import (
    MarketOpsRun,
    TennisTapeLink,
    TennisTapeMarketSnapshot,
    TennisTapeRun,
    TennisTapeScoreSnapshot,
)
from app.services import tennis_tape as tt
from app.services.tennis_tape import (
    SESSION_ABORTED,
    SESSION_DRY_RUN,
    SESSION_INTERVAL_MAX_S,
    SESSION_INTERVAL_MIN_S,
    SESSION_MAX_DURATION_MIN,
    SESSION_OK,
    run_capture_session,
    summarize_session,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def instant_sleep(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(tt.asyncio, "sleep", no_sleep)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


class FakeRecorder:
    def __init__(self, statuses=None, persist=False):
        self.statuses = statuses or []
        self.persist = persist
        self.calls = []
        self._next_run_id = 100

    async def capture_once(self, session, limit=None, hours=24, dry_run=False):
        self.calls.append({"limit": limit, "dry_run": dry_run})
        status = self.statuses[len(self.calls) - 1] if (
            len(self.calls) <= len(self.statuses)
        ) else ("dry_run" if dry_run else "ok")
        out = {"status": status, "score_calls": 3,
               "score_snapshots": 0, "market_snapshots": 0, "links": {}}
        if self.persist and status == "ok":
            self._next_run_id += 1
            out["tape_run_id"] = self._next_run_id
        return out


def run(session, recorder=None, **kw):
    return asyncio.run(run_capture_session(
        session, recorder=recorder or FakeRecorder(), **kw
    ))


class TestCaps:
    def test_duration_cap(self, session):
        recorder = FakeRecorder()
        r = run(session, recorder, duration_min=999, interval_sec=60, dry_run=True)
        assert r["duration_min"] == SESSION_MAX_DURATION_MIN
        assert r["captures_planned"] == SESSION_MAX_DURATION_MIN * 60 // 60
        assert len(recorder.calls) == r["captures_planned"]

    def test_interval_clamp(self, session):
        r_low = run(session, duration_min=1, interval_sec=1, dry_run=True)
        assert r_low["interval_sec"] == SESSION_INTERVAL_MIN_S
        r_high = run(session, duration_min=1, interval_sec=9999, dry_run=True)
        assert r_high["interval_sec"] == SESSION_INTERVAL_MAX_S

    def test_capture_count_derived_and_bounded(self, session):
        recorder = FakeRecorder()
        r = run(session, recorder, duration_min=10, interval_sec=60, dry_run=True)
        assert r["captures_planned"] == 10
        assert r["captures_run"] == 10
        assert r["provider_calls"] == 30      # 3 per capture, from capture_once
        assert all(c["dry_run"] for c in recorder.calls)

    def test_limit_passed_through(self, session):
        recorder = FakeRecorder()
        run(session, recorder, duration_min=2, interval_sec=60,
            limit=40, dry_run=True)
        assert all(c["limit"] == 40 for c in recorder.calls)


class TestAbort:
    def test_abort_on_abnormal_capture_status(self, session):
        recorder = FakeRecorder(statuses=["ok", "skipped_provider_gap", "ok"])
        r = run(session, recorder, duration_min=10, interval_sec=60)
        assert r["status"] == SESSION_ABORTED
        assert "capture 2" in r["abort_reason"]
        assert r["captures_run"] == 2          # stopped immediately

    def test_abort_on_marketops_error(self, session):
        session.add(MarketOpsRun(status="error", started_at=NOW, created_at=NOW))
        session.commit()
        recorder = FakeRecorder()
        r = run(session, recorder, duration_min=10, interval_sec=60, dry_run=True)
        assert r["status"] == SESSION_ABORTED
        assert "MarketOps" in r["abort_reason"]
        assert r["captures_run"] == 1

    def test_ok_and_dry_run_statuses(self, session):
        assert run(session, duration_min=1, interval_sec=60,
                   dry_run=True)["status"] == SESSION_DRY_RUN
        assert run(session, FakeRecorder(persist=True), duration_min=1,
                   interval_sec=60)["status"] == SESSION_OK


class TestSummary:
    def test_dry_run_persists_nothing_and_no_summary(self, session):
        r = run(session, duration_min=5, interval_sec=60, dry_run=True)
        assert r["session_summary"]["available"] is False
        total = session.execute(text(
            "select (select count(*) from tennis_tape_runs) + "
            "(select count(*) from tennis_tape_market_snapshots)"
        )).scalar()
        assert total == 0

    def test_summarize_session_movers_and_coverage(self, session):
        runs = []
        for i in range(2):
            run_row = TennisTapeRun(status="ok", started_at=NOW, created_at=NOW)
            session.add(run_row)
            session.flush()
            runs.append(run_row.id)
        for run_id, mid in zip(runs, (0.40, 0.52)):
            session.add(TennisTapeMarketSnapshot(
                tape_run_id=run_id, observed_at=NOW,
                market_ticker="KXITFMATCH-26JUL11AABB-AA",
                yes_bid=int(mid * 100) - 1, yes_ask=int(mid * 100) + 1,
                midpoint=mid, created_at=NOW,
            ))
            session.add(TennisTapeLink(
                tape_run_id=run_id, market_ticker="KXITFMATCH-26JUL11AABB-AA",
                link_label="source_backed_link", created_at=NOW,
            ))
        session.add(TennisTapeScoreSnapshot(
            tape_run_id=runs[0], observed_at=NOW, provider_source="t",
            created_at=NOW,
        ))
        session.commit()
        s = summarize_session(session, runs)
        assert s["available"] is True
        assert s["runs"] == 2
        assert s["market_snapshots"] == 2
        assert s["score_snapshots"] == 1
        assert s["links"] == {"source_backed_link": 2}
        assert s["quote_coverage"] == 1.0
        assert s["top_movers"][0]["abs_range"] == pytest.approx(0.12)
        assert s["db_impact_rows"] == 2 + 1 + 2 + 2


class TestCLI:
    def test_cli_parses(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 3

        monkeypatch.setattr(cli, "tennis_tape_capture_session", fake)
        rc = cli.main([
            "tennis-tape-capture-session", "--duration-min", "20",
            "--interval-sec", "60", "--limit", "40", "--dry-run",
        ])
        assert rc == 0
        assert captured == {"duration_min": 20, "interval_sec": 60,
                            "limit": 40, "dry_run": True}

    def test_cli_renders(self, session, capsys, monkeypatch):
        monkeypatch.setattr(
            tt, "TennisTapeRecorder", lambda *a, **k: FakeRecorder()
        )
        n = asyncio.run(cli.tennis_tape_capture_session(
            duration_min=2, interval_sec=60, dry_run=True, session=session
        ))
        out = capsys.readouterr().out
        assert n == 2
        assert "never advice" in out
        assert "status=dry_run" in out
        assert "captures=2/2" in out


class TestSafety:
    def test_no_forbidden_vocab(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "tennis_tape.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "recommend_trade", "execution",
                    "buy", "sell", "markov"):
            assert bad not in code, bad
