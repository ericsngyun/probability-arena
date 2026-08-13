"""CRYPTO-RECONCILER-GUARDED-TIMER-001 — the guard rails on the recurring
6-hourly reconciler timer.

WHAT THESE TESTS DEFEND, AND THE ONE RULE THEY WERE WRITTEN AGAINST: a circuit
breaker that cannot be shown to FIRE is decoration, and a circuit breaker that
fires on healthy traffic is worse than none. So every breaker here has both a
test that fires it and a test that proves it stays quiet on the shape a healthy
production pass actually has — which, at production density, is
`partial` + `stop_reason="deadline"` + `truncated` on EVERY pass.

The contention tests use a REAL second SQLite connection holding a REAL
RESERVED lock on a REAL file-backed database, following
test_crypto_reconciler_lock_wait_budget_001.py. Nothing here simulates
contention with a patched clock.

Where a threshold has to be crossed deterministically, the THRESHOLD is
lowered (monkeypatched) rather than the WAIT inflated: the shipped 5000/7000 ms
lines would otherwise cost 5-7 s of real blocking per test, and the previous
milestone deliberately cut this suite's lock-wait file from 239 s to 49 s.
Separate tests pin the shipped values so a lowered threshold in one test can
never be mistaken for the shipped policy.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base
from app.models import CryptoPriceTick, CryptoToken, CryptoTokenLifecycleRun
from app.services import crypto_reconciler_guard as guard
from app.services import crypto_tape as ct
from app.services.crypto_tape import run_scheduled_reconciliation

REPO = Path(__file__).resolve().parents[1]
CHAIN = "solana"
HOLDER_TIMEOUT_SECONDS = 0.2


# --- harness ---------------------------------------------------------------

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


class _FileDb:
    """A real, file-backed SQLite database. Required twice over here: the
    lock-wait meter needs real locks, and the health gate deliberately keeps
    NO state unless there is a real database file to co-locate it with."""

    def __init__(self, tmp_path: Path, *, tokens: int = 12):
        self.path = tmp_path / "guarded_timer.db"
        self.engine = create_engine(
            f"sqlite:///{self.path}", connect_args={"timeout": 5.0},
        )
        Base.metadata.create_all(self.engine)
        self.Factory = sessionmaker(bind=self.engine)
        seed = self.Factory()
        for i in range(tokens):
            _mint(seed, f"tok-gt-{i:03d}", born_hours_ago=30 + i)
        seed.commit()
        seed.close()

    @property
    def url(self) -> str:
        return f"sqlite:///{self.path}"

    def settings(self, **over) -> Settings:
        base = {
            "database_url": self.url,
            "enable_crypto_tape_reconciler": True,
            "crypto_tape_reconciler_window_hours": 48,
            "crypto_tape_reconciler_limit": 1000,
            "crypto_tape_reconciler_batch_size": 2,
        }
        base.update(over)
        return Settings(**base)

    def state_path(self) -> Path:
        return guard.health_state_path(self.url, CHAIN)

    def read_state(self) -> dict:
        return json.loads(self.state_path().read_text())

    def close(self):
        self.engine.dispose()


class _Holder:
    """An independent connection holding SQLite's RESERVED write lock until
    released."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), timeout=HOLDER_TIMEOUT_SECONDS)
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("UPDATE crypto_tokens SET last_seen_at = last_seen_at")

    def release(self):
        try:
            self.conn.rollback()
        finally:
            self.conn.close()


@pytest.fixture
def filedb(tmp_path):
    db = _FileDb(tmp_path)
    try:
        yield db
    finally:
        db.close()


def _record(**over) -> dict:
    """A recorded run, in the shape `guard.run_record` produces. The DEFAULT
    is the shape of a HEALTHY production pass: `partial` because the 20 s
    deadline stopped it, `truncated` work left for the next pass, no severe
    wait, nothing to review."""
    base = {
        "seq": 1,
        "status": "partial",
        "stop_reason": "deadline",
        "batch_lock_wait_ms_max": 1037,
        "batch_lock_wait_warnings": 0,
        "batch_lock_wait_aborts": 0,
        "write_hold_ms_max": 194,
        "write_hold_slo_violations": 0,
        "lock_retry_events": 1,
        "wall_time_model_exceeded": False,
        "review": False,
        "review_reasons": [],
    }
    base.update(over)
    return base


def _severe(**over) -> dict:
    return _record(batch_lock_wait_ms_max=5200, batch_lock_wait_warnings=1,
                   review=True, review_reasons=["batch_lock_wait_warning"], **over)


# ---------------------------------------------------------------------------
# 1. The thresholds are POLICY, and they are the shipped numbers.
# ---------------------------------------------------------------------------

def test_the_shipped_policy_thresholds_are_exactly_these_numbers():
    """A pin, not a tautology: several tests below LOWER these thresholds to
    fire a breaker deterministically, and without this pin a lowered value
    could silently become the shipped one."""
    assert guard.BATCH_LOCK_WAIT_REVIEW_MS == 2000
    assert guard.BATCH_LOCK_WAIT_WARN_MS == 5000
    assert guard.BATCH_LOCK_WAIT_ABORT_MS == 7000
    assert guard.WRITE_HOLD_REVIEW_MS == 700
    assert guard.HEALTH_WINDOW_RUNS == 4
    assert guard.HEALTH_SEVERE_WAIT_RUNS == 2
    assert guard.HEALTH_CONSECUTIVE_CONTENTION_RUNS == 3
    assert guard.HEALTH_CONSECUTIVE_MARKETOPS_SKIP_RUNS == 2
    assert guard.HEALTH_REVIEW_RUNS == 3


