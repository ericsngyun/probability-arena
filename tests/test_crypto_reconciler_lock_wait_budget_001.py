"""CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001 — the reconciler's own lock-wait
budget, distinct from its write-hold SLO.

THE MOTIVATING PRODUCTION MEASUREMENT (EVO, `tape_run_id=3618`, a bounded
`--force` run-once): the pass SUCCEEDED — `external_calls=0`, 236 batches
committed, 1,182 final outcomes, `classify_ms=266` — and still recorded
`lock_retry_events=1`, `blocked_ms=45,744` and `duration_ms=61,047` against
`--max-duration-seconds 30`. A concurrent MarketOps run (#9405) completed
`ok`, so the reconciler was purely the blocked party, not the aggressor.

Twenty-eight synthetic coexistence trials across three commits had reported
ZERO lock failures and ZERO retries, so every test here that claims anything
about contention uses a REAL second SQLite connection holding a REAL RESERVED
lock on a REAL file-backed database. Nothing in this file simulates
contention with a patched clock or a fake exception.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base
from app.models import (
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenLifecycleRun,
)
from app.services import crypto_tape as ct
from app.services.crypto_tape import (
    RECONCILE_LOCK_WAIT_ATTEMPT_MULTIPLIER,
    RECONCILE_LOCK_WAIT_FLOOR_SECONDS,
    RECONCILE_WRITE_TIME_SLO_SECONDS,
    CryptoLifecycleTapeRecorder,
    CryptoTapeConfig,
    derive_lock_wait_budget_seconds,
    run_scheduled_reconciliation,
)

REPO = Path(__file__).resolve().parents[1]
CHAIN = "solana"
# The competing writer's own busy timeout. Deliberately tiny: the HOLDER must
# never itself wait, so the only waiting party in these tests is the
# reconciler under measurement.
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
    return first_seen


class _FileDb:
    """A real, file-backed SQLite database with a real connection pool — the
    only shape in which busy_timeout, RESERVED locks and PRAGMA scoping mean
    anything at all (in-memory `sqlite://` cannot express them)."""

    def __init__(self, tmp_path: Path, *, tokens: int = 20, busy_timeout_seconds: float = 30.0):
        self.path = tmp_path / "lock_wait.db"
        self.engine = create_engine(
            f"sqlite:///{self.path}", connect_args={"timeout": busy_timeout_seconds},
        )
        Base.metadata.create_all(self.engine)
        self.Factory = sessionmaker(bind=self.engine)
        seed = self.Factory()
        for i in range(tokens):
            _mint(seed, f"tok-lwb-{i:03d}", born_hours_ago=30 + i)
        seed.commit()
        seed.close()
        self.pragmas: list[int] = []
        event.listen(self.engine, "before_cursor_execute", self._spy)

    def _spy(self, conn, cursor, statement, parameters, context, executemany):
        text = statement.strip().lower()
        if text.startswith("pragma busy_timeout ="):
            self.pragmas.append(int(text.split("=")[1]))

    def close(self):
        try:
            event.remove(self.engine, "before_cursor_execute", self._spy)
        except Exception:
            pass
        self.engine.dispose()


class _Holder:
    """An independent connection holding SQLite's RESERVED write lock."""

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


# --- the measurement the budget is derived from ----------------------------

