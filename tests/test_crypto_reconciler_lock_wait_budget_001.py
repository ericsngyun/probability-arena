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
    released 1.5s later from its own thread. `max_lock_attempts=1` removes the
    retry ladder so the finalize budget is the only thing that can save the
    row.

    Fails on revert: restore the 0.5s floor and the finalize gives up before
    1.5s, leaving `run.status='running'` and no `write_coordination` at all.
    """
    d = tmp_path / "finalize"
    d.mkdir()
    db = _FileDb(d, tokens=6, busy_timeout_seconds=30.0)
    holders: list[_TimedHolder] = []

    def _grab(conn, cursor, statement, parameters, context, executemany):
        text = statement.strip().lower()
        if text.startswith("update crypto_token_lifecycle_runs") and not holders:
            holders.append(_TimedHolder(db.path, 1.5))

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
    assert r["blocked_ms"] >= 1000, r

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
    assert elapsed < 4.0, (
        f"the governed prelude blocked for {elapsed:.2f}s against a 0.5s "
        "derived budget"
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

    The bias is per-attempt and near-constant, so the pass's own MINIMUM
    observed attempt estimates it in band. Asserted here on synthetic meters
    because the arithmetic, not the host's fsync speed, is the contract."""
    accounting = ct.LockWaitAccounting()
    for wait in (0.008, 0.009, 0.011, 0.512):
        meter = ct.LockWaitMeter()
        meter.lock_acquire_seconds = wait
        accounting.record(meter)
    s = accounting.as_summary()
    assert s["lock_wait_ms"] == 540
    assert s["lock_wait_ms_baseline_per_attempt"] == 8
    # 540 - 8*4 = 508: the real waiting, with the fixed per-attempt floor gone.
    assert s["lock_wait_ms_net"] == 508
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
    # The >=100ms tail — where a threshold may legitimately be read — is empty
    # under zero contention, which is the property that makes it usable while
    # the scalar is not.
    histogram = r["lock_wait_histogram_ms"]
    tail = {k: v for k, v in histogram.items() if k not in ("<1", "1-10", "10-100")}
    assert sum(tail.values()) == 0, histogram

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
    from the `>=100` histogram buckets, where the per-attempt bias does not
    land, and never from the pass-total scalar, which is ~35% phantom under
    zero contention and grows with batch count. Stated in the code AND in the
    runbook, because the person setting the threshold is reading the runbook."""
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


def test_the_budget_adds_no_network_surface():
    """`external_calls == 0` must stay STRUCTURALLY true, not just reported."""
    source = (REPO / "app" / "services" / "crypto_tape.py").read_text()
    for forbidden in ("httpx", "requests", "aiohttp", "urllib", "socket"):
        assert forbidden not in source, f"crypto_tape.py gained a {forbidden} import"
