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

import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
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
    """An independent connection holding SQLite's RESERVED write lock, or —
    with `exclusive=True` — the EXCLUSIVE lock, which blocks READERS too.
    The read-blocking shape is the one BLOCKER 2 is about: a reviewer
    reproduced a 60.11s wall time against a claimed 42.0s bound behind an
    EXCLUSIVE holder, because the pass's read phase and prelude were never
    inside the lock-wait budget at all."""

    def __init__(self, path: Path, *, exclusive: bool = False):
        self.conn = sqlite3.connect(str(path), timeout=HOLDER_TIMEOUT_SECONDS)
        if exclusive:
            self.conn.execute("BEGIN EXCLUSIVE")
        else:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("UPDATE crypto_tokens SET last_seen_at = last_seen_at")

    def release(self):
        try:
            self.conn.rollback()
        finally:
            self.conn.close()


class _TimedHolder:
    """A RESERVED holder that takes the lock on its OWN thread and releases it
    after `hold_seconds`, so the party under measurement can BLOCK AND THEN
    SUCCEED rather than only ever fail. Constructing it blocks until the lock
    is actually held, so callers never race it.

    A thread is required, not a `threading.Timer` over a main-thread
    connection: `sqlite3` connections are `check_same_thread=True` by default,
    so releasing from another thread would raise instead of releasing."""

    def __init__(self, path: Path, hold_seconds: float):
        self.ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(path, hold_seconds), daemon=True
        )
        self._thread.start()
        assert self.ready.wait(10.0), "the timed holder never took its lock"

    def _run(self, path: Path, hold_seconds: float) -> None:
        conn = sqlite3.connect(str(path), timeout=HOLDER_TIMEOUT_SECONDS)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE crypto_tokens SET last_seen_at = last_seen_at")
            self.ready.set()
            time.sleep(hold_seconds)
            conn.rollback()
        finally:
            self.ready.set()
            conn.close()

    def join(self) -> None:
        self._thread.join(timeout=30.0)


@pytest.fixture
def filedb(tmp_path):
    db = _FileDb(tmp_path)
    try:
        yield db
    finally:
        db.close()


# --- the measurement the budget is derived from ----------------------------

def _blocked_acquisition_ratio(tmp_path, budget_seconds: float, name: str) -> float:
    """Wall time one blocked `BEGIN IMMEDIATE` spends, as a MULTIPLE of its
    own configured `busy_timeout`. Raw `sqlite3`, no SQLAlchemy in the path,
    a real competing RESERVED holder."""
    db_path = tmp_path / f"{name}.db"
    seed = sqlite3.connect(str(db_path))
    seed.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    seed.execute("INSERT INTO t (v) VALUES ('a')")
    seed.commit()
    seed.close()

    holder = sqlite3.connect(str(db_path), timeout=HOLDER_TIMEOUT_SECONDS)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE t SET v='held'")
    blocked = sqlite3.connect(str(db_path), timeout=HOLDER_TIMEOUT_SECONDS)
    blocked.execute(f"PRAGMA busy_timeout = {int(budget_seconds * 1000)}")
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError):
            blocked.execute("BEGIN IMMEDIATE")
        elapsed = time.monotonic() - started
    finally:
        blocked.close()
        holder.rollback()
        holder.close()
    return elapsed / budget_seconds


def test_the_blocked_wait_scales_with_the_budget_which_is_all_the_derivation_uses(
    tmp_path,
):
    """THE LOAD-BEARING FACT behind every number in this milestone — and the
    correction here is WHICH fact that is.

    SQLite's `busy_timeout` is a PER-LOCK-ACQUISITION timeout, so the wall
    time one blocked statement can spend is a MULTIPLE of the configured
    timeout. The branch shipped `RECONCILE_LOCK_WAIT_STATEMENT_OVERSHOOT =
    2.0` as "the measured asymptote" of that multiple, defended by a test
    asserting `elapsed > budget_seconds`. Neither survives contact with data:

      * the OLD ASSERTION defended nothing. Re-derived on EVO (SQLite 3.45.1,
        idle) the maximum ratio observed anywhere was 1.01x, so
        `elapsed > budget` was satisfied by `0.251 > 0.250` — one millisecond
        of scheduler noise — and would have passed unchanged whether the true
        factor were 1x, 2x or 20x.
      * a CEILING assertion is equally unavailable. Re-measured on this dev
        Mac at load average ~5-6, same SQLite version, same probe: 5.24-5.80x
        at 250 ms, 2.28-2.42x at 4000 ms. A fixed 2.0 ceiling is violated at
        every budget by a perfectly healthy machine that merely happens to be
        busy. The ratio is a function of HOST LOAD, not of SQLite.

    So the residual bound is a MODEL of the idle host, not a guarantee, and
    the constant is a chosen safety factor — both now stated as such in
    `crypto_tape.py` and pinned by
    `test_the_overshoot_constant_is_labelled_a_chosen_factor_not_a_measurement`.

    What IS stable across a 1.01x host and a 5.8x host, and is the only
    property `derive_lock_wait_budget_seconds` actually relies on, is that
    the wait SCALES WITH THE BUDGET: shrinking the budget shrinks the wait,
    monotonically and materially. If that ever stopped holding, the whole
    mechanism would be inert — a budget that does not change the wait buys
    nothing — and that is the failure this test exists to catch.
    """
    waits = {
        budget: budget * _blocked_acquisition_ratio(tmp_path, budget, f"scale-{i}")
        for i, budget in enumerate((0.25, 1.0, 2.0))
    }
    # A blocked acquisition always waits at least its own budget.
    assert all(waits[b] >= 0.9 * b for b in waits), waits
    # Monotone in the budget...
    ordered = [waits[b] for b in sorted(waits)]
    assert ordered == sorted(ordered), waits
    # ...and MATERIALLY so: an 8x smaller budget must buy a materially
    # smaller wait, not a rounding difference. Deliberately loose (2x against
    # an 8x budget ratio) because the overshoot inflates small budgets most,
    # which is exactly the load effect measured above.
    assert waits[2.0] > 2.0 * waits[0.25], waits


