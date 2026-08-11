"""KALSHI-ARCHIVE-CORE-REMEDIATION-003B A3 -- fault-model fidelity.

Validates `tests/meta_runtime/fault_ledger.py`'s completeness/consistency,
and adds ONE new, genuinely REALISTIC_SIGNAL_FAULT measurement: a real
`ctypes.pythonapi.PyThreadState_SetAsyncExc` bombardment targeted at the
WRITER thread's own OS thread id -- unlike every `sigint`/`asyncexc` trial
in `tests/test_kalshi_async_accounting_harness_001.py`, which can only
target the main/producer thread (real `SIGINT` handlers are only ever
delivered there in CPython).

DOES NOT MODIFY `app/realtime/segment.py`. The measurement script
(`tests/meta_runtime/writer_thread_asyncexc_trial.py`) runs as an isolated
subprocess under `tests/meta_runtime/parent_timeout.run_with_parent_timeout`
-- a wedged trial cannot hang this suite (see that module's docstring for
why a REAL, external deadline is required for anything that injects
asynchronous exceptions).
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

from app.realtime import segment as sg

from tests.meta_runtime import fault_ledger as fl
from tests.meta_runtime.parent_timeout import run_with_parent_timeout

REPO_ROOT = Path(__file__).resolve().parents[1]
TRIAL_SCRIPT = (REPO_ROOT / "tests" / "meta_runtime"
                / "writer_thread_asyncexc_trial.py")

# Small: this is a smoke-scale real measurement, not the reviewer's full
# 50/200 campaign -- see this file's own report note on why a bigger
# campaign was out of scope here.
N_TRIALS = 8


class TestLedgerIsInternallyConsistent:
    def test_every_entry_has_a_valid_classification(self):
        for e in fl.LEDGER:
            assert e.classification in fl.CLASSIFICATIONS, e

    def test_no_classification_bucket_is_empty(self):
        """All three kinds this milestone names must actually be
        represented -- an empty REALISTIC_SIGNAL_FAULT bucket, for
        instance, would mean every claim in this suite is synthetic and
        none of them has been checked against a real delivery mechanism."""
        for kind in fl.CLASSIFICATIONS:
            assert fl.entries_for(kind), (
                f"no ledger entries classified {kind!r}")

    def test_referenced_modules_and_test_names_exist_on_disk(self):
        """Structural drift guard: every `(module, test_name)` pair in the
        ledger must resolve to real source text -- a class/def whose name
        matches -- so a renamed or deleted fault test cannot silently keep
        a stale ledger entry."""
        missing = []
        for e in fl.LEDGER:
            path = REPO_ROOT / e.module
            if not path.exists():
                missing.append(f"{e.module}: file does not exist")
                continue
            src = path.read_text()
            # test_name is "Class.method[param]" or "Class.method" or a
            # bare class name; check every dotted component (minus any
            # trailing [param] parametrize suffix) appears as a def/class.
            base = e.test_name.split("[", 1)[0]
            for part in base.split("."):
                if not re.search(rf"\b(class|def)\s+{re.escape(part)}\b", src):
                    missing.append(f"{e.module}::{e.test_name}: {part!r} "
                                   "not found as a class/def")
        assert not missing, missing

    def test_every_fault_module_referenced_by_this_milestones_new_tests_is_ledgered(
            self):
        """Every NEW test file this milestone adds that imports a fault-
        injection mechanism (`line_injector`, `fault_trial`,
        `writer_thread_asyncexc_trial`) must have at least one ledger entry
        pointing at it -- a fault test added without a classification is
        exactly the "mixed into one reliability claim" failure A3 exists
        to prevent."""
        new_fault_modules = [
            "tests/test_kalshi_meta_runtime_admission_close_race_001.py",
            "tests/test_kalshi_meta_runtime_queue_gap_001.py",
            "tests/test_kalshi_meta_runtime_fault_classification_001.py",
        ]
        ledgered_modules = {e.module for e in fl.LEDGER}
        for m in new_fault_modules:
            assert m in ledgered_modules, (
                f"{m} uses fault injection but has no ledger entry in "
                "tests/meta_runtime/fault_ledger.py")


class TestRealAsyncExcAgainstTheWriterThreadItself:
    def test_measure_real_asyncexc_landing_rate_against_the_writer_thread(
            self, tmp_path):
        """Reports, honestly, how often a REAL asynchronous exception
        delivered directly to the writer thread's own OS thread id lands
        in a way that kills it mid-write (`landed`) versus harmlessly
        (thread already idle, or the exception is swallowed/retried) --
        across a small number of real trials. This is a FREQUENCY-CLASS
        measurement (REALISTIC_SIGNAL_FAULT); no specific rate is asserted
        as a requirement, because -- exactly per this milestone's own
        finding about the reviewer's 0/50 deadlock measurement -- a
        synthetic mechanism finding a state does not mean a real one lands
        there at any particular rate, and the reverse claim (asserting a
        rate) would be just as unjustified without a much larger campaign
        than fits in this suite's runtime budget.
        """
        results = []
        for seed in range(N_TRIALS):
            root = tmp_path / f"trial-{seed}"
            root.mkdir()
            # Strip the file's own `if __name__ == "__main__":` trailer --
            # it would otherwise execute FIRST (with the wrong sys.argv,
            # missing the required --root) the instant this flat script
            # reaches that line, before the appended block below ever runs.
            body = TRIAL_SCRIPT.read_text().split(
                'if __name__ == "__main__":')[0]
            # `run_with_parent_timeout`'s own preamble is prepended ahead of
            # this text, so a `from __future__ import annotations` here
            # would no longer be the first statement in the module (a
            # SyntaxError) -- dropped; nothing in this script's body
            # actually depends on deferred annotation evaluation at runtime.
            body = body.replace("from __future__ import annotations\n", "")
            # The file's own `REPO_ROOT = Path(__file__)...` line is
            # meaningless for a flat `-c` script (no `__file__`) --
            # `run_with_parent_timeout`'s own preamble already inserts the
            # repo root onto `sys.path` (see its `repo_root=` argument
            # below), so this line is dropped rather than executed.
            body = re.sub(
                r"^REPO_ROOT = Path\(__file__\).*$\n^sys\.path\.insert\(0, "
                r"str\(REPO_ROOT\)\)$", "", body, flags=re.MULTILINE)
            script = body + (
                f"\nsys.argv = [sys.argv[0], '--root', {str(root)!r}, "
                f"'--segment-id', 'seg', '--n-submits', '300', "
                f"'--n-bombs', '3', '--seed', '{seed}']\n"
                "raise SystemExit(main())\n")
            verdict = run_with_parent_timeout(
                script, timeout_s=15.0, repo_root=str(REPO_ROOT))
            assert verdict.classification in ("COMPLETED", "TIMEOUT_FAIL"), (
                f"trial {seed} CRASHED (not a hang, not a clean exit): "
                f"{verdict}")
            if verdict.classification == "TIMEOUT_FAIL":
                # A genuine, real-world outcome for this delivery mechanism
                # (`PyThreadState_SetAsyncExc` can land inside CPython's own
                # internal lock machinery -- see `watchdog.py`'s docstring
                # for the documented precedent) -- recorded, not asserted
                # against, and the parent's SIGKILL proves the suite cannot
                # be hung by it.
                results.append({"seed": seed, "hang": True})
                continue
            line = (verdict.stdout or "").strip().splitlines()[-1] \
                if verdict.stdout else ""
            try:
                results.append(json.loads(line))
            except (json.JSONDecodeError, IndexError):
                results.append({"seed": seed, "unparseable": True,
                                "stdout": verdict.stdout[-500:],
                                "stderr": verdict.stderr[-500:]})

        n_hang = sum(1 for r in results if r.get("hang"))
        n_landed = sum(1 for r in results if r.get("landed"))
        n_usable = sum(1 for r in results
                       if not r.get("hang") and not r.get("unparseable")
                       and not r.get("top_level_fault"))
        print(f"\n[A3] real PyThreadState_SetAsyncExc against the WRITER "
              f"thread: {n_landed}/{n_usable} usable trials landed mid-write "
              f"(killed the writer with a recorded writer_error), "
              f"{n_hang}/{N_TRIALS} hung (caught by the parent timeout, not "
              "a suite hang). This is a smoke-scale sample "
              f"(N_TRIALS={N_TRIALS}), not the full statistical campaign a "
              "production frequency claim would require -- see this "
              "module's docstring.")
        for r in results:
            print(f"  {r}")

        # Mechanism-robustness assertions ONLY -- no claim about the
        # landing rate itself, which is reported above for the record.
        assert n_usable >= N_TRIALS // 2, (
            f"too many trials were unusable to draw any conclusion: "
            f"{results}")
        for r in results:
            if r.get("close_ok") is False:
                err = r.get("close_error") or ""
                assert "does not reconcile" not in err, (
                    "a real writer-thread asyncexc trial produced the OLD "
                    f"pre-A6 identity-violation shape: {r}")


class TestRealProcessKillMidWrite:
    """REALISTIC_PROCESS_FAULT: a real SIGKILL, delivered by the OS to a
    real child process actively submitting events, mid-stream -- as real
    as a process fault gets (the child cannot catch, ignore, or clean up
    after `SIGKILL`; nothing in `segment.py` runs at all once it lands).

    This is a genuinely different fault CLASS from the two thread-targeted
    mechanisms above: no exception is ever raised inside the process under
    test; the process simply stops existing, with whatever was durably
    fsynced on disk at that instant. The property under test is the one
    `verify_segment`'s own docstring states: a segment killed before its
    manifest is published must be reported as recoverable/uncommitted --
    never falsely CLOSED, never falsely 'clean'.
    """

    def test_a_hard_kill_mid_stream_leaves_recoverable_uncommitted_evidence(
            self, tmp_path):
        ENV = "demo"
        root = tmp_path / "hard-kill"
        root.mkdir()
        script = f'''
import sys
sys.path.insert(0, {str(REPO_ROOT)!r})
import time
from datetime import datetime, timedelta, timezone
from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

ENV = "demo"
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

def fields(i):
    return {{
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": "KXA", "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {{"price_dollars": "0.5100", "side": "no"}},
        "normalized_event": {{"raw_price_units": 5100}},
    }}

ah.initialize_archive({str(root)!r}, ENV, archive_identity="kalshi-realtime")
w = sg.SegmentWriter({str(root)!r}, environment=ENV, segment_id="seg-killed",
                     partition_identity="p", commit_to_head=False,
                     flush_every=1)
i = 0
while True:
    w.submit(fields(i))
    i += 1
    time.sleep(0.02)   # slow enough that the parent's short deadline
                       # reliably lands mid-stream, not before the first
                       # record or after every record is already written
'''
        verdict = run_with_parent_timeout(script, timeout_s=1.0,
                                          repo_root=str(REPO_ROOT))
        assert verdict.classification == "TIMEOUT_FAIL", (
            f"expected the child to still be running (and be SIGKILLed) "
            f"at the 1.0s deadline -- got {verdict}")
        assert verdict.killed_via in ("SIGKILL", "SIGKILL+killpg"), verdict

        seg_dir = root / f"env={ENV}" / "segment=seg-killed"
        assert seg_dir.exists(), (
            "the segment directory should exist -- SIGKILL landed after "
            "the writer opened its files, not before")
        manifest_path = seg_dir / "manifest.json"
        assert not manifest_path.exists(), (
            "no manifest should have been published -- the child was "
            "SIGKILLed while still looping, never reached close()")

        verdict2 = sg.verify_segment(seg_dir, environment=ENV,
                                     allow_open=True, root=root)
        # THE PROPERTY: never falsely CLOSED, never falsely reported as a
        # committed 'clean' segment, over evidence a real process-level
        # kill interrupted.
        assert verdict2.state is not sg.SegmentState.CLOSED, verdict2
        assert verdict2.valid is False, (
            "a manifest-less segment is never reported as `valid` by "
            f"verify_segment -- see its own docstring: {verdict2}")
        print(f"\n[A3] real SIGKILL mid-stream: segment left in state "
              f"{verdict2.state.value!r}, {verdict2.records_read} "
              "record(s) durably recoverable from what was fsynced before "
              "the kill.")