def test_every_threshold_is_labelled_chosen_policy_not_a_measurement():
    """The same defence `RECONCILE_LOCK_WAIT_STATEMENT_OVERSHOOT` has: a later
    edit must not be able to quietly re-promote a chosen number to a measured
    one. Every policy constant's comment block must say so."""
    source = (REPO / "app/services/crypto_reconciler_guard.py").read_text()
    assert "OPERATIONAL POLICY CHOICE" in source
    assert "Not one" in source and "derived or measured constant" in source
    # Each rule states its own chosen-ness, so a reader who lands on one
    # constant does not have to find the module header to learn it.
    assert source.count("Chosen.") >= 4
    assert "CHOSEN OPERATIONAL POLICY, NOT A MEASUREMENT." in source


@pytest.mark.parametrize("ms,expected", [
    (0, None), (4999, None),
    (5000, guard.ESCALATION_WARNING), (6999, guard.ESCALATION_WARNING),
    (7000, guard.ESCALATION_ABORT), (30000, guard.ESCALATION_ABORT),
    (None, None),
])
def test_batch_lock_wait_escalation_boundaries(ms, expected):
    assert guard.batch_lock_wait_escalation(ms) is expected


def test_the_escalation_reads_the_batch_phase_never_the_pass_total():
    """The whole point of phase attribution: `run_row` and `finalize` are
    once-per-pass instrument events, so a sample from either must never be
    classified as contention however large it is."""
    accounting = ct.LockWaitAccounting()

    class _FakeMeter:
        def __init__(self, phase, seconds):
            self.phase = phase
            self.lock_wait_seconds = seconds
            self.hold_seconds = 0.0

    assert accounting.record(_FakeMeter(ct.LOCK_WAIT_PHASE_RUN_ROW, 30.0)) is None
    assert accounting.record(_FakeMeter(ct.LOCK_WAIT_PHASE_FINALIZE, 30.0)) is None
    assert accounting.batch_lock_wait_aborts == 0
    assert accounting.record(_FakeMeter(ct.LOCK_WAIT_PHASE_BATCH, 8.0)) == (
        guard.ESCALATION_ABORT
    )
    assert accounting.batch_lock_wait_aborts == 1
    assert accounting.as_summary()["batch_lock_wait_abort_ms"] == 8000
    # The pass total still carries all three, unchanged.
    assert accounting.as_summary()["lock_wait_measurements"] == 3


# ---------------------------------------------------------------------------
# 2. The per-run breaker fires — and stays quiet on a healthy pass.
# ---------------------------------------------------------------------------

def test_a_healthy_pass_at_the_shipped_thresholds_never_aborts(filedb):
    """The measured production shape: batch maxima 16-1037 ms, zero lock
    failures. At the shipped 5000/7000 ms lines that must be a quiet run."""
    session = filedb.Factory()
    try:
        r = run_scheduled_reconciliation(session, settings=filedb.settings())
    finally:
        session.close()
    assert r["status"] in ("ok", "partial", "truncated", "backlog_expiring")
    assert r["batch_lock_wait_aborts"] == 0
    assert r["batch_lock_wait_warnings"] == 0
    assert r["external_calls"] == 0
    assert r["stop_reason"] != "lock_wait_budget"
    assert r["health_gate_tripped"] is False


def test_a_committed_batch_past_the_abort_line_stops_the_pass_and_keeps_its_work(
    filedb, monkeypatch
):
    """The committed-but-slow shape. The batch that crossed the line COMMITTED
    (rolling it back would destroy work already paid for); what must not happen
    is another batch being started into that host. Prior batches stay durable
    and the typed status is the EXISTING lock_wait_budget one — no second
    mechanism."""
    monkeypatch.setattr(guard, "BATCH_LOCK_WAIT_ABORT_MS", 0)
    monkeypatch.setattr(guard, "BATCH_LOCK_WAIT_WARN_MS", 0)
    session = filedb.Factory()
    try:
        r = run_scheduled_reconciliation(session, settings=filedb.settings())
    finally:
        session.close()
    assert r["stop_reason"] == "lock_wait_budget"
    assert r["status"] == "partial"
    assert r["batches_committed"] == 1, "exactly one batch, then the breaker"
    assert r["batch_lock_wait_aborts"] >= 1
    assert "abort line" in r["error"]
    assert "operational policy, not a measured constant" in r["error"]
    assert r["external_calls"] == 0
    # The committed batch is durable: its outcomes survived the stop.
    check = filedb.Factory()
    try:
        assert check.query(CryptoTokenLifecycleRun).count() == 1
    finally:
        check.close()


def test_a_blocked_batch_past_the_abort_line_stops_the_ladder_not_the_row(
    filedb, monkeypatch
):
    """The blocked shape, against a REAL RESERVED holder: the ladder stops
    instead of sleeping into the same holder again, only the current batch is
    rolled back, and the outcome is the existing typed contention vocabulary."""
    monkeypatch.setattr(guard, "BATCH_LOCK_WAIT_ABORT_MS", 0)
    monkeypatch.setattr(guard, "BATCH_LOCK_WAIT_WARN_MS", 0)
    session = filedb.Factory()
    holder = _Holder(filedb.path)
    try:
        r = run_scheduled_reconciliation(
            session, settings=filedb.settings(), max_duration_seconds=10.0,
        )
    finally:
        holder.release()
        session.close()
    assert r["status"] in ("skipped_contention", "partial", "db_locked")
    if r["status"] != "db_locked":
        # The run row itself may lose the race first (that is the pre-existing
        # `skipped_contention` path); when it does not, the breaker is what
        # stopped the ladder.
        assert r["stop_reason"] in ("lock_wait_budget", "contention")
    assert r["external_calls"] == 0
    assert r["review_required"] is True
    assert "lock_failure" in r["review_reasons"]


