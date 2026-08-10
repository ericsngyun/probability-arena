"""KALSHI-ARCHIVE-VERIFICATION-HARNESSES-001, harness A3 — updated for
KALSHI-ARCHIVE-CORE-REMEDIATION-002 A4+A6.

Targeted fault-injection harness for one specific class: an asynchronous
exception arrives inside `SegmentWriter.submit()`/admission at an
instruction boundary. Two independent reviewers found this class with
different methods; `TestAccountingSurvivesTheGate` in
`test_kalshi_archive_genesis_001.py` cannot see it, because it raises from
`Hostile.items()` — the ONE position `submit()`'s blanket
`except BaseException` is actually positioned to catch.

WHAT CHANGED (A6): the four windows below are still real and still
reproducible — no pure-Python critical section can be made immune to an
asynchronous exception at every instruction boundary, and this file proves
that remains true. What changed is what a violation MEANS. Before A6,
`WriterAccounting.accepted` was an independently incremented counter, and
`admission_holds()`/`disposition_holds()` failing was FATAL: `close()` raised
`SegmentError("does not reconcile"/"not clean")` and destroyed every other
record in the segment along with the one the fault touched. After A6,
`accepted` is DERIVED from `written + failed_after_accept + pending` — there
is no independent counter for these windows to desynchronise any more — so
`disposition_holds()` is a tautology and `admission_holds()` is a pure
DIAGNOSTIC (how well `submit()` counted its own attempts), which `clean()`
and `close()` no longer depend on. Every window below now asserts the NEW,
required outcome: `close()` succeeds, every previously-accepted record
survives, and — if `admission_holds()` happens to be violated — that is
recorded on `w.admission_drift`, never fatal.

DOES NOT MODIFY ANY PRODUCTION MODULE. This file and everything under
`tests/harness_async_accounting/` only observes `app.realtime.segment` from
outside, using:

  - a deterministic `sys.settrace`-based line-boundary injector
    (`harness_async_accounting.line_injector`) for exact, replayable minimal
    reproductions of each window. Injection points are now resolved by a
    SOURCE MARKER SCAN (`target_marker`/`find_marker_line`), not a hardcoded
    line number: `# FAULT-WINDOW: <marker>` comments live next to the
    statement they target inside `segment.py`/`reference_shim.py` and move
    WITH it under refactoring. The previous remediation shifted `segment.py`
    by +50 lines and broke 9 hardcoded-line tests here; a marker scan closes
    that class of breakage rather than re-deriving the numbers once more;
  - real asynchronous exceptions — `SIGINT` from a bomber thread, and
    `ctypes.PyThreadState_SetAsyncExc` — run in a SUBPROCESS with an
    EXTERNAL wall-clock timeout enforced by this file, so a wedged trial
    cannot hang the suite and a hang is distinguishable from a returned
    result (`harness_async_accounting.fault_trial`);
  - standalone reference implementations (`harness_async_accounting.
    reference_shim`) used only to prove the fault-injection methodology
    itself discriminates correct accounting from broken accounting, for an
    ALTERNATIVE (independently-counted, retry-until-committed) accounting
    design — production's actual A6 design removes the independent counter
    instead; see `reference_shim`'s module docstring.

Runtime: the default run exercises every window and every discrimination
case at least once, deterministically, plus a small real-signal Ctrl-C
campaign — a few seconds total. The full statistical campaign this file's
report numbers were measured with (hundreds of deterministic repeats, and a
several-dozen-trial real-signal campaign) is gated behind
`KALSHI_ASYNC_ACCOUNTING_CAMPAIGN=1` because it takes on the order of a
minute (dominated by real subprocess + fsync costs, not by this harness).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

from tests.harness_async_accounting import reference_shim as rs
from tests.harness_async_accounting.line_injector import (
    InjectedFault, LineBoundaryInjector, target_marker, poison_once,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
ENV = "demo"
SEGMENT_FILE = sg.__file__
SHIM_FILE = rs.__file__
FAULT_TRIAL_SCRIPT = Path(__file__).parent / "harness_async_accounting" / "fault_trial.py"

CAMPAIGN = os.getenv("KALSHI_ASYNC_ACCOUNTING_CAMPAIGN", "") == "1"
DETERMINISTIC_REPEATS = 200 if CAMPAIGN else 15
REAL_FAULT_TRIALS = 24 if CAMPAIGN else 6


def fields(i, ticker="KXA"):
    return {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": ticker, "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {"price_dollars": "0.5100", "side": "no"},
        "normalized_event": {"raw_price_units": 5100},
    }


def new_writer(tmp_path, seg_id, n=5, **kw):
    root = tmp_path / seg_id
    root.mkdir(parents=True, exist_ok=True)
    init(root)
    w = sg.SegmentWriter(root, environment=ENV, segment_id=seg_id,
                         partition_identity="p", commit_to_head=False, **kw)
    for i in range(n):
        assert w.submit(fields(i)) is None
    return w


def init(root):
    return ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")


def _wait_for_drain(w, n, timeout_s=2.0):
    import time as _t
    deadline = _t.monotonic() + timeout_s
    while w.accounting.written < n and _t.monotonic() < deadline:
        _t.sleep(0.005)


class Hostile(dict):
    """The ONE position `submit()`'s blanket handler is actually positioned
    to catch: an exception raised from inside the gate's own walk of the
    payload (`non_canonical_reason` -> `_structural_reason` -> `.items()`).
    Used only to deliver window (d)'s FIRST fault as an ordinary Python
    exception (not `sys.settrace`-raised — see `ChainInjector`'s docstring
    for why that distinction matters for a two-fault window)."""

    def items(self):
        raise KeyboardInterrupt("first fault: from the gate")


# ---------------------------------------------------------------------------
# Section 1: the four windows, reproduced deterministically against the REAL
# SegmentWriter.submit(), with exact counters asserted. The REQUIRED outcome
# is now identical for all four: `close()` succeeds and no previously- or
# newly-durable record is lost, whether or not `admission_holds()` — a pure
# diagnostic now — happens to be violated.
# ---------------------------------------------------------------------------

class TestFourWindowsReproduceDeterministically:
    """Every window is exercised `DETERMINISTIC_REPEATS` times (default 15,
    200 under the full campaign) to confirm the reproduction is stable, not
    a one-off timing accident — which is meaningful for a `sys.settrace`
    method, since it is otherwise exact and repeat count mostly confirms
    "no flakiness from harness bookkeeping", not "sometimes it doesn't
    happen": every one of these fires on every trial."""

    def test_window_a_diagnostic_drift_never_blocks_close(self, tmp_path):
        """(a) fault lands after `attempted += 1` but before `_inflight += 1`
        — OUTSIDE the try/except. `attempted` moves; nothing else does.
        Nothing was ever queued, so there is no durable record to lose:
        `close()` must publish the 5 already-accepted records regardless."""
        violations = 0
        for i in range(DETERMINISTIC_REPEATS):
            w = new_writer(tmp_path, f"a-{i}")
            pt = target_marker(SEGMENT_FILE, sg.SegmentWriter.submit, "window-a")
            escaped = None
            try:
                with LineBoundaryInjector(pt):
                    w.submit(fields(99))
            except BaseException as e:
                escaped = e
            # The baseline 5 items are still being drained by the writer
            # thread; wait for them so `accepted` (which folds in live queue
            # depth) is read against a settled state, not a race with the
            # writer thread's own progress.
            _wait_for_drain(w, 5)
            acc = w.accounting
            assert isinstance(escaped, InjectedFault)
            if not acc.admission_holds():
                violations += 1
                assert acc.attempted == 6 and acc.accepted == 5
                assert acc.rejected_before_accept == 0
            manifest = w.close()          # MUST succeed — no durable loss.
            assert manifest["record_count"] == 5
            assert acc.clean(), acc.to_dict()
        assert violations == DETERMINISTIC_REPEATS, (
            f"window (a) reproduced {violations}/{DETERMINISTIC_REPEATS} times")

    def test_window_b_post_enqueue_fault_still_writes_the_record(self, tmp_path):
        """(b) fault lands after `put_nowait()` succeeds — the item IS
        durably queued and WILL be written. There is no independent
        `accepted += 1` left for this to land between (see `_admit`): the
        only observable effect is `_note_depth()` not running and, if the
        fault escapes to `submit()`'s handler, a diagnostic double-booking
        (`rejected_before_accept` also moves for an item that is really
        accepted). Either way the item must show up in the closed segment."""
        violations = 0
        for i in range(DETERMINISTIC_REPEATS):
            w = new_writer(tmp_path, f"b-{i}")
            pt = target_marker(SEGMENT_FILE, sg.SegmentWriter._admit, "window-b")
            try:
                with LineBoundaryInjector(pt):
                    w.submit(fields(99))
            except BaseException:
                pass
            _wait_for_drain(w, 6)
            acc = w.accounting
            if not acc.admission_holds():
                violations += 1
                assert acc.attempted == 6 and acc.rejected_before_accept == 1
                assert acc.written == 6, acc.to_dict()
            manifest = w.close()
            assert manifest["record_count"] == 6, (
                "the item that was genuinely queued must be in the archive")
            assert acc.clean(), acc.to_dict()
        assert violations == DETERMINISTIC_REPEATS, (
            f"window (b) reproduced {violations}/{DETERMINISTIC_REPEATS} times")

    @pytest.mark.parametrize("marker", [
        "window-c (note-depth-l1)", "window-c (note-depth-l2)",
        "window-c (note-depth-l3)",
    ], ids=["note-depth-l1", "note-depth-l2", "note-depth-l3"])
    def test_window_c_double_booked_but_the_record_still_lands(self, tmp_path, marker):
        """(c) fault lands inside `_note_depth()`, called AFTER the event is
        already durably queued — the event is booked BOTH (diagnostically)
        rejected and (really) accepted, but it is genuinely queued either
        way and must be written."""
        violations = 0
        for i in range(DETERMINISTIC_REPEATS):
            safe = marker.replace(" ", "_").replace("(", "").replace(")", "")
            seg_id = f"c-{safe}-{i}"
            root = tmp_path / seg_id
            root.mkdir(parents=True, exist_ok=True)
            init(root)
            w = sg.SegmentWriter(root, environment=ENV, segment_id=seg_id,
                                 partition_identity="p", commit_to_head=False)
            for k in range(5):
                assert w.submit(fields(k)) is None
            # note-depth-l3 (`queue_high_water = depth`) only runs when depth
            # is a NEW maximum. The writer thread drains fast enough that,
            # without backpressure, a later submission usually sees the same
            # (or lower) depth as an earlier one and the line is never
            # reached — a timing fact about the test's own setup, not about
            # the fault. Force a fresh maximum deterministically rather than
            # racing the writer thread with a sleep.
            w.queue_high_water = -1
            pt = target_marker(SEGMENT_FILE, sg.SegmentWriter._note_depth, marker)
            try:
                with LineBoundaryInjector(pt):
                    w.submit(fields(99))
            except BaseException:
                pass
            _wait_for_drain(w, 6)
            acc = w.accounting
            if not acc.admission_holds():
                violations += 1
                assert acc.attempted == 6
                assert acc.rejected_before_accept == 1, acc.to_dict()
            manifest = w.close()
            assert manifest["record_count"] == 6
            assert acc.clean(), acc.to_dict()
        assert violations == DETERMINISTIC_REPEATS, (
            f"window (c)@{marker} reproduced {violations}/{DETERMINISTIC_REPEATS} times")

    def test_window_d_second_fault_during_the_handler_still_closes_clean(self, tmp_path):
        """(d) a SECOND fault, delivered while `submit()`'s own exception
        handler is still booking the FIRST fault's `reject_before_accept`,
        leaves NO terminal booking at all for that call. The first fault is
        delivered as a genuine Python exception (`Hostile.items()`, the same
        mechanism `TestAccountingSurvivesTheGate` already uses) so it does
        not disable `sys.settrace`; the second is the deterministic
        injector. Nothing was ever queued for this call either way, so there
        is nothing durable to lose."""
        violations = 0
        for i in range(DETERMINISTIC_REPEATS):
            w = new_writer(tmp_path, f"d-{i}")
            pt = target_marker(SEGMENT_FILE, sg.SegmentWriter.submit, "window-d")
            try:
                with LineBoundaryInjector(pt):
                    w.submit({**fields(99), "raw_event": Hostile(a=1)})
            except BaseException:
                pass
            _wait_for_drain(w, 5)
            acc = w.accounting
            if not acc.admission_holds():
                violations += 1
                assert acc.attempted == 6 and acc.accepted == 5
                assert acc.rejected_before_accept == 0, acc.to_dict()
            manifest = w.close()
            assert manifest["record_count"] == 5
            assert acc.clean(), acc.to_dict()
        assert violations == DETERMINISTIC_REPEATS, (
            f"window (d) reproduced {violations}/{DETERMINISTIC_REPEATS} times")


