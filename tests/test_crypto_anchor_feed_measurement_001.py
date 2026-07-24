"""CRYPTO-HORIZON-ANCHOR-FEED-MEASUREMENT-001 tests.

Exact-cycle provider-free anchor materialization: the `record_discovery_run`
service contract (exact canonical ids, exact originating run, input order,
no fallback/substitution, cap fail-closed, idempotent dedup, one bounded
transaction), the isolated default-off MarketOps hook (runs after crypto
persistence and before readiness, once per cycle, no second scan, failure
isolated + recorded, same-cycle readiness visibility), the CLI exact-run
mode, provider-call impossibility (structural + runtime), lock-contention
behavior on a disposable file DB, safety audits, and the overhead benchmark.
In-memory/disposable SQLite only; no network anywhere.
"""

import ast
import asyncio
import glob
import re
import socket
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import cli
from app.config import Settings
from app.models import (
    Base,
    CryptoHorizonCohort,
    CryptoHorizonObservation,
    CryptoPair,
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenBirthEvent,
    CryptoTokenDiscoveryEvent,
    CryptoTokenLifecycleRun,
    CryptoWatcherRun,
)
from app.services.crypto_tape import (
    MAX_ANCHOR_FEED_TOKENS_PER_CYCLE,
    CryptoLifecycleTapeRecorder,
    new_token_ids_for_run,
)
from app.services.marketops import MarketOpsConfig, MarketOpsAutopilotService
from tests.test_marketops import (
    FakeCalibrationService,
    FakeCCService,
    FakeCryptoService,
    FakeOutcomeService,
)
from app.services.marketops import MarketOpsAlertService, SignalPromotionService

REPO = Path(__file__).resolve().parents[1]


# --- fixtures -----------------------------------------------------------------


@pytest.fixture()
def shared_db():
    """One in-memory DB shared across sessions (StaticPool) so the hook's
    isolated session sees the same database as the cycle session."""
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session, factory
    session.close()
    engine.dispose()


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def seed_cycle(
    session,
    *,
    n_tokens=2,
    start=None,
    duration_s=30,
    with_tick=True,
    liquidity=15000.0,
    prefix="T",
):
    """One synthetic crypto discovery run + the raw rows it 'persisted'."""
    start = _naive_utc(start or datetime.now(timezone.utc) - timedelta(seconds=90))
    finish = start + timedelta(seconds=duration_s)
    run = CryptoWatcherRun(status="ok", started_at=start, finished_at=finish)
    session.add(run)
    session.flush()
    ids = []
    for i in range(n_tokens):
        # base58-safe canonical ids (no 0/O/I/l) — the readiness evaluator
        # validates canonical token ids and rejects non-base58 characters
        addr = (prefix + "abcdefgh"[i % 8] + "x" * 44)[:44]
        seen = start + timedelta(seconds=2 + i)
        session.add(CryptoToken(
            chain="solana", token_address=addr, symbol=f"{prefix}{i}",
            first_seen_at=seen, last_seen_at=seen,
        ))
        session.add(CryptoPair(
            chain="solana", pair_address=f"PAIR{prefix}{i}" + "p" * 30,
            base_token_address=addr, quote_token_address="So1" + "1" * 40,
            dex_id="pumpswap", pair_created_at=seen - timedelta(minutes=8),
            first_seen_at=seen, last_seen_at=seen,
        ))
        if with_tick:
            session.add(CryptoPriceTick(
                chain="solana", token_address=addr,
                pair_address=f"PAIR{prefix}{i}" + "p" * 30,
                observed_at=seen, price_usd=1e-4 + i * 1e-5,
                liquidity_usd=liquidity, volume_24h_usd=1000.0,
            ))
        session.add(CryptoTokenDiscoveryEvent(
            chain="solana", token_address=addr, source="dexscreener",
            event_type="profile", observed_at=seen,
        ))
        ids.append(addr)
    session.commit()
    return run, ids