def test_one_blocked_statement_exceeds_its_configured_busy_timeout(tmp_path):
    """THE LOAD-BEARING FACT behind every number in this milestone, measured
    here rather than asserted from memory.

    SQLite's `busy_timeout` is a PER-LOCK-ACQUISITION timeout, not a
    per-statement or per-transaction one, and one blocked write statement
    performs more than one acquisition. So the wall time a single statement
    can spend blocked is a MULTIPLE of the configured timeout, not the
    timeout itself. Dev-Mac measurements while writing this milestone (raw
    `sqlite3`, no SQLAlchemy in the path):

        250 ms  -> 0.734 s / 0.663 s   (2.93x / 2.65x)
        500 ms  -> 1.077 s / 1.151 s   (2.15x / 2.30x)
        1000 ms -> 1.779 s / 1.993 s   (1.78x / 1.99x)
        2000 ms -> 3.416 s / 3.373 s   (1.71x / 1.69x)

    ...converging on ~2x, which is `RECONCILE_LOCK_WAIT_STATEMENT_OVERSHOOT`.
    Production is consistent with this and NOT with a 1x bound:
    45.744 s blocked against a nominal 30 s busy timeout is 1.52x.

    This test asserts only the DIRECTION (wall > configured timeout), because
    that is the part that is a property of SQLite rather than of this
    machine's load; the ratios above are host measurements and are quoted as
    such. If this ever fails, the budget derivation's divisor is wrong.
    """
    db_path = tmp_path / "overshoot.db"
    seed = sqlite3.connect(str(db_path))
    seed.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    seed.execute("INSERT INTO t (v) VALUES ('a')")
    seed.commit()
    seed.close()

    budget_seconds = 0.25
    holder = sqlite3.connect(str(db_path), timeout=HOLDER_TIMEOUT_SECONDS)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE t SET v='held'")
    blocked = sqlite3.connect(str(db_path), timeout=HOLDER_TIMEOUT_SECONDS)
    blocked.execute(f"PRAGMA busy_timeout = {int(budget_seconds * 1000)}")
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        blocked.execute("BEGIN IMMEDIATE")
    elapsed = time.monotonic() - started
    blocked.close()
    holder.rollback()
    holder.close()

    assert elapsed > budget_seconds, (
        f"one blocked acquisition took {elapsed:.3f}s against a configured "
        f"{budget_seconds:.3f}s busy timeout — if this is ever <= 1x, "
        "RECONCILE_LOCK_WAIT_STATEMENT_OVERSHOOT is too conservative"
    )


# --- the derivation --------------------------------------------------------

def test_budget_is_derived_from_the_remaining_deadline_not_a_constant():
    """`max(floor, remaining / ATTEMPT_MULTIPLIER)`. The divisor is what makes
    a single attempt's worst-case WAIT fit inside the remaining deadline."""
    assert derive_lock_wait_budget_seconds(None, 30.0) == pytest.approx(
        30.0 / RECONCILE_LOCK_WAIT_ATTEMPT_MULTIPLIER
    )
    assert derive_lock_wait_budget_seconds(None, 20.0) == pytest.approx(
        20.0 / RECONCILE_LOCK_WAIT_ATTEMPT_MULTIPLIER
    )
    # It TIGHTENS as the deadline is consumed — that is the whole point.
    assert derive_lock_wait_budget_seconds(None, 4.0) > derive_lock_wait_budget_seconds(
        None, 2.0
    )


def test_budget_never_falls_below_the_floor_and_the_floor_equals_the_hold_slo():
    """At the floor, one attempt's worst-case wait is
    `floor x ATTEMPT_MULTIPLIER` = exactly the write-HOLD SLO: the reconciler
    may never wait longer, worst case, than it is allowed to hold. That
    identity is the derivation of the floor; it is not a chosen number."""
    assert derive_lock_wait_budget_seconds(None, 0.0) == RECONCILE_LOCK_WAIT_FLOOR_SECONDS
    assert derive_lock_wait_budget_seconds(None, -50.0) == RECONCILE_LOCK_WAIT_FLOOR_SECONDS
    assert (
        RECONCILE_LOCK_WAIT_FLOOR_SECONDS * RECONCILE_LOCK_WAIT_ATTEMPT_MULTIPLIER
        == pytest.approx(RECONCILE_WRITE_TIME_SLO_SECONDS)
    )


def test_an_explicit_budget_is_a_cap_never_a_floor():
    """An operator value may only ever make the reconciler wait LESS. If it
    could raise the budget above the deadline-derived share, it would be a
    way to re-create the 61s-against-30s overshoot from configuration."""
    derived = derive_lock_wait_budget_seconds(None, 30.0)
    assert derive_lock_wait_budget_seconds(1.0, 30.0) == 1.0          # tighter wins
    assert derive_lock_wait_budget_seconds(9999.0, 30.0) == derived   # looser ignored
    # With no deadline at all there is nothing to derive from: the explicit
    # value (or None, meaning "leave the connection's busy timeout alone")
    # is the whole answer.
    assert derive_lock_wait_budget_seconds(None, None) is None
    assert derive_lock_wait_budget_seconds(3.0, None) == 3.0


def test_hold_slo_and_wait_budget_are_not_the_same_quantity():
    """Guard against the two being collapsed into one knob by a later edit.
    `time_budget_seconds` bounds the HOLD; `lock_wait_budget_seconds` bounds
    the WAIT. They are separate Settings fields and separate CLI flags."""
    s = Settings()
    assert hasattr(s, "crypto_tape_reconciler_time_budget_seconds")
    assert hasattr(s, "crypto_tape_reconciler_lock_wait_budget_seconds")
    assert s.crypto_tape_reconciler_lock_wait_budget_seconds is None
    source = (REPO / "app" / "cli.py").read_text()
    assert "--lock-wait-budget-seconds" in source
    assert "--time-budget-seconds" in source


