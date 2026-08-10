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
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import Base

from app.config import Settings
from app.models import (
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenBirthEvent,
    CryptoTokenSurvivalOutcome,
)
from app.services.crypto_tape import (
    HORIZON_TOLERANCE,
    HORIZONS,
    SHORTEST_HORIZON_CLOSING_EDGE_MINUTES,
    SURVIVAL_LIQUIDITY_FRACTION,
    CryptoLifecycleTapeRecorder,
    CryptoTapeConfig,
    run_scheduled_reconciliation,
)

REPO = Path(__file__).resolve().parents[1]
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
    # the docstring's actual claim: the pass must NAME what it dropped. Before
    # remediation the result dict had no such field and this test passed anyway.
    assert r["universe_size"] == 10
    assert r["tokens_omitted"] == 7
    assert r["truncated"] is True
    assert r["status"] == "truncated"


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
    # not `o is None or ...`: that disjunct passes when the token was never
    # selected, which would prove nothing about evidence sufficiency.
    assert o is not None, "token must be selected for this test to mean anything"
    assert o.survived_24h is None


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


# --- review remediation: silent truncation, gate honesty, bounds ------------

def test_truncation_is_loud_and_drops_the_unmatured_tail(session):
    """The load-bearing regression. Newest-first truncation drops exactly the
    MATURED tokens — the ones whose horizons have closed and whose evidence is
    about to be pruned — while keeping fresh tokens that have no due horizon at
    all. A review proved 0/5 matured tokens reconciled with status=ok and no
    truncation signal anywhere in the result."""
    for i in range(5):
        born = _mint(session, f"tok-matured-{i}", born_hours_ago=40, liquidity=10_000.0)
        _tick_at(session, f"tok-matured-{i}", born + timedelta(hours=24), liquidity=9_000.0)
    # HIGH-1: "fresh" here must still be OLDER than
    # SHORTEST_HORIZON_CLOSING_EDGE_MINUTES (22.5min) or the scheduled path's
    # age-exclusion would drop these from selection entirely (which is
    # correct behaviour, but not what THIS test is pinning — it pins
    # selection-limit truncation ordering, not the age exclusion, which has
    # its own dedicated tests below). 1h clears that bar with margin while
    # staying far short of the 24h+tolerance finality edge.
    for i in range(50):
        _mint(session, f"tok-fresh-{i}", born_hours_ago=1)

    r = run_scheduled_reconciliation(session, settings=_settings(), limit=10)

    assert r["status"] == "truncated", "a capped pass must not report plain ok"
    assert r["universe_size"] == 55
    assert r["tokens_omitted"] == 45
    assert "not reconciled" in r["error"]
    # and the matured tokens are the ones that survived the cap
    matured = [_outcome(session, f"tok-matured-{i}") for i in range(5)]
    assert all(o is not None and o.survived_24h is True for o in matured)


def test_truncated_status_is_persisted_on_the_durable_run_row(session):
    """MEDIUM fix (fourth re-review). `run_once`'s own finalize commits the
    run row's status BEFORE `run_scheduled_reconciliation` (the caller) has
    the universe/backlog/limit context needed to relabel a fully-completed
    pass as "truncated" — so, pre-fix, that relabelling only ever happened
    in the in-memory return value; the durable row a reader gets from
    `build_tape_report` stayed status="ok" forever, misreporting a pass that
    dropped 45/55 tokens as healthy. This asserts the DB row agrees with the
    returned status."""
    from app.models import CryptoTokenLifecycleRun

    for i in range(5):
        born = _mint(session, f"tok-durtrunc-{i}", born_hours_ago=40, liquidity=10_000.0)
        _tick_at(session, f"tok-durtrunc-{i}", born + timedelta(hours=24), liquidity=9_000.0)
    for i in range(50):
        _mint(session, f"tok-durtruncfresh-{i}", born_hours_ago=1)

    r = run_scheduled_reconciliation(session, settings=_settings(), limit=10)
    assert r["status"] == "truncated"
    assert r["tape_run_id"] is not None

    row = session.get(CryptoTokenLifecycleRun, r["tape_run_id"])
    assert row is not None
    assert row.status == "truncated", (
        "the durable run row must agree with the returned status — a "
        "reader of build_tape_report (which reads this table, not the "
        "return value) must not see a healthy 'ok' for a pass that "
        "silently dropped work"
    )


def test_backlog_expiring_status_fires_when_frontier_crosses_retention_threshold(
    session,
):
    """NEW-HIGH-2 regression pin (second re-review, convergence lens). The
    routine `partial`/`truncated` statuses fire on every pass at production
    density and carry no signal about whether the frontier is actually at
    risk of being pruned — this is the separable, rare, actionable status.
    `crypto_retention_days=2` puts the frontier threshold at
    `2*24 - 6 = 42h`; a backlog token aged 50h crosses it."""
    born = _mint(session, "tok-frontier-old", born_hours_ago=50, liquidity=10_000.0)
    session.add(CryptoTokenSurvivalOutcome(
        birth_event_id=1, chain=CHAIN, token_address="tok-frontier-old", final=False,
    ))
    session.flush()

    r = run_scheduled_reconciliation(
        session, settings=_settings(crypto_retention_days=2),
    )
    assert r["status"] == "backlog_expiring"
    assert r["oldest_unreconciled_age_hours"] is not None
    assert r["oldest_unreconciled_age_hours"] >= 42.0
    assert "frontier" in r["error"].lower()

    # a healthy frontier (nothing old enough) must NOT trigger it
    session.query(CryptoTokenSurvivalOutcome).delete()
    session.query(CryptoTokenBirthEvent).delete()
    session.query(CryptoToken).delete()
    session.flush()
    _mint(session, "tok-frontier-fresh", born_hours_ago=1)
    r2 = run_scheduled_reconciliation(
        session, settings=_settings(crypto_retention_days=2),
    )
    assert r2["status"] != "backlog_expiring"


def test_deadline_stop_and_selection_truncation_error_texts_both_survive(session):
    """NEW-HIGH-1(b)/(c) regression pin (second re-review, convergence
    lens). At production density EVERY pass is both selection-truncated AND
    deadline-stopped. `summary["error"] = summary.get("error") or (...)`
    meant the deadline-stop text (set first, inside `_assemble_pass_locked`)
    always won and the truncation text — the ONLY message naming
    universe_size/backlog_size/tokens_omitted, i.e. the frozen backlog — was
    silently discarded, and the log line at the same site (labelled "crypto
    reconciliation truncated:") inherited that same incomplete text. This
    binds both conditions in one pass and asserts BOTH fragments are present
    in the single returned error string."""
    for i in range(5):
        born = _mint(session, f"tok-bothmsg-{i}", born_hours_ago=40, liquidity=10_000.0)
        _tick_at(session, f"tok-bothmsg-{i}", born + timedelta(hours=24), liquidity=9_000.0)
    for i in range(50):
        _mint(session, f"tok-bothmsgfresh-{i}", born_hours_ago=1)

    r = run_scheduled_reconciliation(
        session, settings=_settings(), limit=10, batch_size=2,
        max_duration_seconds=0.0,  # binding deadline: stop after batch 1
    )
    assert r["status"] == "partial"  # deadline stop wins the STATUS
    assert r["truncated"] is True
    # both messages must survive in the one error string
    assert "stop_reason=deadline" in r["error"]
    assert "universe_size" not in r["error"]  # sanity: not a key dump
    assert "aged-out unreconciled" in r["error"]
    assert "backlog_processed=" in r["error"]


def test_untruncated_pass_reports_the_full_universe(session):
    _mint(session, "tok-full", born_hours_ago=40)
    r = run_scheduled_reconciliation(session, settings=_settings())
    assert r["truncated"] is False
    assert r["tokens_omitted"] == 0
    assert r["universe_size"] == r["tokens_considered"] == 1


def test_negative_limit_is_refused_not_treated_as_unbounded(session):
    """SQLite reads LIMIT -1 as 'no limit', so an unvalidated cap is an
    unbounded pass wearing a bound's clothing."""
    for i in range(5):
        _mint(session, f"tok-neg-{i}", born_hours_ago=40)
    r = run_scheduled_reconciliation(session, settings=_settings(), limit=-1)
    assert r["status"] == "invalid_limit"
    assert r["tokens_considered"] == 0
    assert _outcome(session, "tok-neg-0") is None


def test_window_guard_accounts_for_the_scheduling_interval(session):
    """36h clears the closing edge but not the edge plus one 6h interval: a
    token born 37h ago matures and leaves the window between two passes."""
    from app.services.crypto_tape import RECONCILER_CADENCE_HOURS

    r = run_scheduled_reconciliation(
        session, settings=_settings(crypto_tape_reconciler_window_hours=36)
    )
    assert r["status"] == "invalid_window"
    assert str(RECONCILER_CADENCE_HOURS) in r["error"]


def test_default_window_clears_edge_plus_cadence():
    from app.services.crypto_tape import RECONCILER_CADENCE_HOURS

    s = Settings()
    edge = max(m for _, m in HORIZONS) * (1 + HORIZON_TOLERANCE) / 60
    assert s.crypto_tape_reconciler_window_hours >= edge + RECONCILER_CADENCE_HOURS


def test_gate_bypass_is_recorded_in_the_result(session):
    _mint(session, "tok-bypass", born_hours_ago=40)
    off = _settings(enable_crypto_tape_reconciler=False)

    assert run_scheduled_reconciliation(session, settings=off, force=True)[
        "gate_bypassed"] == "force"
    assert run_scheduled_reconciliation(session, settings=off, dry_run=True)[
        "gate_bypassed"] == "dry_run"
    assert run_scheduled_reconciliation(session, settings=_settings())[
        "gate_bypassed"] is None