def autopilot(factory, cfg=None, crypto_service=None, **overrides):
    defaults = dict(
        config=cfg or MarketOpsConfig(
            promote_limit=0, include_probability_markets=False,
        ),
        promotion_service=SignalPromotionService(),
        crypto_service=crypto_service or FakeCryptoService(),
        outcome_service=FakeOutcomeService(),
        calibration_service=FakeCalibrationService(),
        champion_challenger_service=FakeCCService(),
        alert_service=MarketOpsAlertService(),
        anchor_feed_session_factory=factory,
    )
    defaults.update(overrides)
    return MarketOpsAutopilotService(**defaults)


def feed_cfg(**overrides) -> MarketOpsConfig:
    base = dict(
        promote_limit=0, include_probability_markets=False,
        include_crypto=True, include_crypto_tape_anchor_feed=True,
    )
    base.update(overrides)
    return MarketOpsConfig(**base)


def crypto_for(run) -> FakeCryptoService:
    svc = FakeCryptoService()
    svc._next_id = run.id - 1  # scan_once returns SimpleNamespace(id=run.id)
    return svc


# --- 1, 30: flag off is a complete no-op, defaults are false ------------------


def test_flag_defaults_false():
    assert Settings(_env_file=None).marketops_include_crypto_tape_anchor_feed is False
    assert MarketOpsConfig().include_crypto_tape_anchor_feed is False


@pytest.mark.asyncio
async def test_flag_off_is_complete_noop(shared_db):
    session, factory = shared_db
    run_row, ids = seed_cycle(session)
    result = await autopilot(factory, crypto_service=crypto_for(run_row)).run_once(session)
    import json
    summary = result.summary if isinstance(result.summary, dict) else json.loads(result.summary)
    assert "anchor_feed" not in summary
    assert session.execute(select(CryptoTokenBirthEvent)).scalars().all() == []
    assert result.status == "ok"


# --- 2-5, 16, 33: hook order, once per cycle, same-cycle visibility -----------


@pytest.mark.asyncio
async def test_hook_creates_anchors_and_readiness_sees_them(shared_db, monkeypatch):
    session, factory = shared_db
    # fresh pair whose shared 15m window is OPEN now: evidence ~8 min ago
    start = datetime.now(timezone.utc) - timedelta(minutes=8, seconds=10)
    run_row, ids = seed_cycle(session, n_tokens=2, start=start)

    import app.services.crypto_horizon_readiness as rmod
    captured = []
    monkeypatch.setattr(rmod, "append_readiness_record", lambda rec: captured.append(rec))

    calls = []
    original = CryptoLifecycleTapeRecorder.record_discovery_run

    def counting(self, s, rid, tids, *, dry_run=False):
        calls.append(rid)
        return original(self, s, rid, tids, dry_run=dry_run)

    monkeypatch.setattr(CryptoLifecycleTapeRecorder, "record_discovery_run", counting)

    cfg = feed_cfg(include_candidate_readiness=True)
    crypto = crypto_for(run_row)
    result = await autopilot(factory, cfg=cfg, crypto_service=crypto).run_once(session)
    summary = result.summary

    # hook ran exactly once, after crypto persistence (5)
    assert calls == [run_row.id]
    feed = summary["anchor_feed"]
    assert feed["status"] == "ok"
    assert feed["source_crypto_run_id"] == run_row.id
    assert feed["anchors_created"] == 2
    assert feed["complete_anchors"] == 2
    assert feed["external_calls"] == 0

    # anchors persisted (16) and visible to readiness IN THE SAME CYCLE (4, 33)
    births = session.execute(select(CryptoTokenBirthEvent)).scalars().all()
    assert sorted(b.token_address for b in births) == sorted(ids)
    readiness = summary["candidate_readiness"]
    assert readiness["complete_candidates"] == 2
    assert readiness["external_calls"] == 0
    # 35: fresh same-instant pair inside its open shared window -> due-now
    assert readiness["state"] == "shared_due_now_ready"
    assert sorted(readiness["candidate_pair"]) == sorted(ids)
    assert len(captured) == 1  # 32: still exactly one readiness record

    # 6: exactly one scan
    assert len(crypto.calls) == 1
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_pair_ready_for_manual_preparation_regression(shared_db, monkeypatch):
    session, factory = shared_db
    # evidence ~5.5 min ago: window opens in ~2 min (< 180 s operator margin)
    start = datetime.now(timezone.utc) - timedelta(minutes=5, seconds=30)
    run_row, ids = seed_cycle(session, n_tokens=2, start=start, prefix="R")
    import app.services.crypto_horizon_readiness as rmod
    monkeypatch.setattr(rmod, "append_readiness_record", lambda rec: None)
    cfg = feed_cfg(include_candidate_readiness=True)
    result = await autopilot(
        factory, cfg=cfg, crypto_service=crypto_for(run_row)
    ).run_once(session)
    assert result.summary["anchor_feed"]["status"] == "ok"
    assert result.summary["candidate_readiness"]["state"] == (
        "pair_ready_for_manual_preparation"
    )