# --- the budget is actually applied, and scoped to this connection ---------

def test_the_pass_applies_its_budget_as_a_connection_pragma_and_restores_it(filedb):
    """The budget must be a PER-CONNECTION `PRAGMA busy_timeout` on the
    reconciler's own connection — never a change to the process-wide
    `sqlite_busy_timeout_ms`, which every other writer on this host shares."""
    session = filedb.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    r = rec.run_once(
        session, limit=20, hours=48, batch_size=5,
        max_duration_seconds=20.0, sleeper=lambda _s: None,
    )
    session.close()

    assert r["status"] == "ok"
    assert r["external_calls"] == 0
    assert filedb.pragmas, "no busy_timeout PRAGMA was ever applied"
    # Every applied budget is the derived share of a 20s deadline or less —
    # never the connection's own 30s default.
    ceiling_ms = int(1000 * 20.0 / RECONCILE_LOCK_WAIT_ATTEMPT_MULTIPLIER)
    budgets = [ms for ms in filedb.pragmas if ms != 30000]
    assert budgets, "only the restore value was applied"
    assert max(budgets) <= ceiling_ms, filedb.pragmas
    # ...and the pass puts the connection back the way it found it.
    assert filedb.pragmas[-1] == 30000, filedb.pragmas


def test_a_dry_run_neither_budgets_nor_meters(filedb):
    """A dry run never commits, so it has no write lock to wait for. It must
    stay byte-identical to its pre-milestone behaviour: no PRAGMA, no meter."""
    session = filedb.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    r = rec.run_once(
        session, limit=20, hours=48, batch_size=5, dry_run=True,
        max_duration_seconds=20.0, sleeper=lambda _s: None,
    )
    session.close()
    assert r["status"] == "dry_run"
    assert filedb.pragmas == []
    assert r["lock_wait_ms"] == 0
    assert r["lock_wait_measurements"] == 0


# --- contention: only the current batch is discarded ----------------------

def test_a_blocked_batch_rolls_back_only_itself_and_prior_batches_stay_durable(filedb):
    """The core contract. A competing writer takes RESERVED partway through
    the pass; the in-flight batch is rolled back and the pass returns a typed
    `partial`, while every batch committed BEFORE the block stays durable."""
    session = filedb.Factory()
    holder: list[_Holder] = []

    def _sleeper(seconds: float) -> None:
        # The post-batch yield fires only after a REAL commit, so this runs
        # exactly once the first batch is durable — the moment to introduce
        # the competing writer.
        if not holder:
            holder.append(_Holder(filedb.path))

    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    try:
        r = rec.run_once(
            session, limit=20, hours=48, batch_size=5,
            # A short deadline keeps this test fast: the derived budget is a
            # quarter of what remains, so a small deadline means a small
            # blocked window. The behaviour under test is unchanged by it.
            max_duration_seconds=6.0,
            max_lock_attempts=2, lock_retry_seconds=0.0,
            sleeper=_sleeper,
        )
    finally:
        session.close()
        if holder:
            holder[0].release()

    assert r["status"] == "partial", r
    assert r["stop_reason"] in ("contention", "lock_wait_budget"), r["stop_reason"]
    assert r["batches_committed"] >= 1
    assert r["external_calls"] == 0
    # The measured WAIT is real and is NOT the same number as `blocked_ms`
    # (which also contains the pass's own write work).
    assert r["lock_wait_ms"] > 0, r
    assert r["lock_wait_ms_max"] > 0
    assert r["blocked_ms"] >= r["lock_wait_ms"]
    # ...and the first batch's work is still on disk after the failure.
    verify = filedb.Factory()
    durable = verify.execute(
        select(ct.CryptoTokenBirthEvent).where(ct.CryptoTokenBirthEvent.chain == CHAIN)
    ).scalars().all()
    verify.close()
    assert len(durable) >= 5, f"committed batches were not durable: {len(durable)}"


