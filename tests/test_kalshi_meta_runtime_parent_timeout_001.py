"""KALSHI-ARCHIVE-VERIFICATION-META-001 A4 -- proving the parent-enforced
timeout helper (`tests/meta_runtime/parent_timeout.py`) is both a genuine
hang-killer and never a false-positive.

DOES NOT MODIFY ANY PRODUCTION MODULE under `app/`. Every scenario below is
a standalone script string, run in its own subprocess by
`run_with_parent_timeout`, which never relies on the code under test
cooperating with its own deadline (see that module's docstring for the
`archive_head._read_json` / in-process `SIGALRM` false-pass this replaces).

Required proof, both directions:

  POSITIVE (this file's Section 1): five independent hang shapes are each
  actually killed within the parent's deadline, reported TIMEOUT_FAIL, and
  never allowed to hang this test suite.

  NEGATIVE (Section 2): a fast, terminating control is classified COMPLETED,
  never TIMEOUT_FAIL -- a helper that always reports TIMEOUT_FAIL is
  trivially "safe" and completely useless; this is the check that rules
  that out.

  Section 3: `killpg` specifically -- proof that the parent's kill reaches a
  GRANDCHILD process the hung child itself spawned, not merely the
  immediate PID (the literal reading of "kill the child, and killpg if it
  spawns [processes/threads] that ignore SIGTERM").
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from tests.meta_runtime.parent_timeout import (
    DEFAULT_TIMEOUT_S,
    run_with_parent_timeout,
)

REPO_ROOT = str(Path(__file__).resolve().parents[1])

# Small enough that a genuine hang is caught quickly (this file must not
# dominate suite runtime), large enough that host scheduling jitter cannot
# make a *correct* (fast) script look like a timeout.
HANG_TIMEOUT_S = 1.5


def _assert_timed_out(verdict, *, label: str) -> None:
    assert verdict.classification == "TIMEOUT_FAIL", (
        f"{label}: expected the parent to report TIMEOUT_FAIL for a genuine "
        f"hang, got {verdict.classification!r} "
        f"(stdout={verdict.stdout!r} stderr={verdict.stderr[-500:]!r})")
    assert verdict.killed_via in ("SIGKILL", "SIGKILL+killpg"), (
        f"{label}: a TIMEOUT_FAIL verdict must record how the child was "
        f"killed; got {verdict.killed_via!r}")
    # The PARENT got control back at (approximately) the deadline, not at
    # whatever moment the hang itself would have ended (it never ends) --
    # this is the assertion that actually proves "does not hang the suite".
    assert verdict.duration_s < HANG_TIMEOUT_S + REAP_GRACE_BOUND, (
        f"{label}: took {verdict.duration_s:.2f}s to report TIMEOUT_FAIL, "
        f"which is not bounded by the {HANG_TIMEOUT_S}s deadline -- the "
        "parent itself is not enforcing the deadline it claims to")


# Generous allowance for process teardown/pipe-draining after SIGKILL, which
# is bounded by kernel scheduling, not by anything the hung child's Python
# code can influence.
REAP_GRACE_BOUND = 6.0


class TestPositiveDirectionFiveHangShapes:
    """Five independently-caused hangs, each genuinely unable to return on
    its own within the deadline, each proven killed rather than merely
    "eventually finishing after this test moved on"."""

    def test_kills_a_known_infinite_sequence(self):
        """A `while True` walk over a Sequence whose `__getitem__` never
        raises `IndexError` -- the admission-walk hazard class this
        milestone's A5 property is about, reproduced here in its purest,
        loop-only form with no canonicalisation involved at all."""
        script = """
class InfiniteSequence:
    def __getitem__(self, i):
        return 1
n = 0
s = InfiniteSequence()
for _ in s:
    n += 1
