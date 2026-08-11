"""CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R1/R2/R4/R7 — the review
round that followed B0-B9. Three independent reviewers each returned REQUEST
CHANGES; these are the code-side blockers.

  R1 — `classify_backlog` ran TWICE per pass (once inside
       `unreconciled_backlog`, once inside `recoverable_backlog_summary`,
       with the same session/cutoff/now) and neither call is reachable by
       `max_duration_seconds`: the deadline is anchored at pass start
       (`_assemble_pass_locked`'s `deadline = started + ...`) but only
       CHECKED between batches, so the whole prelude is subtracted from the
       batch loop's budget before the first token is reconciled. Measured
       1.92s + 1.94s cold / 4.02s + 3.21s warm on a fast Mac. Classify once,
       thread the partition through, and report the residual as
       `classify_ms`.
  R2 — `db-integrity-check` / `db-schema-report` CREATED the file they
       claimed to inspect and then certified the empty result green
       (`integrity_check duration: 0.00s / ok / PASS`, exit 0, 0-byte
       database left behind). Same defect class as the `sqlite3 ""` HIGH-2
       the command was written to replace.
  R4 — `--dry-run` inherits `backlog_expiring` as a terminal status and
       therefore exits non-zero; the runbook prescribes it as a mandatory
       verification step. This pins the contract the runbook now documents.
  R7 — the RECOVERABLE frontier (the one that actually moves) existed only
       inside the `backlog_expiring` error string; the raw frontier that IS
       printed reads ~204h regardless of pass health.

In-memory SQLite for the service tests; a real temp file for the CLI guard
tests (the whole point of R2 is what happens to a file on disk). No network
anywhere.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import CryptoPriceTick, CryptoToken
from app.services.crypto_tape import CryptoLifecycleTapeRecorder, CryptoTapeConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
CHAIN = "solana"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _mint(session, address: str, *, born_hours_ago: float, liquidity: float | None = 10_000.0):
    now = datetime.now(timezone.utc)
    first_seen = now - timedelta(hours=born_hours_ago)
    session.add(CryptoToken(
        chain=CHAIN, token_address=address, symbol=address[:6],
        first_seen_at=first_seen, last_seen_at=now,
    ))
    if liquidity is not None:
        session.add(CryptoPriceTick(
            chain=CHAIN, token_address=address, pair_address=f"pair-{address}",
            observed_at=first_seen, price_usd=1.0, liquidity_usd=liquidity,
            volume_24h_usd=5_000.0,
        ))
    session.flush()
    return first_seen


def _tick_at(session, address: str, when: datetime, liquidity: float):
    session.add(CryptoPriceTick(
        chain=CHAIN, token_address=address, pair_address=f"pair-{address}",
        observed_at=when, price_usd=1.0, liquidity_usd=liquidity,
        volume_24h_usd=5_000.0,
    ))
    session.flush()


def _recorder(retention_days: int = 7) -> CryptoLifecycleTapeRecorder:
    return CryptoLifecycleTapeRecorder(
        CryptoTapeConfig(chain=CHAIN, retention_days=retention_days)
    )


def _mixed_backlog(session):
    """A backlog with both write-offs and genuinely recoverable work, so the
    pass takes the `room > 0` path that calls BOTH classify sites."""
    now = datetime.now(timezone.utc)
    _mint(session, "tok-lost-a", born_hours_ago=200, liquidity=10_000.0)
    _mint(session, "tok-lost-b", born_hours_ago=210, liquidity=10_000.0)
    session.add(CryptoToken(
        chain=CHAIN, token_address="tok-no-liq",
        first_seen_at=now - timedelta(hours=60), last_seen_at=now, symbol="x",
    ))
    born = _mint(session, "tok-recoverable", born_hours_ago=60, liquidity=10_000.0)
    _tick_at(session, "tok-recoverable", born + timedelta(hours=24), liquidity=9_000.0)
    _mint(session, "tok-no-evidence", born_hours_ago=60, liquidity=10_000.0)
    session.flush()
    return now


# --- R1: classify ONCE per pass ----------------------------------------------

def test_run_once_classifies_the_backlog_exactly_once(session, monkeypatch):
    """R1 — the whole blocker. `run_once` used to call `classify_backlog`
    twice with identical arguments: once via `unreconciled_backlog` and once
    via `recoverable_backlog_summary`. Both are full-backlog scans, both run
    BEFORE `_assemble_pass` (so `max_duration_seconds` cannot bound either),
    and both produce the same partition because nothing writes in between.

    Reverting the fix (dropping either `classes=` argument, or the eager
    `classify_backlog` call in `run_once`) makes this count 2 and fails.
    """
    _mixed_backlog(session)
    rec = _recorder(retention_days=2)

    calls = []
    real = CryptoLifecycleTapeRecorder.classify_backlog

    def counting(self, sess, cutoff, *, now=None):
        calls.append((cutoff, now))
        return real(self, sess, cutoff, now=now)

    monkeypatch.setattr(CryptoLifecycleTapeRecorder, "classify_backlog", counting)

    summary = rec.run_once(
        session, limit=100, hours=48, dry_run=True, include_backlog=True,
    )

    assert summary["backlog_size"] > 0, (
        "test would be vacuous: the pass must actually have a backlog, "
        "otherwise neither classify site is reached"
    )
    assert len(calls) == 1, (
        f"classify_backlog ran {len(calls)} times in one pass "
        f"(args: {calls}) — it must run exactly once and be threaded "
        "through to both unreconciled_backlog and recoverable_backlog_summary"
    )


def test_threaded_partition_produces_the_same_selection_as_classifying_twice(session):
    """R1 safety: threading a precomputed partition must not change WHAT is
    selected or reported — only how many times it is computed."""
    _mixed_backlog(session)
    rec = _recorder(retention_days=2)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=48)

    classes = rec.classify_backlog(session, cutoff, now=now)

    independent_sel = [t.token_address for t in rec.unreconciled_backlog(
        session, cutoff, limit=10, now=now)]
    threaded_sel = [t.token_address for t in rec.unreconciled_backlog(
        session, cutoff, limit=10, now=now, classes=classes)]
    assert independent_sel == threaded_sel

    independent_sum = rec.recoverable_backlog_summary(session, cutoff, now=now)
    threaded_sum = rec.recoverable_backlog_summary(
        session, cutoff, now=now, classes=classes)
    assert independent_sum == threaded_sum


def test_classify_ms_is_reported_on_the_summary_and_the_run_config(session):
    """R1 — bounding was deferred (see the report/docstrings), so the
    residual must at least be OBSERVABLE. `classify_ms` is the portion of
    `duration_ms` that `max_duration_seconds` structurally cannot reach."""
    _mixed_backlog(session)
    rec = _recorder(retention_days=2)

    summary = rec.run_once(
        session, limit=100, hours=48, dry_run=True, include_backlog=True,
    )

    assert "classify_ms" in summary, "classify_ms missing from the pass summary"
    assert isinstance(summary["classify_ms"], float)
    assert summary["classify_ms"] >= 0.0
    # The durable half of this contract is pinned by
    # `test_recoverable_frontier_lands_in_the_durable_run_config`, which
    # asserts `classify_ms` is in the persisted run row's config["frontier"].


def test_classify_ms_is_none_when_the_backlog_lane_is_off(session):
    """No backlog lane, no classification — and therefore no misleading 0.0
    that would read as 'classification was free'."""
    _mixed_backlog(session)
    rec = _recorder(retention_days=2)
    summary = rec.run_once(
        session, limit=100, hours=48, dry_run=True, include_backlog=False,
    )
    assert summary["classify_ms"] is None


# --- R7: the RECOVERABLE frontier must be durable and printable --------------

def test_recoverable_frontier_lands_in_the_durable_run_config(session):
    """R7 — `config["frontier"]` carried only the RAW frontier, which B3
    proved is pinned at ~204h by a single permanent write-off and therefore
    cannot distinguish a healthy pass from a starving one after the fact.
    The four recoverable metrics existed but appeared nowhere durable."""
    _mixed_backlog(session)
    rec = _recorder(retention_days=2)

    from app.models import CryptoTokenLifecycleRun

    rec.run_once(session, limit=100, hours=48, dry_run=False, include_backlog=True)
    run = session.query(CryptoTokenLifecycleRun).order_by(
        CryptoTokenLifecycleRun.id.desc()).first()
    assert run is not None
    frontier = (run.config or {}).get("frontier") or {}

    for key in (
        "recoverable_backlog_count",
        "oldest_recoverable_due_at",
        "oldest_recoverable_age_seconds",
        "recoverable_at_retention_risk",
        "writeoff_count",
        "classify_ms",
    ):
        assert key in frontier, (
            f"{key!r} missing from the durable run row's config['frontier']: "
            f"{sorted(frontier)}"
        )
    assert frontier["writeoff_count"], (
        "the write-off census should be non-empty for this fixture"
    )


def test_cli_prints_the_recoverable_frontier(capsys):
    """R7 — an operator reads stdout, not the run row's JSON blob. In a
    milestone named ...-AND-OPERATOR-PATH-001 the recoverable frontier must
    be on the operator path."""
    import asyncio

    from app import cli as cli_mod

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _mixed_backlog(s)
        s.commit()
        asyncio.run(cli_mod.crypto_tape_reconcile(
            dry_run=True, force=True, hours=48, limit=100, session=s,
        ))
    out = capsys.readouterr().out
    for token in (
        "recoverable_backlog_count=",
        "oldest_recoverable_age_seconds=",
        "recoverable_at_retention_risk=",
        "writeoff_count=",
        "classify_ms=",
    ):
        assert token in out, f"{token!r} not printed by crypto-tape-reconcile:\n{out}"
    # Printed LABELS are not enough — a `None` next to every one of them would
    # satisfy the check above while telling an operator nothing.
    assert re.search(r"recoverable_backlog_count=\d+", out), out
    assert re.search(r"classify_ms=\d+\.\d+", out), out
    assert "classify_ms=None" not in out
    assert "recoverable_backlog_count=None" not in out


# --- R2: the integrity gate must not create the file it inspects -------------

def _run_cli(args: list[str], db_url: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = db_url
    env.setdefault("PYTHONPATH", str(REPO_ROOT))
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.mark.parametrize("command", ["db-integrity-check", "db-schema-report"])
def test_missing_database_fails_and_creates_nothing(tmp_path, command):
    """R2 — the reported defect verbatim: pointed at a nonexistent path,
    `db-integrity-check` printed `0.00s / ok / PASS`, exited 0, and left a
    0-byte database behind. It is the SOLE integrity gate for the 4.55 GB
    production migration and for the restore flow, where pointing at a
    not-yet-restored target is a realistic mistake.

    Reverting the `_resolve_db_target` stat guard restores exit 0 and the
    stray file, failing both assertions below."""
    missing = tmp_path / "not-restored-yet.db"
    assert not missing.exists()

    proc = _run_cli([command], f"sqlite:///{missing}")

    assert proc.returncode != 0, (
        f"{command} passed against a nonexistent database:\n{proc.stdout}"
    )
    assert "FAIL" in proc.stdout
    assert not missing.exists(), (
        f"{command} CREATED the database file it claimed to inspect: {missing}"
    )


@pytest.mark.parametrize("command", ["db-integrity-check", "db-schema-report"])
def test_zero_byte_database_fails(tmp_path, command):
    """R2 — a 0-byte file trivially satisfies every check while proving
    nothing; that is precisely the artifact the pre-fix command left behind,
    so re-running it would have 'passed' a second time."""
    empty = tmp_path / "empty.db"
    empty.touch()
    assert empty.stat().st_size == 0

    proc = _run_cli([command], f"sqlite:///{empty}")

    assert proc.returncode != 0, (
        f"{command} passed against a 0-byte database:\n{proc.stdout}"
    )
    assert "0 bytes" in proc.stdout


def test_real_database_still_passes(tmp_path):
    """Guard against over-correction: a genuine database must still PASS,
    otherwise the fix has simply broken the gate in the other direction."""
    db = tmp_path / "real.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    engine.dispose()
    assert db.stat().st_size > 0

    proc = _run_cli(["db-integrity-check"], f"sqlite:///{db}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout
    assert "  ok" in proc.stdout


def test_integrity_check_opens_the_database_read_only(tmp_path, monkeypatch):
    """R2 — the stat guard alone is a check that can be raced or bypassed;
    `mode=ro` makes creation and mutation PHYSICALLY impossible rather than
    merely guarded against. An integrity check has no business being able to
    write to a 4.55 GB production database.

    Reverting to `get_engine()` makes the captured URL lack `mode=ro` and
    fails."""
    import asyncio

    import sqlalchemy

    from app import cli as cli_mod
    from app.config import get_settings

    db = tmp_path / "ro.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    engine.dispose()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    get_settings.cache_clear()

    captured: list[str] = []
    real_create_engine = sqlalchemy.create_engine

    def spy(url, *a, **kw):
        captured.append(str(url))
        return real_create_engine(url, *a, **kw)

    monkeypatch.setattr(sqlalchemy, "create_engine", spy)
    try:
        rc = asyncio.run(cli_mod.db_integrity_check())
    finally:
        get_settings.cache_clear()

    assert rc == 0
    assert captured, "db_integrity_check did not build its own engine"
    assert any("mode=ro" in u and "uri=true" in u for u in captured), (
        f"integrity check did not open the database read-only: {captured}"
    )


# --- R4: the documented exit-code contract for runbook step 8 ----------------

def test_dry_run_inherits_backlog_expiring_and_exits_non_zero(capsys, monkeypatch):
    """R4 — runbook step 8 prescribes
    `crypto-tape-reconcile --dry-run --force --hours 48
    --max-duration-seconds 30` as a mandatory post-migration verification.
    On EVO's real data it returns `status=backlog_expiring` and exit 1,
    because `app/cli.py` returns -1 for any status other than ok/dry_run —
    so an operator reads a successful step as a failed deploy, and under
    `set -e` the deploy halts.

    The fix chosen was the DOC fix (the brief's stated preference), which
    means this behaviour is now a documented contract rather than a
    surprise. This test pins that contract: if someone later makes
    `--dry-run` swallow `backlog_expiring`, the runbook's stated expected
    exit code becomes wrong and this fails, forcing the doc to be updated
    with the code."""
    import asyncio

    from app import cli as cli_mod
    from app.config import get_settings

    monkeypatch.setenv("ENABLE_CRYPTO_TAPE_RECONCILER", "true")
    monkeypatch.setenv("CRYPTO_RETENTION_DAYS", "2")
    get_settings.cache_clear()

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as s:
            # retention_days=2 with a ~60h-old recoverable token puts the
            # RECOVERABLE frontier past the `2*24 - 6 = 42h` threshold.
            born = _mint(s, "tok-expiring", born_hours_ago=60, liquidity=10_000.0)
            _tick_at(s, "tok-expiring", born + timedelta(hours=24), liquidity=9_000.0)
            s.commit()
            rc = asyncio.run(cli_mod.crypto_tape_reconcile(
                dry_run=True, force=True, hours=48, limit=100, session=s,
            ))
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "status=backlog_expiring" in out, out
    assert rc != 0, (
        "a dry run with an expiring RECOVERABLE frontier must keep its "
        "non-zero exit — the runbook documents that exact expectation"
    )