def test_an_expired_budget_stops_the_ladder_instead_of_sleeping_and_retrying(filedb):
    """Before this milestone the retry ladder slept and retried regardless of
    the deadline, so a blocked pass kept spending wall-clock time it no longer
    had — the shape that turned a 30s run into a 61s one. With no deadline
    left, the FIRST lock failure must end the pass with a typed status.

    Sequenced through the injected sleeper, which is the only deterministic
    hook into the pass's own timeline:
      call 1 — the post-batch yield after batch 1 COMMITTED: introduce the
               competing writer. Batch 2 then blocks for its derived budget
               and, by the time it fails, the 1s deadline is gone.
      call 2 — with the fix, this can only be the run row's FINALIZE retry
               (the batch ladder stopped without sleeping); release the
               holder so the run row can be written. WITHOUT the fix, call 2
               would instead be the batch ladder's own retry sleep, the
               holder would be released, and the retried batch would
               SUCCEED — producing a completely different stop_reason.
    """
    session = filedb.Factory()
    holder: list[_Holder] = []
    retry_sleeps: list[float] = []
    retry_interval = 5.0

    def _sleeper(seconds: float) -> None:
        retry_sleeps.append(seconds)
        if not holder:
            holder.append(_Holder(filedb.path))
        elif holder[0] is not None:
            holder[0].release()
            holder[0] = None

    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    try:
        r = rec.run_once(
            session, limit=20, hours=48, batch_size=5,
            max_duration_seconds=1.0,
            max_lock_attempts=3, lock_retry_seconds=retry_interval,
            sleeper=_sleeper,
        )
    finally:
        session.close()
        if holder and holder[0] is not None:
            holder[0].release()

    assert r["stop_reason"] == "lock_wait_budget", r
    assert r["status"] == "partial", r
    assert r["batches_committed"] == 1, r
    assert r["lock_wait_ms"] > 0
    assert "lock-wait budget exhausted" in r["error"]
    # The batch ladder contributed NO retry sleep: at most one full retry
    # interval was ever slept, and that one belongs to the finalize commit.
    assert retry_sleeps.count(retry_interval) <= 1, retry_sleeps


def test_the_budget_shortens_a_blocked_attempt_versus_the_connection_default(tmp_path):
    """The end-to-end point of the mechanism: with the budget in force, an
    attempt blocked by a real competing writer gives up in a fraction of the
    time the connection's own busy timeout would have taken.

    Run the SAME contended single-attempt pass twice, changing only whether
    the budget is derived — the control patches `derive_lock_wait_budget_
    seconds` to return None, which is exactly what reverting this milestone
    would leave behind.
    """
    connection_timeout_seconds = 2.0

    def _one_pass(budgeted: bool, monkey) -> float:
        db = _FileDb(tmp_path / ("b" if budgeted else "c"),
                     tokens=5, busy_timeout_seconds=connection_timeout_seconds)
        holder = _Holder(db.path)
        session = db.Factory()
        rec = CryptoLifecycleTapeRecorder(
            CryptoTapeConfig(chain=CHAIN, lock_dir=db.path.parent)
        )
        started = time.monotonic()
        try:
            rec.run_once(
                session, limit=5, hours=48, batch_size=2,
                max_duration_seconds=1.0,
                max_lock_attempts=1, lock_retry_seconds=0.0,
                sleeper=lambda _s: None,
            )
        finally:
            elapsed = time.monotonic() - started
            session.close()
            holder.release()
            db.close()
        return elapsed

    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()
    budgeted = _one_pass(True, None)

    original = ct.derive_lock_wait_budget_seconds
    ct.derive_lock_wait_budget_seconds = lambda explicit, remaining: None
    try:
        control = _one_pass(False, None)
    finally:
        ct.derive_lock_wait_budget_seconds = original

    assert budgeted < control, (
        f"budgeted pass took {budgeted:.2f}s, unbudgeted control took "
        f"{control:.2f}s — the budget bought nothing"
    )


# --- telemetry: the distribution this milestone exists to make possible ----