def test_the_overshoot_constant_is_labelled_a_chosen_factor_not_a_measurement():
    """BLOCKER 3, the documentation half — and the part that is actually
    enforceable, since no numeric assertion about the factor survives across
    hosts (see the test above).

    The constant was justified in the source as "the measured asymptote, not
    a guess", on the strength of an idle-dev-Mac table that describes neither
    EVO (1.01x) nor the same dev Mac under load (up to 5.80x). This repo has
    generalised a Mac measurement to EVO before. The fix is to say plainly
    what the number is; a future edit that re-promotes it to a measurement,
    or that re-describes the residual as a guarantee, has to delete this test
    to do it."""
    source = (REPO / "app" / "services" / "crypto_tape.py").read_text()
    head = source.split("RECONCILE_LOCK_WAIT_STATEMENT_OVERSHOOT = ")[0]
    assert "the measured asymptote, not a guess" not in head
    assert "CHOSEN SAFETY FACTOR" in head
    # Both measurements that forced the relabel stay on the record...
    assert "1.01x" in head
    assert "5.80x" in head
    # ...as does the conclusion drawn from them.
    assert "NOT A GUARANTEE" in head


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
    `partial`, while every batch committed BEFORE the block stays durable.

    B2 (independent review of this branch): this test used to take **141.3 s**,
    88 % of it spent in the FINALIZE. Its holder is released only in the
    `finally` below — i.e. after `run_once` has already returned — so the
    finalize sat out the connection's whole 30 s busy timeout, times this
    host's load-driven overshoot, for every attempt. That cost is not part of
    the property under test: the contract here is about BATCH rollback and
    prior-batch durability, and the finalize's wait budget has two dedicated
    tests of its own (`test_a_contended_finalize_still_persists_the_run_row_
    and_the_histogram`, `test_the_finalize_budget_is_not_the_data_deadline_
    share`). Bounding the finalize's wait explicitly — the same override those
    tests exercise — scopes the holder's cost to the block actually under
    measurement and leaves every assertion below unchanged.
    """
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
            # B2: the finalize's wait is not what this test measures. Left at
            # the connection's inherited 30s it dominated the whole suite.
            finalize_lock_wait_budget_seconds=0.5,
            sleeper=_sleeper,
        )
    finally:
        session.close()
        if holder:
            holder[0].release()

    assert r["status"] == "partial", r
    # `stop_reason` is the discoverable field, and the one to key on. The
    # returned `status` on budget expiry can legitimately be
    # `backlog_expiring` rather than `partial` — the frontier override runs
    # AFTER the lock-wait status assignment — so a reader keying on `status`
    # to detect a lock-wait stop will silently miss cases. Both values below
    # are lock/wait stops; nothing else may appear here.
    assert r["stop_reason"] in ("contention", "lock_wait_budget"), r["stop_reason"]
    assert r["batches_committed"] >= 1
    assert r["external_calls"] == 0
    # The measured WAIT is real and is NOT the same number as `blocked_ms`
    # (which also contains the pass's own write work).
    assert r["lock_wait_ms"] > 0, r
    assert r["lock_wait_ms_max"] > 0
    assert r["blocked_ms"] >= r["lock_wait_ms"]
    # ...and the first batch's work is still on disk after the failure.
    #
    # TIGHTENED PIN (independent review of this branch): this used to assert
    # `len(durable) >= 5` and nothing else, which passes even if the REPORTED
    # accounting had drifted away from the DURABLE state — the one thing a
    # partial pass's contract is actually about. A reviewer's stricter check
    # (reported == durable, exactly) passed, so there is no defect to fix
    # here; what was missing was the pin. Assert the equality, not a bound.
    verify = filedb.Factory()
    durable = verify.execute(
        select(ct.CryptoTokenBirthEvent).where(ct.CryptoTokenBirthEvent.chain == CHAIN)
    ).scalars().all()
    verify.close()
    assert len(durable) >= 5, f"committed batches were not durable: {len(durable)}"
    assert len(durable) == r["batches_committed"] * 5, (
        f"{r['batches_committed']} batches of 5 were reported committed but "
        f"{len(durable)} birth events are durable — reported accounting and "
        "durable state disagree"
    )
    assert r["tokens_processed"] == len(durable), r


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
      any further call — with the fix there is none: the batch ladder stops
               without sleeping, and the finalize now runs a single attempt
               (`RECONCILE_FINALIZE_MAX_LOCK_ATTEMPTS`), so it never sleeps
               either. WITHOUT the fix, call 2 is the batch ladder's own retry
               sleep; the holder is released there and the retried batch
               SUCCEEDS, producing a completely different stop_reason. The
               release is kept for exactly that reverted case.

    B2 (independent review of this branch): this test used to take **69.5 s**,
    almost all of it the finalize waiting out the connection's inherited 30 s
    busy timeout behind the still-held lock. Nothing the test pins requires
    that; the finalize's budget is bounded explicitly below.
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
            # B2: see the docstring — the finalize's inherited 30s wait is not
            # the property under test and was 88% of this test's wall time.
            finalize_lock_wait_budget_seconds=0.5,
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
    # interval was ever slept, and that one could only belong to the finalize
    # commit (which, since B1, does not retry at all — so in practice zero).
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


# --- BLOCKER 1(a): the abandon path must not censor its own tail ----------

def test_the_abandon_path_reports_the_accounting_it_measured_instead_of_none(filedb):
    """A pass that hit severe contention returned `status="db_locked"`,
    `error="database is locked; pass abandoned"` — and EVERY lock-wait field
    absent, so the CLI printed `None` for all of them. That is precisely the
    tail of the distribution the recurring-timer gate exists to measure,
    censored on exactly the passes that carry the most information.

    The accounting lives in a frame that is unwinding, so it can only get out
    on the exception itself (`_attach_lock_wait_evidence`). This test drives
    that end to end WITHOUT relying on the timing of real contention: it
    raises the same `database is locked` error the real path raises, with the
    same evidence attached that `_assemble_pass_locked` attaches, and asserts
    the refusal reports it.

    Fails on revert in TWO independent ways: drop `**_lock_wait_evidence(exc)`
    from `_refused` and the keys vanish; drop the `exc` argument at the
    `db_locked` call site and the values come back as zeros."""
    accounting = ct.LockWaitAccounting()
    for wait in (0.004, 0.130, 0.740):
        meter = ct.LockWaitMeter()
        meter.lock_acquire_seconds = wait
        accounting.record(meter)
    exc = OperationalError("stmt", {}, Exception("database is locked"))
    ct._attach_lock_wait_evidence(exc, accounting, blocked_seconds=1.9, lock_retry_events=2)

    class _Boom:
        def run_once(self, *a, **k):
            raise exc

    session = filedb.Factory()
    r = run_scheduled_reconciliation(
        session,
        settings=Settings(
            enable_crypto_tape_reconciler=True,
            crypto_tape_reconciler_window_hours=48,
            crypto_tape_reconciler_limit=1000,
            crypto_tape_reconciler_batch_size=5,
        ),
        recorder=_Boom(),
    )
    session.close()

    assert r["status"] == "db_locked", r
    # The whole telemetry contract is present...
    for key in (
        "lock_wait_ms", "lock_wait_ms_max", "lock_wait_measurements",
        "lock_wait_histogram_ms", "lock_wait_ms_net",
        "lock_wait_ms_baseline_per_attempt", "blocked_ms", "lock_retry_events",
    ):
        assert key in r, f"{key} missing from a db_locked refusal"
        assert r[key] is not None, f"{key} reported as None on a db_locked refusal"
    # ...carrying what the abandoned pass actually measured, not zeros.
    assert r["lock_wait_measurements"] == 3, r
    assert r["lock_wait_ms"] == 874, r
    assert r["lock_wait_ms_max"] == 740, r
    assert r["blocked_ms"] == 1900, r
    assert r["lock_retry_events"] == 2, r
    # The >=100ms tail — the buckets a threshold may actually be read from —
    # survives the trip out along the exception.
    assert r["lock_wait_histogram_ms"]["100-1000"] == 2, r["lock_wait_histogram_ms"]


def test_a_refusal_with_no_measurement_reports_honest_zeros_not_missing_keys(filedb):
    """The other half of the same contract. A validation refusal genuinely
    measured nothing, and `lock_wait_measurements=0` says exactly that — a
    different and honest statement from a missing key the CLI renders as
    `None`, which is indistinguishable from "we waited an unknown amount"."""
    session = filedb.Factory()
    r = run_scheduled_reconciliation(
        session,
        settings=Settings(
            enable_crypto_tape_reconciler=True,
            crypto_tape_reconciler_window_hours=48,
            crypto_tape_reconciler_limit=0,
        ),
    )
    session.close()
    assert r["status"] == "invalid_limit", r
    assert r["lock_wait_measurements"] == 0
    assert r["lock_wait_ms"] == 0
    assert r["lock_wait_histogram_ms"] is not None
    assert sum(r["lock_wait_histogram_ms"].values()) == 0


# Refused statuses that ALREADY exceed `CryptoTokenLifecycleRun.status`'s
# VARCHAR(32), pinned as a closed set. Adding to this set is the thing this
# test exists to make deliberate; it is not a place to quietly park new ones.
_KNOWN_OVERLONG_REFUSED_STATUSES = {"invalid_initial_per_token_cost_seconds"}


def test_every_refused_status_fits_the_run_row_status_column():
    """LOW, from the same review — and it turned out to be worse than the
    review's framing, which is why this pins a set rather than asserting a
    clean bill of health.

    The review noted that `invalid_lock_wait_budget_seconds` is EXACTLY 32
    characters against `CryptoTokenLifecycleRun.status`'s VARCHAR(32) — zero
    headroom — and called it a trap for the next longer refused status.
    Enumerating the actual call sites shows the trap is not in the future:
    `invalid_initial_per_token_cost_seconds` (38 chars, pre-existing, from
    CRYPTO-COVERAGE-REPAIR-001 B1/B3) already exceeds the column.

    It is harmless TODAY for a reason that is easy to lose: every refused
    status is returned BEFORE a run row exists, so none is ever written to
    that column, and SQLite would not enforce the width even if one were.
    Renaming a shipped status is an operator-visible contract change
    (`tests/test_crypto_adaptive_batching_b1_b3.py` pins the string, and the
    CLI prints it), so it is deliberately NOT done here — this milestone is
    about lock-wait instrument fidelity. What is done is to make the state
    explicit and to fail on any NEW over-length status, so nobody has to
    rediscover it.
    """
    width = CryptoTokenLifecycleRun.__table__.c.status.type.length
    source = (REPO / "app" / "services" / "crypto_tape.py").read_text()
    statuses = set(re.findall(r'_refused\(\s*"([a-z_]+)"', source))
    assert "invalid_lock_wait_budget_seconds" in statuses, statuses
    assert len("invalid_lock_wait_budget_seconds") == width  # zero headroom
    too_long = {s for s in statuses if len(s) > width}
    assert too_long == _KNOWN_OVERLONG_REFUSED_STATUSES, (
        f"the set of refused statuses longer than the run row's "
        f"VARCHAR({width}) changed: {sorted(too_long)}. A refused status is "
        "never persisted today, but if one ever reaches a run row this is "
        "where it silently truncates."
    )


def test_a_cte_wrapped_write_is_classified_as_a_write():
    """LOW, same review. `_is_write_statement` classified on the first seven
    characters, so `WITH ... INSERT ...` read as a plain SELECT and its
    RESERVED acquisition would never have been timed. SQLAlchemy emits no
    such statement for these mappings today — a trap closed before it is
    stepped in."""
    assert ct._is_write_statement("INSERT INTO t VALUES (1)")
    assert ct._is_write_statement("  update t set v = 1")
    assert not ct._is_write_statement("SELECT 1")
    assert ct._is_write_statement(
        "WITH src AS (SELECT 1 AS a) INSERT INTO t (a) SELECT a FROM src"
    )
    assert not ct._is_write_statement("WITH src AS (SELECT 1) SELECT * FROM src")


# --- BLOCKER 1(b): the finalize keeps its own budget ----------------------

# How long the competing writer holds the lock across the run row's finalize
# commit. Must comfortably exceed what a REVERTED (0.5s floor) finalize can
# spend before giving up, INCLUDING this host's load-driven overshoot —
# measured up to 3.8x at 500ms on the dev Mac at load average ~5-6.
_FINALIZE_HOLD_SECONDS = 3.0

def test_a_contended_finalize_still_persists_the_run_row_and_the_histogram(tmp_path):
    """THE GATE'S EVIDENCE, on the passes that carry it.

    `write_coordination` — the persisted lock-wait scalars AND the whole
    histogram — is staged inside `_prepare_finalize` and written by ONE
    commit. That commit's budget was `derive_lock_wait_budget_seconds(
    explicit, 0.0)`, i.e. always the 0.5s floor, cutting it 60x below the
    connection's 30s busy timeout. A reviewer's discriminating experiment,
    same fixture and same 20s holder, floor the only variable:

        floor=0.5s  wall= 5.6s  status=partial/lock_wait_budget
                    run_row=running   histogram_persisted=False
        floor=30s   wall=20.2s  status=ok
                    run_row=ok        histogram_persisted=True

    So the milestone's real 5.6s-vs-20.2s win belonged to the BATCH LOOP, and
    the price was the gate's evidence on exactly the contended passes that
    matter. The finalize writes one small row once and holds the lock for
    microseconds — bounded by construction, unlike the batch loop — so it now
    keeps the connection's original busy timeout.

    Deterministic, not timing-hopeful: a real RESERVED holder is taken at the
    exact moment the run row's finalize UPDATE is about to execute, and
    released `_FINALIZE_HOLD_SECONDS` later from its own thread.
    `max_lock_attempts=1` removes the retry ladder so the finalize budget is
    the only thing that can save the row.

    Fails on revert: restore the 0.5s floor and the finalize gives up long
    before the holder releases, leaving `run.status='running'` and no
    `write_coordination` at all. The hold is sized against the OVERSHOOT, not
    against the nominal floor — a 500ms busy timeout was measured reaching
    1.91s of wall time on this dev Mac under load, so a 1.5s hold made this
    test pass under BOTH the fix and the revert on a busy machine. It is not
    a discriminator unless it clears the loaded-host overshoot with margin.
    """
    d = tmp_path / "finalize"
    d.mkdir()
    db = _FileDb(d, tokens=6, busy_timeout_seconds=30.0)
    holders: list[_TimedHolder] = []

    def _grab(conn, cursor, statement, parameters, context, executemany):
        text = statement.strip().lower()
        if text.startswith("update crypto_token_lifecycle_runs") and not holders:
            holders.append(_TimedHolder(db.path, _FINALIZE_HOLD_SECONDS))

    event.listen(db.engine, "before_cursor_execute", _grab)
    session = db.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=d))
    try:
        r = rec.run_once(
            session, limit=6, hours=48, batch_size=3,
            max_duration_seconds=20.0,
            max_lock_attempts=1, lock_retry_seconds=0.0,
            sleeper=lambda _s: None,
        )
    finally:
        session.close()
        event.remove(db.engine, "before_cursor_execute", _grab)
        for h in holders:
            h.join()

    assert holders, "the finalize UPDATE never fired, so nothing was contended"
    assert r["status"] == "ok", r
    assert r["external_calls"] == 0
    # The finalize really did block — otherwise this proves nothing.
    assert r["blocked_ms"] >= 1000 * _FINALIZE_HOLD_SECONDS * 0.6, r

    verify = db.Factory()
    run = verify.execute(
        select(CryptoTokenLifecycleRun).order_by(CryptoTokenLifecycleRun.id.desc())
    ).scalars().first()
    status, config = run.status, dict(run.config or {})
    verify.close()
    db.close()

    assert status == "ok", (
        f"the run row was left at status={status!r} — the finalize commit lost "
        "the lock race and the pass's whole lock-wait evidence went with it"
    )
    coordination = config["write_coordination"]
    assert "lock_wait_histogram_ms" in coordination, coordination
    assert sum(coordination["lock_wait_histogram_ms"].values()) >= 1, coordination
    assert coordination["lock_wait_measurements_before_finalize"] >= 1


def test_the_finalize_budget_is_not_the_data_deadline_share(filedb):
    """The reasoning, pinned separately from the behaviour. The finalize's
    budget must not be derived from the pass's DATA-WORK deadline: a spent
    `max_duration_seconds` says nothing about what a bookkeeping commit may
    wait, and deriving from it is what produced the always-the-floor value.
    Here the data deadline is 0.0 — fully consumed before the pass even
    starts — and the finalize must still get the connection's own timeout."""
    session = filedb.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    r = rec.run_once(
        session, limit=20, hours=48, batch_size=5,
        max_duration_seconds=0.0, sleeper=lambda _s: None,
    )
    session.close()
    # A fully-spent data deadline stops the pass after one batch...
    assert r["stop_reason"] == "deadline", r
    # ...and the finalize still wrote the run row, with the histogram on it.
    verify = filedb.Factory()
    run = verify.execute(
        select(CryptoTokenLifecycleRun).order_by(CryptoTokenLifecycleRun.id.desc())
    ).scalars().first()
    status, config = run.status, dict(run.config or {})
    verify.close()
    assert status != "running", status
    assert "lock_wait_histogram_ms" in config["write_coordination"]
    # The applied finalize budget is the connection's 30s, not the 0.5s floor
    # a deadline-derived value would have produced. The last two PRAGMAs of
    # the pass are the finalize's own budget and then the restore, so the
    # second-from-last is the number under test — and with a data deadline of
    # 0.0 every OTHER budget in this pass IS the 500ms floor, which is exactly
    # what makes this position discriminating.
    assert filedb.pragmas[-1] == 30000, filedb.pragmas
    assert filedb.pragmas[-2] == 30000, filedb.pragmas
    assert 500 in filedb.pragmas, filedb.pragmas

    # And the explicit override is live, not dead scaffolding: a caller that
    # names the finalize's budget gets exactly that, still independent of the
    # spent data deadline.
    filedb.pragmas.clear()
    session = filedb.Factory()
    rec.run_once(
        session, limit=20, hours=48, batch_size=5,
        max_duration_seconds=0.0, sleeper=lambda _s: None,
        finalize_lock_wait_budget_seconds=7.0,
    )
    session.close()
    assert filedb.pragmas[-2] == 7000, filedb.pragmas


# --- BLOCKER 2: the budget covers the reads, not just the write loop ------

def test_the_selection_prelude_is_budgeted_not_left_at_the_process_timeout(tmp_path):
    """THE 12.0s RESIDUAL BOUND WAS EXCEEDED BY 18s, and this is why.

    `_apply_lock_wait_budget` was called only inside the chunked write loop
    and the finalize. Everything before them — `run_once`'s whole selection
    prelude (`backlog_size`, `classify_backlog`, `_universe`,
    `universe_size`, `unreconciled_backlog`, the frontier queries) and
    `_assemble_pass_locked`'s read phase — ran at the process-wide
    `sqlite_busy_timeout_ms`. A reviewer measured a real
    `run_scheduled_reconciliation` on a production copy at 60.11s and 60.09s
    against `--max-duration-seconds 30` and a claimed 42.0s bound, with
    `lock_retry_events=0`: the budget machinery never engaged at all, and
    60.09s is two successive READ acquisitions each burning the full 30s.

    Reads are the unlisted third blocking class. Here the connection's own
    busy timeout is 6s and the pass's deadline is 2s, so the derived budget
    is 0.5s: with the fix the blocked prelude read gives up in a fraction of
    the connection timeout; without it, it burns 6s x the host's overshoot.
    """
    d = tmp_path / "prelude"
    d.mkdir()
    db = _FileDb(d, tokens=5, busy_timeout_seconds=6.0)
    holder = _Holder(db.path, exclusive=True)
    session = db.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=d))
    started = time.monotonic()
    try:
        with pytest.raises(OperationalError):
            rec.run_once(
                session, limit=5, hours=48, batch_size=2,
                max_duration_seconds=2.0, sleeper=lambda _s: None,
            )
        elapsed = time.monotonic() - started
    finally:
        session.close()
        holder.release()
        db.close()

    assert elapsed < 4.0, (
        f"a prelude read blocked for {elapsed:.2f}s against a 0.5s derived "
        "budget — the budget is not reaching the read phase, which is the "
        "shape that measured 60.11s against a claimed 42.0s bound"
    )


def test_the_governed_prelude_read_is_budgeted_and_refuses_with_full_telemetry(
    tmp_path,
):
    """The same hole in the GOVERNED entry point. `run_scheduled_reconciliation`
    runs its own MarketOps-health read before handing off, and that read was
    unbudgeted too — one more acquisition at the process timeout ahead of
    every bound the function advertises.

    This also closes BLOCKER 1(a) end to end: the refusal that comes out of a
    genuinely blocked prelude carries the whole lock-wait contract rather
    than a set of `None`s.

    THE ASSERTION IS THE PRAGMA WITNESS, NOT THE WALL CLOCK, and that is a
    correction. This test used to assert `elapsed < 4.0`, reasoning from the
    fixture (`busy_timeout=6.0`, deadline `2.0` -> derived budget
    `max(0.5, 2.0/4) = 0.5 s`) that 0.5 s discriminates the budgeted path from
    a 6.0 s unbudgeted regression. But the governed prelude makes MORE THAN ONE
    acquisition — the MarketOps-health read here, then `run_once`'s own
    selection prelude — and this branch's own documented overshoot is 1.01x on
    idle EVO and **5.80x at load 5-6**. Two acquisitions at `0.5 s x 5.8` is
    5.8 s: over the old 4.0 s line ON THE BUDGETED PATH, with no regression
    present. A reviewer measured 3.60 s against it at load ~4 — a 0.4 s margin,
    i.e. a coin flip. The threshold was chosen against an implicit ~1x
    overshoot that the same repo documents as unbounded above.

    `db.pragmas` is a DETERMINISTIC witness for the same contract at any load:
    `500` proves each prelude budget engaged and `6000` proves each was
    restored — both halves, twice, in order. (`lock_wait_budget_ms_min` is
    `None` on this path and cannot serve: the prelude is bounded but NOT
    metered.) Measured on this fixture: `[500, 6000, 500, 6000]` identically on
    5/5 runs at load 15.75, `elapsed` 2.37-2.51 s. Mutation-checked — deleting
    the governed prelude's budget yields `[500, 6000]` (only `run_once`'s pair)
    and `elapsed` 10.73 s, so both the exact sequence and the coarse backstop
    below fail on the regression this test exists to catch.
    """
    d = tmp_path / "governed"
    d.mkdir()
    db = _FileDb(d, tokens=5, busy_timeout_seconds=6.0)
    holder = _Holder(db.path, exclusive=True)
    session = db.Factory()
    started = time.monotonic()
    try:
        r = run_scheduled_reconciliation(
            session,
            settings=Settings(
                enable_crypto_tape_reconciler=True,
                crypto_tape_reconciler_window_hours=48,
                crypto_tape_reconciler_limit=1000,
                crypto_tape_reconciler_batch_size=5,
                crypto_tape_reconciler_max_duration_seconds=2.0,
            ),
            recorder=CryptoLifecycleTapeRecorder(
                CryptoTapeConfig(chain=CHAIN, lock_dir=d)
            ),
            sleeper=lambda _s: None,
        )
        elapsed = time.monotonic() - started
    finally:
        session.close()
        holder.release()
        db.close()

    assert r["status"] == "db_locked", r
    assert r["external_calls"] == 0
    # The deterministic half of the contract: both prelude acquisitions took
    # the 0.5 s derived budget (500) and both handed the connection back at its
    # original 6.0 s timeout (6000), in that order. Load-independent.
    assert db.pragmas == [500, 6000, 500, 6000], (
        f"prelude budget/restore witness broken: {db.pragmas} — [500, 6000] "
        "alone means the GOVERNED prelude read ran unbudgeted at the "
        "connection timeout and only run_once's prelude was covered"
    )
    # A coarse backstop only, deliberately far from both measured populations
    # (budgeted 2.37-2.51 s here / 3.60 s at load ~4; unbudgeted 10.73 s). It
    # is NOT the load-bearing assertion — see the docstring.
    assert elapsed < 6.0, (
        f"the governed prelude blocked for {elapsed:.2f}s; the unbudgeted "
        "shape measures ~10.7s on this fixture"
    )
    for key in ("lock_wait_ms", "lock_wait_histogram_ms", "lock_wait_measurements"):
        assert r[key] is not None, f"{key} censored on a db_locked refusal"


def test_the_prelude_hands_the_connection_back_at_its_original_timeout(filedb):
    """The prelude budget is connection-scoped and must be UNDONE, or
    `_assemble_pass_locked` — which reads the connection's busy timeout at its
    own start to know what to restore — would silently redefine "original" as
    the prelude budget and never hand the connection back intact."""
    session = filedb.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    r = rec.run_once(
        session, limit=20, hours=48, batch_size=5,
        max_duration_seconds=20.0, sleeper=lambda _s: None,
    )
    session.close()
    assert r["status"] == "ok"
    # The prelude budget was applied (5s = the 20s deadline's derived share)
    # and then restored, BEFORE the write phase applied its own.
    assert filedb.pragmas[0] == 5000, filedb.pragmas
    assert filedb.pragmas[1] == 30000, filedb.pragmas
    # ...and then `_assemble_pass_locked`'s READ PHASE gets a budget of its
    # own, which it did not have before this branch's fix either: the third
    # PRAGMA is the read phase, ahead of any write. (Only the prelude's
    # BOUNDING is provable end to end here — see
    # `test_the_selection_prelude_is_budgeted_not_left_at_the_process_timeout`.
    # The read phase cannot be contended in-process the same way: the pass
    # holds SHARED continuously from the prelude onward, so no second
    # connection can take EXCLUSIVE mid-pass to block it.)
    # (Slightly under 5000: the read-phase budget is re-derived from what is
    # LEFT of the deadline, which the prelude has just spent a little of.)
    assert 4500 <= filedb.pragmas[2] < 5000, filedb.pragmas
    assert filedb.pragmas[-1] == 30000, filedb.pragmas


# --- BLOCKER 4: the scalar is biased high, and by how much is reported ----

def test_the_per_attempt_measurement_bias_is_estimated_and_subtracted():
    """`LockWaitMeter` cannot split a SUCCEEDING statement's duration into
    "slept in SQLite's busy handler" and "did work" — Python's `sqlite3`
    exposes no busy-handler callback — so every attempt's reported wait
    carries that attempt's own DML plus its commit fsync. Measured on a pass
    with ZERO contention, no competing writer at all, so every millisecond is
    bias:

        lock_wait_ms=3380  lock_wait_measurements=402  lock_wait_ms_max=21
        blocked_ms=9578    histogram {'1-10': 372, '10-100': 30}

    ~8.4ms per attempt against a true wait of exactly zero — ~6x the "~1.3ms
    DML" the docstring implied, because the commit fsync dominates and was
    unquantified. 35% of `blocked_ms` under no contention, and it scales with
    batch count (~340s of phantom wait over 100 passes).

    The bias is per-attempt, so the pass's own attempts estimate it in band.
    A1 (independent review of this branch) changed WHICH statistic: the
    estimator is the MEDIAN retained attempt, not the minimum — see
    `test_the_bias_baseline_is_the_median_because_the_minimum_under_corrects`
    for the measurement that forced it. Asserted here on synthetic meters
    because the arithmetic, not the host's fsync speed, is the contract."""
    accounting = ct.LockWaitAccounting()
    for wait in (0.008, 0.009, 0.011, 0.512):
        meter = ct.LockWaitMeter()
        meter.lock_acquire_seconds = wait
        accounting.record(meter)
    s = accounting.as_summary()
    assert s["lock_wait_ms"] == 540
    # median of (8, 9, 11, 512) = (9+11)//2 = 10, NOT the minimum's 8.
    assert s["lock_wait_ms_baseline_per_attempt"] == 10
    # 540 - 10*4 = 500: the real waiting, with the per-attempt floor gone.
    assert s["lock_wait_ms_net"] == 500
    assert s["lock_wait_ms_min"] == 8

    # A single measurement cannot distinguish bias from signal, and must not
    # declare its only sample to be pure bias.
    lone = ct.LockWaitAccounting()
    meter = ct.LockWaitMeter()
    meter.lock_acquire_seconds = 0.400
    lone.record(meter)
    s1 = lone.as_summary()
    assert s1["lock_wait_ms_baseline_per_attempt"] == 0
    assert s1["lock_wait_ms_net"] == 400

    # Nothing measured: honest zeros, never None.
    s0 = ct.LockWaitAccounting().as_summary()
    assert s0["lock_wait_ms_net"] == 0
    assert s0["lock_wait_ms_baseline_per_attempt"] == 0


def test_a_zero_contention_pass_reports_a_non_zero_scalar_and_names_it_bias(filedb):
    """The measurement above, taken end to end on a real pass with no
    competing writer anywhere: `lock_wait_ms` is NOT zero even though the
    true wait is exactly zero. That is the defect the corrected fields
    describe — and the reason a threshold may never be read off the scalar."""
    session = filedb.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    r = rec.run_once(
        session, limit=20, hours=48, batch_size=5,
        max_duration_seconds=20.0, sleeper=lambda _s: None,
    )
    session.close()

    assert r["status"] == "ok"
    assert r["lock_wait_measurements"] >= 2
    for key in ("lock_wait_ms_baseline_per_attempt", "lock_wait_ms_net", "lock_wait_ms_min"):
        assert key in r, f"{key} missing from the pass summary"
    # The correction is arithmetically consistent with what was reported...
    assert r["lock_wait_ms_net"] == max(
        0,
        r["lock_wait_ms"]
        - r["lock_wait_ms_baseline_per_attempt"] * r["lock_wait_measurements"],
    )
    # ...and, with zero contention, the corrected net is a small fraction of
    # the raw scalar: the raw number is mostly this host's fsync.
    assert r["lock_wait_ms_net"] <= r["lock_wait_ms"]
    # The DECISION tail — where a threshold may legitimately be read — is
    # empty under zero contention, which is the property that makes it usable
    # while the scalar is not. Read through the helper, not by hand: A2
    # measured that reading the `>=100` tail by hand counts the pass's own
    # fsync as contention (485ms of "wait" against a 484ms hold).
    assert ct.lock_wait_decision_tail(r["lock_wait_histogram_ms"]) == 0, r

    # ...and the corrected fields are persisted with the rest, not summary-only.
    verify = filedb.Factory()
    run = verify.execute(
        select(CryptoTokenLifecycleRun).order_by(CryptoTokenLifecycleRun.id.desc())
    ).scalars().first()
    coordination = (run.config or {})["write_coordination"]
    verify.close()
    assert "lock_wait_ms_net" in coordination
    assert "lock_wait_ms_baseline_per_attempt" in coordination


def test_the_threshold_source_is_documented_as_the_histogram_tail_not_the_scalar():
    """The operational half of BLOCKER 4, and the one that actually protects
    the gate: whoever sets the eventual `lock_wait_ms` threshold must read it
    from the histogram's DECISION tail, where neither the per-attempt bias nor
    the pass's own fsync lands, and never from the pass-total scalar, which is
    ~35% phantom under zero contention and grows with batch count. Stated in
    the code AND in the runbook, because the person setting the threshold is
    reading the runbook."""
    source = (REPO / "app" / "services" / "crypto_tape.py").read_text()
    assert "never from pass-total `lock_wait_ms`" in source
    runbook = (REPO / "docs" / "EVO_X2_RUNBOOK.md").read_text()
    assert "lock-wait" in runbook.lower()
    assert "lock_wait_histogram_ms" in runbook
    assert "never from pass-total `lock_wait_ms`" in runbook
    # The runbook must also carry the honest caveats a reader would otherwise
    # have to reconstruct from the source.
    assert "stop_reason" in runbook
    assert "prelude" in runbook.lower()
    # A2 — the decision bucket, and the measurement that moved it.
    assert "lock_wait_decision_tail" in runbook
    assert ">=1000 ms" in runbook
    assert "write_hold_ms_max 484" in runbook
    # A1 — the corrected estimator, and the fact that the corrected scalar
    # does NOT converge to zero. The old sentence must be gone, not merely
    # contradicted somewhere further down.
    assert "goes to ~0, which is the correct answer" not in runbook
    assert "MEDIAN attempt" in runbook
    assert "not zero" in runbook
    # A3 — the prelude-blocked signature and what to do with those rows.
    assert "lock_wait_distribution_eligible" in runbook
    assert "counted separately" in runbook
    # A5 — the timer preconditions the telemetry now supports.
    assert "Recurring-timer preconditions" in runbook
    assert "wall_time_model_exceeded" in runbook
    assert "initial_per_token_cost_seconds" in runbook


def test_the_decision_edge_margin_is_stated_as_measured_with_a_numeric_trigger():
    """Carry-forward from the final review: the runbook justified the
    `>=1000 ms` edge by saying the contamination sits "an order of magnitude"
    below it. That was wrong by ~5x. The claim rested on two samples
    (484/479); four uncontended passes on EVO copies read

        write_hold_ms_max : 479, 544, 92, 532
        lock_wait_ms_max  : 480, 530, 20, 521

    so the peak is 544 ms — 54% of the edge, a margin of ~1.8x. The edge
    STAYS (decision tail 0 on all four, `100-1000` collecting 1/2/0/1), but
    the margin must read as measured, and the revisit trigger must be a
    NUMBER: "approaches 1000 ms" is already half-true at 544.

    Doc-only change; this test is the guard that the over-claim cannot come
    back and that the trigger stays numeric."""
    runbook = (REPO / "docs" / "EVO_X2_RUNBOOK.md").read_text()
    source = (REPO / "app" / "services" / "crypto_tape.py").read_text()
    for text in (runbook, source):
        # Whitespace-normalised: these claims are prose and get re-wrapped.
        flat = " ".join(text.split())
        # The over-claim is GONE, not merely contradicted further down.
        assert "an order of magnitude below 1000 ms" not in flat
        assert "484/479 ms — an order of magnitude" not in flat
        # The measured margin, and the sample that sets it.
        assert "544" in flat
        assert "1.8x" in flat
        # The numeric revisit trigger.
        assert "write_hold_ms_max > 700" in flat
    # ...and the edge itself is unchanged: the finding was the JUSTIFICATION,
    # not the choice.
    assert ct.RECONCILE_LOCK_WAIT_DECISION_EDGE_MS == 1000


def test_the_sample_cap_prefix_effect_is_documented_with_its_DIRECTION():
    """Carry-forward from the final review. The runbook said the estimator
    "under-reports its net", which is ambiguous about SIGN — and a reader
    cannot act on an unsigned error.

    Measured: passes run 250-262 attempts (right at the 256 cap) and earlier
    30s passes 390-392, so the median comes from an early PREFIX. Early
    attempts are the cheapest (smaller journal, warm-up), so the prefix median
    sits BELOW the true median, the subtracted baseline is too small, and the
    correction UNDER-corrects. That is the conservative direction:
    `lock_wait_ms_net` errs HIGH and can never go negative from this effect.

    Doc-only change, plus the arithmetic guard below that the net is clamped
    at zero regardless."""
    runbook = (REPO / "docs" / "EVO_X2_RUNBOOK.md").read_text()
    source = (REPO / "app" / "services" / "crypto_tape.py").read_text()
    for text in (runbook, source):
        # Whitespace-normalised: these claims are prose and get re-wrapped.
        flat = " ".join(text.split())
        assert "RECONCILE_LOCK_WAIT_SAMPLE_CAP" in flat
        assert "PREFIX median" in flat or "prefix median" in flat
        assert "errs HIGH, never low" in flat
        assert "can never drive it negative" in flat
    # The clamp the direction claim leans on, asserted rather than trusted.
    accounting = ct.LockWaitAccounting()
    for wait in (0.002, 0.002, 0.002, 0.002):
        meter = ct.LockWaitMeter()
        meter.lock_acquire_seconds = wait
        accounting.record(meter)
    s = accounting.as_summary()
    assert s["lock_wait_ms_net"] >= 0, s


# --- A1: the bias baseline is the median, not the minimum -----------------

def test_the_bias_baseline_is_the_median_because_the_minimum_under_corrects():
    """A1 (independent review of this branch). The runbook claimed
    `lock_wait_ms_net` "goes to ~0 on a zero-contention pass, which is the
    correct answer". Measured on a genuine zero-contention pass — no competing
    writer at all, so the true wait is exactly 0 — it did not:

        lock_wait_ms=3810  measurements=390  -> mean bias 9.77 ms/attempt
        min-estimated baseline = 4 ms
        lock_wait_ms_net = 3810 - 4*390 = 2250 ms      (41% recovered)

    Reproduced across two independent runs (3810/390 and 3809/392). The
    per-attempt bias is RIGHT-SKEWED, and a min-estimator under-corrects a
    right-skewed distribution.

    Fails on revert in both directions: restore the minimum and the baseline
    below drops to the smallest sample, and the recovery assertion — the
    property that actually motivated the change — is violated."""
    # A deliberately right-skewed pass: a tight bulk plus a long tail, which
    # is the shape the reviewer measured.
    waits_ms = [4, 5, 6, 7, 8, 9, 9, 10, 11, 12, 14, 18, 40, 95, 210]
    accounting = ct.LockWaitAccounting()
    for ms in waits_ms:
        meter = ct.LockWaitMeter()
        meter.lock_acquire_seconds = ms / 1000.0
        accounting.record(meter)
    s = accounting.as_summary()

    ordered = sorted(waits_ms)
    median = ordered[len(ordered) // 2]
    assert s["lock_wait_ms_baseline_per_attempt"] == median
    assert s["lock_wait_ms_min"] == min(waits_ms)
    # The minimum is still REPORTED — it just no longer drives the correction.
    assert s["lock_wait_ms_baseline_per_attempt"] > s["lock_wait_ms_min"]

    # Read the totals off the summary, not off the nominal list: seconds
    # round-trip through `int(seconds * 1000)` and can land a millisecond
    # low. The PROPERTY under test is the estimator, not float arithmetic.
    total = s["lock_wait_ms"]
    n = s["lock_wait_measurements"]
    net_median = max(0, total - median * n)
    net_min = max(0, total - s["lock_wait_ms_min"] * n)
    assert s["lock_wait_ms_net"] == net_median
    # THE PROPERTY: the median recovers materially more of the bias than the
    # minimum did. On this fixture the min recovers ~40%, the median ~90%.
    assert (total - net_median) > 2 * (total - net_min), (total, net_min, net_median)

    # ...and the conservative direction is preserved: a median sits below a
    # right-skewed mean, so the net stays an UPPER bound on the true wait.
    assert median < total / n
    assert s["lock_wait_ms_net"] > 0

    # A single measurement still cannot distinguish bias from signal.
    lone = ct.LockWaitAccounting()
    meter = ct.LockWaitMeter()
    meter.lock_acquire_seconds = 0.400
    lone.record(meter)
    assert lone.as_summary()["lock_wait_ms_baseline_per_attempt"] == 0


# --- A2: the decision bucket is >=1000ms, not >=100ms ---------------------

def test_the_decision_tail_excludes_the_fsync_contaminated_100ms_bucket():
    """A2 (independent review of this branch). The `>=100 ms` bucket the
    runbook nominated as the decision basis is contaminated: on two genuinely
    uncontended passes the reviewer measured

        lock_wait_ms_max=485  write_hold_ms_max=484
        lock_wait_ms_max=480  write_hold_ms_max=479

    — ONE fsync stall counted once as a hold and once again as a "lock wait",
    landing a phantom sample in `100-1000` on a pass whose true wait is zero.
    Over ~100 counted passes that is ~100 phantom samples in the very bucket a
    threshold would be read from.

    Fails on revert: move the edge back to 100 and the 485 ms self-stall below
    counts toward the decision."""
    assert ct.RECONCILE_LOCK_WAIT_DECISION_EDGE_MS == 1000
    assert ct.RECONCILE_LOCK_WAIT_DECISION_LABELS == (
        "1000-5000", "5000-15000", "15000-30000", ">=30000",
    )

    # The measured contaminated pass: one 485ms sample against a 484ms hold.
    accounting = ct.LockWaitAccounting()
    for wait, hold in ((0.005, 0.004), (0.007, 0.006), (0.485, 0.484)):
        meter = ct.LockWaitMeter()
        meter.lock_acquire_seconds = wait
        meter.hold_seconds = hold
        accounting.record(meter)
    s = accounting.as_summary()
    assert s["lock_wait_histogram_ms"]["100-1000"] == 1, s["lock_wait_histogram_ms"]
    # The old protocol would have counted that phantom; the new one does not.
    assert ct.lock_wait_decision_tail(s["lock_wait_histogram_ms"]) == 0
    # And the cross-check the runbook gives a reader holds on this pass: the
    # largest "wait" is within a few ms of the pass's own largest HOLD.
    assert abs(s["lock_wait_ms_max"] - s["write_hold_ms_max"]) <= 2, s

    # Real, unambiguous contention still counts.
    contended = ct.LockWaitAccounting()
    for wait in (0.005, 1.2, 20.0):
        meter = ct.LockWaitMeter()
        meter.lock_acquire_seconds = wait
        contended.record(meter)
    assert ct.lock_wait_decision_tail(
        contended.as_summary()["lock_wait_histogram_ms"]
    ) == 2
    assert ct.lock_wait_decision_tail(None) == 0
    assert ct.lock_wait_decision_tail({}) == 0


# --- A3: prelude-blocked passes leave the distribution --------------------

def test_a_prelude_blocked_pass_is_flagged_ineligible_for_the_distribution():
    """A3 (independent review of this branch). H3's fix made the abandon path
    emit a full contract of real zeros instead of `None`s — honest per pass,
    and WRONG to aggregate: summed into a histogram those zeros are
    indistinguishable from a healthy pass. "A zero row averaged in as benign
    is worse for the distribution than a missing row was."

    The documented diagnostic did not fire either: both measured
    prelude-blocked passes came in at 15.04s against a 42s model and a 30s
    deadline, i.e. UNDER the model. The signature that IS unambiguous, and the
    one implemented, is asserted here."""
    prelude_blocked = {
        "status": "db_locked", "lock_wait_measurements": 0, "duration_ms": 15040,
    }
    assert ct.lock_wait_distribution_eligible(prelude_blocked) is False

    # A pass abandoned INSIDE the write phase measured something, and is the
    # most informative sample the distribution has. It must stay in.
    abandoned_mid_write = {
        "status": "db_locked", "lock_wait_measurements": 7, "duration_ms": 30000,
    }
    assert ct.lock_wait_distribution_eligible(abandoned_mid_write) is True

    # A validation refusal is not db_locked, so it is not prelude-blocked...
    assert ct.lock_wait_distribution_eligible(
        {"status": "invalid_limit", "lock_wait_measurements": 0, "duration_ms": 1}
    ) is True
    # ...and a healthy pass is obviously eligible.
    assert ct.lock_wait_distribution_eligible(
        {"status": "ok", "lock_wait_measurements": 120, "duration_ms": 4000}
    ) is True


def test_a_real_blocked_prelude_reports_the_signature_end_to_end(tmp_path):
    """The same classification, driven through a REAL blocked prelude rather
    than asserted on a dict: an EXCLUSIVE holder blocks the governed path's
    reads, and the resulting refusal must carry the whole signature AND the
    verdict computed from it.

    Fails on revert: drop the field and the assertion below raises."""
    d = tmp_path / "eligible"
    d.mkdir()
    db = _FileDb(d, tokens=5, busy_timeout_seconds=6.0)
    holder = _Holder(db.path, exclusive=True)
    session = db.Factory()
    try:
        r = run_scheduled_reconciliation(
            session,
            settings=Settings(
                enable_crypto_tape_reconciler=True,
                crypto_tape_reconciler_window_hours=48,
                crypto_tape_reconciler_limit=1000,
                crypto_tape_reconciler_batch_size=5,
                crypto_tape_reconciler_max_duration_seconds=2.0,
            ),
            recorder=CryptoLifecycleTapeRecorder(
                CryptoTapeConfig(chain=CHAIN, lock_dir=d)
            ),
            sleeper=lambda _s: None,
        )
    finally:
        session.close()
        holder.release()
        db.close()

    # The signature, field by field...
    assert r["status"] == "db_locked", r
    assert r["lock_wait_measurements"] == 0, r
    assert r["duration_ms"] > 0, r
    # ...and the verdict derived from it, on the result itself.
    assert r["lock_wait_distribution_eligible"] is False, r
    assert r["external_calls"] == 0


# --- orphaned `status='running'` rows leave the distribution too -----------

def test_an_orphaned_running_run_row_is_excluded_and_counted_separately(filedb):
    """Carry-forward from the final review of this branch. A SIGKILL
    mid-finalize (the `TimeoutStartSec` outcome) leaves the committed token
    batches durable and the run row at `status='running'` forever. Nothing
    jams — the overlap guard is a flock released on process death — but
    NOTHING RECONCILES those rows, and the counted-passes analysis reads run
    rows, so an orphan would otherwise be counted as a pass that finished.

    Driven through the real code path rather than asserted on a dict: the
    finalize commit is failed, which leaves EXACTLY the on-disk state a
    SIGKILL leaves (row created, never finalized).

    Fails on revert: without `lock_wait_run_row_orphaned` there is nothing to
    call, and the orphan is indistinguishable from a completed pass."""
    # A pass that finalizes is NOT an orphan.
    session = filedb.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    healthy = rec.run_once(
        session, limit=20, hours=48, batch_size=5,
        max_duration_seconds=20.0, sleeper=lambda _s: None,
    )
    session.close()
    assert healthy["status"] == "ok", healthy

    verify = filedb.Factory()
    finalized = verify.execute(
        select(CryptoTokenLifecycleRun).order_by(CryptoTokenLifecycleRun.id.desc())
    ).scalars().first()
    verify.close()
    assert finalized.status != "running", finalized.status
    assert ct.lock_wait_run_row_orphaned(finalized.status, finalized.config) is False

    # A pass whose finalize never lands leaves the row at `running` with no
    # `write_coordination` — the two are written by the SAME commit.
    session = filedb.Factory()
    rec2 = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    original = rec2._commit_with_retry

    def _lose_the_finalize(sess, prepare, max_attempts, retry_seconds,
                           sleeper=time.sleep, **kw):
        if max_attempts == ct.RECONCILE_FINALIZE_MAX_LOCK_ATTEMPTS:
            return (False, 1)          # the finalize, lost — nothing staged
        return original(sess, prepare, max_attempts, retry_seconds, sleeper, **kw)

    rec2._commit_with_retry = _lose_the_finalize
    try:
        lost = rec2.run_once(
            session, limit=20, hours=48, batch_size=5,
            max_duration_seconds=20.0, max_lock_attempts=3,
            lock_retry_seconds=0.0, sleeper=lambda _s: None,
        )
    finally:
        rec2._commit_with_retry = original
        session.close()
    assert "status=running" in (lost.get("error") or ""), lost

    verify = filedb.Factory()
    orphan = verify.get(CryptoTokenLifecycleRun, lost["tape_run_id"])
    verify.close()
    assert orphan.status == "running", orphan.status
    assert "write_coordination" not in (orphan.config or {})
    assert ct.lock_wait_run_row_orphaned(orphan.status, orphan.config) is True

    # The classifier is exact, not a guess at `status` alone: a row carrying
    # `write_coordination` finalized by definition, and a `None` config is the
    # created-but-never-finalized shape too.
    assert ct.lock_wait_run_row_orphaned("running", None) is True
    assert ct.lock_wait_run_row_orphaned("running", {}) is True
    assert ct.lock_wait_run_row_orphaned(
        "running", {"write_coordination": {"lock_retry_events": 0}}
    ) is False
    assert ct.lock_wait_run_row_orphaned("ok", None) is False
    assert ct.lock_wait_run_row_orphaned(None, None) is False


def test_the_cli_prints_the_lock_wait_contract_on_a_refusal(filedb):
    """A refusal carries the whole lock-wait contract (BLOCKER-1(a)) but the
    CLI's refusal branch printed only `status` and `error` — so the
    `db_locked` passes that carry the most information for the gate were
    invisible to the operator running the counted `--force` passes, and the
    prelude-blocked tally could not be kept at all.

    Fails on revert: remove the print and the refusal branch stops naming
    these fields."""
    source = (REPO / "app" / "cli.py").read_text()
    marker = "a unit that reconciles nothing must never look healthy"
    assert marker in source
    # The refusal branch is everything from that marker to its `return -1`.
    branch = source.split(marker, 1)[1].split("return -1", 1)[0]
    for field in (
        "lock_wait_ms", "lock_wait_measurements", "duration_ms",
        "lock_wait_distribution_eligible", "lock_wait_histogram_ms",
    ):
        assert field in branch, f"the CLI refusal branch never prints {field}"

    # ...and every field it names is actually present on a real refusal, so
    # the branch cannot print a row of `None`s.
    session = filedb.Factory()
    r = run_scheduled_reconciliation(
        session,
        settings=Settings(
            enable_crypto_tape_reconciler=True,
            crypto_tape_reconciler_window_hours=48,
            crypto_tape_reconciler_limit=0,
        ),
    )
    session.close()
    assert r["status"] == "invalid_limit", r
    for field in (
        "lock_wait_ms", "lock_wait_measurements", "duration_ms",
        "lock_wait_distribution_eligible", "lock_wait_histogram_ms",
    ):
        assert r.get(field) is not None, f"{field} missing from a refusal"


# --- A4: the whole prelude is timed, not only its classify step -----------

def test_the_whole_budgeted_prelude_is_timed_not_only_classify_backlog(filedb):
    """A4 (independent review of this branch). `classify_ms` wraps ONLY
    `classify_backlog`, while `backlog_size`, `_universe`, `universe_size`,
    `unreconciled_backlog` and both frontier queries sit outside that timer and
    INSIDE the budgeted block — so a block in any of those five inflated
    `duration_ms` alone and pointed at nothing. That is why the runbook's
    prelude diagnostic never fired.

    Fails on revert: drop `prelude_ms` and both the summary key and the
    persisted run-row key disappear."""
    session = filedb.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    r = rec.run_once(
        session, limit=20, hours=48, batch_size=5, include_backlog=True,
        max_duration_seconds=20.0, sleeper=lambda _s: None,
    )
    session.close()

    assert "prelude_ms" in r, r
    assert isinstance(r["prelude_ms"], float)
    assert r["prelude_ms"] >= 0.0
    # The whole block is at least its own classify step — the containment
    # relationship is the point of the field.
    assert r["classify_ms"] is not None
    assert r["prelude_ms"] >= r["classify_ms"], r
    # ...and it is persisted next to `classify_ms`, not summary-only.
    verify = filedb.Factory()
    run = verify.execute(
        select(CryptoTokenLifecycleRun).order_by(CryptoTokenLifecycleRun.id.desc())
    ).scalars().first()
    frontier = (run.config or {})["frontier"]
    verify.close()
    assert "prelude_ms" in frontier, frontier
    assert frontier["prelude_ms"] >= frontier["classify_ms"]


def test_the_prelude_is_timed_even_when_the_backlog_lane_is_off(filedb):
    """`classify_ms` is None with `include_backlog=False`, but the other
    prelude queries still run and can still block — so `prelude_ms` must be
    reported on EVERY pass, not only backlog ones."""
    session = filedb.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    r = rec.run_once(
        session, limit=20, hours=48, batch_size=5,
        max_duration_seconds=20.0, sleeper=lambda _s: None,
    )
    session.close()
    assert r["classify_ms"] is None
    assert isinstance(r["prelude_ms"], float)
    assert r["prelude_ms"] >= 0.0


# --- A5 / B1: the model, recorded per pass; the finalize, sized -----------

def test_the_pass_records_its_wall_time_against_the_model(filedb):
    """A5 (independent review of this branch). "Model, not guarantee" is
    enough for the attended `--force` phase but not for an unattended timer,
    and no better constant exists — the overshoot term tracks HOST LOAD (1.01x
    idle EVO, 5.80x dev Mac at load 5-6). What can be required instead is
    OBSERVED non-exceedance, which needs the model recorded next to the wall
    time on every counted pass.

    Fails on revert: drop the fields and the operator is back to re-deriving
    the model by hand from constants."""
    session = filedb.Factory()
    r = run_scheduled_reconciliation(
        session,
        settings=Settings(
            enable_crypto_tape_reconciler=True,
            crypto_tape_reconciler_window_hours=48,
            crypto_tape_reconciler_limit=1000,
            crypto_tape_reconciler_batch_size=5,
            crypto_tape_reconciler_max_duration_seconds=20.0,
        ),
        recorder=CryptoLifecycleTapeRecorder(
            CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent)
        ),
        sleeper=lambda _s: None,
    )
    session.close()

    assert r["status"] in ("ok", "truncated", "partial"), r
    assert r["wall_time_model_ms"] > 0, r
    # The model is strictly larger than the data deadline it contains...
    assert r["wall_time_model_ms"] > 20_000, r
    # ...and an uncontended pass must not exceed it. This is precondition 1
    # for the recurring timer, asserted on the one host we can assert it on.
    assert r["wall_time_model_exceeded"] is False, (
        r["duration_ms"], r["wall_time_model_ms"]
    )
    assert r["duration_ms"] <= r["wall_time_model_ms"]
    assert r["lock_wait_distribution_eligible"] is True, r


def test_the_modelled_wall_time_is_the_documented_derivation():
    """The arithmetic, pinned separately from the plumbing — this is the
    number the unit file's `TimeoutStartSec` derivation and the runbook's
    timer precondition both quote."""
    model = ct.modelled_pass_wall_seconds(
        deadline_seconds=20.0,
        lock_wait_budget_seconds=5.0,
        finalize_lock_wait_budget_seconds=30.0,
    )
    # 20 (deadline) + 3*4*5 + 2*3 (one in-flight batch ladder) + 1*2.0*30
    # (the single-attempt finalize at the inherited busy timeout) = 146s.
    assert model == pytest.approx(146.0)
    # A dry run / legacy pass has no budget to model; the deadline is all
    # there is, and the missing terms are omitted rather than guessed at 0.
    assert ct.modelled_pass_wall_seconds(
        deadline_seconds=20.0,
        lock_wait_budget_seconds=None,
        finalize_lock_wait_budget_seconds=None,
    ) == pytest.approx(20.0)


def test_the_finalize_commit_gets_exactly_one_attempt(filedb):
    """B1 (independent review of this branch). The restored finalize inherits
    the connection's busy timeout (30s in production), so a 3-attempt ladder
    against a holder that never releases costs `3 x 30s x overshoot + 2 x 3s`:
    ~97s at EVO's measured 1.01x, ~186s at the shipped 2.0 constant, ~528s at
    the 5.80x measured on this dev Mac at load 5-6 — against
    `TimeoutStartSec=5min`. A real blocked pass measured lock_wait_ms=206284.

    One attempt, because the finalize is BOOKKEEPING: a second attempt 3s
    later against the same 20-45s holder rarely helps while tripling the
    ceiling, the failure mode is non-corrupting (batches durable, run row left
    at `running`), and the accounting still reaches the operator through the
    returned summary.

    Asserted on the ladder itself, with no contention needed: the BATCH
    commits get the caller's `max_lock_attempts`, the FINALIZE gets 1."""
    session = filedb.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    seen: list[int] = []
    original = rec._commit_with_retry

    def _recording(sess, prepare, max_attempts, retry_seconds, sleeper=time.sleep, **kw):
        seen.append(max_attempts)
        return original(sess, prepare, max_attempts, retry_seconds, sleeper, **kw)

    rec._commit_with_retry = _recording
    try:
        r = rec.run_once(
            session, limit=20, hours=48, batch_size=5,
            max_duration_seconds=20.0, max_lock_attempts=3,
            lock_retry_seconds=0.0, sleeper=lambda _s: None,
        )
    finally:
        rec._commit_with_retry = original
        session.close()

    assert r["status"] == "ok", r
    assert ct.RECONCILE_FINALIZE_MAX_LOCK_ATTEMPTS == 1
    # Two ladders go through this helper: the run row's CREATION commit and
    # its FINALIZE commit (batch commits run their own inline loop). The
    # creation commit keeps the caller's ladder; the finalize does not.
    assert len(seen) == 2, seen
    assert seen[0] == 3, seen            # run-row creation, caller's ladder
    assert seen[-1] == 1, seen           # the finalize, bounded
    # The finalize's own budget is reported, so the model above can be
    # computed from the pass rather than from assumed constants.
    assert r["finalize_lock_wait_budget_ms"] == 30000, r


def test_the_budget_adds_no_network_surface():
    """`external_calls == 0` must stay STRUCTURALLY true, not just reported."""
    source = (REPO / "app" / "services" / "crypto_tape.py").read_text()
    for forbidden in ("httpx", "requests", "aiohttp", "urllib", "socket"):
        assert forbidden not in source, f"crypto_tape.py gained a {forbidden} import"


# --- CRYPTO-RECONCILER-LOCK-WAIT-PHASE-ATTRIBUTION-001 ---------------------
#
# THE MEASUREMENT THAT FORCED THE SPLIT. Three production `--force` passes on
# EVO (tape_run_id 3813/3814/3815) at load 0.4-0.8:
#
#   pass  duration_ms  lock_wait_ms  max    measurements  >=1000 tail  batches
#   1     30,126       4,615         1,191  326           1            323
#   2     30,148       4,461         1,334  389           1            387
#   3     30,117       4,502         1,337  383           1            381
#
# EXACTLY ONE sample in the decision bucket on every pass — never zero, never
# two — with maxima clustered just above the 1000 ms edge. Random co-tenant
# contention does not produce that; a once-per-pass systematic event does, and
# the pass has TWO of them (the run row's CREATION commit and its FINALIZE
# commit; `measurements = batches + 2 + retries` on all three passes). A timer
# threshold read off the pass TOTAL would therefore be measuring the instrument,
# not the host.
#
# The three end-to-end tests below drive one real, phase-scoped competing
# RESERVED holder each and assert the wait lands in that phase AND NOT in the
# others. They are the pin that makes the split trustworthy: swap any two phase
# labels at the call sites and each of them fails.
#
# The holder is deliberately short. It only has to clear the 1000 ms decision
# edge with margin; on this dev Mac a blocked acquisition overshoots its budget
# (measured up to 5.80x under load), so a longer hold buys no discrimination and
# costs the whole suite. The 49 s this file was cut to must not regress.
_PHASE_HOLD_SECONDS = 1.6


def test_the_per_phase_tails_sum_to_the_histogram_tail():
    """The split must be a DECOMPOSITION of the same samples, not a second,
    differently-collected series — otherwise "batch tail 0, total tail 1" could
    mean either good news or a lost sample.

    Pinned as an identity over both routes: `_in_decision_tail` (used per
    sample, per phase) and `lock_wait_decision_tail` (used over the whole
    histogram) must agree on every sample, including the ones exactly ON the
    edge, which is where an off-by-one would live."""
    accounting = ct.LockWaitAccounting()
    scripted = [
        (ct.LOCK_WAIT_PHASE_RUN_ROW, 1.191),      # the production signature
        (ct.LOCK_WAIT_PHASE_BATCH, 0.004),
        (ct.LOCK_WAIT_PHASE_BATCH, 0.485),        # fsync self-stall, not a wait
        (ct.LOCK_WAIT_PHASE_BATCH, 0.999),        # one ms BELOW the edge
        (ct.LOCK_WAIT_PHASE_BATCH, 1.000),        # exactly ON the edge
        (ct.LOCK_WAIT_PHASE_FINALIZE, 20.0),
    ]
    for phase, wait in scripted:
        meter = ct.LockWaitMeter(phase)
        meter.lock_acquire_seconds = wait
        accounting.record(meter)
    s = accounting.as_summary()

    total_tail = ct.lock_wait_decision_tail(s["lock_wait_histogram_ms"])
    assert total_tail == s["lock_wait_decision_tail"] == 3, s
    assert sum(p["decision_tail"] for p in s["lock_wait_phases"].values()) == total_tail
    assert s["lock_wait_decision_tail_batch"] == 1, s          # the 1.000, only
    assert s["lock_wait_decision_tail_finalize"] == 1, s
    assert s["lock_wait_phases"][ct.LOCK_WAIT_PHASE_RUN_ROW]["decision_tail"] == 1
    # Measurements decompose too — no sample is dropped or double-counted.
    assert sum(
        p["measurements"] for p in s["lock_wait_phases"].values()
    ) == s["lock_wait_measurements"] == len(scripted)
    # The finalize stays visible in its own right, not merely subtracted away.
    assert s["finalize_lock_wait_ms"] == 20000, s
    assert s["finalize_lock_wait_measurements"] == 1, s
    # The operator-facing predicate reads the same numbers off the blob.
    assert ct.lock_wait_phase_decision_tail(s, ct.LOCK_WAIT_PHASE_BATCH) == 1
    assert ct.lock_wait_phase_decision_tail(s, ct.LOCK_WAIT_PHASE_FINALIZE) == 1
    assert ct.lock_wait_phase_decision_tail(None, ct.LOCK_WAIT_PHASE_BATCH) == 0
    assert ct.lock_wait_phase_decision_tail({}, ct.LOCK_WAIT_PHASE_BATCH) == 0

    # An UNLABELLED sample never lands in `batch`. A meter built without a
    # phase gets its own visible bucket instead, because silently folding it
    # into the timer's basis is the exact contamination this milestone removes.
    stray = ct.LockWaitAccounting()
    m = ct.LockWaitMeter()
    m.lock_acquire_seconds = 9.0
    stray.record(m)
    t = stray.as_summary()
    assert t["lock_wait_decision_tail_batch"] == 0, t
    assert t["lock_wait_decision_tail"] == 1, t
    assert t["lock_wait_phases"][ct.LOCK_WAIT_PHASE_UNATTRIBUTED]["decision_tail"] == 1


def test_a_holder_scoped_to_a_mid_pass_batch_lands_in_the_batch_phase(filedb):
    """A real RESERVED holder taken AFTER the first batch commits and released
    while the second batch is blocked. Its wait is host contention, and it is
    the ONLY class a recurring-timer threshold may be derived from — so it must
    land in `batch` and nowhere else.

    Fails if the attribution were wrong in either direction: label the batch
    ladder `finalize` and `lock_wait_decision_tail_batch` goes to 0; label the
    bookkeeping commits `batch` and the finalize/run-row assertions below stop
    discriminating."""
    session = filedb.Factory()
    holders: list[_TimedHolder] = []

    def _sleeper(seconds: float) -> None:
        # The post-batch yield fires only after a REAL commit — the first call
        # is the moment batch 1 is durable and batch 2 has not started.
        if not holders:
            holders.append(_TimedHolder(filedb.path, _PHASE_HOLD_SECONDS))

    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    try:
        r = rec.run_once(
            session, limit=20, hours=48, batch_size=5,
            # The derived budget is a quarter of what remains, i.e. ~5s here —
            # comfortably longer than the hold, so batch 2 BLOCKS AND THEN
            # SUCCEEDS rather than failing, which is the common production
            # shape and the one that produces a recorded sample.
            max_duration_seconds=20.0,
            max_lock_attempts=1, lock_retry_seconds=0.0,
            # Not the phase under test, and left at the connection's inherited
            # 30s it has dominated this file's wall time before.
            finalize_lock_wait_budget_seconds=0.5,
            sleeper=_sleeper,
        )
    finally:
        session.close()
        for h in holders:
            h.join()

    assert holders, "the post-batch yield never fired, so nothing was contended"
    assert r["external_calls"] == 0
    phases = r["lock_wait_phases"]
    assert phases[ct.LOCK_WAIT_PHASE_BATCH]["lock_wait_ms_max"] >= 1000, r
    assert r["lock_wait_decision_tail_batch"] >= 1, r
    # ...and neither once-per-pass bookkeeping commit absorbed it.
    assert r["lock_wait_decision_tail_finalize"] == 0, r
    assert phases[ct.LOCK_WAIT_PHASE_RUN_ROW]["decision_tail"] == 0, r
    assert r["finalize_lock_wait_ms"] < 1000, r


def test_a_holder_released_only_after_the_pass_returns_lands_in_the_finalize_phase(
    filedb,
):
    """The hypothesis this milestone exists to test, driven directly: a holder
    introduced once every batch is durable, and released only after `run_once`
    has returned, can be waited on by NOTHING except the run row's finalize
    commit. Its wait must therefore appear in `finalize` and must NOT move
    `lock_wait_decision_tail_batch`.

    `limit=5, batch_size=5` makes the single batch the whole data pass, so the
    sleeper's one call is unambiguously "all batches are committed"."""
    session = filedb.Factory()
    holder: list[_Holder] = []

    def _sleeper(seconds: float) -> None:
        if not holder:
            holder.append(_Holder(filedb.path))

    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    try:
        r = rec.run_once(
            session, limit=5, hours=48, batch_size=5,
            max_duration_seconds=20.0,
            max_lock_attempts=1, lock_retry_seconds=0.0,
            # The finalize gets ONE attempt (RECONCILE_FINALIZE_MAX_LOCK_
            # ATTEMPTS) at this budget, then gives up — so the pass returns in
            # ~1.6s instead of waiting out the connection's inherited 30s.
            finalize_lock_wait_budget_seconds=_PHASE_HOLD_SECONDS,
            sleeper=_sleeper,
        )
    finally:
        session.close()
        if holder:
            holder[0].release()

    assert holder, "the post-batch yield never fired, so the finalize was not contended"
    assert r["external_calls"] == 0
    assert r["batches_committed"] == 1, r
    # The finalize lost its single attempt against the still-held lock; the
    # committed batch stays durable and the run row stays at `running`.
    assert "status=running" in (r.get("error") or ""), r
    # THE ATTRIBUTION. The whole-pass tail carries the production signature of
    # exactly one sample — and it is NOT contention.
    assert r["lock_wait_decision_tail"] >= 1, r
    assert r["lock_wait_decision_tail_finalize"] == 1, r
    assert r["finalize_lock_wait_ms"] >= 1000, r
    assert r["finalize_lock_wait_measurements"] == 1, r
    assert r["lock_wait_decision_tail_batch"] == 0, r
    assert r["lock_wait_phases"][ct.LOCK_WAIT_PHASE_RUN_ROW]["decision_tail"] == 0, r


def test_a_holder_taken_before_the_pass_starts_lands_in_the_run_row_phase(filedb):
    """The SECOND once-per-pass systematic event, and the reason the split has
    four names rather than the three the milestone was scoped with.

    A RESERVED holder taken before `run_once` does not block the selection
    prelude (RESERVED admits readers) — the first thing it blocks is the run
    row's CREATION commit, the pass's first write, on a cold page cache. That
    is a second source of a once-per-pass `>=1000` sample, and folding it into
    `batch` would leave the timer's basis contaminated by exactly the defect
    class this milestone removes."""
    session = filedb.Factory()
    holder = _TimedHolder(filedb.path, _PHASE_HOLD_SECONDS)
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    try:
        r = rec.run_once(
            session, limit=5, hours=48, batch_size=5,
            max_duration_seconds=20.0,
            max_lock_attempts=1, lock_retry_seconds=0.0,
            finalize_lock_wait_budget_seconds=0.5,
            sleeper=lambda _s: None,
        )
    finally:
        session.close()
        holder.join()

    assert r["external_calls"] == 0
    phases = r["lock_wait_phases"]
    assert phases[ct.LOCK_WAIT_PHASE_RUN_ROW]["measurements"] == 1, r
    assert phases[ct.LOCK_WAIT_PHASE_RUN_ROW]["decision_tail"] == 1, r
    # The pass-total tail reads exactly like the three production passes —
    # and this one demonstrably contains no contention at all.
    assert r["lock_wait_decision_tail"] >= 1, r
    assert r["lock_wait_decision_tail_batch"] == 0, r
    assert r["lock_wait_decision_tail_finalize"] == 0, r


def test_the_run_row_persists_the_phase_split_without_the_finalize_mirrors(filedb):
    """What a later reader of the run rows can and cannot conclude.

    `write_coordination` is staged INSIDE the finalize commit, so the finalize's
    own wait cannot be in it. The batch entry — the timer's basis — is complete
    and persisted; the flat `*_finalize` mirrors are OMITTED rather than written
    as zeros, because a persisted zero reads as "the finalize did not wait",
    which is a claim the row cannot make about the commit writing it."""
    session = filedb.Factory()
    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain=CHAIN, lock_dir=filedb.path.parent))
    r = rec.run_once(
        session, limit=20, hours=48, batch_size=5,
        max_duration_seconds=20.0, sleeper=lambda _s: None,
    )
    session.close()
    assert r["status"] == "ok", r

    verify = filedb.Factory()
    run = verify.execute(
        select(CryptoTokenLifecycleRun).order_by(CryptoTokenLifecycleRun.id.desc())
    ).scalars().first()
    coordination = (run.config or {})["write_coordination"]
    verify.close()

    assert "lock_wait_phases_before_finalize" in coordination, coordination
    assert "lock_wait_decision_tail_before_finalize" in coordination, coordination
    persisted = coordination["lock_wait_phases_before_finalize"]
    assert persisted[ct.LOCK_WAIT_PHASE_BATCH]["measurements"] == r["batches_committed"]
    assert persisted[ct.LOCK_WAIT_PHASE_FINALIZE]["measurements"] == 0, persisted
    for omitted in (
        "finalize_lock_wait_ms", "finalize_lock_wait_measurements",
        "lock_wait_decision_tail_finalize", "lock_wait_decision_tail_batch",
        "lock_wait_phases", "lock_wait_decision_tail",
    ):
        assert omitted not in coordination, (
            f"{omitted} is on the run row unqualified — either as a misleading "
            "structural zero or as a duplicate of the phase container"
        )
    # The operator-facing predicate reads the persisted blob directly, so the
    # protocol never depends on remembering the suffix.
    assert ct.lock_wait_phase_decision_tail(
        coordination, ct.LOCK_WAIT_PHASE_BATCH
    ) == persisted[ct.LOCK_WAIT_PHASE_BATCH]["decision_tail"]


def test_the_prelude_is_a_named_phase_this_instrument_cannot_meter():
    """Stated rather than discovered later. The selection prelude IS budgeted
    (`_lock_wait_budgeted_reads`) but it is not METERED: `LockWaitMeter` times
    the transaction's first WRITE statement, and the prelude is pure reads, so a
    blocked SELECT there produces no sample at all — not a zero one.

    So `prelude` is a named constant but deliberately NOT a bucket in
    `lock_wait_phases`: a permanently-zero entry alongside the real ones would
    read as "the prelude never waits", which is a stronger claim than this
    instrument can make. The end-to-end evidence that a blocked prelude reports
    `lock_wait_measurements == 0` is
    `test_a_real_blocked_prelude_reports_the_signature_end_to_end`; this test
    pins that the phase vocabulary agrees with it."""
    assert ct.LOCK_WAIT_PHASE_PRELUDE == "prelude"
    assert ct.LOCK_WAIT_PHASE_PRELUDE not in ct.LOCK_WAIT_PHASES
    assert ct.LOCK_WAIT_PHASES == ("run_row", "batch", "finalize")
    assert ct.LOCK_WAIT_PHASE_PRELUDE not in ct.LockWaitAccounting().phases()
    # A read statement is not a lock-acquisition point for this meter, which is
    # the mechanical reason the prelude cannot be metered by it.
    assert not ct._is_write_statement("SELECT 1")

    runbook = (REPO / "docs" / "EVO_X2_RUNBOOK.md").read_text()
    assert "the prelude is bounded but not metered" in runbook.lower()


def test_the_runbook_bases_the_timer_decision_on_the_batch_phase_only():
    """The protocol change, pinned where an editor will trip over it. The timer
    decision is derived from BATCH/read-path contention, explicitly not from a
    known systematic once-per-pass event, and the finalize tail is tracked
    against `TimeoutStartSec` instead of against the contention threshold. The
    pre-existing numeric revisit trigger stays."""
    runbook = (REPO / "docs" / "EVO_X2_RUNBOOK.md").read_text()
    assert "lock_wait_decision_tail_batch" in runbook
    assert "lock_wait_phase_decision_tail" in runbook
    assert "write_hold_ms_max > 700" in runbook       # the trigger is untouched
    # The three production passes that motivated the split are recorded with
    # their numbers, so the "exactly one sample every pass" observation cannot
    # be softened into a vague claim later.
    for token in ("3813", "1,191", "1,334", "1,337"):
        assert token in runbook, f"the motivating measurement lost {token}"
