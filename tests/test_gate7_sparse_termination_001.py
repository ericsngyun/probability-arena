"""GATE7-SPARSE-UNITS-001 — the sparse observer is bounded from OUTSIDE, and
says so when it is.

WHY THESE TESTS RUN A REAL CHILD PROCESS. The defect is that nothing in the
pass bounds it:

  * `httpx.Timeout(15.0)` is PER SOCKET OPERATION, not per request. A provider
    that keeps dripping bytes below that interval never trips connect, write,
    read or pool.
  * `_fetch_phase` checks its own deadline only at the TOP OF THE TOKEN LOOP,
    so a request that has already started runs to completion, however long that
    is.
  * there is no `asyncio.wait_for`, no `SIGALRM`, and no watchdog anywhere in
    the path.

An IN-PROCESS test cannot prove a claim about a process. It can only prove
something about a coroutine it is already sharing an event loop with — and this
repo's suite is documented as structurally blind to exactly this class for
exactly that reason. So every test here spawns a real interpreter, gives it a
real trickling HTTP server on loopback, and supervises it from outside the way
systemd does.

WHAT THE PARENT IS STANDING IN FOR. systemd's contract on this unit is:
`TimeoutStartSec` expires -> send `KillSignal` -> wait `TimeoutStopSec` ->
SIGKILL. The parent applies the same MECHANISM with the same signal, read out
of the unit file rather than hard-coded, but at a test-scale start deadline —
the real `TimeoutStartSec` is 5 minutes and no suite should spend that. The
part that is genuinely under test is what the signal does to the process, not
how long systemd waits before sending it; the declared value is separately
pinned by `tests/test_gate7_sparse_units_001.py`.

WHY NOTHING HERE RACES A CLOCK. The child announces, by creating a file, that
it has entered the provider fetch. Every parent-side wait is either "block until
that file exists" or "block until the child exits", both with generous ceilings
that can only be hit by a genuine failure. The one duration-based assertion —
that the child is STILL ALIVE after its own fetch deadline has passed — is
monotone in the safe direction: extra load can only make the child slower to
exit, never faster, and the child cannot exit at all while the provider is
still dripping. There is no `datetime.now()` bound at import in this module.
"""

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.telemetry.sink import read_events

REPO = Path(__file__).resolve().parents[1]
SERVICE_UNIT = REPO / "infra/systemd/user/probability-arena-crypto-sparse-observe.service"

# The child's own fetch deadline, in seconds. Deliberately tiny: the point of
# the "still alive" assertion is that this number bounds NOTHING once a request
# has started, so the smaller it is the more damning the result.
CHILD_FETCH_DEADLINE_S = 0.5
# How long the parent waits for the child to reach the provider. Not a race —
# it is an upper bound on interpreter start + imports + schema + enrol + plan on
# a loaded machine, and blowing it is a real failure, not a flake.
READY_TIMEOUT_S = 180.0
# The child must die within this after the signal. systemd's own budget here is
# `TimeoutStopSec`, unset on this unit, so the manager default of 90 s applies.
STOP_BUDGET_S = 90.0


def declared_kill_signal() -> signal.Signals:
    """The signal the UNIT says systemd will send. Read from the file, so a
    change to `KillSignal=` changes what these tests actually send — a unit that
    silently went back to the SIGTERM default would then be tested under SIGTERM
    and would fail here rather than passing under a signal it no longer uses."""
    text = SERVICE_UNIT.read_text()
    declared = [
        line.partition("=")[2].strip()
        for line in text.splitlines()
        if line.strip().startswith("KillSignal=")
    ]
    assert len(declared) == 1, (
        f"expected exactly one KillSignal= directive, got {declared}. Without "
        "one systemd sends SIGTERM, whose default CPython disposition "
        "terminates the process WITHOUT unwinding — the termination funnel is "
        "then unreachable and a timed-out pass records nothing."
    )
    name = declared[0]
    assert name == "SIGINT", (
        f"KillSignal={name}: only SIGINT has a default CPython handler that "
        "RAISES (KeyboardInterrupt, on the main thread) and therefore unwinds "
        "into the CLI-boundary termination funnel"
    )
    return getattr(signal, name)


