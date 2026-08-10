"""KALSHI-ARCHIVE-VERIFICATION-HARNESSES-001, harness A3.

Targeted fault-injection harness for one specific class: an asynchronous
exception arrives inside `SegmentWriter.submit()`/admission at an
instruction boundary and breaks the writer accounting. Two independent
reviewers found this class with different methods; `TestAccountingSurvivesTheGate`
in `test_kalshi_archive_genesis_001.py` cannot see it, because it raises from
`Hostile.items()` — the ONE position `submit()`'s blanket
`except BaseException` is actually positioned to catch.

DOES NOT MODIFY ANY PRODUCTION MODULE. This file and everything under
`tests/harness_async_accounting/` only observes `app.realtime.segment` from
outside, using:

  - a deterministic `sys.settrace`-based line-boundary injector
    (`harness_async_accounting.line_injector`) for exact, replayable minimal
    reproductions of each window;
  - real asynchronous exceptions — `SIGINT` from a bomber thread, and
    `ctypes.PyThreadState_SetAsyncExc` — run in a SUBPROCESS with an
    EXTERNAL wall-clock timeout enforced by this file, so a wedged trial
    cannot hang the suite and a hang is distinguishable from a returned
    result (`harness_async_accounting.fault_trial`);
  - standalone reference implementations (`harness_async_accounting.
    reference_shim`) used only to prove the fault-injection methodology
    itself discriminates correct accounting from broken accounting.

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
    InjectedFault, LineBoundaryInjector, target, poison_once,
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
# SegmentWriter.submit(), with exact counters asserted.
# ---------------------------------------------------------------------------

class TestFourWindowsReproduceDeterministically:
    """Every window is exercised `DETERMINISTIC_REPEATS` times (default 15,
    200 under the full campaign) to confirm the reproduction is stable, not
    a one-off timing accident — which is meaningful for a `sys.settrace`
    method, since it is otherwise exact and repeat count mostly confirms
    "no flakiness from harness bookkeeping", not "sometimes it doesn't
    happen": every one of these fires on every trial."""

    def test_window_a_no_terminal_booking_at_all(self, tmp_path):
        """(a) fault lands after `attempted += 1` but before `_inflight += 1`
        — OUTSIDE the try/except. `attempted` moves; nothing else does."""
        violations = 0
        for i in range(DETERMINISTIC_REPEATS):
            w = new_writer(tmp_path, f"a-{i}")
            pt = target(SEGMENT_FILE, "submit", 1040, label="window-a")
            escaped = None
            try:
                with LineBoundaryInjector(pt):
                    w.submit(fields(99))
            except BaseException as e:
                escaped = e
            acc = w.accounting
            assert isinstance(escaped, InjectedFault)
            if not acc.admission_holds():
                violations += 1
                assert acc.attempted == 6 and acc.accepted == 5
                assert acc.rejected_before_accept == 0
                with pytest.raises(sg.SegmentError, match="does not reconcile"):
                    w.close()
            else:  # pragma: no cover - would mean the window stopped reproducing
                w.close()
        assert violations == DETERMINISTIC_REPEATS, (
            f"window (a) reproduced {violations}/{DETERMINISTIC_REPEATS} times")

    def test_window_b_written_exceeds_accepted(self, tmp_path):
        """(b) fault lands after `put_nowait()` succeeds but before
        `accepted += 1` — the event IS queued and WILL be written, but
        submit's except books it `reject_before_accept` too."""
        violations = 0
        for i in range(DETERMINISTIC_REPEATS):
            w = new_writer(tmp_path, f"b-{i}")
            pt = target(SEGMENT_FILE, "_admit", 1090, label="window-b")
            try:
                with LineBoundaryInjector(pt):
                    w.submit(fields(99))
            except BaseException:
                pass
            # Give the writer thread a chance to drain the phantom-accepted
            # item; it WILL write it, because it really is in the queue.
            w._queue.join() if hasattr(w._queue, "join") else None
            import time as _t
            deadline = _t.monotonic() + 2.0
            while w.accounting.written < 5 and _t.monotonic() < deadline:
                _t.sleep(0.005)
            acc = w.accounting
            if not acc.disposition_holds():
                violations += 1
                assert acc.accepted == 5 and acc.written == 6, acc.to_dict()
                with pytest.raises(sg.SegmentError, match="does not reconcile"):
                    w.close()
            else:  # pragma: no cover
                w.close()
        assert violations == DETERMINISTIC_REPEATS, (
            f"window (b) reproduced {violations}/{DETERMINISTIC_REPEATS} times")

    @pytest.mark.parametrize("lineno", [1091, 1109, 1110, 1111],
                             ids=["admit-before-note-depth", "note-depth-l1",
                                  "note-depth-l2", "note-depth-l3"])
    def test_window_c_double_booked_accepted_and_rejected(self, tmp_path, lineno):
        """(c) fault lands after `accepted += 1` (at the `_note_depth()`
        call boundary, or at three different points inside `_note_depth`
        itself) — the event is booked BOTH accepted and rejected."""
        funcname = "_admit" if lineno == 1091 else "_note_depth"
        violations = 0
        for i in range(DETERMINISTIC_REPEATS):
            w = new_writer(tmp_path, f"c-{lineno}-{i}")
            pt = target(SEGMENT_FILE, funcname, lineno, label="window-c")
            try:
                with LineBoundaryInjector(pt):
                    w.submit(fields(99))
            except BaseException:
                pass
            acc = w.accounting
            if not acc.admission_holds():
                violations += 1
                assert acc.attempted == 6
                assert acc.accepted == 6 and acc.rejected_before_accept == 1, (
                    acc.to_dict())
                with pytest.raises(sg.SegmentError, match="does not reconcile"):
                    w.close()
            else:  # pragma: no cover
                w.close()
        assert violations == DETERMINISTIC_REPEATS, (
            f"window (c)@{lineno} reproduced {violations}/{DETERMINISTIC_REPEATS} times")

    def test_window_d_second_fault_during_the_handler_loses_the_booking(self, tmp_path):
        """(d) a SECOND fault, delivered while `submit()`'s own exception
        handler is still booking the FIRST fault's `reject_before_accept`,
        leaves NO terminal booking at all. The first fault is delivered as a
        genuine Python exception (`Hostile.items()`, the same mechanism
        `TestAccountingSurvivesTheGate` already uses) so it does not disable
        `sys.settrace`; the second is the deterministic injector."""
        violations = 0
        for i in range(DETERMINISTIC_REPEATS):
            w = new_writer(tmp_path, f"d-{i}")
            pt = target(SEGMENT_FILE, "submit", 1056, label="window-d-second-fault")
            try:
                with LineBoundaryInjector(pt):
                    w.submit({**fields(99), "raw_event": Hostile(a=1)})
            except BaseException:
                pass
            acc = w.accounting
            if not acc.admission_holds():
                violations += 1
                assert acc.attempted == 6 and acc.accepted == 5
                assert acc.rejected_before_accept == 0, acc.to_dict()
                with pytest.raises(sg.SegmentError, match="does not reconcile"):
                    w.close()
            else:  # pragma: no cover
                w.close()
        assert violations == DETERMINISTIC_REPEATS, (
            f"window (d) reproduced {violations}/{DETERMINISTIC_REPEATS} times")