def test_forced_pass_is_auditable_in_the_persisted_run_config(session):
    """A gate-bypassing pass must be distinguishable from a scheduled one in
    the audit trail, or the gate is unenforceable after the fact."""
    from app.models import CryptoTokenLifecycleRun

    _mint(session, "tok-audit", born_hours_ago=40)
    run_scheduled_reconciliation(
        session, settings=_settings(enable_crypto_tape_reconciler=False), force=True
    )
    run = session.query(CryptoTokenLifecycleRun).order_by(
        CryptoTokenLifecycleRun.id.desc()
    ).first()
    cfg = run.config or {}
    assert cfg.get("mode") == "scheduled_reconciliation"
    assert cfg.get("forced") is True


def test_pass_aborts_when_marketops_is_degraded(session):
    """Do not add write pressure while the primary lane is already failing.
    The manual session path already does this; the scheduled path must not be
    less careful than the manual one it wraps."""
    from app.models import MarketOpsRun

    session.add(MarketOpsRun(status="error"))
    session.flush()
    _mint(session, "tok-degraded", born_hours_ago=40)

    r = run_scheduled_reconciliation(session, settings=_settings())
    assert r["status"] == "marketops_degraded"
    assert _outcome(session, "tok-degraded") is None


def test_makes_no_network_call_at_the_socket_layer(session, monkeypatch):
    """Stronger than patching httpx: this catches requests, urllib, aiohttp, a
    cached client, or a module-level singleton, because every one of them must
    eventually reach a socket."""
    import socket

    def _boom(*a, **k):  # pragma: no cover - only runs on regression
        raise AssertionError("reconciliation attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _boom, raising=False)
    monkeypatch.setattr(socket, "create_connection", _boom, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", _boom, raising=False)

    born = _mint(session, "tok-socket", born_hours_ago=40, liquidity=10_000.0)
    _tick_at(session, "tok-socket", born + timedelta(hours=24), liquidity=9_000.0)

    r = run_scheduled_reconciliation(session, settings=_settings())
    assert r["status"] == "ok"
    assert r["external_calls"] == 0
    assert _outcome(session, "tok-socket").survived_24h is True


def test_scheduled_pass_is_row_idempotent_once_final(session):
    """CRYPTO-COVERAGE-REPAIR-001 B2 write classification: once a token's
    survival outcome is final, its window is closed and a lifecycle snapshot /
    actor observation appended on a LATER scheduled pass teaches nothing new
    (REDUNDANT/HISTORICAL_ARTIFACT); labels still converge and the outcome
    row itself stays singular. This deliberately supersedes the milestone's
    prior pinned "rows always accumulate" behaviour for the SCHEDULED path
    only.

    B2 follow-up fix (state-driven selection): the scheduled path now
    excludes already-final tokens from selection ENTIRELY (see
    `_universe(..., exclude_final=True)`), rather than selecting them and
    then skipping their write. That is strictly better — it also stops a
    deadline-stopped pass from wasting its wall-clock budget re-walking
    tokens with nothing left to learn — so the second pass here reports
    `tokens_considered=0` / nothing "skipped" (there was nothing left to
    select), not `snapshots_skipped_redundant=1`."""
    from app.models import (
        CryptoTokenActorObservation,
        CryptoTokenLifecycleSnapshot,
    )

    born = _mint(session, "tok-rows", born_hours_ago=40, liquidity=10_000.0)
    _tick_at(session, "tok-rows", born + timedelta(hours=24), liquidity=9_000.0)
    s = _settings()

    r1 = run_scheduled_reconciliation(session, settings=s)
    assert r1["status"] == "ok"
    snaps1 = session.query(CryptoTokenLifecycleSnapshot).count()
    actors1 = session.query(CryptoTokenActorObservation).count()
    label1 = _outcome(session, "tok-rows").survived_24h
    assert _outcome(session, "tok-rows").final is True

    r2 = run_scheduled_reconciliation(session, settings=s)
    assert r2["status"] == "ok"
    snaps2 = session.query(CryptoTokenLifecycleSnapshot).count()
    actors2 = session.query(CryptoTokenActorObservation).count()

    assert _outcome(session, "tok-rows").survived_24h == label1  # labels converge
    assert snaps2 == snaps1 and actors2 == actors1  # no redundant rows appended
    assert r2["tokens_considered"] == 0  # already-final token is not re-selected
    assert r2["snapshots_skipped_redundant"] == 0
    assert r2["actor_observations_skipped_redundant"] == 0
    assert session.query(CryptoTokenSurvivalOutcome).count() == 1


def test_manual_path_still_appends_lifecycle_rows_unchanged(session):
    """The manual/CLI path (`run_once` with its historical defaults) is
    explicitly OUT of scope for the B2 redundant-write skip — it must keep
    appending a snapshot and an actor observation per token on every pass,
    exactly as before this milestone."""
    from app.models import (
        CryptoTokenActorObservation,
        CryptoTokenLifecycleSnapshot,
    )
    from app.services.crypto_tape import CryptoLifecycleTapeRecorder

    born = _mint(session, "tok-manual-rows", born_hours_ago=40, liquidity=10_000.0)
    _tick_at(session, "tok-manual-rows", born + timedelta(hours=24), liquidity=9_000.0)
    rec = CryptoLifecycleTapeRecorder()

    rec.run_once(session, limit=10, hours=48)
    snaps1 = session.query(CryptoTokenLifecycleSnapshot).count()
    actors1 = session.query(CryptoTokenActorObservation).count()
    assert _outcome(session, "tok-manual-rows").final is True

    rec.run_once(session, limit=10, hours=48)
    snaps2 = session.query(CryptoTokenLifecycleSnapshot).count()
    actors2 = session.query(CryptoTokenActorObservation).count()

    assert snaps2 == snaps1 * 2 and actors2 == actors1 * 2  # unchanged: rows accumulate
    assert session.query(CryptoTokenSurvivalOutcome).count() == 1


def test_manual_path_selection_order_is_unchanged(session):
    """The oldest-first ordering is opt-in for the scheduled reconciler only;
    the existing manual path must keep its newest-first behaviour."""
    for i, age in enumerate((50, 40, 30, 20, 10)):
        _mint(session, f"tok-order-{i}", born_hours_ago=age)

    rec = CryptoLifecycleTapeRecorder()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=200)
    newest = rec._universe(session, 2, cutoff)
    oldest = rec._universe(session, 2, cutoff, oldest_first=True)

    assert [t.token_address for t in newest] == ["tok-order-4", "tok-order-3"]
    assert [t.token_address for t in oldest] == ["tok-order-0", "tok-order-1"]


# --- state-driven selection: missed passes and the existing backlog ---------

def test_matured_token_aged_out_of_the_window_is_still_reconciled(session):
    """Window-driven selection alone carries only (window - closing_edge) of
    slack — 12h at the shipped defaults. Two missed passes would push a cohort
    out of the window permanently, and the pre-existing backlog would never be
    reconciled at first enablement. Selection must be driven by outcome STATE,
    not only by recency."""
    born = _mint(session, "tok-aged", born_hours_ago=60, liquidity=10_000.0)
    _tick_at(session, "tok-aged", born + timedelta(hours=24), liquidity=9_000.0)
    # give it an open (non-final) outcome row, as a real aged token would have
    session.add(CryptoTokenSurvivalOutcome(
        birth_event_id=1, chain=CHAIN, token_address="tok-aged", final=False,
    ))
    session.flush()

    r = run_scheduled_reconciliation(session, settings=_settings())
    assert r["backlog_size"] >= 1
    assert r["tokens_considered"] >= 1
    o = _outcome(session, "tok-aged")
    assert o is not None and o.survived_24h is True


def test_backlog_is_reported_even_when_it_cannot_all_be_worked(session):
    """A shortfall must be visible, not inferred from a silent 'ok'."""
    for i in range(6):
        born = _mint(session, f"tok-bk-{i}", born_hours_ago=60 + i, liquidity=10_000.0)
        _tick_at(session, f"tok-bk-{i}", born + timedelta(hours=24), liquidity=9_000.0)
        session.add(CryptoTokenSurvivalOutcome(
            birth_event_id=100 + i, chain=CHAIN,
            token_address=f"tok-bk-{i}", final=False,
        ))
    session.flush()

    r = run_scheduled_reconciliation(session, settings=_settings(), limit=2)
    assert r["backlog_size"] == 6
    assert r["work_available"] == 6
    assert r["status"] == "truncated"
    assert r["tokens_omitted"] == 4


def test_final_outcomes_are_not_re_selected_as_backlog(session):
    """Settled work must not be redone every six hours forever."""
    born = _mint(session, "tok-final", born_hours_ago=60, liquidity=10_000.0)
    _tick_at(session, "tok-final", born + timedelta(hours=24), liquidity=9_000.0)
    session.add(CryptoTokenSurvivalOutcome(
        birth_event_id=900, chain=CHAIN, token_address="tok-final", final=True,
    ))
    session.flush()

    r = run_scheduled_reconciliation(session, settings=_settings())
    assert r["backlog_size"] == 0


# --- B3 chunked commits -------------------------------------------------------

def test_scheduled_pass_commits_in_bounded_batches(session):
    """NEW-H2 docstring fix (third re-review). This test's ORIGINAL title
    claimed to test the DEFAULT `crypto_tape_reconciler_batch_size`, but it
    passes `batch_size=10` explicitly — an OVERRIDE, not the default — so it
    never actually exercised the shipped default at all. It pins the
    override mechanism (commits in bounded transactions of the GIVEN size);
    see `test_default_batch_size_is_actually_used_when_unset` below for a
    genuine default-value test."""
    for i in range(23):
        _mint(session, f"tok-batch-{i}", born_hours_ago=30, liquidity=10_000.0)

    r = run_scheduled_reconciliation(
        session, settings=_settings(crypto_tape_reconciler_limit=1000),
        batch_size=10,
    )
    assert r["status"] == "ok"
    assert r["batch_size"] == 10
    assert r["batches_committed"] == 3  # ceil(23 / 10)
    assert r["tokens_processed"] == 23


def test_default_batch_size_is_actually_used_when_unset(session):
    """NEW-H2 fix (third re-review). Genuine default-value test: no
    `batch_size` override, so `run_scheduled_reconciliation` must fall back
    to `crypto_tape_reconciler_batch_size` from settings (RECONCILE_BATCH_SIZE
    == 25, pinned below)."""
    for i in range(30):
        _mint(session, f"tok-defaultbatch-{i}", born_hours_ago=30, liquidity=10_000.0)

    r = run_scheduled_reconciliation(
        session, settings=_settings(crypto_tape_reconciler_limit=1000),
    )
    assert r["status"] == "ok"
    assert r["batch_size"] == 25
    assert r["batches_committed"] == 2  # ceil(30 / 25)
    assert r["tokens_processed"] == 30


def test_shipped_write_coordination_constants_are_pinned():
    """NEW-H2 mutation-battery fix (third re-review). A full-suite mutation
    battery found that widening RECONCILE_BATCH_SIZE (25 -> 5000) or
    RECONCILE_MAX_DURATION_SECONDS (20 -> 300) — either the module constant
    OR the matching Settings field default — left the ENTIRE crypto suite
    green, because 5000 exceeds the 2000 selection limit and silently
    restores the pre-milestone single-transaction-pass shape. These are the
    numbers that ARE the write-lock safety argument (see the milestone doc's
    "Write-lock defect" section); pin them exactly, plus the invariant that
    makes the batch size meaningful against the selection cap."""
    from app.config import Settings
    from app.services.crypto_tape import (
        RECONCILE_BATCH_SIZE,
        RECONCILE_MAX_DURATION_SECONDS,
    )

    assert RECONCILE_BATCH_SIZE == 25
    assert RECONCILE_MAX_DURATION_SECONDS == 20.0

    defaults = Settings()
    assert defaults.crypto_tape_reconciler_batch_size == 25
    assert defaults.crypto_tape_reconciler_max_duration_seconds == 20.0
    # the invariant that makes batching meaningful: if the batch size ever
    # grows to meet or exceed the selection limit, a single "batch" IS the
    # whole pass again — silently restoring the single-transaction shape
    # this milestone exists to remove.
    assert defaults.crypto_tape_reconciler_batch_size < defaults.crypto_tape_reconciler_limit
    assert RECONCILE_BATCH_SIZE < defaults.crypto_tape_reconciler_limit


def test_manual_path_defaults_to_one_commit_for_the_whole_pass(session):
    """`run_once`'s historical default (`batch_size=None`) must stay a single
    committed transaction — this is the exact shape
    `test_one_bounded_transaction` (CRYPTO-HORIZON-ANCHOR-FEED-MEASUREMENT-001)
    pins for the anchor feed, which shares this code path."""
    from sqlalchemy import event

    for i in range(12):
        _mint(session, f"tok-legacy-{i}", born_hours_ago=30, liquidity=10_000.0)
    rec = CryptoLifecycleTapeRecorder()
    commits = []

    @event.listens_for(session, "after_commit")
    def _count(sess):
        commits.append(1)

    try:
        r = rec.run_once(session, limit=100, hours=48)
    finally:
        event.remove(session, "after_commit", _count)
    assert r["status"] == "ok"
    assert len(commits) == 1


# --- B4 overlap guard ---------------------------------------------------------

def test_overlap_lock_skips_a_concurrent_pass(session, tmp_path):
    """B4 — a non-blocking, per-chain flock: a second concurrent pass over
    the SAME chain must be skipped loudly (`status=skipped_overlap`), never
    race the first pass's pre-transaction reads."""
    from app.services.crypto_tape import (
        RECONCILE_LOCK_FILENAME,
        _reconcile_overlap_lock,
    )

    _mint(session, "tok-overlap", born_hours_ago=30, liquidity=10_000.0)
    lock_path = tmp_path / RECONCILE_LOCK_FILENAME.format(chain=CHAIN)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with _reconcile_overlap_lock(tmp_path, CHAIN) as acquired:
        assert acquired is True  # the test itself holds the lock now
        rec = CryptoLifecycleTapeRecorder(
            CryptoTapeConfig(chain=CHAIN, lock_dir=tmp_path)
        )
        r = rec.run_once(session, limit=10, hours=48)
        assert r["status"] == "skipped_overlap"
        assert r["stop_reason"] == "overlap"
        assert r["tokens_considered"] == 0
        assert "overlap lock" in r["error"]
    # released — a normal pass now succeeds
    rec2 = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=tmp_path))
    r2 = rec2.run_once(session, limit=10, hours=48)
    assert r2["status"] == "ok"