# --- 7-10: exact membership, canonical ids, order, no fallback ----------------


def test_exact_membership_no_freshest_fallback(shared_db):
    session, factory = shared_db
    # an OLD complete token outside the cycle must never be selected
    old_run, old_ids = seed_cycle(
        session, n_tokens=1,
        start=datetime.now(timezone.utc) - timedelta(hours=3), prefix="O",
    )
    run_row, ids = seed_cycle(session, n_tokens=2, prefix="N")
    token_ids = new_token_ids_for_run(session, run_row.id)
    assert token_ids == ids  # persistence order preserved (9)
    assert old_ids[0] not in token_ids
    r = CryptoLifecycleTapeRecorder().record_discovery_run(
        session, run_row.id, token_ids, dry_run=False
    )
    assert r["status"] == "ok" and r["anchors_created"] == 2
    births = session.execute(select(CryptoTokenBirthEvent)).scalars().all()
    assert sorted(b.token_address for b in births) == sorted(ids)
    # input order preserved in creation order (ascending pk follows input) (9)
    ordered = sorted(births, key=lambda b: b.id)
    assert [b.token_address for b in ordered] == ids


def test_membership_mismatch_fails_closed_before_any_write(shared_db):
    session, factory = shared_db
    run_row, ids = seed_cycle(
        session, n_tokens=2, prefix="M",
        start=datetime.now(timezone.utc) - timedelta(minutes=30),
    )
    other_run, other_ids = seed_cycle(session, n_tokens=1, prefix="Q")
    r = CryptoLifecycleTapeRecorder().record_discovery_run(
        session, run_row.id, ids + other_ids, dry_run=False
    )
    assert r["status"] == "membership_mismatch"
    # fail-closed: NOTHING persisted, not even a partial membership (no writes)
    assert session.execute(select(CryptoTokenBirthEvent)).scalars().all() == []
    assert session.execute(select(CryptoTokenLifecycleRun)).scalars().all() == []


def test_malformed_and_unknown_tokens_rejected(shared_db):
    session, factory = shared_db
    run_row, ids = seed_cycle(session, n_tokens=1, prefix="V")
    for bad in ("", "   ", "x" * 65):
        r = CryptoLifecycleTapeRecorder().record_discovery_run(
            session, run_row.id, [bad], dry_run=False
        )
        assert r["status"] == "invalid_token"
    r = CryptoLifecycleTapeRecorder().record_discovery_run(
        session, run_row.id, ["Unknown" + "u" * 30], dry_run=False
    )
    assert r["status"] == "invalid_token"
    assert session.execute(select(CryptoTokenBirthEvent)).scalars().all() == []


# --- 11-13: unknown run, empty run, over-cap ----------------------------------


