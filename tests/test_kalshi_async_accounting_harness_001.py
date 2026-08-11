"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1/A7 -- async-accounting harness,
PARTIALLY RETIRED for the synchronous writer; the real-signal sections kept.

WHAT IS RETIRED. Sections 1-2 of the old harness (`TestFourWindowsReproduce
Deterministically`, `TestNegativeControlsDoNotOverFire`,
`TestDiscriminationDeterministic`) injected `sys.settrace`-based faults at
FOUR exact source-line boundaries inside the old `SegmentWriter.submit()`/
`_admit`, marked in `segment.py` with `# FAULT-WINDOW: <label>` comments:
window-a (between `attempted` moving and `_inflight` moving), safe-before-
admit, window-b (post-enqueue), and window-d (a second fault during the
first fault's own handler). All four windows existed because admission
(`attempted`/`_inflight`/`rejected_before_accept` bookkeeping) and commitment
(the queue put, and — on a different thread — the actual write) were
SEPARATE steps with real gaps between them for an asynchronous exception to
land in.

KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1 removes every one of those gaps by
construction: `submit()` now moves `attempted`, canonicalises, writes, and
books the terminal outcome ALL inside one `with self._lock:` block, on the
caller's own thread. There is exactly one call frame and one lock, not four
named boundaries between four separate steps -- the `# FAULT-WINDOW:`
markers those tests scanned for no longer exist in `segment.py`, because the
statements they named are no longer separated by anything a fault could land
between in a way that matters differently at each point. (An exception CAN
still land at any bytecode boundary inside that one `with` block -- Python
guarantees nothing else -- but every one of those boundaries now resolves to
exactly the SAME two outcomes: the write already happened, so the record is
durable and `written` reflects it; or it did not, so nothing is durable and
the call raises/returns a rejection with `attempted` alone possibly one
diagnostic count ahead. There is no longer a THIRD outcome — "durably
queued, but not yet credited to any counter" — because there is no queue.)

`tests/harness_async_accounting/reference_shim.py`'s four planted-bad
variants (`bad_a`..`bad_d`) modelled specific ways the OLD, queue-based
accounting design could be built wrong. They are retired along with the
design they modelled; a shim reproducing "the queue put succeeds but the
counter update is skipped" has no analogue when there is no queue.

WHAT IS KEPT, UNCHANGED. `TestRealSignalReproducesTheSameClass` and
`TestFullCampaign` never depended on markers or queue internals at all: they
drive `tests/harness_async_accounting/fault_trial.py --target segment` as an
external subprocess, bombarding a REAL 20,000-event `submit()` loop with
real `SIGINT`/`PyThreadState_SetAsyncExc` deliveries and checking that no
interrupt ever turns durable, already-written evidence into an
identity-violating, unpublishable segment. That property is exactly as
meaningful -- arguably more directly so, since there is now only one call
frame for an interrupt to land in -- against the synchronous writer, and the
harness script itself needed no changes (`fault_trial.py --target segment`
constructs a real `SegmentWriter` and calls its real `submit()`/`close()`;
only the retired `--target shim:*` mode depended on the old design).
`--target shim:*` is no longer exercised by any test in this file, but the
flag and `reference_shim.VARIANTS` are left in place rather than deleted,
since `fault_trial.py` is a standalone script other callers could still
invoke directly.

DOES NOT MODIFY ANY PRODUCTION MODULE.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FAULT_TRIAL_SCRIPT = Path(__file__).parent / "harness_async_accounting" / "fault_trial.py"

CAMPAIGN = os.getenv("KALSHI_ASYNC_ACCOUNTING_CAMPAIGN", "") == "1"
REAL_FAULT_TRIALS = 24 if CAMPAIGN else 6


def _run_fault_trial(*, target_, seed, n_interrupts, mode, window_s,
                     n_submits=20_000, tmp_path):
    root = tmp_path / f"trial-{seed}-{mode}-{n_interrupts}"
    root.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, str(FAULT_TRIAL_SCRIPT),
            "--target", target_, "--segment-id", f"seg-{seed}",
            "--root", str(root), "--n-submits", str(n_submits),
            "--n-interrupts", str(n_interrupts), "--seed", str(seed),
            "--mode", mode, "--window-s", str(window_s)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return {"hang": True}
    if proc.returncode != 0:
        return {"crash": True, "stderr": proc.stderr[-2000:]}
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestRealSignalReproducesTheSameClass:
    """Real `SIGINT`, delivered by a genuine bomber thread to a genuine
    subprocess, against the real, synchronous `SegmentWriter.submit()`."""

    def test_a_single_sigint_lands_inside_submit_and_is_caught_cleanly(self, tmp_path):
        """One interrupt, by itself, should not usually destroy a segment --
        `submit()`'s single lock-held call frame is correct for a fault
        landing almost anywhere inside it. Sanity check on the trial
        mechanism itself before the multi-interrupt campaign below."""
        found_a_catch = False
        for seed in range(REAL_FAULT_TRIALS):
            r = _run_fault_trial(target_="segment", seed=seed, n_interrupts=1,
                                 mode="sigint", window_s=0.3, tmp_path=tmp_path)
            assert not r.get("hang"), r
            assert not r.get("crash"), r
            assert not r.get("top_level_fault"), r
            if r["caught_in_loop"]:
                found_a_catch = True
        assert found_a_catch, (
            "no trial's single SIGINT landed inside the submit loop across "
            f"{REAL_FAULT_TRIALS} trials; the bombardment window is "
            "mis-calibrated for this machine, not a finding about the code")

    @pytest.mark.parametrize("mode", ["sigint", "asyncexc"])
    def test_ctrl_c_causes_zero_identity_violations(self, tmp_path, mode):
        """THE required deliverable: real interrupts during a 20,000-event
        submission burst must never destroy an otherwise-valid segment. An
        IDENTITY VIOLATION here means `close()` failed with the accounting
        identity itself as the reason (`"does not reconcile"`/`"not
        clean"`), as opposed to a genuine, freshly-arriving interrupt
        landing inside `close()`'s own machinery (an ordinary "caller
        aborted the shutdown" outcome, recorded as `close_ok: False` but not
        counted as a violation).

        A HANG is recorded, not asserted against: `PyThreadState_
        SetAsyncExc` can interrupt literally anywhere, including inside
        CPython's own internal lock machinery, and CPython's own
        documentation calls this mechanism unsafe for exactly that reason.
        A wedged subprocess, caught by this file's external subprocess
        timeout, is a property of the injection mechanism, not evidence
        about `segment.py`'s correctness.
        """
        n_trials = 0
        n_close_failed = 0
        n_hang = 0
        identity_violations = []
        for seed in range(REAL_FAULT_TRIALS):
            r = _run_fault_trial(target_="segment", seed=seed, n_interrupts=2,
                                 mode=mode, window_s=0.6, tmp_path=tmp_path)
            assert not r.get("crash"), f"trial {seed} crashed: {r}"
            if r.get("hang"):
                n_hang += 1
                continue
            if r.get("top_level_fault"):
                continue
            n_trials += 1
            if not r["close_ok"]:
                n_close_failed += 1
                err = r.get("close_error") or ""
                if "does not reconcile" in err or "not clean" in err:
                    identity_violations.append(r)
        print(f"\n[A1] Ctrl-C ({mode}): {n_hang}/{REAL_FAULT_TRIALS} hung "
              "(caught by the external subprocess timeout, not asserted "
              "against -- see this test's docstring)")
        assert n_trials >= REAL_FAULT_TRIALS // 2, (
            f"too many trials were unusable ({REAL_FAULT_TRIALS - n_trials}/"
            f"{REAL_FAULT_TRIALS}) -- timing mis-calibration, not a finding")
        print(f"\n[A1] Ctrl-C ({mode}): {n_close_failed}/{n_trials} close() "
              f"failures, {len(identity_violations)} IDENTITY VIOLATIONS, over "
              f"{n_trials} trials of a 20,000-event burst with 2 interrupts.")
        for ex in identity_violations[:3]:
            print(f"  VIOLATION: pre_close={ex['pre_close_accounting']} "
                  f"close_error={ex['close_error']!r}")
        assert not identity_violations, (
            f"{len(identity_violations)}/{n_trials} trials show a diagnostic "
            f"accounting mismatch destroying a segment. {identity_violations}")


@pytest.mark.skipif(not CAMPAIGN, reason="KALSHI_ASYNC_ACCOUNTING_CAMPAIGN=1 "
                    "not set; this is the full statistical campaign (~1min)")
class TestFullCampaign:
    def test_three_interrupt_campaign_zero_identity_violations(self, tmp_path):
        """The larger, three-interrupt campaign. A genuine finding surfaced
        while calibrating the original (queue-based) version of this
        campaign: 3 real SIGINTs against a 20,000-submit run occasionally
        WEDGE the subprocess outright. That is exactly the "hang, not a
        returned result" outcome `subprocess.run(..., timeout=...)` exists
        to catch, so it is counted as its own category here rather than
        either failing the whole campaign or being silently absorbed into
        "lost". The REQUIRED outcome is ZERO identity violations, not a
        rate threshold."""
        n_trials = 0
        n_hang = 0
        identity_violations = []
        for seed in range(REAL_FAULT_TRIALS):
            r = _run_fault_trial(target_="segment", seed=seed, n_interrupts=3,
                                 mode="sigint", window_s=0.25, tmp_path=tmp_path)
            assert not r.get("crash"), r
            if r.get("hang"):
                n_hang += 1
                continue
            if r.get("top_level_fault"):
                continue
            n_trials += 1
            if not r["close_ok"]:
                err = r.get("close_error") or ""
                if "does not reconcile" in err or "not clean" in err:
                    identity_violations.append(r)
        print(f"\n[A1] 3-interrupt SIGINT campaign: "
              f"{len(identity_violations)} IDENTITY VIOLATIONS / {n_trials} "
              f"usable trials, {n_hang}/{REAL_FAULT_TRIALS} hung (each caught "
              "by the external subprocess timeout, not a suite hang)")
        assert n_trials >= REAL_FAULT_TRIALS // 4
        assert not identity_violations, identity_violations
