"""CRYPTO-COVERAGE-REPAIR-001 — scheduled provider-free reconciliation.

The defect this milestone repairs is NOT that reconciliation is wrong; it is
that reconciliation is never SELECTED to run on matured tokens:

  * the only tape path production runs (`record_discovery_run`) validates that
    every token was first persisted by the originating discovery run, so it
    sees each token exactly once, at age ~0, when no horizon is due;
  * the windowed reconciler that WOULD revisit matured tokens (`run_once`) is
    CLI-only and no timer schedules it;
  * the ticks needed to mature a horizon are pruned after
    crypto_retention_days, so unreconciled evidence expires permanently.

These tests pin the repair AND the two distinct coverage notions the defect
proved must be tracked separately: a token can be OBSERVATION-covered (a real
tick landed inside the canonical window) and still RECONCILIATION-uncovered
(no label, because it was never selected).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base

from app.config import Settings
from app.models import (
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenSurvivalOutcome,
)
from app.services.crypto_tape import (
    HORIZON_TOLERANCE,
    HORIZONS,
    SURVIVAL_LIQUIDITY_FRACTION,
    CryptoLifecycleTapeRecorder,
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


def _mint(session, address: str, *, born_hours_ago: float, liquidity: float = 10_000.0):
    """A token born N hours ago with a birth-time tick establishing liquidity."""
    now = datetime.now(timezone.utc)
    first_seen = now - timedelta(hours=born_hours_ago)
    session.add(CryptoToken(
        chain=CHAIN, token_address=address, symbol=address[:6],
        first_seen_at=first_seen, last_seen_at=now,
    ))
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


# --- the gate ---------------------------------------------------------------

def test_disabled_flag_is_a_complete_no_op(session):
    """Dark deploy must be inert: off = no work, no writes, no provider calls."""
    _mint(session, "tok-off", born_hours_ago=30)
    r = run_scheduled_reconciliation(
        session, settings=_settings(enable_crypto_tape_reconciler=False)
    )
    assert r["status"] == "disabled"
    assert r["external_calls"] == 0
    assert r["tokens_considered"] == 0
    assert r["flag"] == "enable_crypto_tape_reconciler"
    assert _outcome(session, "tok-off") is None


def test_force_runs_one_pass_without_enabling_scheduling(session):
    _mint(session, "tok-forced", born_hours_ago=30)
    r = run_scheduled_reconciliation(
        session, settings=_settings(enable_crypto_tape_reconciler=False), force=True
    )
    assert r["status"] == "ok"
    assert r["external_calls"] == 0
    assert r["tokens_considered"] >= 1


def test_window_shorter_than_the_closing_edge_is_refused(session):
    """A 24h horizon closes at 36h. A window under that silently drops matured
    outcomes, which is the exact class of bug this milestone exists to fix, so
    it must fail loudly rather than quietly under-reconcile."""
    r = run_scheduled_reconciliation(
        session, settings=_settings(crypto_tape_reconciler_window_hours=24)
    )
    assert r["status"] == "invalid_window"
    assert r["external_calls"] == 0
    assert "closing edge" in r["error"]


def test_default_window_outlasts_the_longest_horizon_closing_edge():
    s = Settings()
    closing_edge_h = max(m for _, m in HORIZONS) * (1 + HORIZON_TOLERANCE) / 60
    assert s.crypto_tape_reconciler_window_hours >= closing_edge_h
    # and stays well inside the evidence-retention horizon
    assert s.crypto_tape_reconciler_window_hours < s.crypto_retention_days * 24


def test_shipped_default_is_off():
    assert Settings().enable_crypto_tape_reconciler is False


# --- the actual repair: matured tokens are selected -------------------------

def test_matured_birth_is_selected_and_its_label_populates(session):
    """The headline case. A token born 30h ago with a real tick inside the 24h
    canonical window has ALL the evidence needed; today it is never selected,
    so survived_24h stays null forever."""
    born = _mint(session, "tok-24h", born_hours_ago=30, liquidity=10_000.0)
    _tick_at(session, "tok-24h", born + timedelta(hours=24), liquidity=9_000.0)

    r = run_scheduled_reconciliation(session, settings=_settings())
    assert r["status"] == "ok"
    assert r["external_calls"] == 0

    o = _outcome(session, "tok-24h")
    assert o is not None
    assert o.survived_24h is True  # 9000 >= 0.3 * 10000


def test_survival_uses_the_liquidity_fraction_not_a_guess(session):
    born = _mint(session, "tok-dead", born_hours_ago=30, liquidity=10_000.0)
    below = SURVIVAL_LIQUIDITY_FRACTION * 10_000.0 - 1
    _tick_at(session, "tok-dead", born + timedelta(hours=24), liquidity=below)

    run_scheduled_reconciliation(session, settings=_settings())
    assert _outcome(session, "tok-dead").survived_24h is False


def test_no_recency_starvation_between_new_and_matured_tokens(session):
    """The production selection path is recency-anchored, so a burst of new
    births can push every matured token out of the set. Within the window, a
    matured token must still be reconciled when newer tokens exist."""
    born = _mint(session, "tok-old", born_hours_ago=40, liquidity=10_000.0)
    _tick_at(session, "tok-old", born + timedelta(hours=24), liquidity=9_000.0)
    for i in range(20):
        _mint(session, f"tok-new-{i}", born_hours_ago=0.1)

    run_scheduled_reconciliation(session, settings=_settings())
    o = _outcome(session, "tok-old")
    assert o is not None and o.survived_24h is True


def test_limit_smaller_than_the_universe_reports_what_it_dropped(session):
    """Bounded DB work is required, but a silent cap is how coverage gaps hide.
    The pass must not claim to have considered more than it did."""
    for i in range(10):
        _mint(session, f"tok-cap-{i}", born_hours_ago=30)
    r = run_scheduled_reconciliation(session, settings=_settings(), limit=3)
    assert r["tokens_considered"] == 3
    assert r["selection_limit"] == 3


# --- observation coverage vs reconciliation coverage ------------------------

def test_observation_covered_but_reconciliation_uncovered_is_representable(session):
    """The defect's signature case: real in-window evidence, no label. Before
    the pass runs the token is observation-covered and reconciliation-
    uncovered; after it runs, both are covered. If these ever collapse into one
    metric the regression becomes invisible again."""
    born = _mint(session, "tok-split", born_hours_ago=30, liquidity=10_000.0)
    target = born + timedelta(hours=24)
    _tick_at(session, "tok-split", target, liquidity=9_000.0)

    # observation coverage is a property of the evidence, independent of labels
    tol = timedelta(minutes=1440 * HORIZON_TOLERANCE)
    ticks = session.query(CryptoPriceTick).filter_by(
        chain=CHAIN, token_address="tok-split"
    ).all()
    in_window = [t for t in ticks if abs(
        t.observed_at.replace(tzinfo=timezone.utc) - target
    ) <= tol and t.observed_at.replace(tzinfo=timezone.utc) > born]
    assert in_window, "evidence must exist for this test to mean anything"

    assert _outcome(session, "tok-split") is None  # reconciliation-uncovered

    run_scheduled_reconciliation(session, settings=_settings())
    assert _outcome(session, "tok-split").survived_24h is True


def test_no_tick_in_window_yields_null_not_a_guess(session):
    """Absent evidence must stay NULL. No interpolation, no nearest-tick
    substitution from outside the canonical window."""
    born = _mint(session, "tok-notick", born_hours_ago=30, liquidity=10_000.0)
    # far outside the +-50% window around the 24h mark
    _tick_at(session, "tok-notick", born + timedelta(hours=1), liquidity=9_000.0)

    run_scheduled_reconciliation(session, settings=_settings())
    o = _outcome(session, "tok-notick")
    assert o is not None
    assert o.survived_24h is None


def test_tick_just_outside_tolerance_is_not_substituted(session):
    born = _mint(session, "tok-edge", born_hours_ago=40, liquidity=10_000.0)
    # 24h horizon, tolerance +-12h -> the window closes at born+36h; +36h01m is
    # outside it and must NOT be substituted for a real in-window observation
    _tick_at(session, "tok-edge", born + timedelta(hours=36, minutes=1), liquidity=9_000.0)

    run_scheduled_reconciliation(session, settings=_settings())
    o = _outcome(session, "tok-edge")
    assert o is not None, "token must be selected; otherwise this proves nothing"
    assert o.survived_24h is None


def test_missing_initial_liquidity_is_insufficient_evidence(session):
    """compute_survival needs BOTH an in-window tick and initial liquidity.
    Having only one must not produce a label."""
    now = datetime.now(timezone.utc)
    born = now - timedelta(hours=30)
    session.add(CryptoToken(
        chain=CHAIN, token_address="tok-noliq", symbol="NOLIQ",
        first_seen_at=born, last_seen_at=now,
    ))
    session.add(CryptoPriceTick(
        chain=CHAIN, token_address="tok-noliq", pair_address="pair-noliq",
        observed_at=born, price_usd=1.0, liquidity_usd=None, volume_24h_usd=1.0,
    ))
    session.flush()
    _tick_at(session, "tok-noliq", born + timedelta(hours=24), liquidity=9_000.0)

    run_scheduled_reconciliation(session, settings=_settings())
    o = _outcome(session, "tok-noliq")
    assert o is None or o.survived_24h is None


def test_immature_horizon_stays_null(session):
    born = _mint(session, "tok-young", born_hours_ago=1, liquidity=10_000.0)
    _tick_at(session, "tok-young", born + timedelta(minutes=15), liquidity=9_000.0)

    run_scheduled_reconciliation(session, settings=_settings())
    o = _outcome(session, "tok-young")
    assert o is not None
    assert o.survived_24h is None
    assert o.survived_6h is None


# --- operational safety -----------------------------------------------------

def test_reconciliation_is_idempotent(session):
    born = _mint(session, "tok-idem", born_hours_ago=30, liquidity=10_000.0)
    _tick_at(session, "tok-idem", born + timedelta(hours=24), liquidity=9_000.0)
    s = _settings()

    first = run_scheduled_reconciliation(session, settings=s)
    o1 = _outcome(session, "tok-idem")
    snap1 = (o1.survived_15m, o1.survived_1h, o1.survived_6h, o1.survived_24h)

    second = run_scheduled_reconciliation(session, settings=s)
    o2 = _outcome(session, "tok-idem")
    snap2 = (o2.survived_15m, o2.survived_1h, o2.survived_6h, o2.survived_24h)

    assert snap1 == snap2
    assert o1.id == o2.id, "must update in place, not duplicate the outcome row"
    assert first["external_calls"] == second["external_calls"] == 0
    n = session.query(CryptoTokenSurvivalOutcome).filter_by(
        chain=CHAIN, token_address="tok-idem"
    ).count()
    assert n == 1


def test_dry_run_persists_nothing(session):
    born = _mint(session, "tok-dry", born_hours_ago=30, liquidity=10_000.0)
    _tick_at(session, "tok-dry", born + timedelta(hours=24), liquidity=9_000.0)

    r = run_scheduled_reconciliation(session, settings=_settings(), dry_run=True)
    assert r["status"] == "dry_run"
    assert r["external_calls"] == 0
    assert _outcome(session, "tok-dry") is None


def test_restart_after_a_pass_converges_to_the_same_labels(session):
    """Restart-safety: a fresh recorder instance on the same rows must produce
    identical labels, because reconciliation is a pure function of persisted
    evidence."""
    born = _mint(session, "tok-restart", born_hours_ago=30, liquidity=10_000.0)
    _tick_at(session, "tok-restart", born + timedelta(hours=24), liquidity=9_000.0)
    s = _settings()

    run_scheduled_reconciliation(session, settings=s)
    before = _outcome(session, "tok-restart").survived_24h

    run_scheduled_reconciliation(
        session, recorder=CryptoLifecycleTapeRecorder(), settings=s
    )
    assert _outcome(session, "tok-restart").survived_24h == before


def test_pass_makes_no_external_call(session, monkeypatch):
    """Provider governance: the reconciler must be provably provider-free, not
    merely believed to be. Any HTTP client construction fails the test."""
    import httpx

    def _boom(*a, **k):  # pragma: no cover - only runs on regression
        raise AssertionError("reconciliation attempted an external call")

    monkeypatch.setattr(httpx, "Client", _boom, raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", _boom, raising=False)

    born = _mint(session, "tok-noext", born_hours_ago=30, liquidity=10_000.0)
    _tick_at(session, "tok-noext", born + timedelta(hours=24), liquidity=9_000.0)

    r = run_scheduled_reconciliation(session, settings=_settings())
    assert r["external_calls"] == 0
    assert r["status"] == "ok"


def test_pass_creates_no_cohort_and_no_horizon_observation(session):
    """CANARY-003/004 are closed. Reconciliation must never create a cohort,
    a member, an observation, or anything armable."""
    from app.models import (
        CryptoHorizonCohort,
        CryptoHorizonCohortMember,
        CryptoHorizonObservation,
    )

    born = _mint(session, "tok-nocohort", born_hours_ago=30, liquidity=10_000.0)
    _tick_at(session, "tok-nocohort", born + timedelta(hours=24), liquidity=9_000.0)

    before = (
        session.query(CryptoHorizonCohort).count(),
        session.query(CryptoHorizonCohortMember).count(),
        session.query(CryptoHorizonObservation).count(),
    )
    run_scheduled_reconciliation(session, settings=_settings())
    after = (
        session.query(CryptoHorizonCohort).count(),
        session.query(CryptoHorizonCohortMember).count(),
        session.query(CryptoHorizonObservation).count(),
    )
    assert before == after == (0, 0, 0)


def test_pass_writes_no_price_ticks(session):
    """Provider-free reconciliation reads ticks; it must never fabricate one."""
    born = _mint(session, "tok-notickwrite", born_hours_ago=30, liquidity=10_000.0)
    _tick_at(session, "tok-notickwrite", born + timedelta(hours=24), liquidity=9_000.0)
    before = session.query(CryptoPriceTick).count()

    run_scheduled_reconciliation(session, settings=_settings())
    assert session.query(CryptoPriceTick).count() == before


# --- the scheduling gap itself ----------------------------------------------

def test_marketops_exact_cycle_path_cannot_mature_a_horizon(session):
    """Regression pin for the root cause. record_discovery_run requires that a
    token was FIRST PERSISTED by the originating run, so by construction it
    only ever sees age-0 tokens, where no horizon is due. This test documents
    why a scheduled windowed pass is necessary and must not be removed as
    'redundant with the anchor feed'."""
    from app.models import CryptoWatcherRun

    now = datetime.now(timezone.utc)
    run = CryptoWatcherRun(started_at=now - timedelta(minutes=1), finished_at=now)
    session.add(run)
    session.flush()

    born = _mint(session, "tok-cycle", born_hours_ago=30, liquidity=10_000.0)
    _tick_at(session, "tok-cycle", born + timedelta(hours=24), liquidity=9_000.0)

    rec = CryptoLifecycleTapeRecorder()
    r = rec.record_discovery_run(session, run.id, ["tok-cycle"])
    # the matured token was NOT first persisted by this run, so it is refused
    assert r["status"] == "membership_mismatch"
    assert r["external_calls"] == 0
    assert _outcome(session, "tok-cycle") is None


@pytest.mark.parametrize("label,minutes", list(HORIZONS))
def test_every_horizon_matures_when_its_evidence_exists(session, label, minutes):
    """All four horizons, not just 24h — the live measurement found every one
    of them at zero, so the repair must be proven for each."""
    addr = f"tok-h-{label}"
    age_h = (minutes * (1 + HORIZON_TOLERANCE)) / 60 + 1
    born = _mint(session, addr, born_hours_ago=age_h, liquidity=10_000.0)
    _tick_at(session, addr, born + timedelta(minutes=minutes), liquidity=9_000.0)

    run_scheduled_reconciliation(
        session, settings=_settings(crypto_tape_reconciler_window_hours=200)
    )
    o = _outcome(session, addr)
    assert o is not None
    assert getattr(o, f"survived_{label}") is True