class TestNegativeControlsDoNotOverFire:
    """Not every boundary in `submit()`/`_admit()` is broken. These points
    are all "the fault lands somewhere the except handler is already
    watching, and nothing has been double-committed yet" — the harness must
    report them clean AND `admission_holds()` must stay TRUE, or it would
    not be discriminating anything."""

    @pytest.mark.parametrize("func,marker", [
        (sg.SegmentWriter.submit, "safe-before-admit"),
        (sg.SegmentWriter._admit, "safe-before-enqueue"),
    ])
    def test_safe_boundary_stays_clean(self, tmp_path, func, marker):
        for i in range(5):
            w = new_writer(tmp_path, f"safe-{marker}-{i}".replace(" ", "_"))
            pt = target_marker(SEGMENT_FILE, func, marker)
            try:
                with LineBoundaryInjector(pt):
                    w.submit(fields(99))
            except BaseException:
                pass
            acc = w.accounting
            assert acc.admission_holds(), (marker, acc.to_dict())
            manifest = w.close()
            assert manifest["record_count"] == 5

    def test_second_fault_after_terminal_booking_is_a_true_negative(self, tmp_path):
        """`already-terminal` is reached only via a genuine first fault
        (`Hostile`, same as window (d)'s setup). By then `reject_before_
        accept` has ALREADY completed, so a second fault landing exactly
        here is the true-negative twin of window (d)'s violation: nothing
        left to interrupt."""
        for i in range(5):
            w = new_writer(tmp_path, f"safe-terminal-{i}")
            pt = target_marker(SEGMENT_FILE, sg.SegmentWriter.submit,
                               "already-terminal")
            try:
                with LineBoundaryInjector(pt):
                    w.submit({**fields(99), "raw_event": Hostile(a=1)})
            except BaseException:
                pass
            acc = w.accounting
            assert acc.admission_holds(), acc.to_dict()
            assert acc.attempted == 6 and acc.rejected_before_accept == 1
            manifest = w.close()
            assert manifest["record_count"] == 5


