"""CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B1/B3/B4 — production
selection, finality, and frontier tests closing the three unanimous
merge-gate findings against `scripts/crypto_backlog_partition.py`'s typed
partition (previously derived offline only, and never implemented in
`app/`):

  B1 — `unreconciled_backlog` must PARTITION the full backlog before
       applying the per-pass limit, so genuinely recoverable work is not
       buried behind permanent write-offs.
  B3 — the reported frontier must measure RECOVERABLE backlog age, not the
       oldest backlog ROW regardless of recoverability, so `backlog_expiring`
       is not permanently on.
  B4 — repeated passes over the same unrecoverable tokens must not
       re-materialise permanent write-off rows or advance any frontier.

In-memory SQLite; no network anywhere.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import (
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenActorObservation,
    CryptoTokenLifecycleSnapshot,
    CryptoTokenSurvivalOutcome,
)
from app.services.crypto_tape import (
    CryptoLifecycleTapeRecorder,
    CryptoTapeConfig,
    run_scheduled_reconciliation,
)

CHAIN = "solana"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _settings(**over) -> Settings:
    base = {
        "enable_crypto_tape_reconciler": True,
        "crypto_tape_reconciler_window_hours": 48,
        "crypto_tape_reconciler_limit": 1000,
    }
    base.update(over)
    return Settings(**base)


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


def _outcome(session, address: str):
    return session.query(CryptoTokenSurvivalOutcome).filter_by(
        chain=CHAIN, token_address=address
    ).one_or_none()


def recorder(retention_days: int = 7) -> CryptoLifecycleTapeRecorder:
    return CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, retention_days=retention_days))


# --- B1: classification + prioritised selection ------------------------------

def test_classify_backlog_separates_recoverable_from_writeoffs(session):
    now = datetime.now(timezone.utc)
    # RETENTION_LOST: anchor + 36h is already older than retention (2d).
    born_lost = _mint(session, "tok-lost", born_hours_ago=200, liquidity=10_000.0)
    # MISSING_REQUIRED_INITIAL_STATE: no tick at all -> no liquidity ever.
    session.add(CryptoToken(
        chain=CHAIN, token_address="tok-no-liq",
        first_seen_at=now - timedelta(hours=60), last_seen_at=now, symbol="x",
    ))
    # RECOVERABLE_NOW: a real 24h-tolerance tick exists.
    born_rec = _mint(session, "tok-recoverable", born_hours_ago=60, liquidity=10_000.0)
    _tick_at(session, "tok-recoverable", born_rec + timedelta(hours=24), liquidity=9_000.0)
    # UNRESOLVABLE_WINDOW_CLOSED: liquidity known, window closed, no tick
    # lands in ANY horizon.
    _mint(session, "tok-no-evidence", born_hours_ago=60, liquidity=10_000.0)
    session.flush()

    rec = recorder(retention_days=2)
    cutoff = now - timedelta(hours=48)
    classes = rec.classify_backlog(session, cutoff, now=now)

    assert classes["RETENTION_LOST"] == ["tok-lost"]
    assert classes["MISSING_REQUIRED_INITIAL_STATE"] == ["tok-no-liq"]
    assert classes["RECOVERABLE_NOW"] == ["tok-recoverable"]
    assert classes["UNRESOLVABLE_WINDOW_CLOSED"] == ["tok-no-evidence"]
    # exact partition: every backlog token in exactly one class
    total = sum(len(v) for v in classes.values())
    assert total == 4


def test_unreconciled_backlog_prioritises_recoverable_over_writeoffs(session):
    """B1 — the measured shape: a handful of RECOVERABLE_NOW tokens buried
    behind a much larger pile of RETENTION_LOST write-offs. A naive
    oldest-first LIMIT over the raw backlog (the pre-fix behaviour) would
    select ONLY write-offs here, because they are far older. The fix must
    select the recoverable tokens first regardless of raw age."""
    now = datetime.now(timezone.utc)
    # 20 RETENTION_LOST write-offs, all OLDER than the 3 recoverable tokens.
    for i in range(20):
        _mint(session, f"tok-writeoff-{i:02d}", born_hours_ago=300 + i, liquidity=10_000.0)
    # 3 RECOVERABLE_NOW tokens, younger, but still outside the window.
    for i in range(3):
        born = _mint(session, f"tok-good-{i:02d}", born_hours_ago=60, liquidity=10_000.0)
        _tick_at(session, f"tok-good-{i:02d}", born + timedelta(hours=24), liquidity=9_000.0)
    session.flush()

    rec = recorder(retention_days=2)
    cutoff = now - timedelta(hours=48)
    # A small room: under the old ordering this would select 5 of the
    # oldest (all write-offs) and never reach the recoverable tokens.
    selected = rec.unreconciled_backlog(session, cutoff, limit=5, now=now)
    selected_addrs = {t.token_address for t in selected}

    assert {"tok-good-00", "tok-good-01", "tok-good-02"} <= selected_addrs


def test_unreconciled_backlog_still_reserves_a_writeoff_slice(session):
    """B1 — write-offs must not be starved forever either: a bounded slice
    of `room` still goes to them so they eventually get memorialised
    (`final=True`) and stop competing for room on future passes."""
    now = datetime.now(timezone.utc)
    for i in range(20):
        _mint(session, f"tok-writeoff-{i:02d}", born_hours_ago=300 + i, liquidity=10_000.0)
    for i in range(3):
        born = _mint(session, f"tok-good-{i:02d}", born_hours_ago=60, liquidity=10_000.0)
        _tick_at(session, f"tok-good-{i:02d}", born + timedelta(hours=24), liquidity=9_000.0)
    session.flush()

    rec = recorder(retention_days=2)
    cutoff = now - timedelta(hours=48)
    selected = rec.unreconciled_backlog(session, cutoff, limit=10, now=now)
    selected_addrs = {t.token_address for t in selected}
    writeoffs_selected = {a for a in selected_addrs if a.startswith("tok-writeoff-")}
    assert writeoffs_selected  # not zero — some budget reserved for them


# --- B2 (re-verified end to end through the scheduled pass) ------------------

def test_scheduled_pass_finalises_a_permanently_missing_evidence_backlog_token(session):
    now = datetime.now(timezone.utc)
    _mint(session, "tok-no-evidence", born_hours_ago=60, liquidity=10_000.0)
    session.flush()

    r = run_scheduled_reconciliation(
        session, settings=_settings(crypto_retention_days=7),
    )
    assert r["status"] in ("ok", "backlog_expiring")
    o = _outcome(session, "tok-no-evidence")
    assert o is not None
    assert o.final is True
    assert o.survived_24h is None
    assert o.details["finality"] == "permanently_missing_evidence"


# --- B3: the recoverable frontier ---------------------------------------------

def test_recoverable_backlog_summary_excludes_writeoffs_from_the_frontier(session):
    now = datetime.now(timezone.utc)
    # Only write-offs in the backlog: the recoverable frontier must be empty
    # (None), NOT the write-offs' own (very old) age.
    for i in range(5):
        _mint(session, f"tok-writeoff-{i:02d}", born_hours_ago=300 + i, liquidity=10_000.0)
    session.flush()

    rec = recorder(retention_days=2)
    cutoff = now - timedelta(hours=48)
    summary = rec.recoverable_backlog_summary(session, cutoff, now=now)
    assert summary["recoverable_backlog_count"] == 0
    assert summary["oldest_recoverable_due_at"] is None
    assert summary["oldest_recoverable_age_seconds"] is None
    assert summary["writeoff_count"]["RETENTION_LOST"] == 5


def test_five_repeated_passes_over_unrecoverable_tokens_do_not_advance_frontier_or_duplicate(
    session,
):
    """B3/B4 — five passes over the SAME permanently-unrecoverable backlog:
    the recoverable frontier must stay empty throughout (no phantom
    advancement), and no duplicate permanent outcome rows/snapshots/actors
    may accumulate once each token is written off."""
    now = datetime.now(timezone.utc)
    for i in range(5):
        _mint(session, f"tok-writeoff-{i:02d}", born_hours_ago=300 + i, liquidity=10_000.0)
    session.flush()

    settings = _settings(crypto_retention_days=2)
    results = []
    for _ in range(5):
        r = run_scheduled_reconciliation(session, settings=settings)
        results.append(r)

    # The recoverable frontier never advances (stays empty/None) across all
    # five passes — there is nothing recoverable to advance.
    for r in results:
        assert r.get("recoverable_backlog_count", 0) == 0
        assert r.get("oldest_recoverable_age_seconds") is None
        assert r["status"] != "backlog_expiring"

    # B4 — every token was finalised (written off) and no later pass
    # re-selected it: outcome rows are NOT duplicated (one per token), and
    # once final, no later pass appends another snapshot/actor row for it
    # (`skip_redundant_when_final` on the scheduled path). Written exactly
    # once each, on the first pass that reached them.
    for i in range(5):
        addr = f"tok-writeoff-{i:02d}"
        outcomes = session.execute(
            select(func.count()).select_from(CryptoTokenSurvivalOutcome).where(
                CryptoTokenSurvivalOutcome.chain == CHAIN,
                CryptoTokenSurvivalOutcome.token_address == addr,
            )
        ).scalar()
        assert outcomes == 1
        o = _outcome(session, addr)
        assert o.final is True
        assert o.details["finality"] == "retention_lost"

    snapshot_count = session.execute(
        select(func.count()).select_from(CryptoTokenLifecycleSnapshot)
    ).scalar()
    actor_count = session.execute(
        select(func.count()).select_from(CryptoTokenActorObservation)
    ).scalar()
    # One snapshot/actor per token (written the pass it was first reached),
    # not one per token PER PASS (5 tokens x 5 passes would be 25).
    assert snapshot_count == 5
    assert actor_count == 5