def test_overlap_lock_is_released_after_a_normal_pass(session, tmp_path):
    """The lock must not leak: two SEQUENTIAL passes both succeed."""
    _mint(session, "tok-seq", born_hours_ago=30, liquidity=10_000.0)
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=tmp_path))
    r1 = rec.run_once(session, limit=10, hours=48)
    r2 = rec.run_once(session, limit=10, hours=48)
    assert r1["status"] == "ok"
    assert r2["status"] == "ok"


def test_dry_run_never_takes_the_overlap_lock(session, tmp_path):
    """Two concurrent dry probes are harmless (nothing is mutated), so
    dry-run must never contend for the lock at all."""
    from app.services.crypto_tape import _reconcile_overlap_lock

    _mint(session, "tok-dry-overlap", born_hours_ago=30, liquidity=10_000.0)
    with _reconcile_overlap_lock(tmp_path, CHAIN) as acquired:
        assert acquired is True
        rec = CryptoLifecycleTapeRecorder(
            CryptoTapeConfig(chain=CHAIN, lock_dir=tmp_path)
        )
        r = rec.run_once(session, limit=10, hours=48, dry_run=True)
        assert r["status"] == "dry_run"  # not skipped_overlap


def test_two_connection_file_backed_busy_timeout_and_retry_ladder(tmp_path):
    """docs/SQLITE_WRITER_TOPOLOGY_2026_07.md failure-mode 15: tests using
    in-memory `sqlite://` cannot exercise real busy_timeout/lock contention.
    This is the two-connection, shared-FILE test that closes that gap: a
    real second SQLite connection holds a write transaction open on the same
    file, and `run_once` (opted into batching, so the retry ladder applies)
    must observe genuine lock contention, exhaust its bounded retries, and
    return a typed result — never hang, never raise, never corrupt state."""
    import sqlite3

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "two_connection.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"timeout": 0.2},
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)
    seed_session = Factory()
    _mint(seed_session, "tok-two-conn", born_hours_ago=30, liquidity=10_000.0)
    seed_session.commit()
    seed_session.close()

    # a second, INDEPENDENT real connection holds RESERVED and never commits
    holder = sqlite3.connect(str(db_path), timeout=0.2)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE crypto_tokens SET last_seen_at = last_seen_at")

    try:
        worker_session = Factory()
        rec = CryptoLifecycleTapeRecorder(
            CryptoTapeConfig(chain=CHAIN, lock_dir=tmp_path)
        )
        r = rec.run_once(
            worker_session, limit=10, hours=48,
            batch_size=5,               # opt into the retry ladder
            max_lock_attempts=3, lock_retry_seconds=0.05,
            sleeper=lambda seconds: None,  # no real delay in tests
        )
        worker_session.close()
    finally:
        holder.rollback()
        holder.close()

    assert r["status"] == "skipped_contention"
    assert r["stop_reason"] == "contention"
    assert "database is locked" in r["error"]

    # the holder released; a normal pass now succeeds and sees the token
    verify_session = Factory()
    rec2 = CryptoLifecycleTapeRecorder(
        CryptoTapeConfig(chain=CHAIN, lock_dir=tmp_path)
    )
    r2 = rec2.run_once(verify_session, limit=10, hours=48, batch_size=5)
    assert r2["status"] == "ok"
    assert r2["tokens_considered"] == 1
    verify_session.close()
    engine.dispose()


def test_real_lock_hit_during_finalize_is_caught_not_raised(tmp_path):
    """NEW-B1 regression pin (third re-review, SQLite/concurrency). This is
    deliberately NOT a monkeypatched `session.commit()` — every existing
    contention test in this file patches `commit` to raise, so `prepare()`
    (called OUTSIDE `_commit_with_retry`'s try, pre-fix) never meets a REAL
    lock and this defect was invisible to the whole suite.

    Reproduction: a real second SQLite connection acquires `BEGIN IMMEDIATE`
    right after the run row AND the first token batch have ALREADY committed
    durably (via a real `after_commit` listener, not a timer) — so contention
    hits both the SECOND batch's retry ladder and, once that's exhausted,
    the FINALIZE step's retry ladder. `_prepare_finalize` stages several
    OTHER dirty `run.*` attribute writes before reading the (now-expired,
    post-rollback) `run.config` attribute; that read used to autoflush the
    dirty session into the same real lock, raising OperationalError from
    INSIDE `prepare()` — which pre-fix propagated straight past the retry
    ladder and out of `run_once` itself as an uncaught exception, instead of
    the typed `partial`/`skipped_contention` result this ladder exists to
    produce. This asserts `run_once` returns NORMALLY with a typed result —
    not that it raises."""
    import sqlite3

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "real_lock_during_finalize.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 0.2})
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)
    seed_session = Factory()
    for i in range(10):
        _mint(seed_session, f"tok-reallock-{i}", born_hours_ago=30, liquidity=10_000.0)
    seed_session.commit()
    seed_session.close()

    worker_session = Factory()
    rec = CryptoLifecycleTapeRecorder(
        CryptoTapeConfig(chain=CHAIN, lock_dir=tmp_path)
    )
    holder: dict = {"conn": None}
    commits_seen = {"n": 0}

    @event.listens_for(worker_session, "after_commit")
    def _acquire_real_lock_after_first_batch(sess):
        # commit #1 = the run-row creation, commit #2 = the FIRST token
        # batch. Acquire the external lock right after that second commit,
        # so at least one full batch is durably committed before any
        # contention hits — matching the reproduced failure shape exactly
        # (the reviewer measured 150/750 tokens already durable when the
        # uncaught exception hit).
        commits_seen["n"] += 1
        if commits_seen["n"] == 2 and holder["conn"] is None:
            c = sqlite3.connect(str(db_path), timeout=0.2)
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE crypto_tokens SET last_seen_at = last_seen_at")
            holder["conn"] = c

    try:
        r = rec.run_once(
            worker_session, limit=10, hours=48,
            batch_size=3, max_lock_attempts=3, lock_retry_seconds=0.05,
            sleeper=lambda seconds: None,
        )
    finally:
        event.remove(worker_session, "after_commit", _acquire_real_lock_after_first_batch)
        if holder["conn"] is not None:
            holder["conn"].rollback()
            holder["conn"].close()
        worker_session.close()
        engine.dispose()

    # The whole point: a typed result, never an uncaught exception, and at
    # least the first batch's durable progress is reflected honestly.
    assert r["status"] in ("partial", "skipped_contention")
    assert r["error"]
    if r["status"] == "partial":
        assert r["batches_committed"] >= 1
        assert r["tokens_processed"] >= 3