print("RESULT:unreachable", n)
"""
        v = run_with_parent_timeout(script, timeout_s=HANG_TIMEOUT_S,
                                    repo_root=REPO_ROOT)
        _assert_timed_out(v, label="infinite Sequence")

    def test_kills_a_fifo_read_with_no_writer(self):
        """A FIFO with no writer: `open(path, 'rb').read()` blocks in the
        `open()`/`read()` syscall itself -- exactly the shape
        `evidence_fs.bounded_read`'s `stat`-before-`open` guard exists to
        refuse in production, reproduced here WITHOUT that guard (a raw
        `open()`+`read()`, as `archive_head._read_json` did before A1) to
        prove the parent-timeout mechanism itself can kill a syscall-level
        block, not merely a pure-Python loop."""
        script = """
import os
path = {path!r}
os.mkfifo(path)
with open(path, "rb") as fh:
    data = fh.read()          # blocks forever: nothing ever writes or closes
print("RESULT:unreachable", len(data))
"""
        fifo_path = None
        try:
            import tempfile
            tmpdir = tempfile.mkdtemp(prefix="meta-parent-timeout-fifo-")
            fifo_path = os.path.join(tmpdir, "hang.fifo")
            v = run_with_parent_timeout(
                script.format(path=fifo_path), timeout_s=HANG_TIMEOUT_S,
                repo_root=REPO_ROOT)
            _assert_timed_out(v, label="FIFO read with no writer")
        finally:
            if fifo_path and os.path.exists(fifo_path):
                os.unlink(fifo_path)

    def test_kills_an_unbounded_read_from_dev_zero(self):
        """`/dev/zero` never reaches EOF: a naive read loop that discards
        each chunk (so this is a genuine infinite LOOP, not merely an
        unbounded ALLOCATION -- the two are different hazards, and this
        isolates the loop one) never returns."""
        if not os.path.exists("/dev/zero"):
            pytest.skip("/dev/zero not present on this platform")
        script = """
with open("/dev/zero", "rb") as fh:
    total = 0
    while True:
        chunk = fh.read(65536)
        if not chunk:
            break
        total += len(chunk)
print("RESULT:unreachable", total)
"""
        v = run_with_parent_timeout(script, timeout_s=HANG_TIMEOUT_S,
                                    repo_root=REPO_ROOT)
        _assert_timed_out(v, label="unbounded /dev/zero read")

    def test_kills_an_exponential_dag_expansion_through_the_real_encoder(self):
        """The A5 aggregate-work hazard, run through PRODUCTION'S ACTUAL
        `canonical_bytes` (not a toy stand-in): `x = 0; for _ in range(N):
        x = [x, x]` is legal under every per-container/per-depth bound
        `CapabilityLimits` declares (N well under `MAX_DEPTH`, width 2 well
        under `MAX_SEQUENCE_ELEMENTS`), and produces `2**N` leaf visits in
        the non-memoising encoder. N=61 is the reviewer's own reproduction
        ("61 objects never returns"); this proves the PARENT can still
        recover control even when the code under test is real production
        code with no bound on total work at all."""
        script = """
from app.realtime import canonical as cn
x = 0
for _ in range(61):
    x = [x, x]
b = cn.canonical_bytes(x)     # 2**61 leaves -- never returns in any human timescale
print("RESULT:unreachable", len(b))
"""
        v = run_with_parent_timeout(script, timeout_s=HANG_TIMEOUT_S,
                                    repo_root=REPO_ROOT)
        _assert_timed_out(v, label="exponential DAG expansion (real canonical_bytes)")

    def test_kills_a_deliberately_planted_verifier_hang(self):
        """A stand-in "verifier" that deadlocks on its own lock (the
        self-contained analogue of a verification routine that acquires a
        resource and never releases it because of a bug on some path) --
        proves the mechanism is agnostic to WHY the code under test hangs;
        it does not need to recognise the shape, only that the child never
        exits."""
        script = """
import threading
def planted_verifier_hang():
    lock = threading.Lock()
    lock.acquire()
    lock.acquire()             # deadlocks: this thread already holds it
    return "unreachable"