def test_unknown_run_rejected(shared_db):
    session, factory = shared_db
    r = CryptoLifecycleTapeRecorder().record_discovery_run(
        session, 999999, ["a" * 44], dry_run=False
    )
    assert r["status"] == "unknown_run" and r["anchors_created"] == 0
    assert session.execute(select(CryptoTokenLifecycleRun)).scalars().all() == []


def test_empty_run_is_noop(shared_db):
    session, factory = shared_db
    run_row, _ = seed_cycle(session, n_tokens=0)
    ids = new_token_ids_for_run(session, run_row.id)
    assert ids == []
    r = CryptoLifecycleTapeRecorder().record_discovery_run(
        session, run_row.id, ids, dry_run=False
    )
    assert r["status"] == "no_new_tokens"
    assert session.execute(select(CryptoTokenLifecycleRun)).scalars().all() == []


def test_over_cap_skipped_atomically(shared_db):
    session, factory = shared_db
    run_row, ids = seed_cycle(session, n_tokens=2, prefix="C")
    fake_ids = ids + [f"F{i:02d}" + "f" * 40 for i in range(MAX_ANCHOR_FEED_TOKENS_PER_CYCLE)]
    r = CryptoLifecycleTapeRecorder().record_discovery_run(
        session, run_row.id, fake_ids, dry_run=False
    )
    assert r["status"] == "skipped_cap"
    assert r["skipped_cap"] == len(fake_ids)
    assert r["anchors_created"] == 0
    assert "no anchors created" in r["error"]
    assert session.execute(select(CryptoTokenBirthEvent)).scalars().all() == []
    assert session.execute(select(CryptoTokenLifecycleRun)).scalars().all() == []


# --- 14-15, 28: dedup, mixed, idempotent replay -------------------------------


def test_idempotent_replay_and_mixed_existing(shared_db):
    session, factory = shared_db
    run_row, ids = seed_cycle(session, n_tokens=3, prefix="D")
    rec = CryptoLifecycleTapeRecorder()
    first = rec.record_discovery_run(session, run_row.id, ids, dry_run=False)
    assert first["anchors_created"] == 3 and first["anchors_existing"] == 0
    replay = rec.record_discovery_run(session, run_row.id, ids, dry_run=False)
    assert replay["status"] == "ok"
    assert replay["anchors_created"] == 0
    assert replay["anchors_existing"] == 3
    births = session.execute(select(CryptoTokenBirthEvent)).scalars().all()
    assert len(births) == 3  # no duplicates ever

    # mixed: a new cycle where one token already has an anchor is impossible by
    # membership; mixed within one run = partial pre-existing anchors
    run2, ids2 = seed_cycle(session, n_tokens=2, prefix="E")
    pre = rec.record_discovery_run(session, run2.id, ids2[:1], dry_run=False)
    assert pre["anchors_created"] == 1
    mixed = rec.record_discovery_run(session, run2.id, ids2, dry_run=False)
    assert mixed["anchors_created"] == 1 and mixed["anchors_existing"] == 1


# --- 17: incomplete anchor with honest reason ---------------------------------


def test_incomplete_anchor_recorded_honestly(shared_db):
    session, factory = shared_db
    run_row, ids = seed_cycle(session, n_tokens=2, with_tick=False, prefix="I")
    r = CryptoLifecycleTapeRecorder().record_discovery_run(
        session, run_row.id, ids, dry_run=False
    )
    assert r["status"] == "ok"
    assert r["anchors_created"] == 2
    assert r["complete_anchors"] == 0
    assert r["incomplete_anchors"] == 2  # no tick -> no initial market state
    births = session.execute(select(CryptoTokenBirthEvent)).scalars().all()
    assert all(b.initial_liquidity_usd is None for b in births)
    assert all("initial_market_state" in (b.missing_info or []) for b in births)


# --- 18-20, 25: provider impossibility (structural + runtime) -----------------