def test_the_abort_never_rolls_back_a_previously_committed_batch(filedb, monkeypatch):
    """Explicit, because it is the property the whole 'route into the existing
    expiry path' instruction rests on."""
    monkeypatch.setattr(guard, "BATCH_LOCK_WAIT_ABORT_MS", 0)
    session = filedb.Factory()
    try:
        r = run_scheduled_reconciliation(session, settings=filedb.settings())
        assert r["tokens_processed"] == 2  # one batch of batch_size=2
        assert r["outcomes_updated"] >= 1
    finally:
        session.close()
    check = filedb.Factory()
    try:
        rows = check.execute(select(CryptoTokenLifecycleRun)).scalars().all()
        assert len(rows) == 1
        assert rows[0].status in ("partial", "running")
    finally:
        check.close()


# ---------------------------------------------------------------------------
# 3. The rolling health gate's RULE.
# ---------------------------------------------------------------------------

def test_the_routine_production_pass_shape_never_trips_the_gate():
    """THE regression this rule was written against. At production density
    EVERY pass ends `partial`/`deadline`/`truncated`; a gate that read that as
    'consecutive partial outcomes' would trip on the first three healthy runs
    and be the 'alarm always on carries no information' failure this repo has
    already made once."""
    verdict = guard.evaluate_health([_record(seq=i) for i in range(1, 9)])
    assert verdict["tripped"] is False
    assert verdict["consecutive_contention_runs"] == 0


def test_one_bad_run_is_noise_two_severe_waits_are_a_trend():
    assert guard.evaluate_health([_record(seq=1), _severe(seq=2)])["tripped"] is False
    verdict = guard.evaluate_health([_record(seq=1), _severe(seq=2), _severe(seq=3)])
    assert verdict["tripped"] is True
    assert any("repeated severe batch lock waits" in r for r in verdict["reasons"])


def test_severe_waits_outside_the_window_do_not_trip():
    """Stateful, but BOUNDED: two severe runs five runs apart are not a trend
    at a 4-run window."""
    records = [_severe(seq=1)] + [_record(seq=i) for i in range(2, 6)] + [
        _severe(seq=6)
    ]
    assert guard.evaluate_health(records)["tripped"] is False


def test_three_consecutive_contention_stops_trip_but_two_do_not():
    contention = _record(status="partial", stop_reason="contention", review=True,
                         review_reasons=["lock_failure"])
    two = [_record(seq=1), contention | {"seq": 2}, contention | {"seq": 3}]
    assert guard.evaluate_health(two)["tripped"] is False
    three = two + [contention | {"seq": 4}]
    verdict = guard.evaluate_health(three)
    assert verdict["tripped"] is True
    assert any("sustained contention" in r for r in verdict["reasons"])
    assert verdict["consecutive_contention_runs"] == 3


def test_a_broken_contention_streak_does_not_trip():
    contention = _record(status="skipped_contention", stop_reason="contention")
    records = [
        contention | {"seq": 1}, contention | {"seq": 2},
        _record(seq=3), contention | {"seq": 4},
    ]
    assert guard.evaluate_health(records)["consecutive_contention_runs"] == 1


def test_two_consecutive_marketops_skips_trip_but_one_does_not():
    skip = _record(status="marketops_degraded", stop_reason=None)
    assert guard.evaluate_health([_record(seq=1), skip | {"seq": 2}])["tripped"] is False
    verdict = guard.evaluate_health(
        [_record(seq=1), skip | {"seq": 2}, skip | {"seq": 3}]
    )
    assert verdict["tripped"] is True
    assert any("MarketOps regression" in r for r in verdict["reasons"])


def test_three_reviewed_runs_in_the_window_trip_the_gate():
    """How the item-4 post-run triggers FEED the gate without any one of them
    disabling anything on its own."""
    reviewed = _record(review=True, review_reasons=["wall_time_model_exceeded"])
    records = [
        _record(seq=1), reviewed | {"seq": 2}, _record(seq=3),
        reviewed | {"seq": 4}, reviewed | {"seq": 5},
    ]
    verdict = guard.evaluate_health(records)
    assert verdict["tripped"] is True
    assert any("sustained review pressure" in r for r in verdict["reasons"])


# ---------------------------------------------------------------------------
# 3b. THE BLOCKER: sustained SUB-THRESHOLD degradation.
#
# An independent reviewer exercised `evaluate_health` as a pure function and
# found the gate blind below the 5000 ms cliff. The four sequences below are
# verbatim the ones that returned `tripped=False`; three of them must now trip,
# and the fourth is the STATED escape boundary (item C), asserted as policy so
# it can never become an accident.
# ---------------------------------------------------------------------------

