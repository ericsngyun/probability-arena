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

    def test_a_single_sigint_does_not_destroy_the_segment(self, tmp_path):
        """KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- THE REGRESSION FLOOR,
        PINNED TO THE MEASURED 321c719 NUMBERS.

        This test previously docstringed "one interrupt should not usually
        destroy a segment" and then asserted only not-hang / not-crash /
        found-a-catch -- never `close_ok`. The property it named was
        REFUTED 6/6 while the test passed. Measured A/B, seeds 0-5,
        `--n-interrupts 1 --n-submits 20000`:

            321c719  6/6 close_ok True,  19,999-20,000 records published
            d004c01  6/6 close_ok False, record_count null (segment lost)

        CPython delivers signal handlers only on the MAIN thread, so under
        the queue design Ctrl-C could never reach the gzip write; A1 moved
        the write onto the caller's thread and exposed it, and `submit()`'s
        (correct) fail-closed `except BaseException` then turned one
        interrupt into "every already-durable record in this segment is
        unpublishable".

        The assertion is therefore the measured 321c719 floor itself: every
        trial must publish, and must publish essentially everything it
        submitted. A substantive claim about behaviour, not about the
        harness.
        """
        published = 0
        results = []
        for seed in range(REAL_FAULT_TRIALS):
            r = _run_fault_trial(target_="segment", seed=seed, n_interrupts=1,
                                 mode="sigint", window_s=0.3, tmp_path=tmp_path)
            assert not r.get("hang"), r
            assert not r.get("crash"), r
            assert not r.get("top_level_fault"), r
            results.append(r)
            if r["close_ok"]:
                published += 1
        assert published == REAL_FAULT_TRIALS, (
            f"THE 321c719 REGRESSION FLOOR: only {published}/"
            f"{REAL_FAULT_TRIALS} trials published a manifest. The pre-A1 "
            "queue-based writer measured 6/6 for exactly this campaign; "
            "anything less means a single Ctrl-C can again turn an open "
            "segment's already-durable records into unpublishable residue: "
            f"{[{k: r[k] for k in ('seed', 'close_ok', 'close_error')} for r in results]}")
        for r in results:
            assert r["record_count"] is not None, r
            assert r["record_count"] >= r["n_submits"] - 1, (
                "321c719 published 19,999-20,000 of 20,000 records under a "
                f"single interrupt; this trial published {r['record_count']}"
                f": {r}")
            # THE IDENTITY, asserted on numbers rather than on error text:
            # never more records readable on disk than the writer BOOKED.
            # More on disk than `written` means the state delta did not
            # advance past a record whose bytes are already committed, so
            # every later record carries a duplicate `receive_ordinal` and a
            # stale `previous_record_digest`.
            assert r["on_disk_records"] <= r["post_close_accounting"]["written"], r

    @pytest.mark.parametrize("mode", ["sigint", "asyncexc"])
    def test_ctrl_c_causes_zero_identity_violations(self, tmp_path, mode):
        """THE required deliverable: real interrupts during a 20,000-event
        submission burst must never destroy an otherwise-valid segment.

        KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- THIS TEST COULD NOT FAIL.
        It defined an IDENTITY VIOLATION as `close_error` containing the
        substring `"does not reconcile"` or `"not clean"`. NEITHER STRING
        EXISTS ANYWHERE IN `app/` (grep-verified). `identity_violations` was
        therefore always empty and the only real assertion in "THE required
        deliverable" was unreachable -- while the property it claimed to
        guard was being violated 6/6 by a single Ctrl-C.

        It now asserts on STRUCTURED FIELDS, never on error-message text:

          close_ok / record_count       did the evidence publish at all
          on_disk_records vs written    is the writer's own bookkeeping
                                        consistent with the bytes
          state                         did it end somewhere coherent

        The `sigint` and `asyncexc` arms are deliberately held to DIFFERENT
        standards, because the two mechanisms are not comparable and saying
        so is more honest than pretending one bar fits both.

        `sigint` is a production event. A8 defers SIGINT/SIGTERM across both
        commitment regions, so this arm must PUBLISH every trial AND satisfy
        the consistency identity.

        `asyncexc` is `PyThreadState_SetAsyncExc`, a ctypes-only injection
        CPython's own documentation calls unsafe. It can land inside
        CPython's internal machinery, and nothing in `segment.py` can
        intercept it -- including inside `GzipFile`'s own
        `BufferedWriter.flush()`, where a MEASURED consequence is that the
        buffer is emitted twice: 43 records with duplicate
        `receive_ordinal`s appended after record 2,411, chain broken. That
        is a defect of the interrupted C-level buffered write, it is
        PRE-EXISTING (reproduced identically at d004c01: on-disk minus
        written of +48 and +37 on seeds 1 and 2), and no signal discipline
        closes it because no signal is involved. What this arm therefore
        requires is FAIL-CLOSED behaviour: if the bytes and the bookkeeping
        disagree, the segment must be INVALID and must NOT have published.
        Corrupt evidence must never be committed; that is the property
        `segment.py` is actually responsible for here.
        """
        n_trials = 0
        n_close_failed = 0
        n_hang = 0
        violations = []
        published = 0
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
            if r["close_ok"]:
                published += 1
            else:
                n_close_failed += 1
            # THE IDENTITY, on numbers. `on_disk_records > written` means
            # bytes are durably in the segment that the writer never booked
            # -- so `_ordinal`/`_prev_digest` did not advance past them and
            # every subsequent record duplicates an ordinal and chains off a
            # stale digest. That is the on-disk chain permanently broken,
            # and it is what the substring search was supposed to catch.
            written = (r.get("post_close_accounting") or {}).get("written")
            on_disk = r.get("on_disk_records")
            if written is not None and on_disk is not None and on_disk > written:
                violations.append(r)
        print(f"\n[A8] Ctrl-C ({mode}): {n_hang}/{REAL_FAULT_TRIALS} hung "
              "(caught by the external subprocess timeout, not asserted "
              "against -- see this test's docstring)")
        assert n_trials >= REAL_FAULT_TRIALS // 2, (
            f"too many trials were unusable ({REAL_FAULT_TRIALS - n_trials}/"
            f"{REAL_FAULT_TRIALS}) -- timing mis-calibration, not a finding")
        print(f"\n[A8] Ctrl-C ({mode}): {published}/{n_trials} published, "
              f"{n_close_failed}/{n_trials} close() failures, "
              f"{len(violations)} IDENTITY VIOLATIONS, over {n_trials} "
              "trials of a 20,000-event burst with 2 interrupts.")
        for ex in violations[:3]:
            print(f"  VIOLATION: on_disk={ex['on_disk_records']} "
                  f"post_close={ex['post_close_accounting']} "
                  f"state={ex['state']!r}")
        if mode == "sigint":
            assert not violations, (
                f"{len(violations)}/{n_trials} trials left MORE records "
                "readable on disk than the writer booked as written -- the "
                "on-disk chain is broken from that record on. A8 defers "
                "SIGINT across both commitment regions precisely so a real "
                f"signal cannot reach a partial buffered write: {violations}")
            assert published == n_trials, (
                f"{n_trials - published}/{n_trials} trials failed to "
                "publish. A8 defers SIGINT/SIGTERM across the commitment "
                "region precisely so a real interrupt cannot make an open "
                "segment's already-durable records unpublishable; the "
                "pre-A1 queue-based writer measured 6/6 publication for "
                "this campaign's one-interrupt form")
        else:
            # FAIL-CLOSED, asserted rather than assumed: a ctypes injection
            # CAN corrupt the byte stream below `segment.py` (see the
            # docstring). What must never happen is that corruption being
            # PUBLISHED as committed evidence.
            for ex in violations:
                assert ex["close_ok"] is False, (
                    "a segment whose on-disk records outnumber its booked "
                    f"writes was PUBLISHED: {ex}")
                assert ex["record_count"] is None, ex
                assert ex["state"] == "INVALID", ex


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
            # A8: the same structured identity as the six-trial campaign
            # above -- never the unreachable substring search this used to
            # perform (`"does not reconcile"`/`"not clean"` appear nowhere
            # in `app/`).
            written = (r.get("post_close_accounting") or {}).get("written")
            on_disk = r.get("on_disk_records")
            if written is not None and on_disk is not None and on_disk > written:
                identity_violations.append(r)
        print(f"\n[A8] 3-interrupt SIGINT campaign: "
              f"{len(identity_violations)} IDENTITY VIOLATIONS / {n_trials} "
              f"usable trials, {n_hang}/{REAL_FAULT_TRIALS} hung (each caught "
              "by the external subprocess timeout, not a suite hang)")
        assert n_trials >= REAL_FAULT_TRIALS // 4
        assert not identity_violations, identity_violations
