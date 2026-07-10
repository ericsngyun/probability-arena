"""TENNIS-GOALSERVE-001 tests: bounded Goalserve live-state validation.

Fake-response normalization (dates, players, sets, serve, in-play detection),
key redaction (path-embedded key never in reports/display URLs), missing-key
no-op, bounded call cap, source-backed matching via the existing linker,
provider_no_match fallback, live-state field extraction, state-change
detection across probes, verdict ladder, no persistence, no forbidden
vocabulary. No real network request is ever made.
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
from app.services import tennis_goalserve as gs
from app.services.tennis_goalserve import (
    MAX_PROBES,
    VERDICT_FAIL,
    VERDICT_NO_KEY,
    VERDICT_NO_WINDOW,
    VERDICT_PARTIAL,
    VERDICT_PASS,
    GoalserveTennisClient,
    GoalserveValidationService,
    normalize_goalserve_live,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]
TICKER = "KXITFMATCH-26JUL09MATOCH-MAT"


def gs_match(first="K. Matsuda", second="M. Ochi", mid="777", status="Set 2",
             s1=("6", "4"), s2=("3", "2"), points="40-30", serve_first=True,
             date="10.07.2026"):
    return {
        "@id": mid, "@date": date, "@status": status, "@points": points,
        "player": [
            {"@name": first, "s1": s1[0], "s2": s2[0],
             "@serve": "True" if serve_first else "False"},
            {"@name": second, "s1": s1[1], "s2": s2[1],
             "@serve": "False" if serve_first else "True"},
        ],
    }


def gs_payload(*matches, category="ITF Men Kashiwa, Singles"):
    return {"scores": {"category": [{"@name": category, "match": list(matches)}]}}


class FakeGoalserveClient:
    source_name = "goalserve.com"
    display_url = "https://www.goalserve.com/getfeed/<key-redacted>/tennis_scores/live?json=1"

    def __init__(self, payloads=None, has_key=True):
        self.payloads = list(payloads or [])
        self.has_key = has_key
        self.calls = 0
        self.last_error = None

    async def fetch_live(self):
        self.calls += 1
        if not self.payloads:
            self.last_error = "TimeoutException"
            return None
        return self.payloads.pop(0)


@pytest.fixture(autouse=True)
def instant_sleep(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(gs.asyncio, "sleep", no_sleep)


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


def validate(session, client, **kw):
    service = GoalserveValidationService(
        client=client, settings=Settings(_env_file=None)
    )
    kw.setdefault("interval_sec", 5)
    return asyncio.run(service.validate(session, **kw))


class TestNormalization:
    def test_full_match_normalized(self):
        rows = normalize_goalserve_live(gs_payload(gs_match()))
        assert len(rows) == 1
        r = rows[0]
        assert r["event_key"] == "777"
        assert r["event_date"] == "2026-07-10"           # dd.mm.yyyy converted
        assert r["event_first_player"] == "K. Matsuda"
        assert r["event_status"] == "Set 2"
        assert r["gs_sets"] == [{"set": 1, "a": "6", "b": "4"},
                                {"set": 2, "a": "3", "b": "2"}]
        assert r["gs_point_score"] == "40-30"
        assert r["gs_serve"] == "first"
        assert r["gs_in_play"] is True

    def test_in_play_detection(self):
        for status, in_play in (("Set 1", True), ("Finished", False),
                                ("14:30", False), ("Retired", False),
                                ("Not Started", False)):
            rows = normalize_goalserve_live(gs_payload(gs_match(status=status)))
            assert rows[0]["gs_in_play"] is in_play, status

    def test_single_match_dict_not_list(self):
        payload = {"scores": {"category": {"@name": "ITF", "match": gs_match()}}}
        assert len(normalize_goalserve_live(payload)) == 1

    def test_missing_player_dropped(self):
        broken = gs_match()
        broken["player"] = [broken["player"][0]]
        assert normalize_goalserve_live(gs_payload(broken)) == []

    def test_garbage(self):
        assert normalize_goalserve_live(None) == []
        assert normalize_goalserve_live({"scores": {}}) == []


class TestClientKeyHandling:
    def test_no_key_no_request(self):
        client = GoalserveTennisClient(api_key="")
        assert asyncio.run(client.fetch_live()) is None
        assert client.has_key is False

    def test_display_url_never_contains_key(self):
        client = GoalserveTennisClient(api_key="SUPERSECRET42")
        assert "SUPERSECRET42" not in client.display_url
        assert "key-redacted" in client.display_url


class TestValidation:
    def test_pass_verdict_live_match_and_state_change(self, session):
        seed_market(session)
        client = FakeGoalserveClient([
            gs_payload(gs_match(points="40-30")),
            gs_payload(gs_match(points="AD-40", s2=("4", "2"))),
        ])
        r = validate(session, client, probes=2)
        assert r["calls_made"] == 2
        assert r["live_rows_per_probe"] == [1, 1]
        assert r["in_play_rows_per_probe"] == [1, 1]
        assert r["state_changes"] == 1
        assert r["matched_candidates"] == 1
        assert r["matched_examples"][0]["ticker"] == TICKER
        assert r["live_state_fields"]["sets"] == 1
        assert r["live_state_fields"]["point_score"] == 1
        assert r["live_state_fields"]["serve"] == 1
        assert r["verdict"] == VERDICT_PASS
        assert "TENNIS-TAPE-GOALSERVE-001" in r["recommendation"]

    def test_partial_when_rows_but_no_candidate_match(self, session):
        seed_market(session)
        client = FakeGoalserveClient([
            gs_payload(gs_match("Ana Diaz", "Mia Solis")),
        ])
        r = validate(session, client, probes=1)
        assert r["matched_candidates"] == 0
        assert r["miss_examples"][0]["label"] == "provider_no_match"
        assert r["verdict"] == VERDICT_PARTIAL

    def test_fail_when_all_fetches_fail(self, session):
        seed_market(session)
        client = FakeGoalserveClient([])   # every call errors
        r = validate(session, client, probes=2)
        assert r["fetch_errors"] == ["TimeoutException", "TimeoutException"]
        assert r["verdict"] == VERDICT_FAIL
        assert "market-only" in r["recommendation"]

    def test_fail_when_rows_but_nothing_in_play_and_no_match(self, session):
        seed_market(session)
        client = FakeGoalserveClient([
            gs_payload(gs_match("Ana Diaz", "Mia Solis", status="Finished")),
        ])
        r = validate(session, client, probes=1)
        assert r["verdict"] == VERDICT_FAIL

    def test_insufficient_window(self, session):
        client = FakeGoalserveClient([gs_payload(gs_match())])
        r = validate(session, client, probes=1)
        assert r["verdict"] == VERDICT_NO_WINDOW

    def test_no_key_short_circuits(self, session):
        seed_market(session)
        client = FakeGoalserveClient(has_key=False)
        r = validate(session, client, probes=3)
        assert r["verdict"] == VERDICT_NO_KEY
        assert client.calls == 0

    def test_probe_cap(self, session):
        seed_market(session)
        client = FakeGoalserveClient([gs_payload(gs_match())] * 50)
        r = validate(session, client, probes=50, interval_sec=5)
        assert r["calls_made"] == MAX_PROBES

    def test_persists_nothing(self, session):
        seed_market(session)
        validate(session, FakeGoalserveClient([gs_payload(gs_match())]), probes=1)
        session.commit()
        total = session.execute(text(
            "select (select count(*) from tennis_tape_runs) + "
            "(select count(*) from market_price_ticks)"
        )).scalar()
        assert total == 0

    def test_no_key_string_anywhere_in_report(self, session):
        seed_market(session)
        r = validate(session, FakeGoalserveClient([gs_payload(gs_match())]), probes=1)
        assert "SUPERSECRET" not in json.dumps(r, default=str)
        assert "key-redacted" in r["display_url"]


class TestCLI:
    def test_cli_parses(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "tennis_goalserve_probe", fake)
        rc = cli.main([
            "tennis-goalserve-probe", "--probes", "3", "--interval-sec", "10",
            "--top", "5",
        ])
        assert rc == 0
        assert captured == {"probes": 3, "interval_sec": 10, "top": 5}

    def test_cli_renders(self, session, capsys, monkeypatch):
        seed_market(session)
        monkeypatch.setattr(
            gs, "GoalserveTennisClient",
            lambda api_key="", timeout=15.0: FakeGoalserveClient(
                [gs_payload(gs_match())]
            ),
        )
        n = asyncio.run(cli.tennis_goalserve_probe(
            probes=1, interval_sec=5, session=session
        ))
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "VERDICT: goalserve_pass" in out
        assert "key-redacted" in out
        assert "api_tennis_baseline" in out


class TestSafety:
    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "tennis_goalserve.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "jupiter", "recommend_trade",
                    "execute_trade", "execution", "buy", "sell", "markov"):
            assert bad not in code, bad

    def test_config_default_empty(self):
        assert Settings(_env_file=None).goalserve_tennis_api_key == ""

    def test_note_language(self, session):
        r = validate(session, FakeGoalserveClient(has_key=False))
        assert "never advice" in r["note"]
        assert "not EV" in r["note"]