def test_no_transitive_adapter_import_at_runtime():
    """The anchor-feed path must not load provider adapters, httpx, or even
    crypto_horizon (whose module body imports the DexScreener adapter) as a
    side effect. Proven in a clean subprocess interpreter."""
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "from sqlalchemy import create_engine\n"
        "from sqlalchemy.orm import sessionmaker\n"
        "from app.models import Base\n"
        "from app.services.crypto_tape import CryptoLifecycleTapeRecorder\n"
        "e = create_engine('sqlite://'); Base.metadata.create_all(e)\n"
        "s = sessionmaker(bind=e)()\n"
        "r = CryptoLifecycleTapeRecorder().record_discovery_run(s, 1, ['x'*44], dry_run=True)\n"
        "assert r['status'] == 'unknown_run'\n"
        "banned = [m for m in sys.modules if m.startswith('app.adapters')\n"
        "          or m == 'httpx' or 'crypto_horizon' in m]\n"
        "assert not banned, f'network-capable modules loaded: {banned}'\n"
        "print('CLEAN')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True,
        cwd=REPO, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "CLEAN" in result.stdout


def test_structural_no_provider_reach():
    tape_src = (REPO / "app/services/crypto_tape.py").read_text()
    tree = ast.parse(tape_src)
    banned = ("httpx", "requests", "aiohttp", "adapters", "provider_registry",
              "crypto_provider_policy", "dexscreener", "solana_tracker",
              "birdeye", "goplus", "crypto_horizon")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            module = getattr(node, "module", "") or ""
            for target in [module, *names]:
                assert not any(b in (target or "").lower() for b in banned), (
                    f"crypto_tape must stay provider-free: {target}"
                )
    # the marketops hook body must import only db + tape modules
    mo_src = (REPO / "app/services/marketops.py").read_text()
    hook = mo_src.split("def _materialize_cycle_anchors")[1].split("def _evaluate")[0]
    for b in ("httpx", "adapter", "dexscreener", "solana", "birdeye", "goplus",
              "provider_policy", "fetch", "await "):
        assert b not in hook.lower(), f"anchor-feed hook must not contain {b!r}"


@pytest.mark.asyncio
async def test_runtime_no_network_possible(shared_db, monkeypatch):
    session, factory = shared_db
    run_row, ids = seed_cycle(session, n_tokens=2, prefix="W")

    def explode(*a, **k):  # any socket use fails the test loudly
        raise AssertionError("network attempted during anchor feed")

    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    result = await autopilot(
        factory, cfg=feed_cfg(), crypto_service=crypto_for(run_row)
    ).run_once(session)
    assert result.summary["anchor_feed"]["status"] == "ok"
    assert result.summary["anchor_feed"]["external_calls"] == 0


# --- 21-23: no cohort, no observation, no unit --------------------------------


@pytest.mark.asyncio
async def test_no_cohort_observation_or_unit(shared_db, monkeypatch):
    session, factory = shared_db
    run_row, ids = seed_cycle(session, n_tokens=2, prefix="U")
    import subprocess
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("systemd touched")))
    await autopilot(factory, cfg=feed_cfg(), crypto_service=crypto_for(run_row)).run_once(session)
    assert session.execute(select(CryptoHorizonCohort)).scalars().all() == []
    assert session.execute(select(CryptoHorizonObservation)).scalars().all() == []


# --- 24: one bounded transaction ----------------------------------------------


def test_one_bounded_transaction(shared_db):
    session, factory = shared_db
    run_row, ids = seed_cycle(session, n_tokens=3, prefix="B")
    feed_session = factory()
    commits = []

    @event.listens_for(feed_session, "after_commit")
    def count(sess):
        commits.append(1)

    r = CryptoLifecycleTapeRecorder().record_discovery_run(
        feed_session, run_row.id, ids, dry_run=False
    )
    assert r["status"] == "ok" and r["anchors_created"] == 3
    assert len(commits) == 1  # exactly one bounded transaction
    feed_session.close()