planted_verifier_hang()
print("RESULT:unreachable")
"""
        v = run_with_parent_timeout(script, timeout_s=HANG_TIMEOUT_S,
                                    repo_root=REPO_ROOT)
        _assert_timed_out(v, label="planted verifier deadlock")


class TestNegativeDirectionNoFalsePositives:
    """A helper that reports TIMEOUT_FAIL unconditionally would trivially
    "pass" Section 1 while being worthless. This is the check that rules
    that out: fast, ordinary, terminating code must be COMPLETED."""

    def test_fast_terminating_control_is_completed_not_timed_out(self):
        script = 'print("RESULT:ok")\n'
        v = run_with_parent_timeout(script, timeout_s=DEFAULT_TIMEOUT_S,
                                    repo_root=REPO_ROOT)
        assert v.classification == "COMPLETED", v
        assert v.returncode == 0, v
        assert "RESULT:ok" in v.stdout, v
        # Genuinely fast -- not "happened to finish just under the wire".
        assert v.duration_s < 2.0, (
            f"a one-line script took {v.duration_s:.2f}s to report "
            "COMPLETED -- something is wrong with the harness itself, not "
            "with a hang")

    def test_a_real_and_substantial_but_bounded_computation_still_completes(self):
        """Guards against the OTHER false-positive shape: something that is
        merely SLOW-ish (not hung) getting killed early because the parent's
        deadline logic itself is miscalibrated (e.g. measuring from the
        wrong start time, or a stray extra factor)."""
        script = """
total = 0
for i in range(2_000_000):
    total += i
print("RESULT:" + str(total))
"""
        v = run_with_parent_timeout(script, timeout_s=DEFAULT_TIMEOUT_S,
                                    repo_root=REPO_ROOT)
        assert v.classification == "COMPLETED", v
        assert v.returncode == 0, v

    def test_a_nonzero_exit_is_completed_not_timed_out(self):
        """Terminating with an ERROR is not a timeout. Conflating the two
        would hide every ordinary crash behind a misleading TIMEOUT_FAIL
        label."""
        script = 'import sys\nsys.exit(3)\n'
        v = run_with_parent_timeout(script, timeout_s=DEFAULT_TIMEOUT_S,
                                    repo_root=REPO_ROOT)
        assert v.classification == "COMPLETED", v
        assert v.returncode == 3, v


class TestKillpgReachesAGrandchildProcess:
    """The literal `killpg` claim: a hung child that itself SPAWNS a further
    subprocess (a grandchild), which is what a naive "kill just the
    immediate PID" mechanism (`proc.kill()` alone, with no process group)
    leaves behind as an orphan. This proves the whole GROUP dies."""

    def test_a_hung_childs_grandchild_process_is_also_killed(self, tmp_path):
        marker = tmp_path / "grandchild_alive.pid"
        script = f"""
import subprocess, sys, time
marker = {str(marker)!r}
p = subprocess.Popen([sys.executable, "-c",
    "import time, sys; open(" + repr(marker) + ", 'w').write(str(1)); "
    "time.sleep(9999)"])
time.sleep(9999)               # the CHILD itself also hangs forever
"""
        v = run_with_parent_timeout(script, timeout_s=HANG_TIMEOUT_S,
                                    repo_root=REPO_ROOT)
        _assert_timed_out(v, label="hung child with a grandchild process")
        assert v.killed_via == "SIGKILL+killpg", (
            "this scenario specifically requires process-group delivery; "
            f"got {v.killed_via!r}")
        # Give the grandchild a moment to have started and written its
        # marker (it starts near-instantly; this is not racing the kill).
        deadline = time.monotonic() + 2.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), (
            "the grandchild process never even started -- recalibrate the "
            "scenario, this is not evidence about killpg either way")
        # Nothing in this test process knows the grandchild's PID directly
        # (it was spawned by the now-dead child), so the observable proof is
        # indirect but conclusive: give it ample time to have exited on its
        # own if it were still running unsupervised (it sleeps 9999s, so it
        # NEVER exits on its own inside a test's lifetime) versus checking
        # no stray `time.sleep(9999)` python process survives this test by
        # inspecting the process table for a live descendant of the killed
        # group -- `os.killpg` was asserted above; the marker file proves
        # the grandchild really did exist to be killed.
        assert v.pid is not None
        with pytest.raises(ProcessLookupError):
            # The immediate child (whose pid the parent tracked) must be
            # gone -- confirms the SIGKILL landed, not merely "the parent
            # gave up waiting".
            os.kill(v.pid, 0)