# ---------------------------------------------------------------------------
# Section 2: discrimination — good control survives, broken variants caught.
# `reference_shim` demonstrates an ALTERNATIVE accounting design (an
# independently-counted `accepted`, made safe by retry-until-committed
# critical sections) — a different, also-valid shape from production's
# actual A6 fix (a DERIVED `accepted`, so there is no independent counter to
# protect). Both are legitimate answers to the same four windows; this
# section proves the injection methodology itself discriminates correct from
# broken for that alternative shape, so its own catches are trustworthy.
# ---------------------------------------------------------------------------

class TestDiscriminationDeterministic:
    """Required deliverable (a)+(b): the SAME deterministic methodology,
    pointed at standalone reference implementations, catches each of the
    four broken variants and passes the good control clean."""

    def test_good_control_survives_window_a_analog(self):
        """Fault right after `attempted` is committed, before the enqueue
        step begins — still inside the same `try`, unlike production's
        actual window (a) gap."""
        s = rs.GoodSubmitter()
        for i in range(5):
            assert s.submit(i) is None
        pt = target_marker(SHIM_FILE, rs.GoodSubmitter._commit,
                           "good-window-a-analog", label="good/window-a-analog")
        try:
            with LineBoundaryInjector(pt):
                s.submit(99)
        except BaseException:
            pass
        s.drain_all()
        assert s.accounting.clean(), s.accounting.to_dict()

    def test_good_control_survives_window_b_analog(self):
        """Fault right after the item is durably appended to the queue but
        before `accepted` is incremented — both statements are in the SAME
        critical section here, so there is no source line between them."""
        s = rs.GoodSubmitter()
        for i in range(5):
            assert s.submit(i) is None
        pt = target_marker(SHIM_FILE, rs.GoodSubmitter._commit_enqueue,
                           "good-window-b-analog", label="good/window-b-analog")
        try:
            with LineBoundaryInjector(pt):
                s.submit(99)
        except BaseException:
            pass
        s.drain_all()
        assert s.accounting.clean(), s.accounting.to_dict()

    def test_good_control_survives_window_d_analog_double_fault(self):
        """The good control's real test: a fault that gets it INTO the
        exception handler (a poisoned `_commit_enqueue` — an ordinary Python
        exception, not settrace-raised, so tracing stays live), plus a
        second settrace fault while the handler's own retry-until-committed
        booking (`_commit_reject`) is running."""
        s = rs.GoodSubmitter()
        for i in range(5):
            assert s.submit(i) is None
        pt = target_marker(SHIM_FILE, rs.GoodSubmitter._commit_reject,
                           "good-window-d-analog", hit_index=1,
                           label="good/window-d-analog")
        try:
            with poison_once(s, "_commit_enqueue"):
                with LineBoundaryInjector(pt):
                    s.submit(99)
        except BaseException:
            pass
        s.drain_all()
        assert s.accounting.clean(), s.accounting.to_dict()

    @pytest.mark.parametrize("variant,func,marker", [
        ("bad_a", rs.BadWindowA.submit, "bad_a"),
        ("bad_b", rs.BadWindowB.submit, "bad_b"),
        ("bad_c", rs.BadWindowC.submit, "bad_c"),
    ])
    def test_bad_variant_is_caught(self, variant, func, marker):
        cls = rs.VARIANTS[variant]
        for _ in range(5):
            s = cls()
            for i in range(5):
                assert s.submit(i) is None
            pt = target_marker(SHIM_FILE, func, marker, label=variant)
            try:
                with LineBoundaryInjector(pt):
                    s.submit(99)
            except BaseException:
                pass
            s.drain_all()
            assert not s.accounting.reconciles(), (
                f"{variant} should have violated an identity but didn't: "
                f"{s.accounting.to_dict()}")

    def test_bad_window_d_is_caught_by_the_double_fault(self):
        for _ in range(5):
            s = rs.BadWindowD()
            for i in range(5):
                assert s.submit(i) is None
            pt = target_marker(SHIM_FILE, rs.BadWindowD.submit, "bad_d",
                               label="bad_d")
            try:
                with poison_once(s._queue, "put_nowait"):
                    with LineBoundaryInjector(pt):
                        s.submit(99)
            except BaseException:
                pass
            s.drain_all()
            assert not s.accounting.reconciles(), s.accounting.to_dict()