# --- 26-27: hook failure isolated and recorded --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_fast", (False, True))
async def test_hook_failure_never_fails_marketops(shared_db, monkeypatch, fail_fast):
    session, factory = shared_db
    run_row, ids = seed_cycle(session, n_tokens=1, prefix="X")

    def boom(self, *a, **k):
        raise RuntimeError("synthetic anchor-feed failure")

    monkeypatch.setattr(CryptoLifecycleTapeRecorder, "record_discovery_run", boom)
    result = await autopilot(
        factory, cfg=feed_cfg(fail_fast=fail_fast),
        crypto_service=crypto_for(run_row),
    ).run_once(session)
    assert result.status == "ok"  # cycle unaffected (26)
    feed = result.summary["anchor_feed"]
    assert feed["status"] == "error"
    assert "RuntimeError" in feed["error"]  # recorded (27)
    assert feed["external_calls"] == 0


# --- 29: no migration ---------------------------------------------------------


def test_no_migration_added():
    versions = sorted(
        p.name for p in (REPO / "alembic" / "versions").glob("[0-9]*.py"))
    assert versions and versions[-1].startswith("0027")


@pytest.mark.asyncio
async def test_spike_alert_cycle_does_not_self_lock(tmp_path):
    """Regression (review finding M1): a crypto spike alert flushed on the
    shared session before the hook must not hold the write lock against the
    isolated feed session. The flag-gated checkpoint commit before the hook
    resolves it; on a REAL file DB (separate connections) the hook must
    succeed on a spike cycle."""
    db = tmp_path / "spike.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"timeout": 2})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    run_row, ids = seed_cycle(session, n_tokens=2, prefix="S")
    crypto = crypto_for(run_row)
    crypto.signals = 30  # >= CRYPTO_SIGNAL_SPIKE_PER_CYCLE -> alert flush
    result = await autopilot(
        factory, cfg=feed_cfg(), crypto_service=crypto
    ).run_once(session)
    assert result.status == "ok"
    feed = result.summary["anchor_feed"]
    assert feed["status"] == "ok", feed
    assert feed["anchors_created"] == 2
    session.close()
    engine.dispose()


# --- 39: disposable-DB lock contention ----------------------------------------


def test_lock_contention_isolated_and_bounded(tmp_path, monkeypatch):
    """Disposable file DB: a second connection holds the write lock; the hook
    exhausts the canonical tape ladder (bounded, no infinite retry), records
    an isolated error, and never raises into the caller."""
    db = tmp_path / "contention.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"timeout": 0.1})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    run_row, ids = seed_cycle(session, n_tokens=1, prefix="L")
    run_id = run_row.id
    session.close()

    import app.services.crypto_tape as tape_mod
    monkeypatch.setattr(tape_mod, "DB_LOCKED_RETRY_SECONDS", 0.02)

    holder = sqlite3.connect(str(db), timeout=0.1)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE crypto_tokens SET last_seen_at = last_seen_at")
    summary: dict = {}
    try:
        svc = autopilot(factory, cfg=feed_cfg())
        svc._materialize_cycle_anchors(SimpleNamespace(id=run_id), summary)
    finally:
        holder.rollback()
        holder.close()
    feed = summary["anchor_feed"]
    assert feed["status"] == "error"  # isolated, never raised
    assert "locked" in feed["error"].lower() or "OperationalError" in feed["error"]
    engine.dispose()


