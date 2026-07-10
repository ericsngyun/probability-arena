"""TENNIS-LIVE-FEED-002 tests: bounded WebSocket live-feed validation.

Fake-stream event handling, frame normalization, state-change detection,
matched/unmatched candidate mapping through the existing linker, empty-stream
fallback verdict, no-key short-circuit (no connection attempted), duration
clamping, key redaction, REST-vs-WS comparison rendering, no persistence, no
forbidden vocabulary. No real WebSocket or HTTP connection is ever made.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import cli
from app.config import Settings
from app.db import Base
from app.models import Market
from app.services.tennis_livefeed import (
    MAX_DURATION_SEC,
    VERDICT_FAIL,
    VERDICT_NO_KEY,
    VERDICT_NO_WINDOW,
    VERDICT_PARTIAL,
    VERDICT_PASS,
    TennisLiveFeedProbe,
    normalize_ws_message,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]
TICKER = "KXITFMATCH-26JUL09MATOCH-MAT"


def ws_event(first="K. Matsuda", second="M. Ochi", key=111, status="Set 1",
             scores=None, serve=None, date="2026-07-10"):
    return {
        "event_key": key, "event_date": date,
        "event_first_player": first, "event_second_player": second,
        "event_status": status, "event_serve": serve,
        "event_type_type": "Itf Men Singles",
        "scores": scores or [], "pointbypoint": [],
    }


def fake_stream(frames):
    async def factory(key, duration_sec):
        assert "APIkey" not in str(frames)   # frames carry no key
        for f in frames:
            yield f
    return factory


class FakeRestFetcher:
    source_name = "fake-rest.test"
    has_key = True

    def __init__(self, livescore_rows=0, fixtures=None):
        self.livescore_rows = livescore_rows
        self.fixtures = fixtures or {}
        self.calls = []

    async def _get(self, params):
        self.calls.append(params.get("method"))
        if params.get("method") == "get_livescore":
            return {"result": [{} for _ in range(self.livescore_rows)]}
        return {"result": self.fixtures.get(params.get("date_start"), [])}


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def seed_market(session, ticker=TICKER):
    session.add(Market(
        ticker=ticker, title="m", status="active",
        last_seen_at=NOW - timedelta(minutes=10),
        close_time=NOW + timedelta(hours=8),
    ))
    session.commit()


def probe(session, frames=(), key="k", rest=None, **kw):
    p = TennisLiveFeedProbe(
        settings=Settings(_env_file=None, tennis_provider_api_key=key),
        stream_factory=fake_stream(list(frames)),
        rest_fetcher=rest or FakeRestFetcher(),
    )
    return asyncio.run(p.probe(session, **kw))


class TestNormalization:
    def test_single_object(self):
        assert len(normalize_ws_message(json.dumps(ws_event()))) == 1

    def test_list(self):
        msg = json.dumps([ws_event(key=1), ws_event(key=2)])
        assert len(normalize_ws_message(msg)) == 2

    def test_result_wrapper(self):
        msg = json.dumps({"result": [ws_event()]})
        assert len(normalize_ws_message(msg)) == 1

    def test_garbage(self):
        assert normalize_ws_message("not json") == []
        assert normalize_ws_message(json.dumps(42)) == []


class TestProbe:
    def test_pass_verdict_with_state_changes_and_match(self, session):
        seed_market(session)
        frames = [
            json.dumps(ws_event(status="Set 1")),
            json.dumps(ws_event(status="Set 1", scores=[{"score_first": "3",
                                                         "score_second": "2"}])),
        ]
        r = probe(session, frames)
        assert r["ws_events"] == 2
        assert r["distinct_matches"] == 1
        assert r["state_changes"] == 1
        assert r["matched_candidates"] == 1
        assert r["matched_examples"][0]["ticker"] == TICKER
        assert r["verdict"] == VERDICT_PASS
        assert "keep API-Tennis" in r["recommendation"]

    def test_partial_when_events_but_no_candidate_match(self, session):
        seed_market(session)
        frames = [
            json.dumps(ws_event("Ana Diaz", "Mia Solis", key=9, status="Set 1")),
            json.dumps(ws_event("Ana Diaz", "Mia Solis", key=9, status="Set 2")),
        ]
        r = probe(session, frames)
        assert r["matched_candidates"] == 0
        assert r["verdict"] == VERDICT_PARTIAL

    def test_partial_when_match_but_frozen_state(self, session):
        seed_market(session)
        frames = [json.dumps(ws_event())] * 3    # identical: no state change
        r = probe(session, frames)
        assert r["matched_candidates"] == 1
        assert r["state_changes"] == 0
        assert r["verdict"] == VERDICT_PARTIAL

    def test_fail_verdict_on_empty_stream(self, session):
        seed_market(session)
        r = probe(session, frames=[])
        assert r["ws_events"] == 0
        assert r["verdict"] == VERDICT_FAIL
        assert "Goalserve" in r["recommendation"]

    def test_insufficient_window_without_candidates(self, session):
        r = probe(session, frames=[json.dumps(ws_event())])
        assert r["verdict"] == VERDICT_NO_WINDOW

    def test_no_key_never_connects(self, session):
        seed_market(session)
        called = []

        async def factory(key, duration):
            called.append(True)
            yield "{}"

        p = TennisLiveFeedProbe(
            settings=Settings(_env_file=None, tennis_provider_api_key=""),
            stream_factory=factory,
            rest_fetcher=FakeRestFetcher(),
        )
        r = asyncio.run(p.probe(session))
        assert r["verdict"] == VERDICT_NO_KEY
        assert called == []
        assert r["rest_comparison"] is None

    def test_duration_clamped(self, session):
        seed_market(session)
        r = probe(session, frames=[], duration_sec=99999)
        assert r["duration_sec"] == MAX_DURATION_SEC

    def test_connection_error_reported_by_type_only(self, session):
        seed_market(session)

        async def exploding(key, duration):
            raise ConnectionRefusedError(f"wss://x?APIkey={key}")
            yield  # pragma: no cover

        p = TennisLiveFeedProbe(
            settings=Settings(_env_file=None, tennis_provider_api_key="SECRET99"),
            stream_factory=exploding,
            rest_fetcher=FakeRestFetcher(),
        )
        r = asyncio.run(p.probe(session))
        assert r["connection_error"] == "ConnectionRefusedError"
        assert "SECRET99" not in json.dumps(
            {k: v for k, v in r.items() if k != "note"}
        )

    def test_rest_comparison_counts(self, session):
        seed_market(session)
        rest = FakeRestFetcher(
            livescore_rows=0,
            fixtures={"2026-07-09": [ws_event(date="2026-07-09")]},
        )
        r = probe(session, frames=[json.dumps(ws_event())], rest=rest)
        rc = r["rest_comparison"]
        assert rc["available"] is True
        assert rc["rest_livescore_rows"] == 0
        assert rc["rest_source_backed"] == 1
        assert rest.calls.count("get_livescore") == 1

    def test_persists_nothing(self, session):
        seed_market(session)
        before = session.execute(text(
            "select (select count(*) from tennis_tape_runs) + "
            "(select count(*) from market_price_ticks)"
        )).scalar()
        probe(session, frames=[json.dumps(ws_event())])
        session.commit()
        after = session.execute(text(
            "select (select count(*) from tennis_tape_runs) + "
            "(select count(*) from market_price_ticks)"
        )).scalar()
        assert before == after == 0


class TestCLI:
    def test_cli_parses(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "tennis_api_livefeed_probe", fake)
        rc = cli.main(["tennis-api-livefeed-probe", "--duration-sec", "30", "--top", "3"])
        assert rc == 0
        assert captured == {"duration_sec": 30, "top": 3}

    def test_cli_renders(self, session, capsys, monkeypatch):
        import app.services.tennis_livefeed as mod

        seed_market(session)
        monkeypatch.setattr(
            mod.TennisLiveFeedProbe, "__init__",
            lambda self, settings=None, stream_factory=None, rest_fetcher=None: (
                setattr(self, "settings", Settings(_env_file=None, tennis_provider_api_key="k")),
                setattr(self, "_stream_factory", fake_stream(
                    [json.dumps(ws_event()), json.dumps(ws_event(status="Set 2"))]
                )),
                setattr(self, "_rest_fetcher", FakeRestFetcher()),
            )[-1],
        )
        n = asyncio.run(cli.tennis_api_livefeed_probe(session=session))
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "VERDICT: api_tennis_ws_pass" in out
        assert "APIkey" not in out


class TestSafety:
    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "tennis_livefeed.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "jupiter", "recommend_trade",
                    "execute_trade", "execution", "buy", "sell", "markov"):
            assert bad not in code, bad

    def test_display_url_has_no_key_param(self):
        from app.services.tennis_livefeed import WS_URL

        assert "APIkey" not in WS_URL

    def test_note_language(self, session):
        r = probe(session, frames=[])
        assert "never advice" in r["note"]
        assert "not EV" in r["note"]