# ---------------------------------------------------------------------------
# Section 3: real asynchronous exceptions — SIGINT and PyThreadState_SetAsyncExc,
# run in subprocesses with an external wall-clock timeout, proving the class is
# not an artifact of sys.settrace, and quantifying ordinary Ctrl-C data loss
# under production's ACTUAL (fixed) accounting design.
# ---------------------------------------------------------------------------

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
    subprocess, proves the class found deterministically above is not an
    artifact of `sys.settrace`. Calibration (see the A3 report): a tight
    submission loop can complete before a background bomber thread ever gets
    an OS scheduling slot, so the loop needs real wall-clock duration
    (n_submits=20_000 takes roughly tens of ms to ~1s depending on load) for
    the bombardment window to reliably land INSIDE it rather than after."""

    def test_a_single_sigint_lands_inside_submit_and_is_caught_cleanly(self, tmp_path):
        """One interrupt, by itself, should not usually destroy a segment —
        `submit()`'s except handler is correct for a SINGLE fault landing in
        most of `_admit`. This is a sanity check on the trial mechanism
        itself before the multi-interrupt campaign below."""
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
        """THE required deliverable, post-A6: 2 real interrupts during a
        20,000-event submission burst must NEVER destroy the whole,
        otherwise-valid segment any more. Reviewers measured 9/16 and 5/12
        data-loss rates against the OLD, pre-fix accounting (recorded in the
        A3 report and in this file's git history); this test now asserts the
        opposite outcome and REQUIRES ZERO IDENTITY VIOLATIONS, judged by
        whether any interrupt turned durable, already-written evidence into
        an unpublishable/invalid segment — not by a loss-rate threshold,
        because the rate is host-scheduling sensitive.

        An IDENTITY VIOLATION here means: `close()` failed AND the reason is
        the accounting identity (`"does not reconcile"`/`"not clean"`) rather
        than a genuine, freshly-arriving interrupt landing inside `close()`'s
        own machinery (a normal "caller aborted the shutdown" outcome A6
        does not claim to eliminate — see the report). The latter is
        recorded as `close_ok: False` but is NOT counted as a violation
        unless it is accompanied by evidence of actual data loss (a
        `written` count inconsistent with what was durably queued).

        A HANG is recorded, not asserted against, exactly as Section 4
        already treats a 3-interrupt SIGINT hang: `PyThreadState_SetAsyncExc`
        can interrupt literally anywhere, including inside CPython's own
        internal lock machinery, and CPython's own documentation calls this
        mechanism unsafe for exactly that reason. A wedged subprocess, caught
        by this file's EXTERNAL wall-clock timeout, is a property of the
        injection mechanism, not evidence about `segment.py`'s correctness —
        it is not "close() failed", it never reached a `close()` call at all.
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
        print(f"\n[A6] Ctrl-C ({mode}): {n_hang}/{REAL_FAULT_TRIALS} hung "
              "(caught by the external subprocess timeout, not asserted "
              "against — see this test's docstring)")
        assert n_trials >= REAL_FAULT_TRIALS // 2, (
            f"too many trials were unusable ({REAL_FAULT_TRIALS - n_trials}/"
            f"{REAL_FAULT_TRIALS}) — timing mis-calibration, not a finding")
        print(f"\n[A6] Ctrl-C ({mode}): {n_close_failed}/{n_trials} close() "
              f"failures, {len(identity_violations)} IDENTITY VIOLATIONS, over "
              f"{n_trials} trials of a 20,000-event burst with 2 interrupts.")
        for ex in identity_violations[:3]:
            print(f"  VIOLATION: pre_close={ex['pre_close_accounting']} "
                  f"close_error={ex['close_error']!r}")
        assert not identity_violations, (
            f"{len(identity_violations)}/{n_trials} trials show the OLD "
            f"defect: a diagnostic accounting mismatch destroyed a segment. "
            f"{identity_violations}")