def declared_timespan(key: str) -> float:
    text = SERVICE_UNIT.read_text()
    values = [
        line.partition("=")[2].strip()
        for line in text.splitlines()
        if line.strip().startswith(f"{key}=")
    ]
    assert len(values) == 1, (key, values)
    total = 0.0
    units = {"": 1.0, "s": 1.0, "sec": 1.0, "m": 60.0, "min": 60.0,
             "h": 3600.0, "hour": 3600.0}
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]*)", values[0]):
        assert unit in units, (key, unit)
        total += float(number) * units[unit]
    assert total > 0, (key, values)
    return total


# --- the child ----------------------------------------------------------------
#
# Everything below runs in a PRISTINE interpreter with `cwd` inside the tmp dir,
# so the repo's own `.env` (`SettingsConfigDict(env_file=".env")` resolves
# relative to cwd) can never leak a real DATABASE_URL or a real flag into it.

_DRIVER = r'''
import os, socket, sys, threading, time
sys.path.insert(0, {root!r})

work = sys.argv[1]
scenario = sys.argv[2]          # "trickle" | "closes_immediately"
sink_mode = sys.argv[3]         # "available" | "unavailable"
ready_path = sys.argv[4]

# --- where the 001A sink lives ------------------------------------------------
if sink_mode == "unavailable":
    # A regular FILE where a directory has to be: `mkdir` fails with ENOTDIR for
    # every uid, including root, so this is not a chmod that a privileged runner
    # would sail through.
    blocker = os.path.join(work, "blocker")
    open(blocker, "w").write("a regular file, not a directory")
    os.environ["SQLITE_TELEMETRY_DIR"] = os.path.join(blocker, "tel")
else:
    os.environ["SQLITE_TELEMETRY_DIR"] = os.path.join(work, "tel")

# --- a real provider on loopback ----------------------------------------------
# TRICKLE mode is the documented unbounded case, made cheap: one byte every
# DRIP_S against a declared Content-Length of a million. Every socket operation
# completes far inside httpx's 15 s per-operation timeout, so nothing in-process
# ever fires, and the response never finishes. CLOSES_IMMEDIATELY mode is the
# harness's negative control: same server, same adapter, same everything, but
# the request fails fast and the pass runs to its own normal completion.
DRIP_S = 0.2
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(16)
PORT = srv.getsockname()[1]

def _serve_one(conn):
    try:
        conn.recv(65536)
        if scenario == "closes_immediately":
            conn.close()
            return
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: 1000000\r\n\r\n")
        while True:
            conn.sendall(b" ")
            time.sleep(DRIP_S)
    except OSError:
        pass

def _accept_forever():
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        threading.Thread(target=_serve_one, args=(conn,), daemon=True).start()

threading.Thread(target=_accept_forever, daemon=True).start()

# --- settings, entirely from the environment ---------------------------------
db_path = os.path.join(work, "sparse.db")
os.environ["DATABASE_URL"] = "sqlite:///" + db_path
os.environ["ENABLE_CRYPTO_SPARSE_OBSERVATION"] = "true"
os.environ["CRYPTO_CHAIN"] = "solana"

import app.adapters.dexscreener as dex
dex.DEXSCREENER_API_BASE = "http://127.0.0.1:%d" % PORT

# READINESS, announced from inside the real adapter. `_get` is the frame that
# owns the httpx request, so the file appearing means the pass is IN the
# provider call — not merely near it. Written before the request, so the parent
# never signals during import, migration, enrolment or planning.
_real_get = dex.DexScreenerAdapter._get
async def _announcing_get(self, *a, **k):
    with open(ready_path, "w") as fh:
        fh.write("fetching\n")
    return await _real_get(self, *a, **k)
dex.DexScreenerAdapter._get = _announcing_get

# --- one enrollable birth whose 6h band is open RIGHT NOW ---------------------
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, CryptoTokenBirthEvent

engine = create_engine("sqlite:///" + db_path)
Base.metadata.create_all(engine)
anchor = datetime.now(timezone.utc) - timedelta(hours=6)
with Session(engine) as s:
    s.add(CryptoTokenBirthEvent(
        chain="solana",
        token_address="So0001" + "T" * 34,
        symbol="T1",
        observed_at=anchor,
        first_evidence_at=anchor,
        launch_source="dexscreener:profile",
        first_pair_address="Pair0001",
        first_dex_id="raydium",
        initial_price_usd=0.001,
        initial_liquidity_usd=5000.0,
        created_at=anchor,
    ))
    s.commit()
engine.dispose()

from app import cli

sys.exit(cli.main([
    "crypto-sparse-observe", "--max-duration-seconds", {deadline!r},
]))
'''