def test_lock_wait_is_reported_per_pass_and_persisted_on_the_run_row(filedb):
    """`blocked_ms` alone could never answer "how long do we WAIT?" — it is
    wait plus the pass's own write work. The persisted fields here are what a
    later `lock_wait_ms` DISTRIBUTION across many real passes is built from,
    and the histogram's fixed edges are what make per-pass rows ADDABLE."""
    session = filedb.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    r = rec.run_once(
        session, limit=20, hours=48, batch_size=5,
        max_duration_seconds=20.0, sleeper=lambda _s: None,
    )
    session.close()

    for key in (
        "lock_wait_ms", "lock_wait_ms_max", "lock_wait_measurements",
        "lock_wait_histogram_ms", "lock_wait_budget_ms", "lock_wait_budget_ms_min",
        "write_hold_ms_max", "write_hold_slo_seconds", "write_hold_slo_violations",
    ):
        assert key in r, f"{key} missing from the pass summary"
    assert r["lock_wait_measurements"] >= r["batches_committed"]
    assert r["lock_wait_budget_ms"] is not None
    assert sum(r["lock_wait_histogram_ms"].values()) == r["lock_wait_measurements"]

    verify = filedb.Factory()
    run = verify.execute(
        select(CryptoTokenLifecycleRun).order_by(CryptoTokenLifecycleRun.id.desc())
    ).scalars().first()
    coordination = (run.config or {})["write_coordination"]
    verify.close()
    # The finalize commit has not happened when this blob is staged, so the
    # pass total is named for what it actually is — same convention as the
    # pre-existing `blocked_ms_before_finalize`.
    assert "lock_wait_ms_before_finalize" in coordination
    assert "lock_wait_histogram_ms" in coordination
    assert "lock_wait_budget_ms" in coordination
    assert coordination["lock_wait_measurements_before_finalize"] >= 1


def test_the_write_hold_slo_is_recorded_even_though_it_is_not_enforced(filedb):
    """Related open item, closed only as far as it honestly can be: adaptive
    batching (the ENFORCEMENT of the 2.0s write-hold SLO) still needs a
    measured `initial_per_token_cost_seconds`, which has no default. But the
    lock-wait meter already knows when RESERVED was taken and when COMMIT
    returned, so the actual HOLD is now recorded on every pass for free — an
    operator can read a real per-host hold distribution off the run rows
    instead of having to guess the seed the enforcement needs."""
    session = filedb.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    r = rec.run_once(
        session, limit=20, hours=48, batch_size=5,
        max_duration_seconds=20.0, sleeper=lambda _s: None,
    )
    session.close()
    assert r["write_hold_slo_seconds"] == RECONCILE_WRITE_TIME_SLO_SECONDS
    assert r["write_hold_ms_max"] >= 0
    assert r["write_hold_slo_violations"] == 0
    # Recorded, not enforced — and the reason is still true.
    assert Settings().crypto_tape_reconciler_initial_per_token_cost_seconds is None


# --- governed path ---------------------------------------------------------

def test_scheduled_path_refuses_a_non_positive_wait_budget(filedb):
    session = filedb.Factory()
    r = run_scheduled_reconciliation(
        session,
        settings=Settings(
            enable_crypto_tape_reconciler=True,
            crypto_tape_reconciler_window_hours=48,
            crypto_tape_reconciler_limit=1000,
            crypto_tape_reconciler_lock_wait_budget_seconds=0.0,
        ),
    )
    session.close()
    assert r["status"] == "invalid_lock_wait_budget_seconds"
    assert r["external_calls"] == 0


def test_scheduled_path_caps_the_derived_budget_with_the_operator_value(filedb):
    session = filedb.Factory()
    r = run_scheduled_reconciliation(
        session,
        settings=Settings(
            enable_crypto_tape_reconciler=True,
            crypto_tape_reconciler_window_hours=48,
            crypto_tape_reconciler_limit=1000,
            crypto_tape_reconciler_batch_size=5,
            crypto_tape_reconciler_max_duration_seconds=20.0,
            crypto_tape_reconciler_lock_wait_budget_seconds=0.75,
        ),
        sleeper=lambda _s: None,
    )
    session.close()
    assert r["status"] in ("ok", "truncated", "partial"), r
    assert r["external_calls"] == 0
    assert r["lock_wait_budget_seconds"] == 0.75
    budgets = [ms for ms in filedb.pragmas if ms != 30000]
    assert budgets, filedb.pragmas
    # 0.75s caps the 20s-deadline-derived 5.0s share.
    assert max(budgets) == 750, filedb.pragmas


def test_the_budget_adds_no_network_surface():
    """`external_calls == 0` must stay STRUCTURALLY true, not just reported."""
    source = (REPO / "app" / "services" / "crypto_tape.py").read_text()
    for forbidden in ("httpx", "requests", "aiohttp", "urllib", "socket"):
        assert forbidden not in source, f"crypto_tape.py gained a {forbidden} import"