def test_real_read_blocking_lock_after_batch_commit_yields_honest_partial(tmp_path):
    """BLOCKING-3 regression pin (fourth re-review, SQLite/concurrency).

    The existing `test_real_lock_hit_during_finalize_is_caught_not_raised`
    pin above uses a real second connection holding `BEGIN IMMEDIATE`. That
    is a real lock — but SQLite's IMMEDIATE lock only blocks other WRITERS;
    a same-process/other-connection SELECT (a lazy-load on an expired ORM
    attribute, exactly the BLOCKING-1/BLOCKING-2 defect shape) is still
    perfectly readable under IMMEDIATE. That is exactly why a lazy-load
    escape on `run.config` / `run.id` survived five review rounds: the only
    "real lock" regression test in the suite could never observe it. This
    test closes that gap with `BEGIN EXCLUSIVE`, which DOES block readers
    too, acquired only AFTER at least one full token batch has already
    committed durably (via a real `after_commit` listener, not a timer).

    Expected (fixed) behaviour: `run_once` returns a typed `status=partial`
    result whose `tokens_considered` matches the durable row count exactly
    — no uncaught exception, no phantom `tokens_considered=0` while rows
    were actually written (the reviewer's measured defeat of the prior fix).

    Proof this test actually exercises the fix: reverting either BLOCKING-1
    (`existing_config = dict(run_config or {})`) or BLOCKING-2 (the `run_id`
    local capture) back to a `run.config` / `run.id` read makes this test
    fail — verified by hand during this fix (see the fix commit message)."""
    import sqlite3

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "real_exclusive_lock_after_batch.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 0.2})
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)
    seed_session = Factory()
    for i in range(10):
        _mint(seed_session, f"tok-exlock-{i}", born_hours_ago=30, liquidity=10_000.0)
    seed_session.commit()
    seed_session.close()

    worker_session = Factory()
    rec = CryptoLifecycleTapeRecorder(
        CryptoTapeConfig(chain=CHAIN, lock_dir=tmp_path)
    )
    holder: dict = {"conn": None}
    commits_seen = {"n": 0}

    @event.listens_for(worker_session, "after_commit")
    def _acquire_exclusive_lock_after_first_batch(sess):
        # commit #1 = the run-row creation, commit #2 = the FIRST token
        # batch. Acquire the READ-BLOCKING external lock right after that,
        # so at least one batch's rows are durable in the DB file before the
        # rest of the pass has to fight a real, read-blocking holder.
        commits_seen["n"] += 1
        if commits_seen["n"] == 2 and holder["conn"] is None:
            c = sqlite3.connect(str(db_path), timeout=0.2)
            c.execute("BEGIN EXCLUSIVE")
            holder["conn"] = c

    try:
        r = rec.run_once(
            worker_session, limit=10, hours=48,
            batch_size=3, max_lock_attempts=3, lock_retry_seconds=0.05,
            sleeper=lambda seconds: None,
        )
    finally:
        event.remove(worker_session, "after_commit", _acquire_exclusive_lock_after_first_batch)
        if holder["conn"] is not None:
            holder["conn"].rollback()
            holder["conn"].close()
        worker_session.close()
        engine.dispose()

    assert r["status"] == "partial", r
    # The durable ground truth: count birth events actually committed, via a
    # brand-new connection/session so nothing here can be fooled by ORM
    # session-level caching.
    check_session = Factory()
    try:
        durable_births = check_session.execute(
            select(CryptoTokenBirthEvent).where(
                CryptoTokenBirthEvent.token_address.like("tok-exlock-%")
            )
        ).scalars().all()
    finally:
        check_session.close()
    assert r["tokens_considered"] == len(durable_births) > 0
    assert r["batches_committed"] >= 1


# --- B6 internal deadline ------------------------------------------------------

def test_deadline_stops_the_pass_between_batches_not_mid_batch(session):
    """A wall-clock deadline that has already passed before the SECOND batch
    starts must stop the pass there — durable partial progress, not a hang,
    not a half-committed batch."""
    for i in range(15):
        _mint(session, f"tok-deadline-{i}", born_hours_ago=30, liquidity=10_000.0)
    rec = CryptoLifecycleTapeRecorder()
    r = rec.run_once(
        session, limit=100, hours=48, batch_size=5,
        max_duration_seconds=0.0,  # already "past due" before batch 2
    )
    assert r["status"] == "partial"
    assert r["stop_reason"] == "deadline"
    assert 0 < r["tokens_processed"] < 15
    assert r["tokens_processed"] % 5 == 0  # stopped on a batch boundary


def test_scheduled_reconciliation_reports_partial_status_not_ok(session):
    """B7 — a unit that stops early must never look healthy. `status=ok`
    while eligible rows remain unreconciled is exactly the failure class this
    milestone exists to remove."""
    for i in range(15):
        _mint(session, f"tok-partial-{i}", born_hours_ago=30, liquidity=10_000.0)
    r = run_scheduled_reconciliation(
        session, settings=_settings(crypto_tape_reconciler_limit=1000),
        batch_size=5, max_duration_seconds=0.0,
    )
    assert r["status"] == "partial"
    assert r["stop_reason"] == "deadline"
    assert r["error"]


# --- B3/B5 restart safety and idempotence under batching ----------------------

def test_restart_after_a_partial_batch_stop_is_idempotent(session):
    """A pass stopped mid-way by a deadline commits real, durable batches; a
    SECOND pass over the same window must not duplicate a birth event and
    (with B2's skip-when-final opted in, as the scheduled path always does)
    must not duplicate a snapshot/actor for a token that already finalized."""
    from app.models import (
        CryptoTokenActorObservation,
        CryptoTokenLifecycleSnapshot,
    )

    # born_hours_ago > 36h (the 24h horizon's closing edge) so every token's
    # outcome finalizes the FIRST time either pass touches it.
    for i in range(12):
        born = _mint(session, f"tok-restart-{i}", born_hours_ago=40, liquidity=10_000.0)
        _tick_at(session, f"tok-restart-{i}", born + timedelta(hours=24), liquidity=9_000.0)

    rec = CryptoLifecycleTapeRecorder()
    r1 = rec.run_once(
        session, limit=100, hours=48, batch_size=4, max_duration_seconds=0.0,
        oldest_first=True, skip_redundant_when_final=True,
    )
    assert r1["status"] == "partial"
    processed_after_1 = r1["tokens_processed"]
    assert 0 < processed_after_1 < 12
    births_after_1 = session.query(CryptoTokenBirthEvent).count()
    snaps_after_1 = session.query(CryptoTokenLifecycleSnapshot).count()
    assert births_after_1 == processed_after_1
    assert snaps_after_1 == processed_after_1

    r2 = rec.run_once(
        session, limit=100, hours=48, batch_size=4, oldest_first=True,
        skip_redundant_when_final=True,
    )
    assert r2["status"] == "ok"
    births_after_2 = session.query(CryptoTokenBirthEvent).count()
    snaps_after_2 = session.query(CryptoTokenLifecycleSnapshot).count()
    actors_after_2 = session.query(CryptoTokenActorObservation).count()
    # every token now has exactly one birth (no duplicates) and every token
    # got exactly one snapshot/actor, from whichever single pass first saw it
    assert births_after_2 == 12
    assert snaps_after_2 == 12
    assert actors_after_2 == 12
    for i in range(12):
        assert _outcome(session, f"tok-restart-{i}").survived_24h is True


# --- B9 benchmark harness sanity: result-shape checks --------------------------

def test_result_reports_lock_retry_events_and_batches_committed_fields(session):
    """B5/B9 telemetry surface: every pass reports lock_retry_events and
    batches_committed so a caller can distinguish a clean run from a
    contended one without re-deriving it from logs."""
    _mint(session, "tok-telemetry", born_hours_ago=30, liquidity=10_000.0)
    r = run_scheduled_reconciliation(session, settings=_settings(), batch_size=5)
    assert r["status"] == "ok"
    assert r["lock_retry_events"] == 0
    assert r["batches_committed"] == 1


# --- second review: B1 exact-cycle path must ignore the overlap lock --------