class Child:
    """A running sparse pass, plus the files it announces itself through."""

    def __init__(self, proc, work: Path, ready: Path):
        self.proc = proc
        self.work = work
        self.ready = ready

    @property
    def sink_path(self) -> Path:
        return self.work / "tel" / "sqlite-writes.jsonl"

    def wait_until_fetching(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.ready.exists():
                return
            if self.proc.poll() is not None:
                out, err = self.proc.communicate()
                raise AssertionError(
                    "the child exited before it ever reached the provider "
                    f"(rc={self.proc.returncode}); this harness proves nothing "
                    f"about the fetch path.\nSTDOUT:\n{out}\nSTDERR:\n{err}")
            time.sleep(0.05)
        self.proc.kill()
        raise AssertionError(
            f"the child never reached the provider within {READY_TIMEOUT_S}s")

    def signal_and_reap(self, sig: signal.Signals) -> tuple[int, str, str]:
        self.proc.send_signal(sig)
        try:
            out, err = self.proc.communicate(timeout=STOP_BUDGET_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, err = self.proc.communicate()
            raise AssertionError(
                f"the child survived {sig.name} for {STOP_BUDGET_S}s — longer "
                "than systemd's TimeoutStopSec budget, after which it would be "
                f"SIGKILLed and would record nothing.\nSTDOUT:\n{out}\n"
                f"STDERR:\n{err}")
        return self.proc.returncode, out, err


@pytest.fixture
def spawn(tmp_path):
    """Start one sparse pass in a real child process. Always reaped."""
    started: list[Child] = []

    def _spawn(scenario: str = "trickle", sink: str = "available") -> Child:
        root = str(REPO)
        work = tmp_path / f"work-{len(started)}"
        work.mkdir()
        driver = tmp_path / f"driver-{len(started)}.py"
        driver.write_text(_DRIVER.format(
            root=root, deadline=str(CHILD_FETCH_DEADLINE_S)))
        ready = work / "fetching"
        env = dict(os.environ)
        # The suite's autouse telemetry isolation exports SQLITE_TELEMETRY_DIR
        # and subprocesses INHERIT it; the driver sets its own, but strip the
        # inherited one so an ordering change can never leave the child writing
        # into the parent's directory.
        env.pop("SQLITE_TELEMETRY_DIR", None)
        env.pop("INVOCATION_ID", None)
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [sys.executable, str(driver), str(work), scenario, sink,
             str(ready)],
            cwd=str(work),          # never the repo: keeps the real .env out
            env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        child = Child(proc, work, ready)
        started.append(child)
        return child

    yield _spawn

    for child in started:
        if child.proc.poll() is None:
            child.proc.kill()
            child.proc.communicate()


# --- 0. the harness itself works ----------------------------------------------


def test_the_same_child_finishes_on_its_own_when_the_provider_answers(spawn):
    """THE NEGATIVE CONTROL, and the tests below are worthless without it.

    "The child was still running" proves the pass is unbounded only if the child
    is a real, working pass that WOULD have finished. Same driver, same adapter,
    same loopback server, same database — the only difference is that the server
    closes the connection instead of dripping. The pass then degrades to a
    provider miss (`DexScreenerAdapter._get` never raises on transport failure)
    and returns normally, exits 0, and files an ordinary NON-terminated record.

    If this ever fails, every "still alive" assertion below is measuring a
    broken harness rather than an unbounded pass.
    """
    child = spawn(scenario="closes_immediately")
    out, err = child.proc.communicate(timeout=READY_TIMEOUT_S)
    assert child.proc.returncode == 0, f"STDOUT:\n{out}\nSTDERR:\n{err}"
    assert "status=" in out, out

    events, malformed = read_events(child.sink_path)
    assert malformed == 0
    assert len(events) == 1, events
    assert events[0]["run_status"] != "terminated", (
        "an uninterrupted pass filed a termination record; the label would then "
        "mean nothing")


# --- 1. externally bounded -----------------------------------------------------


def test_a_trickling_provider_is_bounded_by_nothing_in_process(spawn):
    """THE DEFECT, DEMONSTRATED. Not "the code has no timeout" as an argument —
    a real process, a real socket, and a real fetch deadline that does not bound
    it.

    The child runs with `--max-duration-seconds 0.5`. That budget is anchored at
    fetch start and checked only BETWEEN tokens, so once the single due token's
    request is in flight nothing consults it again. The provider drips one byte
    every 0.2 s against `httpx.Timeout(15.0)`, which is per socket OPERATION —
    no connect, write or read ever takes 15 s, so httpx never fires either.

    THE ASSERTION IS LOAD-MONOTONE. It only claims the child is STILL RUNNING,
    which load can only make MORE true; a child that exited early would mean the
    fetch returned, which the dripping server does not permit.
    """
    child = spawn(scenario="trickle")
    child.wait_until_fetching()

    # generous multiple of a budget that has already been exceeded
    time.sleep(CHILD_FETCH_DEADLINE_S + 2.0)
    assert child.proc.poll() is None, (
        f"the child exited on its own {CHILD_FETCH_DEADLINE_S + 2.0}s after "
        "entering a fetch that never completes — the harness is not producing "
        "the unbounded case it claims to")

    # ...and the declared mechanism does bound it.
    rc, out, err = child.signal_and_reap(declared_kill_signal())
    assert rc != 0, (
        "a pass ended from outside exited 0; a supervisor, a wrapper, or an "
        f"operator would read that as success.\nSTDOUT:\n{out}\nSTDERR:\n{err}")


def test_the_unit_declares_a_start_bound_and_a_signal_that_can_unwind(spawn):
    """The bound the child is held to is the unit's, not this test's invention.

    Two halves, and both have to be in the file: a finite `TimeoutStartSec`
    (WHEN the supervisor acts) and `KillSignal=SIGINT` (WHAT it sends). Only the
    second is exercisable at suite speed — the first is 5 minutes — so it is
    asserted statically here and its VALUE is derived and pinned in
    `tests/test_gate7_sparse_units_001.py`.
    """
    assert declared_kill_signal() is signal.SIGINT
    timeout_start = declared_timespan("TimeoutStartSec")
    assert 0 < timeout_start < 3600, timeout_start
    # `TimeoutStopSec` is deliberately NOT declared: the manager default (90 s)
    # is the persistence budget, and it is ample for one `os.write()`.
    assert "TimeoutStopSec=" not in SERVICE_UNIT.read_text()


# --- 2. the termination is observable ------------------------------------------


def test_a_killed_pass_leaves_a_typed_termination_record(spawn):
    """THE POINT OF THE WHOLE CHANGE.

    Before this, `_emit_pass_telemetry` ran only from `_finish`, on the return
    path, and there was no run-row insert at pass start — so a pass killed by
    `TimeoutStartSec` appended NOTHING. The lane whose entire purpose is
    observation coverage could not record its own overrun.

    Asserted against the record ON DISK, read back through the sink's own
    validator, not against a mock.
    """
    child = spawn(scenario="trickle", sink="available")
    child.wait_until_fetching()
    rc, out, err = child.signal_and_reap(declared_kill_signal())

    assert rc != 0, f"STDOUT:\n{out}\nSTDERR:\n{err}"
    # ...and non-zero because the FUNNEL said so, not because an uncaught
    # exception happened to produce a non-zero status. 130 = 128 + SIGINT.
    from app.cli import SPARSE_TERMINATION_EXIT_CODE

    assert rc == SPARSE_TERMINATION_EXIT_CODE, (
        f"exit status {rc}, not the funnel's {SPARSE_TERMINATION_EXIT_CODE}; "
        f"the termination may not have gone through it at all.\nSTDOUT:\n{out}"
        f"\nSTDERR:\n{err}")
    assert "status=terminated" in out, out

    events, malformed = read_events(child.sink_path)
    assert malformed == 0, f"malformed telemetry lines: {malformed}"
    assert len(events) == 1, (
        f"expected exactly one termination record, got {len(events)}: {events}")
    event = events[0]

    assert event["writer_name"] == "crypto_horizon_observe"
    assert event["operation_name"] == "scheduled_sparse_observation"

    # TYPED AS A TERMINATION...
    assert event["run_status"] == "terminated", event
    assert event["exception_category"] == "process_interrupted", event
    # THE CLASS IS REPORTED HONESTLY, WHICHEVER SHAPE THE INTERRUPT TOOK, and
    # both are reachable from one SIGINT depending on what the pass was doing:
    #   * suspended at an `await` (a trickling provider — this test) the loop
    #     signal handler cancels the pass and it arrives as `CancelledError`;
    #   * genuinely running Python (the write phase, the receipt) CPython's own
    #     handler raises `KeyboardInterrupt` on this frame.
    # The funnel does not normalise one into the other — `run_status` and
    # `exception_category` carry the meaning, and this field carries the fact.
    assert event["exception_class"] in (
        "CancelledError", "KeyboardInterrupt", "SystemExit"), event

    # ...AND NEVER AS A SUCCESS. This lane has closed five distinct fabrication
    # shapes; a killed pass reading as `ok` would be the sixth.
    assert event["outcome"] not in (
        "success", "partial_success", "retried_success"), event

    # AND IT CLAIMS NO OBSERVATIONS. Counters advance only after a commit
    # RETURNS, and the funnel holds no result to read them from, so the fields
    # are ABSENT rather than 0 — a 0 would assert "committed nothing", which is
    # false for any pass killed after its first batch.
    for fabricable in ("rows_committed", "rows_attempted", "rows_skipped",
                       "batch_count", "external_calls"):
        assert fabricable not in event, (
            f"the termination record claims {fabricable}={event[fabricable]!r}; "
            "this frame cannot know it")


def test_the_termination_record_carries_the_run_source_it_derived(spawn):
    """`run_source` is DERIVED from systemd's `INVOCATION_ID`, never asserted by
    the funnel — so a scheduled `TimeoutStartSec` kill and an operator's Ctrl-C
    stay distinguishable in the corpus without the funnel claiming either. The
    fixture strips `INVOCATION_ID`, so this child is honestly `manual`."""
    child = spawn(scenario="trickle", sink="available")
    child.wait_until_fetching()
    child.signal_and_reap(declared_kill_signal())

    events, _ = read_events(child.sink_path)
    assert events[0]["run_source"] == "manual", events[0]
    assert events[0]["writer_class"] == "manual_command", events[0]
    assert events[0]["gate_bypassed"] is False, (
        "the unit passes neither --force nor --dry-run, so an attended bypass "
        "must not be claimed")


# --- 3. sink unavailable --------------------------------------------------------


def test_termination_still_exits_non_zero_when_the_record_cannot_be_written(
        spawn):
    """BEST-EFFORT MEANS BEST-EFFORT. The record is a courtesy; the exit status
    is the contract.

    The child's `SQLITE_TELEMETRY_DIR` points under a regular FILE, so the
    sink's `mkdir` fails with ENOTDIR for every uid — no chmod a privileged CI
    runner could ignore. The funnel must still exit non-zero, must not hang, and
    must not raise a second exception on the way out.
    """
    child = spawn(scenario="trickle", sink="unavailable")
    child.wait_until_fetching()
    rc, out, err = child.signal_and_reap(declared_kill_signal())

    assert rc != 0, (
        "an unavailable telemetry sink softened the exit status of a "
        f"terminated pass.\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    assert not child.sink_path.exists()
    # The sink's ONE bounded fallback line is expected; a traceback is not.
    assert "Traceback" not in err, err
    assert "telemetry_sink_unavailable" in err, (
        "the sink did not even take its own fallback path, so this test did not "
        f"exercise an unavailable sink.\nSTDERR:\n{err}")


def test_termination_is_not_delayed_by_an_unavailable_sink(spawn):
    """The persistence attempt must never become a second thing that hangs.

    Measured against the SAME child in the SAME state as the happy path, so the
    comparison is between two runs of one code path and not between a code path
    and a guess. The ceiling is deliberately coarse — this is a "does not hang"
    assertion, not a latency budget, and a coarse ceiling cannot flake into a
    false failure the way a tight one would.
    """
    child = spawn(scenario="trickle", sink="unavailable")
    child.wait_until_fetching()
    sent = time.monotonic()
    rc, _out, _err = child.signal_and_reap(declared_kill_signal())
    elapsed = time.monotonic() - sent
    assert rc != 0
    assert elapsed < 30.0, (
        f"the child took {elapsed:.1f}s to die after {declared_kill_signal().name} "
        "with an unwritable sink; persisting the record is best-effort and must "
        "never delay termination")
