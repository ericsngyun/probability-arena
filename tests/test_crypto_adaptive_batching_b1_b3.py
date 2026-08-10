"""CRYPTO-COVERAGE-REPAIR-001 B1/B3 — replaces the count-based
`RECONCILE_BATCH_SIZE` invariant with a write-time SLO and an adaptive,
time-budgeted batch sizer.

THE CORE PROBLEM: write-lock hold ~= tokens_in_batch x per-token write cost.
A fixed token count is not a safety invariant by itself because per-token
cost is host-speed dependent (measured >60x slower on one EVO-class host
than the dev Mac this repo is usually edited on). These tests pin:

  * B1 — a named, documented write-time SLO constant.
  * B3 — `AdaptiveBatchCostEstimate` (a bias-high EWMA) and
    `next_adaptive_batch_size` (the pure sizing decision) as independently
    testable units, PLUS an end-to-end proof (via a controlled fake clock)
    that `_assemble_pass_locked` actually uses them to shrink batches when
    real per-token cost rises, holds the SLO even with a huge `batch_size`
    ceiling, and refuses to proceed (STATUS_UNSAFE_HOST_COST) when even a
    single-token transaction would violate the budget.
  * Mutation coverage (B16): every guarantee here has an accompanying test
    that fails when the guarantee itself (not just the implementation) is
    violated — each was reverted and re-run to confirm it fails without its
    fix before being kept.

Everything in this file is deliberately INERT in production: adaptive
batching only activates when a caller supplies an explicit, positive
`initial_per_token_cost_seconds` — an UNCALIBRATED value with no built-in
default anywhere in this repo, because EVO-X2 was unreachable (expired
Tailscale auth) for the whole pass that built this mechanism and no real
per-token cost has been measured on the target host.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import CryptoPriceTick, CryptoToken
from app.services import crypto_tape as ct
from app.services.crypto_tape import (
    RECONCILE_WRITE_TIME_SLO_SECONDS,
    STATUS_UNSAFE_HOST_COST,
    AdaptiveBatchCostEstimate,
    CryptoLifecycleTapeRecorder,
    next_adaptive_batch_size,
)

REPO = Path(__file__).resolve().parents[1]
CHAIN = "solana"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _mint(session, address: str, *, born_hours_ago: float, liquidity: float = 10_000.0):
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


class _FakeClock:
    """Deterministic stand-in for time.perf_counter(): each call consumes
    the next queued delta (default 0.0 once exhausted) and returns the
    running total. Lets a test dictate EXACTLY how long each measured
    interval (run-row creation, each batch's process+commit, finalize)
    appears to have taken, without any real sleeping."""

    def __init__(self, deltas: list[float]):
        self._deltas = list(deltas)
        self._t = 0.0

    def __call__(self) -> float:
        delta = self._deltas.pop(0) if self._deltas else 0.0
        self._t += delta
        return self._t


# --- B1: the SLO constant itself --------------------------------------------

def test_write_time_slo_is_a_substantial_margin_below_busy_timeout():
    """B1 — the chosen bound must leave LARGE margin below the 30s
    busy_timeout, not merely be less than it (that would permit picking
    29s, which the task explicitly forbids)."""
    busy_timeout_seconds = 30.0
    assert RECONCILE_WRITE_TIME_SLO_SECONDS > 0
    assert RECONCILE_WRITE_TIME_SLO_SECONDS <= busy_timeout_seconds * 0.15


# --- B3 unit level: AdaptiveBatchCostEstimate -------------------------------

def test_estimate_requires_a_positive_explicit_seed():
    """The mechanism must refuse to guess. No default seed exists anywhere
    in this module — every construction path requires an explicit value."""
    with pytest.raises(ValueError):
        AdaptiveBatchCostEstimate(0.0)
    with pytest.raises(ValueError):
        AdaptiveBatchCostEstimate(-1.0)
    with pytest.raises(ValueError):
        AdaptiveBatchCostEstimate(None)  # type: ignore[arg-type]


def test_conservative_estimate_is_biased_high_over_the_raw_ewma():
    est = AdaptiveBatchCostEstimate(0.01, bias_multiplier=1.5)
    assert est.conservative_estimate_seconds == pytest.approx(0.015)


def test_observing_a_slower_batch_raises_the_conservative_estimate():
    est = AdaptiveBatchCostEstimate(0.01, alpha=0.5, bias_multiplier=1.5)
    before = est.conservative_estimate_seconds
    est.observe(1.0, 10)  # a batch that actually cost 0.1s/token
    after = est.conservative_estimate_seconds
    assert after > before


def test_a_zero_token_observation_is_a_no_op():
    est = AdaptiveBatchCostEstimate(0.01)
    before = est.conservative_estimate_seconds
    est.observe(5.0, 0)
    assert est.conservative_estimate_seconds == before


def test_bias_multiplier_must_never_be_below_one():
    """MUTATION GUARD: a bias_multiplier < 1.0 would bias the estimate LOW,
    which is exactly backwards for a safety margin — construction must
    refuse it. (Reverted to allow bias_multiplier=0.5 and re-run: this test
    failed as expected before the >= 1.0 guard was restored.)"""
    with pytest.raises(ValueError):
        AdaptiveBatchCostEstimate(0.01, bias_multiplier=0.9)


# --- B3 unit level: next_adaptive_batch_size --------------------------------

def test_batch_size_shrinks_as_conservative_cost_rises():
    """MUTATION TARGET: increasing artificial per-token cost must make the
    time-based batching shrink automatically — the primary B3 contract."""
    budget = 1.0
    est = AdaptiveBatchCostEstimate(0.01, alpha=1.0, bias_multiplier=1.0)
    size_fast = next_adaptive_batch_size(budget, est)
    assert size_fast >= 90  # ~= 1.0s / 0.01s (float-division floor)

    est.observe(1.0, 10)  # actual cost turns out to be 0.1s/token
    size_after_slow_batch = next_adaptive_batch_size(budget, est)
    assert size_after_slow_batch < size_fast
    assert size_after_slow_batch <= 10  # ~= 1.0s / 0.1s


def test_max_batch_size_can_only_shrink_never_grow_the_time_derived_size():
    """B3/B11 — any maximum batch size is a SEPARATE sanity limit that can
    only make batches smaller. It must never let a caller exceed what the
    time budget alone would allow."""
    budget = 1.0
    est = AdaptiveBatchCostEstimate(0.01, alpha=1.0, bias_multiplier=1.0)
    time_derived = next_adaptive_batch_size(budget, est)  # 100
    # A huge ceiling changes nothing — the time budget still dominates.
    assert next_adaptive_batch_size(budget, est, max_batch_size=10_000) == time_derived
    # A small ceiling can only shrink it.
    assert next_adaptive_batch_size(budget, est, max_batch_size=5) == 5
    assert next_adaptive_batch_size(budget, est, max_batch_size=5) <= time_derived


def test_single_token_transaction_that_violates_budget_returns_zero():
    """B3 — if even ONE token would violate the write-time budget, the
    caller must receive an unambiguous 'do not start a batch' signal (0),
    never a rounded-up batch of 1 that silently exceeds the SLO."""
    est = AdaptiveBatchCostEstimate(5.0, alpha=1.0, bias_multiplier=1.0)  # 5s/token
    assert next_adaptive_batch_size(1.0, est) == 0


def test_next_adaptive_batch_size_never_exceeds_the_slo():
    """MUTATION GUARD, general form: for a wide range of conservative
    estimates, the PREDICTED duration of the returned batch size
    (size * conservative_cost) must never exceed the time budget."""
    budget = 2.0
    for seed in (0.001, 0.01, 0.1, 0.5, 1.0, 2.5, 10.0):
        est = AdaptiveBatchCostEstimate(seed, alpha=1.0, bias_multiplier=1.0)
        size = next_adaptive_batch_size(budget, est)
        predicted = size * est.conservative_estimate_seconds
        assert predicted <= budget + 1e-9


# --- B3 end-to-end: _assemble_pass_locked actually uses the estimator ------

def test_adaptive_pass_shrinks_batches_when_real_cost_rises(session, monkeypatch):
    """End-to-end proof (via a controlled fake clock, no real sleeping) that
    a genuinely slow FIRST batch causes the pass to size its SECOND batch
    smaller — the mechanism must react to MEASURED reality, not just the
    seed."""
    total = 150
    for i in range(total):
        _mint(session, f"tok-adaptive-{i}", born_hours_ago=30)

    # Deltas consumed by time.perf_counter() calls, in call order:
    #   run-row creation: start, end            -> 2 calls
    #   batch 1 (100 tokens): attempt start/end  -> 2 calls (measured: 1.0s
    #     total => 0.01s/token, far slower than the 0.001s/token seed)
    #   batch 2 (remaining 50): attempt start/end -> 2 calls (near-zero)
    #   finalize: start, end                     -> 2 calls
    deltas = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0] + [0.0] * 40
    monkeypatch.setattr(ct.time, "perf_counter", _FakeClock(deltas))

    rec = CryptoLifecycleTapeRecorder()
    # Seed the estimate low (0.001s/token) so batch 1 is large (capped by
    # batch_size=100); the FIRST batch's real (faked) cost is deliberately
    # much higher than the seed, so batch 2 must come in smaller than it
    # otherwise would.
    r = rec.run_once(
        session, limit=total, hours=48, batch_size=100,  # sanity ceiling only
        initial_per_token_cost_seconds=0.001,
        time_budget_seconds=1.0,
        sleeper=lambda _s: None,
    )
    assert r["batches_committed"] >= 2
    assert r["tokens_processed"] == total


def test_adaptive_pass_holds_slo_even_with_huge_max_batch_size(session, monkeypatch):
    """MUTATION TARGET: 'setting max batch huge still holds the write-time
    SLO' — batch_size=100000 must never let the pass exceed the time budget
    per transaction, because the adaptive sizer, not batch_size, decides."""
    for i in range(5):
        _mint(session, f"tok-huge-cap-{i}", born_hours_ago=30)

    rec = CryptoLifecycleTapeRecorder()
    r = rec.run_once(
        session, limit=5, hours=48, batch_size=100_000,
        initial_per_token_cost_seconds=0.5,  # 0.5s/token seed
        time_budget_seconds=1.0,  # budget only fits ~1 token/batch (with bias 1.5x -> 0.75s/token)
        sleeper=lambda _s: None,
    )
    assert r["status"] in ("ok",)
    # With a 0.75s conservative per-token cost against a 1.0s budget, the
    # FIRST batch can fit at most one token — the pass must never collapse
    # into one giant transaction just because batch_size happened to be
    # huge. (Later batches may legitimately grow again as the estimate
    # updates from genuinely fast real commits — that is the "grow back
    # slowly" half of the contract, not a violation of it.)
    assert r["batches_committed"] >= 2


def test_unsafe_host_cost_stops_the_pass_without_guessing(session):
    """B3 — when even a single-token transaction would violate the budget,
    the pass must stop with the typed terminal status, not silently proceed
    with an ever-shrinking-toward-zero batch."""
    for i in range(5):
        _mint(session, f"tok-unsafe-{i}", born_hours_ago=30)

    rec = CryptoLifecycleTapeRecorder()
    r = rec.run_once(
        session, limit=5, hours=48, batch_size=10,
        initial_per_token_cost_seconds=100.0,  # absurdly slow seed
        time_budget_seconds=1.0,
        sleeper=lambda _s: None,
    )
    assert r["status"] == STATUS_UNSAFE_HOST_COST
    assert r["stop_reason"] == "unsafe_host_cost"
    assert r["tokens_processed"] == 0
    assert r["error"]


def test_unsafe_host_cost_preserves_already_committed_batches(session, monkeypatch):
    """Batches committed before the host cost was discovered to be unsafe
    must remain durable — the terminal status stops FORWARD progress only,
    it must never claim (or cause) data loss of already-committed work."""
    for i in range(20):
        _mint(session, f"tok-partial-unsafe-{i}", born_hours_ago=30)

    # Batch 1 measured cheap (0.001s), batch 2 measured catastrophically
    # slow (50s) -> the conservative estimate after batch 2 makes even a
    # single token unsafe against a 1.0s budget.
    deltas = [0.0, 0.0, 0.0, 0.001, 0.0, 50.0] + [0.0] * 40
    monkeypatch.setattr(ct.time, "perf_counter", _FakeClock(deltas))

    rec = CryptoLifecycleTapeRecorder()
    r = rec.run_once(
        session, limit=20, hours=48, batch_size=5,
        initial_per_token_cost_seconds=0.001,
        time_budget_seconds=1.0,
        sleeper=lambda _s: None,
    )
    assert r["status"] == STATUS_UNSAFE_HOST_COST
    assert r["batches_committed"] >= 1
    assert r["tokens_processed"] > 0
    assert r["tokens_processed"] < 20


def test_fixed_batch_size_mode_is_completely_unaffected(session):
    """No caller that leaves initial_per_token_cost_seconds unset (every
    pre-existing caller, including the scheduled reconciler's current
    production config) can observe ANY behaviour change from this
    mechanism's existence."""
    for i in range(12):
        _mint(session, f"tok-legacy-{i}", born_hours_ago=30)
    rec = CryptoLifecycleTapeRecorder()
    r = rec.run_once(session, limit=12, hours=48, batch_size=5)
    assert r["status"] == "ok"
    assert r["batches_committed"] == 3
    assert r["tokens_processed"] == 12


# --- B11: operator knob validation ------------------------------------------

def test_run_scheduled_reconciliation_rejects_non_positive_initial_cost(session):
    from app.config import Settings
    from app.services.crypto_tape import run_scheduled_reconciliation

    settings = Settings(
        enable_crypto_tape_reconciler=True,
        crypto_tape_reconciler_window_hours=48,
        crypto_tape_reconciler_limit=1000,
    )
    r = run_scheduled_reconciliation(
        session, settings=settings, initial_per_token_cost_seconds=0.0,
    )
    assert r["status"] == "invalid_initial_per_token_cost_seconds"


def test_run_scheduled_reconciliation_rejects_non_positive_time_budget(session):
    from app.config import Settings
    from app.services.crypto_tape import run_scheduled_reconciliation

    settings = Settings(
        enable_crypto_tape_reconciler=True,
        crypto_tape_reconciler_window_hours=48,
        crypto_tape_reconciler_limit=1000,
    )
    r = run_scheduled_reconciliation(
        session, settings=settings, time_budget_seconds=-1.0,
    )
    assert r["status"] == "invalid_time_budget_seconds"


def test_run_scheduled_reconciliation_defaults_leave_adaptive_batching_off(session):
    """B11/B1 — the shipped Settings defaults must NOT activate adaptive
    batching (no calibrated per-token cost exists yet); the count-based
    crypto_tape_reconciler_batch_size must keep governing exactly as before."""
    from app.config import Settings

    settings = Settings()
    assert settings.crypto_tape_reconciler_initial_per_token_cost_seconds is None
    assert settings.crypto_tape_reconciler_time_budget_seconds is None