def test_record_discovery_run_ignores_a_held_overlap_lock(session, tmp_path):
    """B1 fix. `record_discovery_run` (the exact-cycle anchor feed — the only
    tape path production actually runs, every cycle) must NOT be skipped by
    another pass's overlap flock. Before the fix, `_assemble_pass`'s
    `use_overlap_lock` default (True) meant a held lock made this return
    `status="ok"` with birth_events_created=0, and the caller derived
    `anchors_existing=len(tokens) - 0` — i.e. a FABRICATED "all anchors
    already existed" count, when in fact nothing was read or written. The
    anchor feed is exact-cycle: a skipped cycle is never retried, so this
    would silently zero out real anchor-feed cycles at dark install, with the
    flag off, whenever a ~105s manual pass happened to overlap the 5-minute
    MarketOps cadence."""
    from app.models import CryptoWatcherRun
    from app.services.crypto_tape import _reconcile_overlap_lock

    now = datetime.now(timezone.utc)
    run = CryptoWatcherRun(started_at=now - timedelta(minutes=1), finished_at=now)
    session.add(run)
    session.flush()
    born = now - timedelta(seconds=30)
    session.add(CryptoToken(
        chain=CHAIN, token_address="tok-b1-overlap", symbol="B1",
        first_seen_at=born, last_seen_at=born,
    ))
    session.add(CryptoPriceTick(
        chain=CHAIN, token_address="tok-b1-overlap", pair_address="pair-b1",
        observed_at=born, price_usd=1.0, liquidity_usd=10_000.0,
        volume_24h_usd=5_000.0,
    ))
    session.flush()

    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=tmp_path))
    with _reconcile_overlap_lock(tmp_path, CHAIN) as acquired:
        assert acquired is True  # the test itself holds the lock now
        r = rec.record_discovery_run(session, run.id, ["tok-b1-overlap"])
        # must NOT be status="ok" with a fabricated anchors_existing count
        assert r["status"] == "ok"
        assert r["anchors_created"] == 1
        assert r["anchors_existing"] == 0
        assert r["complete_anchors"] + r["incomplete_anchors"] == 1


def test_record_discovery_run_never_fabricates_anchors_from_a_degraded_pass(session):
    """B1 defensive fix: if `_assemble_pass` ever returns a non-normal
    (not ok/dry_run) summary for the exact-cycle path, `record_discovery_run`
    must report a distinct non-ok status rather than deriving
    anchors_created/anchors_existing from that summary's (zeroed) counters."""
    from unittest.mock import patch

    from app.models import CryptoWatcherRun

    now = datetime.now(timezone.utc)
    run = CryptoWatcherRun(started_at=now - timedelta(minutes=1), finished_at=now)
    session.add(run)
    session.flush()
    born = now - timedelta(seconds=30)
    session.add(CryptoToken(
        chain=CHAIN, token_address="tok-b1-degraded", symbol="B1D",
        first_seen_at=born, last_seen_at=born,
    ))
    session.flush()

    rec = CryptoLifecycleTapeRecorder()
    degraded_summary = {
        "status": "skipped_overlap",
        "birth_events_created": 0,
        "tokens_considered": 0,
        "snapshots_created": 0,
        "outcomes_updated": 0,
        "error": "synthetic: another pass holds the lock",
        "_births": [],
    }
    with patch.object(rec, "_assemble_pass", return_value=degraded_summary):
        r = rec.record_discovery_run(session, run.id, ["tok-b1-degraded"])
    assert r["status"] not in ("ok", "dry_run")
    assert r["anchors_created"] == 0
    assert r["anchors_existing"] == 0  # NOT len(tokens) - 0 == 1 (fabricated)
    assert "skipped_overlap" in r["error"]


def test_record_discovery_run_reports_concurrent_write_conflict_not_a_traceback(
    session, monkeypatch,
):
    """HIGH fix (fourth re-review). `record_discovery_run` deliberately
    opts OUT of the B4 overlap lock, so a residual race against another
    concurrent tape writer can surface as a real IntegrityError. Before this
    fix, ONLY `run_scheduled_reconciliation` mapped that to a typed
    `concurrent_write_conflict` result (see
    test_integrity_error_reports_concurrent_write_conflict_not_a_traceback
    above) — `record_discovery_run` had no such mapping, so the exact same
    IntegrityError propagated straight out of this method, uncaught. The
    CLI's exact-run path (app/cli.py:2795) calls this method directly with
    no surrounding try/except, so that really would have been an unhandled
    traceback, not a typed non-zero-exit result."""
    from sqlalchemy.exc import IntegrityError

    from app.models import CryptoWatcherRun

    now = datetime.now(timezone.utc)
    run = CryptoWatcherRun(started_at=now - timedelta(minutes=1), finished_at=now)
    session.add(run)
    session.flush()
    born = now - timedelta(seconds=30)
    session.add(CryptoToken(
        chain=CHAIN, token_address="tok-anchorrace", symbol="ANR",
        first_seen_at=born, last_seen_at=born,
    ))
    session.commit()  # durable, so the racing rollback below can't undo it

    real_commit = type(session).commit

    def racing_commit(self):
        raise IntegrityError(
            "INSERT INTO crypto_token_birth_events ...", {},
            Exception(
                "UNIQUE constraint failed: crypto_token_birth_events.chain, "
                "crypto_token_birth_events.token_address"
            ),
        )

    monkeypatch.setattr(type(session), "commit", racing_commit)

    rec = CryptoLifecycleTapeRecorder()
    r = rec.record_discovery_run(session, run.id, ["tok-anchorrace"])
    assert r["status"] == "concurrent_write_conflict"
    assert r["error"]
    assert "unique-constraint" in r["error"].lower() or "UNIQUE" in r["error"]

    # session must be usable afterwards (proves rollback happened)
    monkeypatch.setattr(type(session), "commit", real_commit)
    r2 = rec.record_discovery_run(session, run.id, ["tok-anchorrace"])
    assert r2["status"] == "ok"


# --- second review: NEW-B2 state-driven selection ---------------------------

