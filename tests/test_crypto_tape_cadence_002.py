"""CRYPTO-TAPE-CADENCE-002 tests: lock-safe crypto tape session.

Reproduces the production crash — a capture's run-row INSERT raised
sqlite3.OperationalError "database is locked", the loop set an abort reason
but never rolled back, and summarize_tape_session then queried a
pending-rollback session and raised PendingRollbackError. Covers: locked-error
detection, rollback on failure, bounded retry recovery, clean abort after
exhausted retries (reason=database_locked, failed_capture_index, rows before
abort), non-locked errors, abort after a successful capture, run_once error
path not masking the original error, defensive summary, dry-run unaffected,
no network, no forbidden capability. In-memory SQLite.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app import cli
from app.models import CryptoTokenLifecycleRun
from app.services.crypto_tape import (
    ABORT_DB_LOCKED,
    DB_LOCKED_MAX_ATTEMPTS,
    CryptoLifecycleTapeRecorder,
    CryptoTapeConfig,
    _is_db_locked,
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


def locked_error() -> OperationalError:
    """An OperationalError shaped like SQLAlchemy wrapping sqlite's lock."""
    return OperationalError(
        "INSERT INTO crypto_token_lifecycle_runs ...",
        {},
        Exception("database is locked"),
    )


class ScriptedRecorder:
    """run_once returns/raises per a script of ('ok'|exc) entries. Counts calls
    and (optionally) rolls back a real session to mimic a poisoned transaction."""

    def __init__(self, script, session=None, run_id_start=1):
        self.script = script
        self.calls = 0
        self.session = session
        self._next_run_id = run_id_start

    def run_once(self, session, limit=None, hours=None, dry_run=False):
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            # emulate the real failure: the pending transaction is now unusable
            raise item
        rid = self._next_run_id
        self._next_run_id += 1
        return {
            "status": "ok",
            "tokens_considered": 10,
            "external_calls": 0,
            "birth_events_created": 2,
            "snapshots_created": 10,
            "actor_observations_created": 10,
            "outcomes_updated": 10,
            "survival_label_mix": {"provider_gap": 6},
            "tape_run_id": rid,
        }


# --- detection ---------------------------------------------------------------------


class TestDetection:
    def test_detects_sqlalchemy_wrapped_lock(self):
        assert _is_db_locked(locked_error()) is True

    def test_detects_plain_locked_message(self):
        assert _is_db_locked(Exception("sqlite3.OperationalError: database is locked"))

    def test_non_lock_errors_are_not_locked(self):
        assert _is_db_locked(ValueError("bad value")) is False
        assert _is_db_locked(None) is False


# --- retry + rollback --------------------------------------------------------------


class TestRetry:
    async def test_retry_recovers_on_second_attempt(self, session, monkeypatch):
        rollbacks = _count_rollbacks(session, monkeypatch)
        fake = ScriptedRecorder([locked_error(), "ok"])
        r = await run_tape_session(
            session, recorder=fake, duration_hours=1, interval_min=60,
            sleeper=FakeSleeper(),
        )
        assert r["status"] == "ok"
        assert fake.calls == 2                # failed once, retried, succeeded
        assert rollbacks["n"] >= 1            # rolled back the poisoned tx
        assert r["captures_run"] == 1

    async def test_retry_sleeps_between_attempts(self, session):
        sleeper = FakeSleeper()
        fake = ScriptedRecorder([locked_error(), locked_error(), "ok"])
        await run_tape_session(
            session, recorder=fake, duration_hours=1, interval_min=60,
            sleeper=sleeper, max_lock_attempts=3, lock_retry_seconds=4.0,
        )
        # two retry sleeps of 4.0s (no inter-capture sleep: only 1 capture planned)
        assert sleeper.calls == [4.0, 4.0]

    async def test_aborts_clean_after_exhausted_retries(self, session, monkeypatch):
        rollbacks = _count_rollbacks(session, monkeypatch)
        fake = ScriptedRecorder([locked_error()])  # always locked
        r = await run_tape_session(
            session, recorder=fake, duration_hours=2, interval_min=30,
            sleeper=FakeSleeper(),
        )
        assert r["status"] == "aborted"
        assert r["aborted"] is True
        assert r["abort_reason"] == ABORT_DB_LOCKED
        assert r["failed_capture_index"] == 0
        assert r["captures_run"] == 0
        assert fake.calls == DB_LOCKED_MAX_ATTEMPTS          # tried the full budget
        assert rollbacks["n"] >= DB_LOCKED_MAX_ATTEMPTS
        # the crash was here: summary must render, not raise
        assert r["session_summary"]["available"] is False

    async def test_summary_does_not_raise_pending_rollback(self, session):
        # end-to-end: a persistent lock must NOT leak PendingRollbackError
        fake = ScriptedRecorder([locked_error()])
        r = await run_tape_session(
            session, recorder=fake, duration_hours=1, interval_min=60,
            sleeper=FakeSleeper(),
        )
        # session is usable afterwards (proves it was rolled back cleanly)
        assert session.execute(
            select(CryptoTokenLifecycleRun)
        ).scalars().all() == []
        assert r["session_summary"]["available"] is False