def _evaluate_after_every_run(records: list[dict]) -> int | None:
    """The run index (1-based) at which the gate first trips, or None. An
    unattended timer evaluates after EVERY run, so a test that evaluates only
    the final history can miss a trip that already happened — and, worse, can
    miss one that never does."""
    for i in range(1, len(records) + 1):
        if guard.evaluate_health(records[:i])["tripped"]:
            return i
    return None


def _mid_band(ms: int, seq: int) -> dict:
    """A run whose worst batch wait is `ms`, marked exactly as the live path
    would mark it — via `guard.review_reasons`, never by hand, so the record
    and the shipped rule cannot drift apart."""
    summary = {
        "status": "partial", "stop_reason": "deadline",
        "batch_lock_wait_ms_max": ms, "write_hold_ms_max": 194,
        "wall_time_model_exceeded": False,
    }
    return guard.run_record(summary) | {"seq": seq}


def test_four_consecutive_runs_at_4999ms_now_trip_the_gate():
    """4,999 ms is ~5x the worst healthy batch wait ever observed (1,037 ms)
    and ~3,800x the healthy median. Before the fix this returned False."""
    records = [_mid_band(4999, i) for i in range(1, 5)]
    assert _evaluate_after_every_run(records) == 3
    verdict = guard.evaluate_health(records)
    assert verdict["tripped"] is True
    assert any("sustained review pressure" in r for r in verdict["reasons"])


def test_eight_consecutive_runs_at_4999ms_are_not_two_unattended_days_of_silence():
    """Eight runs at the 6-hourly cadence is two full days. Before the fix:
    False."""
    records = [_mid_band(4999, i) for i in range(1, 9)]
    assert _evaluate_after_every_run(records) == 3


def test_a_monotonic_climb_from_1s_to_49s_is_escalation_and_trips():
    """`1000 -> 2500 -> 3800 -> 4900` is escalation by any reading; before the
    fix it returned False because no rule existed below 5000 ms. The first run
    is inside the band Eric explicitly accepted and is NOT marked, so the trip
    lands on the fourth run (the third MARKED one)."""
    records = [
        _mid_band(1000, 1), _mid_band(2500, 2),
        _mid_band(3800, 3), _mid_band(4900, 4),
    ]
    assert records[0]["review"] is False, "1.0s is the accepted band, not a mark"
    assert [r["review"] for r in records[1:]] == [True, True, True]
    assert _evaluate_after_every_run(records) == 4


def test_the_50_percent_contention_escape_boundary_is_policy_not_an_accident(
):
    """ITEM C, pinned. A host on which half of every pass stops on contention,
    indefinitely, never latches on the contention rule — with waits inside the
    accepted band. That is a STATED policy line: every contended pass still
    commits durable batches and returns unworked tokens to the backlog, and
    each is individually marked for review, so it is REPORTED without being
    auto-disabled.

    The second half of the test is the other half of the policy: the same
    alternating pattern with waits past the review line DOES latch, which is
    what makes the boundary a choice about severity rather than a blind spot."""
    def _contended(seq, ms):
        return guard.run_record({
            "status": "partial", "stop_reason": "contention",
            "batch_lock_wait_ms_max": ms,
        }) | {"seq": seq}

    benign = []
    for i in range(1, 41):
        benign.append(
            _contended(i, 1037) if i % 2 else _mid_band(1037, i)
        )
    assert _evaluate_after_every_run(benign) is None, (
        "the 50% escape boundary is documented policy; changing it is a "
        "deliberate decision, not a silent one"
    )
    # ... and every one of those contended runs was still REPORTED.
    assert all(r["review"] for r in benign[::2])

    harmful = []
    for i in range(1, 9):
        harmful.append(
            _contended(i, 2500) if i % 2 else _mid_band(2500, i)
        )
    assert _evaluate_after_every_run(harmful) == 3


def test_the_review_line_does_not_fire_on_the_measured_healthy_band():
    """The other half of every breaker in this file: it must stay quiet on the
    shape a healthy production pass actually has. Measured healthy batch
    maxima: 16 / 514 / 1037 ms, and Eric accepted 1.0-1.3 s explicitly."""
    for ms in (0, 16, 514, 1037, 1300, 1999):
        assert guard.review_reasons({"status": "ok", "batch_lock_wait_ms_max": ms}) == []
    assert guard.review_reasons(
        {"status": "ok", "batch_lock_wait_ms_max": 2000}
    ) == ["batch_lock_wait_ms_max>=2000"]
    # Sustained healthy waits never trip, however many of them there are.
    assert _evaluate_after_every_run([_mid_band(1300, i) for i in range(1, 41)]) is None


def test_a_severe_run_carries_one_wait_reason_not_two():
    """The review line is the LAST arm of the chain: 5000 >= 2000, and one wait
    deserves one reason. Otherwise every warning would read as two findings."""
    assert guard.review_reasons(
        {"status": "ok", "batch_lock_wait_ms_max": 5200, "batch_lock_wait_warnings": 1}
    ) == ["batch_lock_wait_warning"]
    assert guard.review_reasons(
        {"status": "ok", "batch_lock_wait_ms_max": 8000, "batch_lock_wait_aborts": 1}
    ) == ["batch_lock_wait_abort"]