def test_deadline_stopped_scheduled_passes_advance_not_restart(session):
    """NEW-B2(a) fix. A deadline-stopped, oldest-first scheduled pass must
    select a DIFFERENT head on its NEXT invocation, not re-select the
    identical set forever. Before the fix, `_universe`'s oldest-first query
    had no outcome-state predicate, so a deadline-stopped pass re-selected
    the same oldest tokens on every subsequent call — reproduced upstream as
    "30 matured tokens, 6 consecutive passes, each processed the same 5; 25
    of 30 never reconciled". This asserts count(CryptoTokenSurvivalOutcome)
    strictly increases across repeated deadline-stopped passes until every
    token is covered."""
    n = 12
    batch = 2
    # WITHIN the window and past the 24h*(1+HORIZON_TOLERANCE)=36h closing
    # edge — this exercises `_universe`'s oldest-first, in-window selection,
    # not the (already state-driven) `unreconciled_backlog` top-up, which
    # would mask a broken `_universe` since it independently excludes final
    # tokens regardless of the `exclude_final` flag threaded to `_universe`.
    # A window of 100h clears the required closing-edge-plus-cadence floor
    # (~42h) while keeping every token in the primary window query, not the
    # backlog.
    window_hours = 100
    for i in range(n):
        born = _mint(
            session, f"tok-advance-{i:02d}", born_hours_ago=40 + i,
            liquidity=10_000.0,
        )
        _tick_at(
            session, f"tok-advance-{i:02d}", born + timedelta(hours=24),
            liquidity=9_000.0,
        )

    settings = _settings(
        crypto_tape_reconciler_limit=1000,
        crypto_tape_reconciler_window_hours=window_hours,
    )
    counts = [session.query(CryptoTokenSurvivalOutcome).count()]
    for i in range(n // batch):
        r = run_scheduled_reconciliation(
            session, settings=settings, batch_size=batch, max_duration_seconds=0.0,
        )
        # every pass except the very last (which exactly exhausts the
        # remaining backlog in one batch) is stopped early by the deadline
        if i < n // batch - 1:
            assert r["status"] == "partial"
            assert r["stop_reason"] == "deadline"
        counts.append(session.query(CryptoTokenSurvivalOutcome).count())

    assert counts == sorted(counts)  # never regresses
    assert len(set(counts)) == len(counts)  # STRICTLY increasing every pass
    assert counts[-1] == n  # every token eventually covered, none starved
    for i in range(n):
        assert _outcome(session, f"tok-advance-{i:02d}").final is True


def test_backlog_recovers_a_token_that_was_never_reconciled_at_all(session):
    """NEW-B2(b) fix. `unreconciled_backlog`/`backlog_size` must be OUTER
    joins against CryptoTokenSurvivalOutcome. A token no pass has EVER
    reached has NO outcome row at all; an INNER join makes such a token
    invisible to both the moment it ages out of the window — permanently, by
    construction — which silently caps how much of a pre-existing backlog
    can ever be recovered. This mints a token with no outcome row, entirely
    outside the window, and proves the scheduled pass still finds and
    reconciles it via backlog."""
    born = _mint(session, "tok-never-touched", born_hours_ago=200, liquidity=10_000.0)
    _tick_at(session, "tok-never-touched", born + timedelta(hours=24), liquidity=9_000.0)
    assert _outcome(session, "tok-never-touched") is None  # never selected by any pass

    r = run_scheduled_reconciliation(
        session, settings=_settings(crypto_tape_reconciler_window_hours=48)
    )
    assert r["backlog_size"] >= 1
    o = _outcome(session, "tok-never-touched")
    assert o is not None and o.survived_24h is True


# --- fourth review: NEW-H1 the backlog is a one-way trapdoor behind the
# in-window head under a binding deadline ------------------------------------

def test_backlog_makes_forward_progress_under_a_binding_deadline(session):
    """NEW-H1 fix. `run_once` built the selected token list as
    [in-window oldest->newest] + [aged-out backlog oldest->newest], and a
    binding deadline stops a chunked pass in LIST order. So the backlog —
    which `unreconciled_backlog`'s own docstring calls the evidence "closest
    to being pruned" — was processed LAST and, once the in-window head alone
    exceeded one pass's per-deadline throughput (the structural steady state
    HIGH-1 targets), was NEVER reached: a one-way trapdoor where aged-out
    tokens could only accumulate. This reproduces that shape at small scale
    (a non-empty in-window head that would consume the ENTIRE per-pass batch
    budget under the old ordering, plus a non-empty backlog) and asserts the
    count of RECONCILED BACKLOG tokens strictly increases pass over pass —
    not merely `count(outcomes)`, which the in-window head alone could
    satisfy while the backlog silently starved."""
    batch = 5
    backlog_n = 20
    in_window_n = 20

    # Backlog: aged OUT of the 48h window, past the 36h finality edge, so one
    # touch finalizes each — the evidence closest to being pruned.
    for i in range(backlog_n):
        born = _mint(
            session, f"tok-h1-backlog-{i:02d}", born_hours_ago=60 + i,
            liquidity=10_000.0,
        )
        _tick_at(
            session, f"tok-h1-backlog-{i:02d}", born + timedelta(hours=24),
            liquidity=9_000.0,
        )
    # In-window head: well past the HIGH-1 age-exclusion floor, but far short
    # of finality — this is the population that, under the OLD ordering,
    # would consume every batch of every deadline-bound pass before the
    # backlog was ever reached.
    for i in range(in_window_n):
        _mint(session, f"tok-h1-inwindow-{i:02d}", born_hours_ago=30)

    settings = _settings(crypto_tape_reconciler_limit=1000)

    def backlog_final_count() -> int:
        return sum(
            1 for i in range(backlog_n)
            if (o := _outcome(session, f"tok-h1-backlog-{i:02d}")) is not None
            and o.final
        )

    counts = [backlog_final_count()]
    # exactly enough passes to drain the backlog (20 / 5 = 4) and no more —
    # one pass past that would start consuming the in-window head, which
    # would defeat the "in-window untouched" assertion below for a reason
    # unrelated to the fix this test pins.
    for _ in range(backlog_n // batch):
        r = run_scheduled_reconciliation(
            session, settings=settings, batch_size=batch, max_duration_seconds=0.0,
        )
        assert r["status"] in ("ok", "partial")
        counts.append(backlog_final_count())
        # NEW-LOW-1 fix (second re-review): `>= 0` is `min()` of two
        # non-negative ints and CANNOT FAIL for any implementation,
        # including one that hardcodes 0 — it made no assertion at all
        # despite its own comment claiming to make the trapdoor
        # "journal-visible". Every pass here stops after exactly one batch
        # (`max_duration_seconds=0.0`) and — per the "in-window untouched"
        # assertion below — every token in that batch is a backlog token,
        # so `backlog_processed` must equal `batch` exactly, every pass.
        assert r["backlog_processed"] == batch

    assert counts == sorted(counts)  # never regresses
    assert len(set(counts)) == len(counts)  # STRICTLY increasing every pass
    assert counts[-1] == backlog_n  # the whole backlog eventually drains

    # the in-window head must NOT have been touched yet by these
    # backlog-first passes — proving the reordering, not just eventual
    # coverage, is what changed (an in-window outcome row would only appear
    # once backlog work in front of it is exhausted).
    assert all(
        _outcome(session, f"tok-h1-inwindow-{i:02d}") is None
        for i in range(in_window_n)
    )


def test_three_way_intersection_binding_limit_and_backlog_and_in_window_head(
    session,
):
    """NEW-BLOCKING-1 regression pin (second re-review, convergence lens).
    The backlog-FIRST ordering fix (test_backlog_makes_forward_progress_
    under_a_binding_deadline above) is necessary but not sufficient: it only
    helps once backlog tokens are actually IN the selected list. Before the
    budget-reservation fix, `_universe` was called with the FULL `limit`
    BEFORE any backlog budget existed, so whenever in-window eligible alone
    reached `limit`, `room` was computed as 0 and `unreconciled_backlog` was
    NEVER QUERIED — the ordering fix cannot help a list that never contains
    any backlog tokens to begin with. This constructs exactly that
    intersection — a BINDING limit (60) with a non-empty in-window head (60
    eligible, meeting or exceeding the limit on its own) AND a non-empty
    aged-out backlog (40) AND a binding deadline — and asserts
    `backlog_processed > 0`. This FAILS on 0fe2f14 (backlog_processed == 0,
    because `room == 0`)."""
    batch = 5
    backlog_n = 40
    in_window_n = 60

    for i in range(backlog_n):
        born = _mint(
            session, f"tok-3way-backlog-{i:02d}", born_hours_ago=60 + i,
            liquidity=10_000.0,
        )
        _tick_at(
            session, f"tok-3way-backlog-{i:02d}", born + timedelta(hours=24),
            liquidity=9_000.0,
        )
    for i in range(in_window_n):
        _mint(session, f"tok-3way-inwindow-{i:02d}", born_hours_ago=30)

    settings = _settings(crypto_tape_reconciler_limit=1000)
    r = run_scheduled_reconciliation(
        session, settings=settings, limit=60, batch_size=batch,
        max_duration_seconds=0.0,  # binding deadline: stop after batch 1
    )
    assert r["status"] in ("ok", "partial")
    assert r["backlog_size"] == backlog_n
    assert r["backlog_processed"] > 0, (
        "the aged-out backlog must receive SOME budget even when the "
        "in-window head alone meets/exceeds the selection limit — "
        "backlog_processed == 0 here means unreconciled_backlog was never "
        "even queried (room == 0), the exact trapdoor this fix removes"
    )


def test_age_zero_tokens_are_not_selected_by_the_scheduled_path(session):
    """HIGH-1 fix, selection-level pin. `compute_survival` only sets `final`
    at the 24h+tolerance edge, so under `exclude_final=True` ALONE every
    token younger than that stays selectable forever — including tokens with
    NO horizon due at all yet (not even 15m). Those are pure waste: they can
    never be finalized on this pass and re-selecting them wastes wall-clock
    budget that should go to genuinely reconcilable tokens. This mints
    several tokens younger than `SHORTEST_HORIZON_CLOSING_EDGE_MINUTES`
    alongside one eligible token and proves only the eligible one is
    selected — distinguishing "nothing selected because the window is empty"
    from "age-0 tokens specifically excluded"."""
    for i in range(5):
        _mint(session, f"tok-age0-{i}", born_hours_ago=0.01)  # ~36 seconds old
    _mint(session, "tok-age-eligible", born_hours_ago=1)  # well past 22.5m

    r = run_scheduled_reconciliation(session, settings=_settings())
    assert r["status"] == "ok"
    assert r["tokens_considered"] == 1
    assert r["universe_size"] == 1

    for i in range(5):
        assert _outcome(session, f"tok-age0-{i}") is None  # never touched
    assert _outcome(session, "tok-age-eligible") is not None


def test_manual_path_still_selects_age_zero_tokens_unlike_the_scheduled_path(session):
    """Contrast case: `min_age_minutes` is opt-in — the manual/CLI `run_once`
    path (default `min_age_minutes=None`) must keep selecting age-0 tokens
    exactly as before; only `run_scheduled_reconciliation` opts in."""
    _mint(session, "tok-age0-manual", born_hours_ago=0.01)

    rec = CryptoLifecycleTapeRecorder()
    r = rec.run_once(session, limit=25, hours=48)
    assert r["status"] == "ok"
    assert r["tokens_considered"] == 1
    assert _outcome(session, "tok-age0-manual") is not None


def test_repeated_passes_reach_ok_once_backlog_is_drained(session):
    """HIGH-1 fix. Before this fix, `partial` was the STRUCTURAL steady state
    for a deadline-bound scheduled pass — there was no configuration in
    which repeated invocations ever converged to a clean `ok`, because
    (pre NEW-H1) the backlog was never even reached, and non-final tokens
    were reselected forever. This pins the positive case the review asked
    for directly: a finite, draining backlog (each token finalizes the first
    time it is touched) under a binding per-pass deadline must converge to
    `status=ok` within a bounded number of passes — not stay `partial`
    forever — once all genuinely reconcilable work is exhausted."""
    batch = 5
    n = 12
    for i in range(n):
        born = _mint(
            session, f"tok-converge-{i:02d}", born_hours_ago=60 + i,
            liquidity=10_000.0,
        )
        _tick_at(
            session, f"tok-converge-{i:02d}", born + timedelta(hours=24),
            liquidity=9_000.0,
        )

    settings = _settings(crypto_tape_reconciler_limit=1000)
    statuses = []
    for _ in range(6):
        r = run_scheduled_reconciliation(
            session, settings=settings, batch_size=batch, max_duration_seconds=0.0,
        )
        statuses.append(r["status"])
        if r["status"] == "ok":
            break

    assert "partial" in statuses  # it genuinely had to stop early at least once
    assert statuses[-1] == "ok"  # but it CONVERGES — not perpetual partial
    assert all(
        _outcome(session, f"tok-converge-{i:02d}").final for i in range(n)
    )


# --- second review: NEW-B3 the commit itself must be pinned, not inferred ---

def test_batched_pass_real_commit_count_via_after_commit_listener(session):
    """NEW-B3 fix. `batches_committed` is incremented from chunk iteration,
    not from an actual `session.commit()` call — replacing the real
    `session.commit()` in the batch loop with `pass` left every prior test in
    this file green, including the `batches_committed == 3` assertion, which
    can never detect a missing commit because it counts LOOP ITERATIONS, not
    commits. A SQLAlchemy `after_commit` event listener is the only way to
    observe a REAL commit. Expected real commits for 23 tokens / batch_size
    10: 1 (run-row creation) + 3 (batches) + 1 (finalize) = 5."""
    from sqlalchemy import event

    for i in range(23):
        _mint(session, f"tok-realcommit-{i}", born_hours_ago=30, liquidity=10_000.0)
    commits: list[int] = []

    @event.listens_for(session, "after_commit")
    def _count(sess):
        commits.append(1)

    try:
        r = run_scheduled_reconciliation(
            session, settings=_settings(crypto_tape_reconciler_limit=1000),
            batch_size=10,
        )
    finally:
        event.remove(session, "after_commit", _count)

    assert r["status"] == "ok"
    assert r["batches_committed"] == 3  # ceil(23 / 10)
    assert len(commits) == 5  # run row + 3 batches + finalize — REAL commits


def test_post_batch_yield_sleeps_the_named_duration_after_each_real_commit(session):
    """NEW-H1 fix (third re-review). A short sleep AFTER each real batch
    commit gives a waiting competing writer a genuine chance to win the lock
    race — see `RECONCILE_POST_BATCH_YIELD_SECONDS`. This pins that it is
    invoked exactly once per REAL committed batch (not per dry-run
    "batch", which never commits) with exactly the named constant's value,
    via an injected sleeper — no wall-clock timing dependency."""
    from app.services.crypto_tape import RECONCILE_POST_BATCH_YIELD_SECONDS

    for i in range(23):
        _mint(session, f"tok-yield-{i}", born_hours_ago=30, liquidity=10_000.0)

    sleeps: list[float] = []

    def _sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    r = run_scheduled_reconciliation(
        session, settings=_settings(crypto_tape_reconciler_limit=1000),
        batch_size=10, sleeper=_sleeper,
    )
    assert r["status"] == "ok"
    assert r["batches_committed"] == 3  # ceil(23 / 10)
    yield_sleeps = [s for s in sleeps if s == RECONCILE_POST_BATCH_YIELD_SECONDS]
    assert len(yield_sleeps) == 3  # exactly one per real committed batch


def test_dry_run_batches_committed_reports_zero_real_commits(session):
    """NEW-M2 fix. In CHUNKED dry-run mode `session.commit()` is never
    called (guarded by `if not dry_run` in the batch loop) — a dry run must
    never write. `batches_committed` used to be incremented from the loop
    ITERATION regardless of `dry_run`, so a truncated-by-nothing dry run over
    23 tokens / batch_size 10 reported `batches_committed=3` while a real
    `after_commit` listener observed ZERO commits — indistinguishable from a
    real pass in the CLI output. Grounded: `batches_committed` must be 0 for
    any dry run, verified against the SAME listener used above."""
    from sqlalchemy import event

    for i in range(23):
        _mint(session, f"tok-drycommit-{i}", born_hours_ago=30, liquidity=10_000.0)
    commits: list[int] = []

    @event.listens_for(session, "after_commit")
    def _count(sess):
        commits.append(1)

    try:
        r = run_scheduled_reconciliation(
            session, settings=_settings(crypto_tape_reconciler_limit=1000),
            batch_size=10, dry_run=True,
        )
    finally:
        event.remove(session, "after_commit", _count)

    assert r["status"] == "dry_run"
    assert r["tokens_processed"] == 23
    assert len(commits) == 0  # dry-run NEVER commits — the ground truth
    assert r["batches_committed"] == 0  # must match the ground truth, not 3


def test_deadline_stopped_batch_commits_are_durable_across_a_fresh_session(tmp_path):
    """NEW-B3 durability fix. A batch that has "committed" must actually be
    durable on disk, not merely flushed into the current ORM session's
    identity map. This uses a real file-backed SQLite DB: one worker session
    runs a deadline-stopped batched pass, is explicitly rolled back (proving
    nothing UNCOMMITTED survives), and then a completely FRESH
    session/connection reads the committed rows back — the only way to prove
    they were truly committed to disk, not just held in session-local state
    (e.g. a `flush()`-only bug, or a connection/pool quirk that makes a
    single-process check misleadingly pass).

    Note on scope: this test's `max_duration_seconds=0.0` stop happens after
    exactly one batch, and the pass's own finalize commit (which commits
    unconditionally, success or "partial") follows immediately after — so
    this test alone cannot distinguish "the per-batch commit is real" from
    "only the trailing finalize commit is real" (a finalize commit flushes
    ALL still-pending work in one shot, masking a missing per-batch commit).
    That specific mutation — replacing the per-batch `session.commit()` with
    `pass` — is what
    `test_batched_pass_real_commit_count_via_after_commit_listener` pins,
    via real commit-event counting, which IS sensitive to exactly that."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "durability.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)

    seed_session = Factory()
    for i in range(9):
        _mint(seed_session, f"tok-durable-{i}", born_hours_ago=30, liquidity=10_000.0)
    seed_session.commit()
    seed_session.close()

    worker_session = Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=tmp_path))
    r = rec.run_once(
        worker_session, limit=100, hours=48, batch_size=3,
        max_duration_seconds=0.0,  # already past due before batch 2
    )
    assert r["status"] == "partial"
    assert r["stop_reason"] == "deadline"
    processed = r["tokens_processed"]
    assert 0 < processed < 9
    worker_session.rollback()  # discard anything NOT actually committed
    worker_session.close()

    fresh_session = Factory()
    try:
        persisted = fresh_session.query(CryptoTokenSurvivalOutcome).count()
        assert persisted == processed  # the committed batch survived, durably
    finally:
        fresh_session.close()
    engine.dispose()


# --- second review: cheap fix #2 — finalize-failure must append, not replace -

def test_finalize_failure_appends_to_an_existing_stop_reason_error(session, monkeypatch):
    """Cheap fix #2. `_prepare_finalize` failing to acquire the lock after a
    deadline stop must APPEND its own message to the existing "N batches / X
    of Y tokens" data-shortfall error, not silently overwrite (and thereby
    discard) it — they are two different, both real pieces of information."""
    for i in range(6):
        _mint(session, f"tok-finalize-{i}", born_hours_ago=30, liquidity=10_000.0)

    real_commit = type(session).commit
    calls = {"n": 0}

    def flaky_commit(self):
        calls["n"] += 1
        # let every batch commit succeed, then fail every finalize attempt
        if calls["n"] <= 2:
            return real_commit(self)
        raise OperationalError("UPDATE ...", {}, Exception("database is locked"))

    monkeypatch.setattr(type(session), "commit", flaky_commit)
    rec = CryptoLifecycleTapeRecorder()
    r = rec.run_once(
        session, limit=100, hours=48, batch_size=2, max_duration_seconds=0.0,
        max_lock_attempts=2, lock_retry_seconds=0.0,
        sleeper=lambda seconds: None,
    )
    assert r["status"] == "partial"
    assert "batch(es)" in r["error"]  # the original data-shortfall message
    assert "finalize commit could not acquire the lock" in r["error"]  # appended


# --- original review H1: a truncated dry run must not look complete ---------

def test_dry_run_stopped_by_the_deadline_reports_dry_run_partial_not_dry_run(session):
    """H1 fix. A dry-run probe truncated by the internal wall-clock deadline
    must not report plain `status="dry_run"` — that is indistinguishable from
    a COMPLETE probe to every caller, including the CLI exit code, which is
    exactly the failure the original review reproduced: `status=dry_run
    tokens_considered=5 universe_size=20 omitted=15 stop_reason=deadline` ->
    CLI exit 0."""
    for i in range(9):
        _mint(session, f"tok-dry-deadline-{i}", born_hours_ago=30, liquidity=10_000.0)
    rec = CryptoLifecycleTapeRecorder()
    r = rec.run_once(
        session, limit=100, hours=48, dry_run=True, batch_size=3,
        max_duration_seconds=0.0,  # already past due before batch 2
    )
    assert r["status"] == "dry_run_partial"
    assert r["stop_reason"] == "deadline"
    assert 0 < r["tokens_processed"] < 9
    assert "dry-run probe stopped early" in r["error"]
    # dry-run never writes, regardless of status
    assert session.query(CryptoTokenSurvivalOutcome).count() == 0


def test_dry_run_outcomes_updated_excludes_already_final_tokens(session):
    """NEW-M3 fix. In the REAL (non-dry-run) branch an already-final outcome
    is never counted in `outcomes_updated` (`if not outcome.final: ...
    outcomes += 1`). The dry-run branch used to count unconditionally
    (`elif dry_run: outcomes += 1`), overstating `outcomes_updated` for every
    already-final token a dry-run probe merely re-examined. Harmless on the
    SCHEDULED path only because `exclude_final=True` already removes those
    tokens from selection; the MANUAL path (`run_once` without
    `exclude_final`, exercised directly here) has no such protection."""
    born = _mint(session, "tok-m3-final", born_hours_ago=40, liquidity=10_000.0)
    _tick_at(session, "tok-m3-final", born + timedelta(hours=24), liquidity=9_000.0)

    rec = CryptoLifecycleTapeRecorder()
    real = rec.run_once(session, limit=25, hours=48)
    assert real["status"] == "ok"
    assert _outcome(session, "tok-m3-final").final is True

    probe = rec.run_once(session, limit=25, hours=48, dry_run=True)
    assert probe["status"] == "dry_run"
    assert probe["tokens_considered"] == 1  # manual path still re-examines it
    assert probe["outcomes_updated"] == 0  # but nothing NEW was learned


def test_cli_dry_run_stopped_by_the_deadline_does_not_exit_0(session, monkeypatch):
    """H1 fix, CLI-level. `crypto-tape-reconcile --dry-run` truncated by the
    deadline must exit non-zero, not 0 — a caller relying on the exit code
    (the exact instrument used for pre-activation validation) must never see
    a truncated dry run report success."""
    import app.services.crypto_tape as tape_mod
    from app import cli

    for i in range(9):
        _mint(session, f"tok-cli-dry-deadline-{i}", born_hours_ago=30, liquidity=10_000.0)

    real = tape_mod.run_scheduled_reconciliation

    def wrapped(sess, *a, **kw):
        kw.setdefault("batch_size", 3)
        kw.setdefault("max_duration_seconds", 0.0)
        return real(sess, *a, **kw)

    monkeypatch.setattr(tape_mod, "run_scheduled_reconciliation", wrapped)
    n = await_or_call(
        cli.crypto_tape_reconcile(dry_run=True, force=True, session=session)
    )
    assert n == -1


def await_or_call(coro):
    """Small helper so this module's synchronous-looking test bodies can
    drive the one async CLI call they need without every test in the file
    needing pytest-asyncio's implicit-async collection quirks."""
    import asyncio

    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# --- second review cheap fix #1: crypto-tape-run-once terminal-status exit --

def test_cli_run_once_under_a_held_overlap_lock_does_not_exit_0(
    session, tmp_path, monkeypatch, capsys,
):
    """Cheap fix #1. `crypto-tape-run-once` (the plain, non exact-cycle CLI
    path) mapped every terminal status to `tokens_considered`, so
    `status=skipped_overlap` (tokens_considered=0) exited 0 — indistinguishable
    from "nothing was in the window". It must exit non-zero and print the
    `error` field, matching the sibling `crypto-tape-reconcile` command,
    which already got this right."""
    from app import cli
    from app.services.crypto_tape import (
        CryptoLifecycleTapeRecorder,
        CryptoTapeConfig,
        _reconcile_overlap_lock,
    )

    _mint(session, "tok-cli-overlap", born_hours_ago=30, liquidity=10_000.0)

    # crypto_tape_run_once constructs a default-config recorder; patch the
    # class the CLI module resolves at call time so it uses THIS test's
    # lock_dir (matching the flock this test itself holds below).
    def _locked_recorder(*_a, **_kw):
        return CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=tmp_path))

    import app.services.crypto_tape as tape_mod

    monkeypatch.setattr(tape_mod, "CryptoLifecycleTapeRecorder", _locked_recorder)

    with _reconcile_overlap_lock(tmp_path, CHAIN) as acquired:
        assert acquired is True
        n = await_or_call(cli.crypto_tape_run_once(session=session))

    assert n == -1
    out = capsys.readouterr().out
    assert "status=skipped_overlap" in out
    assert "error:" in out