class TestNegativeControlsDoNotOverFire:
    """Not every boundary in `submit()`/`_admit()` is broken. These three
    points are all "the fault lands somewhere the except handler is already
    watching, and nothing has been double-committed yet" — the harness must
    report them clean, or it would not be discriminating anything."""

    @pytest.mark.parametrize("funcname,lineno,label", [
        ("submit", 1042, "before calling _admit"),
        ("_admit", 1085, "before put_nowait"),
    ])
    def test_safe_boundary_stays_clean(self, tmp_path, funcname, lineno, label):
        for i in range(5):
            w = new_writer(tmp_path, f"safe-{lineno}-{i}")
            pt = target(SEGMENT_FILE, funcname, lineno, label=label)
            try:
                with LineBoundaryInjector(pt):
                    w.submit(fields(99))
            except BaseException:
                pass
            acc = w.accounting
            assert acc.admission_holds(), (funcname, lineno, acc.to_dict())
            manifest = w.close()
            assert manifest["record_count"] == 5

    def test_second_fault_after_terminal_booking_is_a_true_negative(self, tmp_path):
        """Line 1058 — right before the handler's own `raise` — is reached
        only via a genuine first fault (`Hostile`, same as window (d)'s
        setup). By then `reject_before_accept` has ALREADY completed, so a
        second fault landing exactly here is the true-negative twin of
        window (d)'s violation: nothing left to interrupt."""
        for i in range(5):
            w = new_writer(tmp_path, f"safe-1058-{i}")
            pt = target(SEGMENT_FILE, "submit", 1058, label="already-terminal")
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
        pt = target(SHIM_FILE, "submit", 184, label="good/window-a-analog")
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
        pt = target(SHIM_FILE, "_commit_enqueue", 172, label="good/window-b-analog")
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
        pt = target(SHIM_FILE, "_commit_reject", 136, hit_index=1,
                    label="good/window-d-analog")
        try:
            with poison_once(s, "_commit_enqueue"):
                with LineBoundaryInjector(pt):
                    s.submit(99)
        except BaseException:
            pass
        s.drain_all()
        assert s.accounting.clean(), s.accounting.to_dict()

    @pytest.mark.parametrize("variant,funcname,lineno", [
        ("bad_a", "submit", 208),
        ("bad_b", "submit", 232),
        ("bad_c", "submit", 255),
    ])
    def test_bad_variant_is_caught(self, variant, funcname, lineno):
        cls = rs.VARIANTS[variant]
        for _ in range(5):
            s = cls()
            for i in range(5):
                assert s.submit(i) is None
            pt = target(SHIM_FILE, funcname, lineno, label=variant)
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
            pt = target(SHIM_FILE, "submit", 284, label="bad_d")
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
# not an artifact of sys.settrace, and quantifying ordinary Ctrl-C data loss.
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
    def test_ctrl_c_data_loss_quantified(self, tmp_path, mode):
        """THE required deliverable: quantify how often a SMALL number of
        real interrupts (2) during a 20,000-event submission burst destroys
        the ENTIRE otherwise-valid segment at `close()` — not just the
        events touched by the fault. Reviewers measured 9/16 and 5/12 with
        real SIGINT. This independently measures the rate with fresh trials
        and reports it, whatever it is."""
        n_lost = 0
        n_usable = 0
        examples = []
        for seed in range(REAL_FAULT_TRIALS):
            r = _run_fault_trial(target_="segment", seed=seed, n_interrupts=2,
                                 mode=mode, window_s=0.6, tmp_path=tmp_path)
            assert not r.get("hang"), f"trial {seed} hung: {r}"
            assert not r.get("crash"), f"trial {seed} crashed: {r}"
            if r.get("top_level_fault"):
                # A fault landed outside the submission loop and outside
                # close() (e.g. while this trial script itself was tearing
                # down) — recorded, not silently dropped, but not usable for
                # the "did submission-time interrupts destroy the segment"
                # question this test asks.
                continue
            n_usable += 1
            if not r["close_ok"]:
                n_lost += 1
                examples.append(r)
        assert n_usable >= REAL_FAULT_TRIALS // 2, (
            f"too many trials were unusable ({REAL_FAULT_TRIALS - n_usable}/"
            f"{REAL_FAULT_TRIALS}) — timing mis-calibration, not a finding")
        rate = n_lost / n_usable
        print(f"\n[A3] Ctrl-C data loss ({mode}): {n_lost}/{n_usable} usable "
              f"trials lost the ENTIRE segment to 2 interrupts over "
              f"{REAL_FAULT_TRIALS and 20_000} submitted events.")
        for ex in examples[:3]:
            print(f"  example: pre_close={ex['pre_close_accounting']} "
                  f"close_error={ex['close_error']!r}")
        # The reviewers' rates (9/16 ~= 0.56, 5/12 ~= 0.42) establish that
        # this is a COMMON outcome, not a one-in-a-thousand edge case. This
        # harness's own measured rate (recorded in the A3 report) is
        # asserted only to be "not negligible", so the test keeps failing
        # loudly if a future fix makes this rate collapse to near zero
        # without anyone updating this file to say so on purpose.
        assert rate > 0.10, (
            f"Ctrl-C data loss rate ({mode}) dropped to {rate:.0%} — either "
            "the vulnerability was fixed (update this assertion and the A3 "
            "report to say so) or this trial's calibration broke")