@pytest.mark.parametrize("summary,expected", [
    ({"status": "ok", "write_hold_ms_max": 701}, ["write_hold_ms_max>700"]),
    ({"status": "ok", "write_hold_ms_max": 700}, []),
    ({"status": "ok", "wall_time_model_exceeded": True}, ["wall_time_model_exceeded"]),
    ({"status": "ok", "write_hold_slo_violations": 1}, ["write_hold_slo_violation"]),
    ({"status": "db_locked"}, ["lock_failure"]),
    ({"status": "partial", "stop_reason": "lock_wait_budget"}, ["lock_failure"]),
    # The healthy production shape: a deadline stop and one retry are INSIDE
    # the measured healthy band (0-1 retries on all six counted passes).
    ({"status": "partial", "stop_reason": "deadline", "lock_retry_events": 1}, []),
])
def test_post_run_review_triggers(summary, expected):
    assert guard.review_reasons(summary) == expected


# ---------------------------------------------------------------------------
# 4. The latch: recorded, loud, blocking, and human-cleared.
# ---------------------------------------------------------------------------

def _trip_via_marketops(filedb, monkeypatch, times: int = 2):
    """Two consecutive MarketOps-degraded pre-flight skips — the cheapest real
    path to a tripped gate, and one that also proves a SKIP is recorded data
    rather than silence."""
    monkeypatch.setattr(ct, "_marketops_degraded", lambda session: True)
    results = []
    for _ in range(times):
        session = filedb.Factory()
        try:
            results.append(
                run_scheduled_reconciliation(session, settings=filedb.settings())
            )
        finally:
            session.close()
    return results


def test_a_preflight_skip_is_typed_recorded_data_not_silence(filedb, monkeypatch):
    first, second = _trip_via_marketops(filedb, monkeypatch)
    assert first["status"] == "marketops_degraded"
    assert first["external_calls"] == 0
    state = filedb.read_state()
    assert [r["status"] for r in state["runs"]] == [
        "marketops_degraded", "marketops_degraded",
    ]
    assert state["runs"][0]["seq"] == 1


def test_the_gate_trips_loudly_and_blocks_the_next_scheduled_run(
    filedb, monkeypatch, caplog
):
    with caplog.at_level("ERROR"):
        _, second = _trip_via_marketops(filedb, monkeypatch)
    assert second["health_gate_tripped"] is True
    assert any("HEALTH GATE TRIPPED" in m for m in caplog.messages)
    assert filedb.state_path().exists()

    # The MarketOps degradation is over; the latch is not.
    monkeypatch.setattr(ct, "_marketops_degraded", lambda session: False)
    session = filedb.Factory()
    try:
        blocked = run_scheduled_reconciliation(session, settings=filedb.settings())
    finally:
        session.close()
    assert blocked["status"] == guard.STATUS_HEALTH_LATCH
    assert blocked["tokens_considered"] == 0
    assert blocked["external_calls"] == 0
    assert blocked["health_latched"] is True
    # Nothing ran: no run row was created by the blocked invocation.
    check = filedb.Factory()
    try:
        assert check.query(CryptoTokenLifecycleRun).count() == 0
    finally:
        check.close()


def test_the_latch_never_clears_itself_however_healthy_the_host_becomes(
    filedb, monkeypatch
):
    """The property that makes it a latch rather than a moving average."""
    _trip_via_marketops(filedb, monkeypatch)
    monkeypatch.setattr(ct, "_marketops_degraded", lambda session: False)
    for _ in range(6):
        session = filedb.Factory()
        try:
            r = run_scheduled_reconciliation(
                session, settings=filedb.settings(), force=True,
            )
            assert r["health_latched"] is True, "an attended pass must still report it"
        finally:
            session.close()
    session = filedb.Factory()
    try:
        still_blocked = run_scheduled_reconciliation(session, settings=filedb.settings())
    finally:
        session.close()
    assert still_blocked["status"] == guard.STATUS_HEALTH_LATCH


def test_force_and_dry_run_bypass_the_latch_but_report_it(filedb, monkeypatch):
    _trip_via_marketops(filedb, monkeypatch)
    monkeypatch.setattr(ct, "_marketops_degraded", lambda session: False)
    session = filedb.Factory()
    try:
        forced = run_scheduled_reconciliation(
            session, settings=filedb.settings(), force=True,
        )
        probe = run_scheduled_reconciliation(
            session, settings=filedb.settings(), dry_run=True,
        )
    finally:
        session.close()
    assert forced["status"] != guard.STATUS_HEALTH_LATCH
    assert forced["health_latched"] is True
    assert probe["status"] in ("dry_run", "dry_run_partial")
    assert probe["health_gate_state"] == guard.GATE_STATE_NOT_RECORDED_DRY_RUN


def test_clearing_requires_a_human_and_restarts_the_window(filedb, monkeypatch):
    _trip_via_marketops(filedb, monkeypatch)
    state = guard.load_state(filedb.state_path(), CHAIN)
    assert state["latch"] is not None
    cleared = guard.clear_latch(state, operator="eric", note="checked marketops")
    guard.save_state(filedb.state_path(), state)
    assert cleared["cleared_by"] == "eric"
    # The trip is retained, never erased.
    assert len(state["latch_history"]) == 1
    assert state["latch_history"][0]["reasons"]
    # And the SAME history no longer re-trips the gate — otherwise the latch
    # would be uncleanable.
    assert guard.evaluate_health(guard.gate_window(state))["tripped"] is False

    monkeypatch.setattr(ct, "_marketops_degraded", lambda session: False)
    session = filedb.Factory()
    try:
        after = run_scheduled_reconciliation(session, settings=filedb.settings())
    finally:
        session.close()
    assert after["status"] != guard.STATUS_HEALTH_LATCH
    assert after["health_gate_tripped"] is False