# --- third review LOW-3: zero committed batches must not be "partial" -------

def test_zero_batches_committed_is_skipped_contention_not_partial(
    session, monkeypatch,
):
    """LOW-3. If the very FIRST token batch exhausts its own commit retry
    ladder before anything is written, the pass must not report
    `status="partial"` with a message claiming "already-committed batches
    are durable" — describing zero batches. `batches_committed == 0` is
    functionally the same "nothing was written this pass" shape as the
    run-row-creation contention failure (which already reports
    `skipped_contention`); label it the same way."""
    for i in range(6):
        _mint(session, f"tok-lowe3-{i}", born_hours_ago=30, liquidity=10_000.0)

    real_commit = type(session).commit
    calls = {"n": 0}

    def flaky_commit(self):
        calls["n"] += 1
        # call 1 = run-row creation (real); calls 2-3 = batch 1's 2-attempt
        # retry ladder, both locked; call 4+ = finalize, real (isolates this
        # test to the batch-1-never-committed case, not a finalize failure).
        if calls["n"] in (2, 3):
            raise OperationalError("INSERT ...", {}, Exception("database is locked"))
        return real_commit(self)

    monkeypatch.setattr(type(session), "commit", flaky_commit)
    rec = CryptoLifecycleTapeRecorder()
    r = rec.run_once(
        session, limit=100, hours=48, batch_size=2,
        max_lock_attempts=2, lock_retry_seconds=0.0,
        sleeper=lambda seconds: None,
    )
    assert r["status"] == "skipped_contention"
    assert r["batches_committed"] == 0
    assert "nothing was reconciled" in r["error"]
    assert "already-committed batches are durable" not in r["error"]