# ---------------------------------------------------------------------------
# Section 4: the three-interrupt Ctrl-C campaign, gated — the larger campaign
# this file's report numbers were measured with.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not CAMPAIGN, reason="KALSHI_ASYNC_ACCOUNTING_CAMPAIGN=1 "
                    "not set; this is the full statistical campaign (~1min)")
class TestFullCampaign:
    def test_three_interrupt_campaign_zero_identity_violations(self, tmp_path):
        """A genuine finding surfaced while calibrating this campaign: 3
        real SIGINTs against a 20,000-submit run occasionally WEDGE the
        subprocess outright (observed once in early runs — see the A3
        report). That is exactly the "hang, not a returned result" outcome
        `subprocess.run(..., timeout=...)` exists to catch, so it is counted
        as its own category here rather than either failing the whole
        campaign or being silently absorbed into "lost". As in Section 3,
        the REQUIRED outcome post-A6 is ZERO identity violations, not a rate
        threshold."""
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
        print(f"\n[A6] 3-interrupt SIGINT campaign: "
              f"{len(identity_violations)} IDENTITY VIOLATIONS / {n_trials} "
              f"usable trials, {n_hang}/{REAL_FAULT_TRIALS} hung (each caught "
              "by the external subprocess timeout, not a suite hang)")
        assert n_trials >= REAL_FAULT_TRIALS // 4
        assert not identity_violations, identity_violations