def test_an_unreadable_state_file_fails_closed_and_is_not_overwritten(filedb):
    filedb.state_path().write_text("{not json at all")
    session = filedb.Factory()
    try:
        r = run_scheduled_reconciliation(session, settings=filedb.settings())
    finally:
        session.close()
    assert r["status"] == guard.STATUS_HEALTH_LATCH
    assert r["health_latched"] is True
    # The corrupt file is evidence for the human, not something to clobber.
    assert filedb.state_path().read_text() == "{not json at all"


def test_the_gate_is_inert_without_a_durable_state_location_and_says_so():
    """No shared-temp fallback: a safety latch in a world-shared namespace
    would be worse than none. The result must say the gate is inert rather
    than let a reader assume it was watching."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        _mint(session, "tok-inert", born_hours_ago=40)
        session.commit()
        r = run_scheduled_reconciliation(
            session,
            settings=Settings(
                database_url="sqlite://", enable_crypto_tape_reconciler=True,
            ),
        )
    finally:
        session.close()
    assert r["health_gate_state"] == guard.GATE_STATE_INERT
    assert r["health_state_path"] is None
    assert guard.health_state_path("sqlite://", CHAIN) is None
    assert guard.health_state_path("postgresql://x/y", CHAIN) is None


def test_config_refusals_do_not_dilute_a_gate_about_contention(filedb):
    session = filedb.Factory()
    try:
        r = run_scheduled_reconciliation(
            session, settings=filedb.settings(), limit=0,
        )
    finally:
        session.close()
    assert r["status"] == "invalid_limit"
    assert r["health_gate_state"] == guard.GATE_STATE_NOT_RECORDED_CONFIG
    assert not filedb.state_path().exists()


# ---------------------------------------------------------------------------
# 5. The operator surface: the unit must go red, and the disable path must be
#    the first thing an operator can find.
# ---------------------------------------------------------------------------

def test_the_cli_exits_non_zero_when_the_gate_trips_even_on_an_ok_pass(monkeypatch):
    """A gate that trips into a green unit is decoration: `systemctl --user
    list-units` is the loudest channel an unattended timer has."""
    import asyncio

    from app import cli

    healthy_but_tripped = {
        "status": "ok", "external_calls": 0, "window_hours": 48,
        "selection_limit": 1000, "tokens_considered": 5, "duration_ms": 100,
        "health_gate_tripped": True,
        "health_gate_reasons": ["repeated severe batch lock waits: 2 of 4"],
        "health_latched": True, "review_required": True,
        "review_reasons": ["batch_lock_wait_warning"],
    }
    monkeypatch.setattr(
        ct, "run_scheduled_reconciliation",
        lambda session, **kw: healthy_but_tripped,
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        rc = asyncio.run(cli.crypto_tape_reconcile(session=session))
    finally:
        session.close()
    assert rc == -1


def test_the_timer_unit_is_six_hourly_and_the_service_covers_the_derivation():
    service = (
        REPO / "infra/systemd/user/probability-arena-crypto-reconcile.service"
    ).read_text()
    timer = (
        REPO / "infra/systemd/user/probability-arena-crypto-reconcile.timer"
    ).read_text()
    # Cadence: four ticks a day, deliberately conservative — the historical
    # backlog is nearly exhausted, so there is no economic case for more.
    assert "OnCalendar=*-*-* 03,09,15,21:07:00" in timer
    assert ct.RECONCILER_CADENCE_HOURS == 6
    # TimeoutStartSec must cover the ~374 s whole-unit ceiling at the measured
    # 5.80x loaded-host overshoot, with the arithmetic shown in the file.
    assert "TimeoutStartSec=7min" in service
    assert "374" in service
    assert "5.80" in service


def test_the_runbook_puts_the_disable_procedure_before_the_enable_procedure():
    runbook = (REPO / "docs/EVO_X2_RUNBOOK.md").read_text()
    disable = runbook.find("#### Disable the guarded reconciler timer")
    enable = runbook.find("#### Enable the guarded reconciler timer")
    assert disable != -1 and enable != -1
    assert disable < enable, "an operator in trouble must find DISABLE first"
    assert "ENABLE_CRYPTO_TAPE_RECONCILER=false" in runbook


def test_the_flag_still_defaults_off_in_code():
    """Activation is an operator action on EVO, never a code default."""
    assert Settings().enable_crypto_tape_reconciler is False


def test_the_guarded_paths_make_no_external_calls(filedb, monkeypatch):
    """`external_calls == 0` must remain structurally true on every new
    outcome, including the skips."""
    results = _trip_via_marketops(filedb, monkeypatch)
    session = filedb.Factory()
    try:
        results.append(run_scheduled_reconciliation(session, settings=filedb.settings()))
    finally:
        session.close()
    assert [r["external_calls"] for r in results] == [0, 0, 0]
    guard_source = (REPO / "app/services/crypto_reconciler_guard.py").read_text()
    for banned in ("requests", "httpx", "aiohttp", "urllib"):
        assert banned not in guard_source


def test_the_service_states_the_load_factor_at_which_the_timeout_is_exceeded():
    """ITEM D. `26 + 60L` reaches 422 s at L = 6.6, past the chosen 420 s. The
    crossing point is recorded so the next reader does not re-derive it, and
    the orphaned-`running`-row tally is named as the signature of an overrun."""
    service = (
        REPO / "infra/systemd/user/probability-arena-crypto-reconcile.service"
    ).read_text()
    # The arithmetic, checked rather than trusted: 26 + 60L > 420 <=> L > 6.566
    assert (420 - 26) / 60 == pytest.approx(6.566, abs=0.001)
    assert 26 + 60 * 6.6 == pytest.approx(422.0)
    assert "6.57" in service and "422" in service
    assert "lock_wait_run_row_orphaned" in service
    runbook = (REPO / "docs/EVO_X2_RUNBOOK.md").read_text()
    assert "6.57" in runbook and "422 s" in runbook


def test_the_budget_caps_the_wait_and_the_breaker_caps_the_ladder_is_stated():
    """ITEM F. Both reviewers asked for this in as many words: the breaker
    classifies a wait AFTER it completes (a measured `abort_ms=24517` against
    the 7,000 ms line), so it prevents the next rung rather than capping any
    single wait. Nobody may read 7000 ms as a wait ceiling."""
    phrase = "BUDGET CAPS THE WAIT"
    for path in (
        "app/services/crypto_reconciler_guard.py",
        "app/services/crypto_tape.py",
        "infra/systemd/user/probability-arena-crypto-reconcile.service",
    ):
        assert phrase in (REPO / path).read_text().upper(), path
    runbook = (REPO / "docs/EVO_X2_RUNBOOK.md").read_text()
    assert "budget caps the wait; the breaker caps the ladder" in runbook.lower()
    assert "24517" in runbook


def test_the_runbook_forbids_deleting_the_state_file_as_an_instruction():
    """ITEM A. `health_state_path`'s "no file, no gate" is DISCLOSURE; an
    operator needs an INSTRUCTION, because deleting the file is the natural
    response to the corruption warning and it silently clears the latch."""
    runbook = (REPO / "docs/EVO_X2_RUNBOOK.md").read_text()
    assert "NEVER DELETE THE HEALTH STATE FILE" in runbook
    # And the cross-check that makes a deleted file detectable after the fact,
    # on every trip path including the skips that write no run row.
    assert "HEALTH GATE TRIPPED" in runbook and "journalctl" in runbook


def test_a_deleted_state_file_does_not_block_but_a_corrupt_one_does(filedb, monkeypatch):
    """The measured behaviour the runbook instruction exists for, pinned so a
    later change to it is deliberate. Fail-closed on corrupt is CORRECT; the
    deleted case is why the instruction is an instruction."""
    _trip_via_marketops(filedb, monkeypatch)
    monkeypatch.setattr(ct, "_marketops_degraded", lambda session: False)

    def _run():
        session = filedb.Factory()
        try:
            return run_scheduled_reconciliation(session, settings=filedb.settings())
        finally:
            session.close()

    assert _run()["status"] == guard.STATUS_HEALTH_LATCH          # latched
    filedb.state_path().write_text("{corrupt")
    assert _run()["status"] == guard.STATUS_HEALTH_LATCH          # fails closed
    filedb.state_path().unlink()
    assert _run()["status"] != guard.STATUS_HEALTH_LATCH          # NOT blocked


def test_the_runbook_names_the_50_percent_contention_escape_boundary():
    """ITEM C. Defensible, but it must be a STATED policy line rather than an
    emergent one — an operator must not discover it by being surprised."""
    runbook = (REPO / "docs/EVO_X2_RUNBOOK.md").read_text()
    assert "escape boundary" in runbook.lower()
    assert "half of every pass stops on contention" in runbook
    guard_source = (REPO / "app/services/crypto_reconciler_guard.py").read_text()
    assert "ESCAPE BOUNDARY IS A STATED POLICY LINE" in guard_source


def test_clear_latch_refuses_a_blank_operator(filedb, monkeypatch):
    """ITEM B. `app/cli.py` already declined `--clear` without `--operator`, so
    the shipped path was safe — but the invariant that makes the audit trail
    mean anything lived in one caller instead of in the function."""
    _trip_via_marketops(filedb, monkeypatch)
    state = guard.load_state(filedb.state_path(), CHAIN)
    for blank in ("", "   ", None):
        with pytest.raises(ValueError, match="named operator"):
            guard.clear_latch(state, operator=blank)
    # Refused, not half-applied: the latch is untouched by a rejected clear.
    assert state["latch"] is not None
    assert state["latch_history"] == []
    assert guard.clear_latch(state, operator="eric")["cleared_by"] == "eric"


def test_every_run_records_whether_adaptive_batching_was_active(filedb, monkeypatch):
    """ITEM E. Observation only — nothing branches on it. Precondition 3 stays
    an enable-time human check, and human steps drift; recording what was
    actually in force makes a drifted step visible in the gate history."""
    monkeypatch.setattr(ct, "_marketops_degraded", lambda session: False)
    session = filedb.Factory()
    try:
        off = run_scheduled_reconciliation(session, settings=filedb.settings())
        on = run_scheduled_reconciliation(
            session, settings=filedb.settings(),
            initial_per_token_cost_seconds=0.05, time_budget_seconds=2.0,
        )
    finally:
        session.close()
    assert off["adaptive_batching_active"] is False
    assert on["adaptive_batching_active"] is True
    recorded = [r.get("adaptive_batching_active") for r in filedb.read_state()["runs"]]
    assert recorded == [False, True]


# ---------------------------------------------------------------------------
# 6. The skip COUNT that justifies the skip EXCLUSION.
# ---------------------------------------------------------------------------

def test_a_degraded_week_reports_its_skip_rate_not_a_clean_bill_of_health():
    """BEFORE-ENABLE. `lock_wait_distribution_eligible` excludes pre-flight
    skips from the wait distribution and justifies it by promising they are
    "excluded and COUNTED — the skip rate is a result in its own right". A
    reviewer built the degraded week (9 `marketops_degraded` skips in 28
    recorded runs, a 32% skip rate) and read back `latch=CLEAR review_runs=0
    consecutive_marketops_skips=0`, because the gate's count is TRAILING and
    the last run happened to be healthy."""
    records = []
    for i in range(1, 29):
        if i % 3 == 0 and i < 28:
            records.append(guard.run_record({
                "status": guard.STATUS_MARKETOPS_DEGRADED,
                "lock_wait_distribution_eligible": False,
            }) | {"seq": i})
        else:
            records.append(_mid_band(1037, i))
    skips = guard.skip_summary(records)
    assert skips["runs_total"] == 28
    assert skips["skips_total"] == 9
    assert skips["skip_rate"] == 0.3214
    assert skips["skips_by_status"] == {"marketops_degraded": 9}
    assert skips["distribution_excluded_total"] == 9
    # The trailing count really does read 0 — that is the failure, not a straw
    # man, and it is why a RATE had to be added rather than reused.
    verdict = guard.evaluate_health(records)
    assert verdict["consecutive_marketops_skips"] == 0
    assert verdict["tripped"] is False


def test_the_skip_summary_separates_a_skip_from_a_prelude_blocked_pass():
    """A pass blocked in the prelude DID try; a pre-flight skip did not. Both
    are excluded from the distribution, and they are tallied alongside each
    other rather than merged."""
    records = [
        guard.run_record({"status": guard.STATUS_HEALTH_LATCH,
                          "lock_wait_distribution_eligible": False}),
        guard.run_record({"status": "db_locked",
                          "lock_wait_distribution_eligible": False}),
        _mid_band(500, 3),
    ]
    skips = guard.skip_summary(records)
    assert skips["skips_total"] == 1
    assert skips["distribution_excluded_total"] == 2
    assert skips["distribution_excluded_by_status"] == {
        "db_locked": 1, "skipped_health_latch": 1,
    }
    assert guard.skip_summary([]) == {
        "runs_total": 0, "skips_total": 0, "skip_rate": 0.0,
        "skips_by_status": {}, "distribution_excluded_total": 0,
        "distribution_excluded_by_status": {},
        "contention_total": 0, "contention_rate": 0.0,
    }


def test_the_50_percent_contention_that_never_latches_is_still_READABLE():
    """The other half of item C: a boundary an operator cannot see is not a
    policy, it is a blind spot. The rate the contention rule declines to act on
    is printed, so the host is REPORTED even though it is not auto-disabled."""
    records = []
    for i in range(1, 41):
        records.append(
            guard.run_record({
                "status": "partial", "stop_reason": "contention",
                "batch_lock_wait_ms_max": 1037,
            }) | {"seq": i} if i % 2 else _mid_band(1037, i)
        )
    assert _evaluate_after_every_run(records) is None
    assert guard.skip_summary(records)["contention_rate"] == 0.5


def test_the_health_cli_actually_prints_the_skip_rate(filedb, monkeypatch, capsys):
    """The comment in `crypto_tape.py` claims `crypto-reconciler-health` prints
    the skip rate. Before this fix it did not — the code documented a surface
    that was not there. This test is the surface."""
    import asyncio

    from app import cli

    _trip_via_marketops(filedb, monkeypatch)
    capsys.readouterr()
    rc = asyncio.run(cli.crypto_reconciler_health(settings=filedb.settings()))
    out = capsys.readouterr().out
    assert rc == -1                                    # latched
    assert "skips_total=2" in out
    assert "skip_rate=1.0" in out
    assert "marketops_degraded': 2" in out
    assert "distribution_excluded_total=2" in out
    assert "adaptive_batching_active=" in out


def test_the_preflight_skip_statuses_have_exactly_one_definition():
    """The exclusion and the count must never be able to disagree about which
    statuses they are talking about."""
    assert ct._PREFLIGHT_SKIP_STATUSES is guard.PREFLIGHT_SKIP_STATUSES
    assert guard.PREFLIGHT_SKIP_STATUSES == {
        "marketops_degraded", "skipped_health_latch",
    }


def test_a_preflight_skip_is_excluded_from_the_wait_distribution_but_counted(
    filedb, monkeypatch
):
    """The `lock_wait_distribution_eligible` precedent, applied to the new
    skips: a run that never entered the write phase emits a full contract of
    real zeros, and under a 6-hourly timer a degraded week would inject 28
    benign-looking zero rows into the distribution the thresholds are read
    from. Excluded — and COUNTED, which is the other half of the precedent."""
    first, _ = _trip_via_marketops(filedb, monkeypatch)
    assert first["lock_wait_distribution_eligible"] is False
    assert ct.lock_wait_distribution_eligible(
        {"status": guard.STATUS_HEALTH_LATCH, "lock_wait_measurements": 0}
    ) is False
    # Counted, not censored: both skips are in the recorded history.
    assert len(filedb.read_state()["runs"]) == 2
    # The db_locked signature is untouched.
    assert ct.lock_wait_distribution_eligible(
        {"status": "db_locked", "lock_wait_measurements": 7, "duration_ms": 30000}
    ) is True