class TestNonLockErrors:
    async def test_non_lock_error_aborts_without_retry(self, session):
        fake = ScriptedRecorder([ValueError("boom")])
        sleeper = FakeSleeper()
        r = await run_tape_session(
            session, recorder=fake, duration_hours=2, interval_min=30,
            sleeper=sleeper,
        )
        assert r["status"] == "aborted"
        assert r["abort_reason"] == "capture 1 raised ValueError"
        assert fake.calls == 1                 # NOT retried
        assert sleeper.calls == []             # no retry sleep for a non-lock error


class TestAbortAfterSuccess:
    async def test_abort_after_one_good_capture_reports_rows(self, session):
        fake = ScriptedRecorder(["ok", locked_error()])
        r = await run_tape_session(
            session, recorder=fake, duration_hours=2, interval_min=30,
            sleeper=FakeSleeper(),
        )
        assert r["status"] == "aborted"
        assert r["abort_reason"] == ABORT_DB_LOCKED
        assert r["captures_run"] == 1
        assert r["failed_capture_index"] == 1
        assert r["tape_run_ids"] == [1]
        # 1 run + 2 births + 10 snap + 10 actors + 10 outcomes = 33
        assert r["rows_written_before_abort"] == 33


# --- run_once error path -----------------------------------------------------------


class TestRunOnceErrorPath:
    def test_error_recording_commit_failure_does_not_mask_original(
        self, session, monkeypatch
    ):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        # force the final commit AND the error-record commit to lock
        monkeypatch.setattr(
            type(session), "commit",
            lambda self: (_ for _ in ()).throw(locked_error()),
        )
        with pytest.raises(OperationalError):   # the ORIGINAL lock, not a mask
            recorder().run_once(session)
        # session is not left poisoned: a rollback + query works
        session.rollback()
        assert session.execute(select(CryptoTokenLifecycleRun)).scalars().all() == []


# --- defensive summary -------------------------------------------------------------


class TestDefensiveSummary:
    def test_summary_survives_a_poisoned_session(self, session, monkeypatch):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        recorder().run_once(session)  # commit a real run so run_ids is non-empty
        rid = session.execute(select(CryptoTokenLifecycleRun.id)).scalars().first()

        calls = {"n": 0}
        real_execute = session.execute

        def flaky_execute(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError("SELECT ...", {}, Exception("database is locked"))
            return real_execute(*a, **k)

        monkeypatch.setattr(session, "execute", flaky_execute)
        out = summarize_tape_session(session, [rid])
        assert out["available"] is False
        assert "rolled back" in out["reason"]

    def test_summary_empty_when_no_runs(self, session):
        out = summarize_tape_session(session, [])
        assert out["available"] is False
        assert "aborted before first capture" in out["reason"]


# --- dry-run unaffected ------------------------------------------------------------


class TestDryRun:
    async def test_dry_run_still_persists_nothing_and_no_abort(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        r = await run_tape_session(
            session, recorder=recorder(), duration_hours=2, interval_min=30,
            dry_run=True, sleeper=FakeSleeper(),
        )
        assert r["status"] == "dry_run"
        assert r["aborted"] is False
        assert r["failed_capture_index"] is None
        assert r["rows_written_before_abort"] == 0
        assert all(count == 0 for count in tape_counts(session).values())


# --- CLI ---------------------------------------------------------------------------


class TestCLI:
    async def test_cli_renders_database_locked_abort(self, session, capsys, monkeypatch):
        fake = ScriptedRecorder([locked_error()])
        import app.services.crypto_tape as tape_mod

        async def fake_run(session, **kwargs):
            kwargs.pop("recorder", None)
            return await run_tape_session(
                session, recorder=fake, sleeper=FakeSleeper(), **kwargs
            )

        monkeypatch.setattr(tape_mod, "run_tape_session", fake_run)
        n = await cli.crypto_tape_session(
            duration_hours=1, interval_min=60, session=session
        )
        out = capsys.readouterr().out
        assert n == 0
        assert "aborted=True" in out
        assert "abort_reason=database_locked" in out
        assert "failed_capture_index=0" in out
        assert "check MarketOps / tick-aggregation write contention" in out


# --- safety ------------------------------------------------------------------------


class TestSafety:
    async def test_no_network_even_with_broken_httpx(self, session, monkeypatch):
        import httpx

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("tape session must not construct an HTTP client")

        monkeypatch.setattr(httpx, "AsyncClient", explode)
        monkeypatch.setattr(httpx, "Client", explode)
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        r = await run_tape_session(
            session, recorder=recorder(), duration_hours=1, interval_min=60,
            sleeper=FakeSleeper(),
        )
        assert r["status"] == "ok"


def _count_rollbacks(session, monkeypatch) -> dict:
    """Wrap session.rollback to count calls (still performs the real rollback)."""
    counter = {"n": 0}
    real = session.rollback

    def counting_rollback():
        counter["n"] += 1
        return real()

    monkeypatch.setattr(session, "rollback", counting_rollback)
    return counter