# --- third review NEW-M4: the overlap lock must not touch the real DB dir ---

def test_overlap_lock_dir_is_isolated_from_a_real_sqlite_database_url():
    """NEW-M4 regression pin. `_resolve_lock_dir` derives the overlap lock's
    directory from `settings.database_url`. On a host where `.env` sets a
    real sqlite path (production, EVO, some CI configurations), a Settings
    object this file's `_settings()` helper builds — which never overrides
    `database_url` — would otherwise resolve the lock to that REAL data
    directory: running this suite there would take the SAME
    `.crypto-tape-reconcile-{chain}.lock` file the production timer uses.
    `tests/conftest.py::_isolate_crypto_tape_overlap_lock` (autouse,
    session-scoped per NEW-M1) must force it into an isolated tmp dir
    instead; this proves that isolation is actually ACTIVE during test
    collection, not just present in conftest.py's source and silently not
    wired up. (Session-scoped: the isolated dir is stable across the whole
    run, not per-test, so this compares against a second `_resolve_lock_dir`
    call rather than a hardcoded per-test `tmp_path`.)"""
    from app.services.crypto_tape import _resolve_lock_dir

    baseline = _resolve_lock_dir(None)
    # shaped exactly like a real deployed .env would set it (relative sqlite
    # path under the repo's data/ directory) — the case NEW-M4 measured.
    fake_prod_settings = Settings(
        database_url="sqlite:///./data/probability_arena.db"
    )
    resolved = _resolve_lock_dir(fake_prod_settings)
    assert resolved == baseline  # same patched dir regardless of settings
    assert "data" not in resolved.parts


# --- MEDIUM fix: an unopenable lock file must be a typed status, not a
# traceback --------------------------------------------------------------

def test_unopenable_lock_directory_reports_lock_unavailable_not_a_traceback(
    session, monkeypatch, tmp_path,
):
    """MEDIUM fix. `_reconcile_overlap_lock` used to let a failed `os.open`
    (unwritable/missing lock directory) propagate as a raw `OSError`, which
    `run_scheduled_reconciliation` re-raised straight through — an
    unwritable DB directory on a production host would crash the unit
    instead of producing a typed, refused status the CLI/journal can act on.
    This forces the lock FILE PATH itself to collide with a pre-existing
    DIRECTORY (a portable way to make `os.open` fail with EISDIR without
    relying on filesystem permission semantics that differ under a
    root/CI user) and asserts a `lock_unavailable` status, not an
    exception."""
    import app.services.crypto_tape as tape_mod

    _mint(session, "tok-lockunavailable", born_hours_ago=30, liquidity=10_000.0)

    lock_dir = tmp_path / "lock-dir"
    lock_dir.mkdir()
    # pre-create a DIRECTORY at the exact path the lock file would open —
    # os.open(..., O_CREAT | O_RDWR) on a directory raises OSError (EISDIR)
    # on every POSIX platform, regardless of user/permissions.
    (lock_dir / tape_mod.RECONCILE_LOCK_FILENAME.format(chain=CHAIN)).mkdir()
    monkeypatch.setattr(tape_mod, "_resolve_lock_dir", lambda settings=None: lock_dir)

    r = run_scheduled_reconciliation(session, settings=_settings())
    assert r["status"] == "lock_unavailable"
    assert r["error"]
    assert session.query(CryptoTokenSurvivalOutcome).count() == 0  # nothing written


def test_integrity_error_reports_concurrent_write_conflict_not_a_traceback(
    session, monkeypatch,
):
    """NEW-H3 fix (third re-review). The exact-cycle anchor feed
    (`record_discovery_run`) deliberately opts out of the B4 overlap lock
    (`use_overlap_lock=False`), so the overlap lock does NOT by itself close
    a race against it — HIGH-1's age-disjointness is the real mitigation,
    and this is the residual safety net if that mitigation is ever defeated.
    A real IntegrityError (unique-constraint violation on
    `ix_crypto_birth_chain_token`) is NOT an OperationalError, so the
    DB_LOCKED_* retry ladder does not apply to it — before this fix it
    propagated straight past `run_scheduled_reconciliation`'s error handling
    as an uncaught exception, which would kill the systemd unit with a
    traceback instead of a typed, journal-visible refused status."""
    from sqlalchemy.exc import IntegrityError

    _mint(session, "tok-integrityrace", born_hours_ago=30, liquidity=10_000.0)

    real_commit = type(session).commit

    def racing_commit(self):
        raise IntegrityError(
            "INSERT INTO crypto_token_birth_events ...", {},
            Exception(
                "UNIQUE constraint failed: crypto_token_birth_events.chain, "
                "crypto_token_birth_events.token_address"
            ),
        )

    monkeypatch.setattr(type(session), "commit", racing_commit)

    r = run_scheduled_reconciliation(session, settings=_settings())
    assert r["status"] == "concurrent_write_conflict"
    assert r["error"]
    assert "unique-constraint" in r["error"].lower() or "UNIQUE" in r["error"]

    # the session must be usable afterwards (proves rollback happened) —
    # restore the real commit and confirm a normal pass still works.
    monkeypatch.setattr(type(session), "commit", real_commit)
    r2 = run_scheduled_reconciliation(session, settings=_settings())
    assert r2["status"] == "ok"


def test_systemd_unit_timeout_start_sec_matches_the_computed_worst_case():
    """MEDIUM fix (third re-review, M2). `TimeoutStartSec` on the reconcile
    unit must be a value derived from the ACTUAL worst-case retry-ladder
    math (deadline + one in-flight batch's retry ladder + the finalize
    retry ladder, all at the real 30s busy_timeout — see the unit file's own
    comment), not an arbitrary round number. Pinned so a future edit cannot
    silently drift the timeout below its own justification without touching
    this test."""
    unit_path = (
        REPO / "infra" / "systemd" / "user"
        / "probability-arena-crypto-reconcile.service"
    )
    text = unit_path.read_text()
    assert "TimeoutStartSec=5min" in text
    # the derivation itself must still be documented, not just the number
    assert "~212s" in text or "212s" in text