# ---------------------------------------------------------------------------
# Section 4: the three-interrupt Ctrl-C campaign, gated — the larger campaign
# this file's report numbers were measured with.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not CAMPAIGN, reason="KALSHI_ASYNC_ACCOUNTING_CAMPAIGN=1 "
                    "not set; this is the full statistical campaign (~1min)")
class TestFullCampaign:
    def test_three_interrupt_campaign(self, tmp_path):
        """A genuine finding surfaced while calibrating this campaign: 3
        real SIGINTs against a 20,000-submit run occasionally WEDGE the
        subprocess outright (observed once in early runs — see the A3
        report). That is exactly the "hang, not a returned result" outcome
        `subprocess.run(..., timeout=...)` exists to catch, so it is counted
        as its own category here rather than either failing the whole
        campaign or being silently absorbed into "lost"."""
        n_lost = 0
        n_usable = 0
        n_hang = 0
        for seed in range(REAL_FAULT_TRIALS):
            r = _run_fault_trial(target_="segment", seed=seed, n_interrupts=3,
                                 mode="sigint", window_s=0.25, tmp_path=tmp_path)
            assert not r.get("crash"), r
            if r.get("hang"):
                n_hang += 1
                continue
            if r.get("top_level_fault"):
                continue
            n_usable += 1
            if not r["close_ok"]:
                n_lost += 1
        print(f"\n[A3] 3-interrupt SIGINT campaign: {n_lost}/{n_usable} lost, "
              f"{n_hang}/{REAL_FAULT_TRIALS} hung (each caught by the "
              "external subprocess timeout, not a suite hang)")
        assert n_usable >= REAL_FAULT_TRIALS // 4