# --- CLI exact-run mode -------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_exact_run_preview_and_confirm(shared_db, capsys):
    session, factory = shared_db
    run_row, ids = seed_cycle(session, n_tokens=2, prefix="K")
    # preview: no --confirm -> persists nothing
    n = await cli.crypto_tape_run_once(
        session=session, source_crypto_run_id=run_row.id
    )
    assert n == 2
    out = capsys.readouterr().out
    assert "status=dry_run" in out and "external_calls=0" in out
    assert "persists only with" in out or "not persisted" in out
    assert session.execute(select(CryptoTokenBirthEvent)).scalars().all() == []
    # confirm: persists
    n = await cli.crypto_tape_run_once(
        session=session, source_crypto_run_id=run_row.id, confirm=True
    )
    assert n == 2
    assert len(session.execute(select(CryptoTokenBirthEvent)).scalars().all()) == 2
    # rejects --limit/--hours in exact mode
    n = await cli.crypto_tape_run_once(
        session=session, source_crypto_run_id=run_row.id, limit=5
    )
    assert n == -1
    # unknown run rejected
    n = await cli.crypto_tape_run_once(session=session, source_crypto_run_id=987654)
    assert n == -1
    # no-new-token run rejected explicitly (non-overlapping window)
    empty_run, _ = seed_cycle(
        session, n_tokens=0,
        start=datetime.now(timezone.utc) - timedelta(seconds=8), duration_s=5,
    )
    n = await cli.crypto_tape_run_once(
        session=session, source_crypto_run_id=empty_run.id
    )
    assert n == -1


def test_cli_classic_mode_unchanged(shared_db, capsys):
    session, factory = shared_db
    run_row, ids = seed_cycle(session, n_tokens=2, prefix="Z")
    n = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        cli.crypto_tape_run_once(session=session, hours=1, limit=5, dry_run=True)
    )
    assert n >= 0
    out = capsys.readouterr().out
    assert "status=dry_run" in out and "external_calls=0" in out


# --- 36-37: safety audits -----------------------------------------------------


SAFETY_GREP = re.compile(
    r"expected_value|kelly|position_siz|paper_trad|place_order|submit_order|"
    r"create_order|wallet|recommended_side|trade_recommend|execute_trade",
    re.IGNORECASE,
)


def test_safety_grep_no_new_hits():
    # boundary-statement docstrings are the only acceptable hits (AGENTS.md);
    # pin the per-file counts so this milestone adds none.
    tape_hits = [l for l in (REPO / "app/services/crypto_tape.py").read_text().splitlines()
                 if SAFETY_GREP.search(l)]
    assert len(tape_hits) == 3, tape_hits  # pre-existing boundary docstrings only
    mo_hits = [l for l in (REPO / "app/services/marketops.py").read_text().splitlines()
               if SAFETY_GREP.search(l)]
    # exactly the one pre-existing module-docstring boundary statement
    assert len(mo_hits) == 1 and "no order placement" in mo_hits[0]
    test_own = (REPO / "tests/test_crypto_anchor_feed_measurement_001.py").read_text()
    # this test file's own pattern literal is the only hit here
    assert len([l for l in test_own.splitlines() if SAFETY_GREP.search(l)]) <= 4


def test_summary_carries_no_token_ids():
    """The bounded MarketOps summary must hold counts only."""
    src = (REPO / "app/services/marketops.py").read_text()
    hook = src.split("def _materialize_cycle_anchors")[1].split("def _evaluate")[0]
    assert "token_ids" not in hook.split('summary["anchor_feed"] = {')[1].split("}")[0]
    for field in ("tokens_received", "anchors_created", "complete_anchors",
                  "skipped_cap", "external_calls", "duration_ms"):
        assert field in hook


# --- 40: overhead benchmark ---------------------------------------------------


def test_exact_cycle_overhead_bounded(shared_db):
    session, factory = shared_db
    run_row, ids = seed_cycle(session, n_tokens=5, prefix="P")
    rec = CryptoLifecycleTapeRecorder()
    t0 = time.perf_counter()
    r = rec.record_discovery_run(session, run_row.id, ids, dry_run=False)
    elapsed = time.perf_counter() - t0
    assert r["status"] == "ok"
    assert elapsed < 1.0, f"exact-cycle pass took {elapsed:.2f}s for 5 tokens"
    assert r["duration_ms"] < 1000
