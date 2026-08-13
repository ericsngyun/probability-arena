"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- regression pins for the defects
three independent reviewers found in the synchronous canonical archive (A1).

Every test in this file is written so that REVERTING the corresponding fix in
`app/` makes it FAIL. Where a property genuinely cannot be pinned
behaviourally -- because reproducing it requires injecting an asynchronous
exception at one exact bytecode boundary -- the pin is STRUCTURAL (an AST
assertion about `submit()`'s shape), and says so.

Sections:
  1. signal deferral across the commitment region              (BLOCKER 1)
  2. the state delta is computed before the write and applied
     from a `finally`                                          (BLOCKER 2)
  3. `_MANIFEST_METADATA_WORK_RESERVE`                          (BLOCKER 6)
  4. `_RECORD_ENVELOPE_OVERHEAD_UNITS` == 17                    (BLOCKER 7)
  5. acked != durable: the SIGKILL measurement                  (ITEM 8)
  6. rotation does not stall the producer                       (ITEM 9)
  7. multi-member `consumed` arithmetic                         (ITEM 12)

DOES NOT MODIFY ANY PRODUCTION MODULE.
"""

from __future__ import annotations

import ast
import gzip
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.realtime import archive as ar
from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

ENV = "demo"
UTC = timezone.utc
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[1]


def fields(i, **extra):
    base = {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": "KXA", "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {"price_dollars": "0.5100", "side": "no"},
        "normalized_event": {"raw_price_units": 5100},
    }
    base.update(extra)
    return base


def make_writer(tmp_path, seg_id, **kw):
    root = tmp_path / seg_id
    root.mkdir()
    ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
    kw.setdefault("commit_to_head", False)
    return sg.SegmentWriter(root, environment=ENV, segment_id=seg_id,
                            partition_identity="p", **kw), root


def encode_units(value) -> int:
    """The exact number of `WorkBudget.consume()` calls the REAL encoder
    performs over `value`, measured against production with an effectively
    unbounded budget so the count is exact rather than truncated."""
    wb = cn.WorkBudget(limit=10 ** 12)
    cn._encode(value, 0, wb)
    return wb.units


def _submit_ast():
    module = ast.parse(Path(sg.__file__).read_text())
    cls = next(n for n in module.body
               if isinstance(n, ast.ClassDef) and n.name == "SegmentWriter")
    return next(n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == "submit")


# =====================================================================
# 1. BLOCKER 1 -- SIGNAL DEFERRAL ACROSS THE COMMITMENT REGION.
#
# Measured A/B on the repo's own harness (`fault_trial.py --n-interrupts 1
# --n-submits 20000`, seeds 0-5): 321c719 gave 6/6 `close_ok: True`;
# d004c01 gave 6/6 `close_ok: False, record_count: null` -- the ENTIRE open
# segment lost to one Ctrl-C. The end-to-end proof of the fix lives in
# `test_kalshi_async_accounting_harness_001.py` (real signals, real
# subprocesses, the 321c719 numbers pinned as the floor). What is pinned
# HERE is the mechanism, cheaply and deterministically.
# =====================================================================

@pytest.fixture
def default_sigint():
    """Pin `SIGINT` to CPython's own `default_int_handler` for the duration
    of a test, and restore whatever was there before.

    Necessary, not decorative: `_DeferCommitSignals.__exit__` re-delivers a
    deferred signal with `os.kill`, which reproduces the ORIGINAL
    disposition -- deliberately, so the writer never reinterprets an
    application's own handler. That makes "was a KeyboardInterrupt raised?"
    a question about the AMBIENT disposition unless the test pins it. In a
    full-suite run it is not the default (measured: these assertions passed
    in isolation and failed inside `pytest tests/`), so a test that assumed
    it was measuring the deferral was really measuring the suite.
    """
    import signal as _signal

    prev = _signal.getsignal(_signal.SIGINT)
    _signal.signal(_signal.SIGINT, _signal.default_int_handler)
    try:
        yield
    finally:
        _signal.signal(_signal.SIGINT, prev)


class TestSignalDeferralAcrossTheCommitmentRegion:

    def test_the_deferral_is_actually_installed_around_the_write(
            self, tmp_path, default_sigint):
        """A real `submit()`, with a `pre_write_hook` that inspects the
        process's live signal disposition AT the commitment point. If the
        deferral is removed, `SIGINT`'s handler at that instant is whatever
        the process default is, not the writer's."""
        w, _ = make_writer(tmp_path, "seg-defer")
        seen = {}

        def probe(writer):
            import signal as _signal
            seen["handler"] = _signal.getsignal(_signal.SIGINT)
            seen["installed"] = writer._defer_commit_signals.installed

        w.pre_write_hook = probe
        outer = __import__("signal").getsignal(__import__("signal").SIGINT)
        assert w.submit(fields(0)) is None
        w.close()

        assert seen["installed"] is True, (
            "the commitment region ran with NO signal deferral installed -- "
            "a Ctrl-C landing on the gzip write again marks the segment "
            "INVALID and makes every already-durable record unpublishable")
        assert seen["handler"] is not outer, (
            "SIGINT's handler during the commitment region is the ordinary "
            "process handler; nothing is deferring it")
        # ...and it is restored afterwards. A writer that permanently
        # swallowed Ctrl-C would be a far worse defect than the one being
        # fixed.
        assert __import__("signal").getsignal(
            __import__("signal").SIGINT) is outer

    def test_a_signal_arriving_inside_the_region_is_delivered_after_it(
            self, tmp_path, default_sigint):
        """The deferral must DELAY the signal, never DISCARD it. A writer
        that ate SIGINT would be unshippable."""
        import signal as _signal

        w, _ = make_writer(tmp_path, "seg-redeliver")
        landed = {"inside": False}

        def bomb(writer):
            os.kill(os.getpid(), _signal.SIGINT)
            # Give CPython every chance to raise here if it were going to.
            for _ in range(200000):
                pass

        w.pre_write_hook = bomb
        try:
            w.submit(fields(0))
        except KeyboardInterrupt:
            landed["inside"] = True
        deadline = time.monotonic() + 2.0
        while not landed["inside"] and time.monotonic() < deadline:
            try:
                for _ in range(100000):
                    pass
                time.sleep(0.01)
            except KeyboardInterrupt:
                landed["inside"] = True
        w.close()
        assert landed["inside"], (
            "the SIGINT raised during the commitment region was never "
            "delivered at all -- deferral must postpone a signal to a "
            "boundary the writer chooses, never swallow it")
        # And the record it was interrupting is durable and booked.
        assert w.accounting.written == 1
        assert len(sg.read_segment_records(w.events_path)) == 1

    def test_the_record_survives_a_signal_at_the_commitment_point(
            self, tmp_path, default_sigint):
        """The property the A/B measures, in miniature: a SIGINT delivered
        AT the write must not invalidate the segment or lose the record."""
        import signal as _signal

        w, root = make_writer(tmp_path, "seg-survive")
        fired = {"n": 0}

        def bomb(writer):
            if fired["n"] == 0:
                fired["n"] = 1
                os.kill(os.getpid(), _signal.SIGINT)

        w.pre_write_hook = bomb
        caught = 0
        for i in range(20):
            try:
                w.submit(fields(i))
            except KeyboardInterrupt:
                caught += 1
        assert fired["n"] == 1
        assert w.state is sg.SegmentState.OPEN, (
            f"one SIGINT invalidated the segment: {w._writer_error!r}")
        manifest = w.close()
        assert manifest["record_count"] == 20
        assert manifest["close_status"] == "clean"

    def test_deferral_is_a_no_op_off_the_main_thread(self, tmp_path):
        """`signal.signal()` raises `ValueError` off the main thread, and a
        Python-level handler can never run there anyway. The deferral must
        degrade to nothing rather than break a non-main-thread producer."""
        w, _ = make_writer(tmp_path, "seg-thread")
        seen = {}

        def probe(writer):
            seen["installed"] = writer._defer_commit_signals.installed

        w.pre_write_hook = probe
        err = []

        def produce():
            try:
                assert w.submit(fields(0)) is None
            except BaseException as exc:                     # pragma: no cover
                err.append(exc)

        t = threading.Thread(target=produce)
        t.start()
        t.join(10)
        assert not err, err
        assert seen["installed"] is False
        assert w.close()["record_count"] == 1


class _TerminateHandler:
    """The single most common shutdown idiom -- `raise SystemExit(0)` from a
    `SIGTERM` handler -- as an OBJECT rather than a function.

    Being an object is what makes the regression below deterministic instead
    of a soak. `_DeferCommitSignals.__enter__` evaluates `prev ==
    self._defer` for each signal it is about to replace; when `prev` is an
    object, that comparison is a PYTHON-level call, and a Python-level call
    is a point at which CPython delivers a pending signal. Releasing a
    kernel-pending `SIGTERM` from inside `__eq__` therefore lands a REAL
    signal, running the application's REAL handler, at exactly the boundary
    the reviewer reproduced by brute force (525 SIGTERMs across a
    20,000-record submit loop). Nothing about the writer is stubbed.
    """

    def __init__(self):
        self.release_on_compare = False
        self.calls = 0

    def __call__(self, signum, frame):
        self.calls += 1
        raise SystemExit(0)

    def __eq__(self, other):
        if self.release_on_compare:
            self.release_on_compare = False
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
        return NotImplemented

    __hash__ = object.__hash__


class TestTheDeferralCannotDeafenTheProcessPermanently:
    """A8 ROUND 2 -- BLOCKER 1.

    `__enter__` installed `_defer` for SIGINT, appended to a LOCAL list, then
    looped to SIGTERM -- where the application's still-live SIGTERM handler
    could raise. `SystemExit` is a `BaseException`, outside the
    `except (ValueError, OSError, RuntimeError)` tuple, so the rollback never
    ran and `self._saved` was never assigned: SIGINT stayed bound to `_defer`
    PERMANENTLY, every later Ctrl-C landed in `self._pending` (reset to `[]`
    on the next entry, never drained), and `installed` read False so the
    existing tests could not see it. The operator could no longer interrupt
    the collector at all -- a NEW permanent-controllability failure inside
    the mechanism added to make signals safe.
    """

    def test_a_raising_sigterm_handler_cannot_leave_sigint_deaf(
            self, tmp_path, default_sigint):
        """THE PIN, with a real signal and a real `raise SystemExit(0)`.

        Only the DELIVERY MOMENT is controlled (a kernel-pending SIGTERM
        released from inside the handler object's `__eq__`); the writer, the
        handler and the signal are all genuine.

        `pthread_kill(main_thread)`, not `os.kill(pid)`: a PROCESS-directed
        signal may be handed to any thread whose mask allows it, and in a
        full-suite run other threads left over from earlier tests are exactly
        the condition under which a main-thread `pthread_sigmask` block stops
        holding -- which is the very fact `_DeferCommitSignals`' module
        comment uses to REFUSE `pthread_sigmask` as a PRODUCTION mechanism.
        A thread-directed signal stays pending for THIS thread regardless of
        what else is running, so the instrument works in isolation and inside
        the suite alike.
        """
        w, _ = make_writer(tmp_path, "seg-deaf")
        handler = _TerminateHandler()
        previous_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, handler)
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
            handler.release_on_compare = True
            with pytest.raises(SystemExit):
                w.submit(fields(0))
            assert handler.calls == 1, (
                "the SIGTERM was never actually delivered, so this test did "
                "not exercise the window it exists for")
            assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, (
                "SIGINT is still bound to the writer's deferring handler "
                "after a SystemExit escaped `__enter__`: the process is now "
                "PERMANENTLY deaf to Ctrl-C and every interrupt an operator "
                "sends disappears into `_pending`")
            assert w._defer_commit_signals._saved == ()
            assert w._defer_commit_signals.installed is False
            # ...and the writer is still usable: failing to obtain the
            # deferral must never break the write path.
            assert w.submit(fields(1)) is None
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
            signal.signal(signal.SIGTERM, previous_term)

    def test_a_baseexception_between_the_two_restores_cannot_leak_sigint(
            self, tmp_path, default_sigint):
        """The IDENTICAL hazard in `__exit__`.

        `contextlib.suppress(Exception)` does not catch `BaseException`, and
        `reversed(self._saved)` restored SIGTERM BEFORE SIGINT -- so a
        just-restored SIGTERM handler firing between the two leaked SIGINT
        exactly the same way. There is no Python-level call inside
        `__exit__`'s restore loop to hang a deterministic real-signal release
        on, so the delivery is injected at the mechanism instead: a shim over
        the `signal` module `segment.py` itself uses, which raises
        `SystemExit` from the FIRST restore. That is the same event a real
        SIGTERM produces, at the same place.
        """
        w, _ = make_writer(tmp_path, "seg-exit-leak")
        real_signal = sg.signal
        state = {"restores": 0, "installs": 0}

        class _Shim:
            SIGINT = real_signal.SIGINT
            SIGTERM = real_signal.SIGTERM

            @staticmethod
            def getsignal(sig):
                return real_signal.getsignal(sig)

            @staticmethod
            def signal(sig, handler):
                # Only the RESTORE direction detonates; installing `_defer`
                # must still work or the region is never entered. `==`, not
                # `is`: every attribute access on `_defer` builds a FRESH
                # bound-method object, so an identity test here matches
                # nothing and detonates on the very first INSTALL instead --
                # which silently turns this into a second `__enter__` test.
                deferring = handler == w._defer_commit_signals._defer
                state["installs"] += int(deferring)
                if not deferring and state["restores"] == 0:
                    state["restores"] += 1
                    real_signal.signal(sig, handler)
                    raise SystemExit("a restored SIGTERM handler fired")
                return real_signal.signal(sig, handler)

        try:
            sg.signal = _Shim
            with pytest.raises(SystemExit):
                w.submit(fields(0))
        finally:
            sg.signal = real_signal
        assert state["installs"] == 2, (
            "the deferral was not fully installed, so `__exit__` never had "
            f"two handlers to restore: {state}")
        assert state["restores"] == 1, "the injected restore never fired"
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, (
            "a BaseException raised while restoring the FIRST handler left "
            "SIGINT bound to the writer's deferring handler -- the same "
            "permanent deafness, reached through `__exit__` instead of "
            "`__enter__`")
        assert w._defer_commit_signals._saved == ()
        assert w._defer_commit_signals.installed is False

    def test_the_restore_is_idempotent_and_never_clobbers_a_new_handler(
            self, tmp_path, default_sigint):
        """The retry loop must not undo a handler the application installed
        while the region was open -- restoring is conditional on the signal
        still being bound to `_defer`, not unconditional."""
        w, _ = make_writer(tmp_path, "seg-idem")
        replacement = lambda signum, frame: None      # noqa: E731

        def swap(writer):
            # Whatever the application does inside the region wins.
            signal.signal(signal.SIGINT, replacement)

        w.pre_write_hook = swap
        try:
            assert w.submit(fields(0)) is None
            assert signal.getsignal(signal.SIGINT) is replacement, (
                "the restore clobbered a handler installed AFTER the "
                "deferral went in")
        finally:
            signal.signal(signal.SIGINT, signal.default_int_handler)

    def test_an_ordinary_restore_failure_is_actually_retried(
            self, default_sigint):
        """A8 ROUND 3 -- THE RETRY WAS BLIND TO THE FAILURE ITS OWN NAME
        IMPLIES.

        `_restore`'s inner `signal.signal` call sits in
        `contextlib.suppress(Exception)`, so an ORDINARY restore failure (an
        `OSError` from `signal.signal`) never escaped the pass -- and the
        loop `break`ed unconditionally at the end of it. The `except
        BaseException` retry therefore only ever fired for signal-class
        exceptions, and `len(saved) + 2` bounded a loop that could run
        exactly once.

        Proven by injection at ad5d540, the same technique the `__exit__`
        half uses: `_restore()` returned NORMALLY with BOTH handlers still
        bound to `_defer`, no exception raised and `installed` False -- the
        permanent-deafness state this method exists to prevent. Here the
        injected failure is TRANSIENT (the whole first pass refuses, every
        later attempt succeeds), which is exactly what a retry is for: with
        the loop breaking after one pass the handlers stay deaf; retrying on
        the OBSERVED handler state restores them.

        Not reachable on darwin or Linux today -- `signal.signal` raises
        `ValueError` off the main thread and `__enter__` returns early there,
        so `saved` is empty -- so this pins a latent structural hole.
        """
        real_signal = sg.signal
        deferred = sg._COMMIT_DEFERRED_SIGNALS
        previous = {s: signal.getsignal(s) for s in deferred}
        calls = []

        class _Shim:
            @staticmethod
            def getsignal(sig):
                return real_signal.getsignal(sig)

            @staticmethod
            def signal(sig, handler):
                calls.append(sig)
                if len(calls) <= len(deferred):
                    # The whole FIRST pass refuses; everything after it works.
                    raise OSError("injected restore refusal")
                return real_signal.signal(sig, handler)

        d = sg._DeferCommitSignals()
        try:
            d.__enter__()
            assert d.installed is True, (
                "the deferral did not install, so this test never had a "
                "handler to restore")
            assert all(signal.getsignal(s) == d._defer for s in deferred)
            sg.signal = _Shim
            try:
                d._restore()
            finally:
                sg.signal = real_signal
            still_deaf = [s for s in deferred
                          if signal.getsignal(s) == d._defer]
            assert not still_deaf, (
                f"`_restore()` returned normally with {still_deaf} STILL "
                "bound to the deferring handler: an ordinary restore failure "
                "is suppressed inside the pass, so the unconditional `break` "
                "ended the loop after one attempt and the process is now "
                "permanently deaf to those signals")
            assert len(calls) > len(deferred), (
                "the restore was attempted exactly once per signal, so no "
                f"retry happened at all: {calls}")
            assert len(calls) <= (len(deferred) + 2) * len(deferred), (
                f"the retry is no longer bounded by `len(saved) + 2`: {calls}")
        finally:
            for sig, handler in previous.items():
                real_signal.signal(sig, handler)

    def test_a_permanent_restore_failure_stays_bounded_and_is_loud(
            self, default_sigint):
        """The other half of the same bound: when the restore can NEVER
        succeed, `_restore()` must still terminate -- `len(saved) + 2`
        passes, never `while True` -- rather than livelock a collector.

        A8 ROUND 3, SECOND PASS -- THIS PIN USED TO MEASURE THE WRONG THING.
        It asserted `len(calls) == 8` and nothing else, so it passed happily
        in the state it exists to rule out: at the shape it was written
        against, `_restore()` returned NORMALLY with both handlers still
        bound to `_defer`, no exception and no flag -- byte-identical to the
        pre-fix permanent-deafness defect, just reached after four passes
        instead of one. Its own `finally` put the handlers back, which is the
        tell that the code under test had not.

        The retry cannot make `signal.signal` succeed, so the honest contract
        is not "the handlers end up restored" but "exhaustion is never
        reported as success". Both halves are asserted below: the bound is
        still `len(saved) + 2` passes, AND the terminal handler state is
        inspected BEFORE this test repairs anything.
        """
        real_signal = sg.signal
        deferred = sg._COMMIT_DEFERRED_SIGNALS
        previous = {s: signal.getsignal(s) for s in deferred}
        calls = []

        class _Shim:
            @staticmethod
            def getsignal(sig):
                return real_signal.getsignal(sig)

            @staticmethod
            def signal(sig, handler):
                calls.append(sig)
                raise OSError("injected permanent restore refusal")

        d = sg._DeferCommitSignals()
        try:
            d.__enter__()
            assert d.installed is True
            sg.signal = _Shim
            try:
                with pytest.raises(RuntimeError) as excinfo:
                    d._restore()      # must RETURN (not hang), and be LOUD
            finally:
                sg.signal = real_signal
            # Terminal state, read before this test's own `finally` repairs
            # anything: the process really is deaf, and that is exactly what
            # the raise above has to be reporting.
            still_deaf = [s for s in deferred
                          if real_signal.getsignal(s) == d._defer]
            assert still_deaf, (
                "the injection did not actually leave any handler bound to "
                "`_defer`, so this test is not measuring permanent deafness "
                "at all")
            for sig in still_deaf:
                assert str(sig) in str(excinfo.value), (
                    f"the exhaustion error does not name {sig}, which is "
                    f"still deaf: {excinfo.value!r} -- an operator reading "
                    "this cannot tell which signals the process stopped "
                    "responding to")
            assert len(calls) == (len(deferred) + 2) * len(deferred), (
                "the bounded retry did not run exactly `len(saved) + 2` "
                f"passes over `saved`: {len(calls)} calls")
        finally:
            for sig, handler in previous.items():
                real_signal.signal(sig, handler)

    def test_a_hostile_handler_comparison_does_not_escape_the_restore(
            self, default_sigint):
        """A8 ROUND 3, SECOND PASS -- THE UNGUARDED COMPARISON.

        `_restore`'s per-signal check wrapped `getsignal(sig) != self._defer`
        in `try/except`; the loop's EXIT CONDITION ran the same comparison
        unguarded three lines below it. `getsignal` returns whatever the
        application installed, so a handler whose `__eq__`/`__ne__` raises
        made the exit condition raise, that escaped into the `except
        BaseException`, and `_restore()` re-raised a comparison error out of
        a best-effort cleanup path.

        REACHING IT NEEDS THE RESTORE TO FAIL, not just a hostile handler:
        the inner loop cannot compare such a handler either, so it falls
        through and REPLACES it, and the exit condition then sees an ordinary
        one. Only a signal whose `signal.signal` also refuses survives to be
        compared -- so the shim below refuses, and the hostile handler is
        still installed when the exit condition runs.

        WHAT DISTINGUISHES THE TWO SHAPES is therefore not "raises or not"
        (both raise) but WHAT the operator is told. Guarded, the comparison
        fails closed and `_restore` reports the thing it actually knows -- a
        `RuntimeError` naming the signals this process has stopped responding
        to. Unguarded, it reports the application handler's own comparison
        error, which names no signal and describes no consequence.

        Not reachable through `__enter__` today (it refuses to install over a
        handler it cannot compare), so this drives `_restore` directly with a
        hand-built `_saved`: the asymmetry is what is under test, not a live
        defect.
        """
        class _Hostile:
            MESSAGE = "hostile handler comparison"

            def __eq__(self, other):
                raise RuntimeError(self.MESSAGE)

            def __ne__(self, other):
                raise RuntimeError(self.MESSAGE)

            __hash__ = None

            def __call__(self, signum, frame):
                pass

        real_signal = sg.signal
        sig = signal.SIGINT
        previous = signal.getsignal(sig)

        class _Shim:
            @staticmethod
            def getsignal(s):
                return real_signal.getsignal(s)

            @staticmethod
            def signal(s, handler):
                raise OSError("injected restore refusal")

        try:
            signal.signal(sig, _Hostile())
            d = sg._DeferCommitSignals()
            d._saved = ((sig, signal.default_int_handler),)
            d.installed = True
            sg.signal = _Shim
            try:
                with pytest.raises(RuntimeError) as excinfo:
                    d._restore()
            finally:
                sg.signal = real_signal
            assert _Hostile.MESSAGE not in str(excinfo.value), (
                "`_restore()` re-raised the APPLICATION HANDLER's own "
                f"comparison error ({excinfo.value!r}): the loop's exit "
                "condition compares `getsignal(sig)` against `_defer` "
                "unguarded, three lines below the identical comparison that "
                "IS guarded")
            assert str(sig) in str(excinfo.value), (
                "the failure does not name the signal the process can no "
                f"longer respond to: {excinfo.value!r}")
        finally:
            real_signal.signal(sig, previous)

    def test_the_region_is_documented_as_uninterruptible_while_it_stalls(self):
        """S4. The deferral region has NO timeout and covers `pre_write_hook`
        and `flush()` as well as `write()`. Measured: a 3 s blocking op inside
        it withheld a real Ctrl-C for 2.5 s, and the known `pre_write_hook`
        reentrancy deadlock is now un-interruptible (two real Ctrl-Cs did not
        break it; an external watchdog had to). That is a real loss of
        operator control and the module must SAY so rather than let an
        operator discover it during an incident."""
        text = Path(sg.__file__).read_text()
        head = text.split("class _DeferCommitSignals", 1)[0]
        assert "NO TIMEOUT" in head.upper(), (
            "nothing in the deferral's own documentation says the region is "
            "unbounded in time")
        assert "Ctrl-C-proof" in head or "un-interruptible" in head.lower(), (
            "the docs do not state plainly that a stalled filesystem makes "
            "the collector uninterruptible for the duration of the stall")

    def test_the_pthread_sigmask_refutation_is_a_committed_harness(self):
        """S5. The measurement that justifies rejecting `pthread_sigmask`
        existed ONLY as prose in a code comment -- the exact pattern this
        milestone has been eliminating -- and its magnitude was wrong
        (`300/300` claimed; 14-37/300 measured). The harness is now committed
        next to `segment_close_cost.py` and the comment cites it."""
        harness = REPO_ROOT / "tests" / "benchmarks" / "pthread_sigmask_refutation.py"
        assert harness.is_file(), (
            "the pthread_sigmask refutation is still prose-only; `grep -rn "
            "pthread_sigmask tests/` must find a runnable harness")
        text = Path(sg.__file__).read_text()
        assert "tests/benchmarks/pthread_sigmask_refutation.py" in text, (
            "segment.py's design comment does not cite the harness that "
            "produced its numbers")
        # The refuted magnitude must no longer be STATED AS A RESULT. It may
        # still be named as the thing that was corrected, which is why this
        # matches the result-row form rather than the bare number.
        assert "-> 300/300" not in text, (
            "the refuted magnitude is still asserted as a measurement")
        assert "That magnitude was wrong" in text, (
            "the correction is not recorded, so the next reader has no way "
            "to know the old number was measured wrong rather than lost")


# =====================================================================
# 2. BLOCKER 2 -- THE OWNERSHIP WINDOW WAS RELOCATED, NOT ELIMINATED.
#
# Seven statements sat AFTER the commitment point and OUTSIDE every `try`:
# `_first_digest`, `_last_digest`, `_stream_digest`, `_prev_digest`,
# `_ordinal += 1`, `accounting.written += 1`, `return None`. An interrupt
# among them left the writer OPEN / healthy / accepting / clean -- nothing
# signalling a fault -- with `_ordinal` and `_prev_digest` NOT advanced, so
# every later record carried a duplicate `receive_ordinal` and a stale
# `previous_record_digest`. Reproduced 6/6 with a real SIGINT:
# `written 194, on_disk 200, state OPEN, clean true`.
# =====================================================================

class TestTheStateDeltaIsComputedBeforeTheWrite:

    def test_a_failure_in_the_fold_cannot_leave_an_unbooked_durable_record(
            self, tmp_path, monkeypatch):
        """THE BEHAVIOURAL PIN, and it is fully deterministic.

        `fold_stream_digest` is one of the tail statements. Making it raise
        answers the question "does this work happen before the write, or
        after it?" without needing to hit a bytecode boundary:

          fix reverted (tail after the write):  the record IS on disk, and
             `written`/`_ordinal`/`_prev_digest` never advance -- so
             `on_disk > written`, the writer is still OPEN and `clean()`,
             and the NEXT record reuses the ordinal and the stale digest.
          fix present (delta computed first):   the raise happens before
             any byte is written, so nothing is durable, nothing drifts,
             and the segment is still perfectly consistent afterwards.
        """
        w, root = make_writer(tmp_path, "seg-fold")
        for i in range(5):
            assert w.submit(fields(i)) is None
        w._fh.flush()
        boom = RuntimeError("fold_stream_digest exploded")

        def explode(*a, **kw):
            raise boom

        monkeypatch.setattr(sg, "fold_stream_digest", explode)
        with pytest.raises(RuntimeError):
            w.submit(fields(5))
        monkeypatch.undo()

        w._fh.flush()
        on_disk = len(sg.read_segment_records(w.events_path))
        assert on_disk <= w.accounting.written, (
            f"{on_disk} records are durably on disk but the writer booked "
            f"only {w.accounting.written} -- the state delta was applied "
            "AFTER the commitment point, so `_ordinal`/`_prev_digest` are "
            "now behind the bytes and every subsequent record breaks the "
            "chain")
        assert on_disk == 5 and w.accounting.written == 5

        # The decisive consequence: the segment must still close cleanly and
        # its chain must still verify. Under the reverted shape, record 6
        # would reuse ordinal 5 and chain off a stale digest.
        for i in range(6, 10):
            assert w.submit(fields(i)) is None
        manifest = w.close()
        assert manifest["close_status"] == "clean"
        assert manifest["record_count"] == 9
        verdict = sg.verify_segment(w.dir, environment=ENV, root=root)
        assert verdict.valid, verdict.reasons

    def test_no_state_mutation_survives_outside_the_write_try(self):
        """THE STRUCTURAL PIN. The behavioural test above proves the delta
        is COMPUTED before the write; this proves it is APPLIED from a
        `finally` on the same `try`, which is what makes the window
        genuinely closed rather than merely narrowed. It is structural
        because reproducing the residual window requires landing an
        asynchronous exception on one exact bytecode boundary.

        The rule: inside `submit()`, once a statement writes to `_fh`, every
        subsequent assignment to a piece of commit state -- `self._chain`
        (the whole chain, in one immutable value) and `self.accounting.
        written` -- must be lexically inside a `finally:` handler.

        A8 ROUND 2, TWO CHANGES:

        * `accounting.written` IS NOW PINNED. It was missing from the guard's
          attribute set entirely, AND the visitor required the assignment
          target's `.value` to be an `ast.Name` called `"self"`, so
          `self.accounting.written = ...` -- an `ast.Attribute` value -- was
          invisible to it twice over. Moving that one statement back above
          the `finally` would have passed the pin while reopening the exact
          defect it exists to prevent.
        * The five separate chain attributes are ONE `self._chain`, so a
          single `STORE_ATTR` applies them. `finally` guarantees the body is
          ENTERED, not that it COMPLETES, so five stores were five chances to
          stop halfway; one store cannot be half-applied.
        """
        tree = _submit_ast()
        # (attribute name, required owner path) -- the owner path is matched
        # structurally, so `self.accounting.written` is as visible as
        # `self._chain`.
        STATE_ATTRS = {"_chain": ("self",),
                       "written": ("self", "accounting")}
        RETIRED = {"_first_digest", "_last_digest", "_stream_digest",
                   "_prev_digest", "_ordinal"}

        def _owner_path(node):
            """('self',) for `self.x`, ('self', 'accounting') for
            `self.accounting.x`; None for anything else."""
            parts = []
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if not isinstance(node, ast.Name):
                return None
            parts.append(node.id)
            return tuple(reversed(parts))

        def _match(target):
            if not isinstance(target, ast.Attribute):
                return None
            want = STATE_ATTRS.get(target.attr)
            if want is None:
                return None
            return target.attr if _owner_path(target.value) == want else None

        def _writes_fh(node) -> bool:
            for n in ast.walk(node):
                if (isinstance(n, ast.Attribute) and n.attr == "write"
                        and isinstance(n.value, ast.Attribute)
                        and n.value.attr == "_fh"):
                    return True
            return False

        offenders = []
        found_in_finally = set()

        def _walk(node, in_finally: bool):
            if isinstance(node, ast.Try):
                for child in node.body:
                    _walk(child, in_finally)
                for handler in node.handlers:
                    for child in handler.body:
                        _walk(child, in_finally)
                for child in node.orelse:
                    _walk(child, in_finally)
                for child in node.finalbody:
                    _walk(child, True)
                return
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    hit = _match(target)
                    if hit is not None:
                        if in_finally:
                            found_in_finally.add(hit)
                        else:
                            offenders.append((hit, target.lineno))
            if isinstance(node, ast.AugAssign):
                hit = _match(node.target)
                if hit is not None:
                    offenders.append((hit, node.target.lineno))
            for child in ast.iter_child_nodes(node):
                _walk(child, in_finally)

        assert any(_writes_fh(stmt) for stmt in ast.walk(tree)), (
            "submit() no longer writes to self._fh; this guard needs to be "
            "pointed at wherever the commitment point now lives")
        for stmt in tree.body:
            _walk(stmt, in_finally=False)

        assert not offenders, (
            "submit() assigns commit state outside any `finally:` handler at "
            f"{offenders} -- an asynchronous exception landing there leaves "
            "the writer OPEN, healthy and `clean()` with the chain or "
            "`written` behind the bytes already on disk, which breaks the "
            "on-disk chain for every subsequent record")
        assert found_in_finally == set(STATE_ATTRS), (
            "this commit state is no longer applied from a `finally`: "
            f"{sorted(set(STATE_ATTRS) - found_in_finally)}")

        # And the five retired attributes must not quietly come back as
        # separate stores: that is what re-opens the partial-apply window
        # the single `_chain` store closes.
        source = Path(sg.__file__).read_text()
        cls = next(n for n in ast.parse(source).body
                   if isinstance(n, ast.ClassDef) and n.name == "SegmentWriter")
        resurrected = {
            n.attr for n in ast.walk(cls)
            if isinstance(n, ast.Attribute) and n.attr in RETIRED
            and isinstance(n.value, ast.Name) and n.value.id == "self"}
        assert not resurrected, (
            f"the collapsed chain fields are separate attributes again: "
            f"{sorted(resurrected)} -- `finally` guarantees ENTRY, not "
            "completion, so N stores are N chances to stop halfway")

    def test_a_failure_booking_written_is_loud_not_silently_poisoning(
            self, tmp_path):
        """S2 -- THE BEHAVIOURAL PIN for the one field that cannot join
        `_ChainState`.

        `accounting.written` lives on another object, so it is a second
        `STORE_ATTR` and an asynchronous exception can still land between it
        and the chain store. The round-1 shape left the writer in exactly the
        state the reviewer reproduced -- `written` behind `on_disk`, `state
        OPEN`, `healthy true`, `clean true` -- and `append()` went on
        returning SUCCESS for up to 13,000 further records that `close()` was
        already guaranteed to discard hours later. Fail-closed-eventually is
        not fail-closed.

        The interruption is injected AT the store rather than raced onto it
        (the same amplification this milestone already accepts): a
        `WriterAccounting` whose `written` setter raises once. What is under
        test is the writer's REACTION, and that is production code.
        """

        class _WrittenBomb(sg.WriterAccounting):
            armed = False

            def __setattr__(self, name, value):
                if name == "written" and self.__dict__.get("armed"):
                    self.__dict__["armed"] = False
                    raise SystemExit("async exception between the two stores")
                object.__setattr__(self, name, value)

        w, root = make_writer(tmp_path, "seg-written-guard")
        for i in range(3):
            assert w.submit(fields(i)) is None
        bomb = _WrittenBomb()
        bomb.__dict__.update(w.accounting.__dict__)
        w.accounting = bomb
        assert w.accounting.written == 3
        bomb.__dict__["armed"] = True

        with pytest.raises(SystemExit):
            w.submit(fields(3))

        assert w.state is sg.SegmentState.INVALID, (
            "the writer is still OPEN after a partially-applied commit "
            f"delta (state={w.state!r}) -- `written` is "
            f"{w.accounting.written} with {w._chain.ordinal} records on the "
            "chain, and `append()` will keep returning success for records "
            "that close() is already guaranteed to discard")
        assert isinstance(w._writer_error, SystemExit)
        assert w.healthy is False
        # The decisive consequence: the NEXT submit is refused immediately,
        # rather than accepted and silently discarded much later.
        assert w.submit(fields(4)) is sg.RejectReason.WRITER_FAILED


# =====================================================================
# 3. BLOCKER 6 -- subscription_metadata had a DEPTH reserve but no WORK
#    reserve, so a value certified legal at construction destroyed the
#    segment at close.
# =====================================================================

def _rows(last_len: int, outer_n: int = 1999, inner_n: int = 1999) -> list:
    """`outer_n` sublists of `inner_n` short strings, the last sized to
    order. Every element is list-borne, so both the structural walk and the
    encoder charge it symmetrically and only the ONE enclosing mapping key
    contributes the walk-vs-encode asymmetry. Sublists keep every container
    under `MAX_SEQUENCE_ELEMENTS` while the TOTAL reaches the aggregate
    work ceiling -- the shape `tests/meta_runtime/budget_consistency.py`
    already uses."""
    rows = [["x"] * inner_n for _ in range(max(outer_n - 1, 0))]
    rows.append(["x"] * last_len)
    return rows


def _value_at_encode_units(key: str, target: int) -> dict:
    """`{key: <deep list structure>}` whose REAL encode cost is exactly
    `target` units. Solved arithmetically -- the cost is linear in the
    trailing sublist's length with slope exactly 1 -- rather than by binary
    search, so it costs two constructions instead of eleven."""
    floor = encode_units({key: _rows(0)})
    pad = target - floor
    assert 0 <= pad <= 1_000_000, (target, floor, pad)
    value = {key: _rows(pad)}
    assert encode_units(value) == target, (encode_units(value), target)
    return value


class TestManifestMetadataWorkReserve:

    def test_the_reserve_is_derived_from_the_manifest_schema(self):
        """DERIVED, not hand-counted. The constant used to be
        `len(MANIFEST_FIELDS) + 2`, where the `+ 2` was a tally of the two
        body keys that are not in `MANIFEST_FIELDS`; a third such key would
        have left the reserve one unit short in silence -- exactly the
        failure this reserve exists to prevent. It is now the measured key
        count of a real `build_manifest` body, and this test measures it the
        same way rather than restating the arithmetic."""
        body = sg.build_manifest(
            environment=ENV, segment_id="seg", partition_identity="p",
            opened_at=cn.canonical_datetime(NOW),
            closed_at=cn.canonical_datetime(NOW), record_count=0,
            first_record_digest=None, last_record_digest=None,
            ordered_stream_digest=None, event_file_size_bytes=0,
            event_file_sha256=None, subscription_metadata={})
        assert sg._MANIFEST_METADATA_WORK_RESERVE == len(body)
        assert set(sg.MANIFEST_FIELDS) < set(body), (
            "MANIFEST_FIELDS is no longer a strict subset of the manifest "
            "body; the reserve's derivation needs re-deriving")

    def test_the_reserve_matches_the_measured_wrapper_cost_exactly(self):
        """Derived, then MEASURED against the real `build_manifest`: the
        wrapper costs exactly one `_encode` unit per manifest key, for every
        shape of metadata."""
        gen = sg.genesis_digest(segment_id="seg", environment=ENV)
        worst = 0
        for meta in ({}, {"venue": "kalshi"}, {"a": {"b": [1, 2, 3]}},
                     {"a": list(range(50))}):
            man = sg.build_manifest(
                environment=ENV, segment_id="seg", partition_identity="p",
                opened_at=cn.canonical_datetime(NOW),
                closed_at=cn.canonical_datetime(NOW), record_count=1,
                first_record_digest=gen, last_record_digest=gen,
                ordered_stream_digest=gen, event_file_size_bytes=1,
                event_file_sha256="a" * 64, subscription_metadata=meta)
            worst = max(worst, encode_units(man) - encode_units(meta))
        # Was `assert worst == 20` -- a literal sitting beside a derived
        # constant, so a schema change would have failed HERE rather than
        # where the derivation broke.
        assert worst == sg._MANIFEST_METADATA_WORK_RESERVE, (
            f"the wrapper costs {worst} units but the reserve is "
            f"{sg._MANIFEST_METADATA_WORK_RESERVE}: the reserve is no longer "
            "exactly one `_encode` unit per manifest body key")
        assert sg._MANIFEST_METADATA_WORK_RESERVE >= worst, (
            f"the reserve is {sg._MANIFEST_METADATA_WORK_RESERVE} but the "
            f"measured wrapper overhead is {worst} units")

    def test_metadata_at_the_raw_ceiling_is_refused_at_construction(
            self, tmp_path):
        """THE PIN. `meta` is legal at its OWN root -- `non_canonical_reason`
        with no reservation admits it -- but cannot survive being nested
        inside a 20-key manifest body. Before A8 it was ACCEPTED at
        construction, three records were written durably, and `close()` then
        raised `CanonicalError`: three chain-valid records turned into
        uncommitted residue over a value the writer itself certified."""
        limit = cn.CapabilityLimits.MAX_CANONICAL_WORK_UNITS
        # Sits inside the raw ceiling but within 20 units of it, so wrapping
        # is exactly what pushes it over.
        meta = _value_at_encode_units("m", limit - 10)
        assert sg.non_canonical_reason(meta) is None, (
            "the raw (unreserved) construction must still be legal on its "
            "own, or this test is not exercising the reserve")
        # And the manifest wrap really does break it:
        with pytest.raises(cn.CanonicalError, match="aggregate work bound"):
            cn.canonical_bytes(
                {**{k: 1 for k in sg.MANIFEST_FIELDS},
                 "subscription_metadata": meta, "manifest_digest": "x"})

        root = tmp_path / "meta-ceiling"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        with pytest.raises(sg.SegmentError, match="aggregate work bound"):
            sg.SegmentWriter(root, environment=ENV, segment_id="seg-meta",
                             partition_identity="p", commit_to_head=False,
                             subscription_metadata=meta)

    def test_metadata_respecting_the_reserve_still_survives_the_real_wrap(
            self, tmp_path):
        """The other half: the reserve is exactly the overhead commit adds,
        not an over-broad refusal of everything near the ceiling."""
        limit = cn.CapabilityLimits.MAX_CANONICAL_WORK_UNITS
        meta = _value_at_encode_units("m", limit - 40)
        w, root = make_writer(tmp_path, "seg-meta-ok",
                              subscription_metadata=meta)
        for i in range(3):
            assert w.submit(fields(i)) is None
        manifest = w.close()
        assert manifest["close_status"] == "clean"
        assert manifest["record_count"] == 3

    def test_the_manifest_encode_has_its_own_stage_marker(self, tmp_path):
        """`publish_manifest` encoded BEFORE the first `_s(...)` call, so a
        `CanonicalError` from the encode was attributed to stage `'fsync'`
        -- an event-file stage that had already succeeded. An operator was
        sent to look at event-file durability for a serialisation refusal."""
        w, _ = make_writer(tmp_path, "seg-stage")
        assert w.submit(fields(0)) is None
        seen = []
        w.durability_hooks["manifest_encode"] = lambda: seen.append("hit")
        w.close()
        assert seen == ["hit"], (
            "publish_manifest has no `manifest_encode` stage marker, so a "
            "failure in `canonical_bytes(manifest)` is still reported "
            "against whichever stage last succeeded")


# =====================================================================
# 4. BLOCKER 7 -- `_RECORD_ENVELOPE_OVERHEAD_UNITS` is 17, not 6.
# =====================================================================

class TestRecordEnvelopeOverheadUnits:

    def test_the_constant_is_the_schema_derived_seventeen(self):
        assert sg._RECORD_ENVELOPE_OVERHEAD_UNITS == 17
        assert (sg._RECORD_ENVELOPE_OVERHEAD_UNITS
                == len(sg.REQUIRED_RECORD_FIELDS))
        # The three named components add up to it.
        new_scalars = (len(sg.RECORD_FIELDS)
                       - len(sg._ENVELOPE_SOURCED_RECORD_FIELDS))
        assert new_scalars == 6
        assert (new_scalars + 1 + len(sg._ENVELOPE_SOURCED_RECORD_FIELDS)
                == sg._RECORD_ENVELOPE_OVERHEAD_UNITS)

    @pytest.mark.parametrize("omit", [
        (),                                    # nothing omitted: overhead 7
        ("normalized_event",),
        ("raw_event", "normalized_event"),
        ("connection_generation", "subscription_id", "subscription_generation",
         "message_type", "market_ticker"),
    ])
    def test_property_the_reserve_bounds_the_wrap_for_every_envelope(self, omit):
        """THE PROPERTY, over key-OMITTING envelopes. `build_record` reads
        the ten envelope-sourced keys with `.get()`, so an absent key costs
        0 units at admission and 1 (a `None` node) at commit. The comment
        above the constant states the relation as a GUARANTEE; this is the
        guarantee, asserted."""
        env = {k: v for k, v in fields(0).items() if k not in omit}
        record = sg.build_record(
            envelope_fields=env, segment_id="seg", environment=ENV,
            previous_record_digest=sg.genesis_digest(segment_id="seg",
                                                     environment=ENV),
            receive_ordinal=0)
        overhead = encode_units(record) - encode_units(env)
        assert overhead <= sg._RECORD_ENVELOPE_OVERHEAD_UNITS, (
            f"omitting {omit} makes `build_record`'s wrap cost {overhead} "
            f"units, more than the {sg._RECORD_ENVELOPE_OVERHEAD_UNITS} "
            "admission reserves -- a value admitted at the ceiling would be "
            "destroyed at commit")

    def test_the_empty_envelope_is_the_measured_worst_case_of_seventeen(self):
        record = sg.build_record(
            envelope_fields={}, segment_id="seg", environment=ENV,
            previous_record_digest=sg.genesis_digest(segment_id="seg",
                                                     environment=ENV),
            receive_ordinal=0)
        assert encode_units(record) - encode_units({}) == 17

    def test_admission_refuses_a_value_the_old_constant_would_have_admitted(
            self, tmp_path):
        """THE DISCRIMINATING PIN, and the one the old "budget-consistency"
        test could not be: that test passed with the constant set to 0,
        because it only asserted that a refusal happened somewhere, never
        WHERE. This asserts the ADMISSION side directly.

        `env` is tuned so that:
          * it is legal unreserved (`non_canonical_reason(env) is None`);
          * it would be ADMITTED with a 6-unit reserve;
          * `build_record` cannot encode it -- so admitting it destroys the
            segment at commit;
          * the real writer refuses it at admission.
        """
        limit = cn.CapabilityLimits.MAX_CANONICAL_WORK_UNITS
        # One top-level mapping key and nine ABSENT envelope-sourced keys:
        # commit overhead is 6 + 1 + 9 = 16 units, well past the retired 6.
        env = _value_at_encode_units("raw_event", limit - 10)

        assert sg.non_canonical_reason(env) is None, (
            "the unreserved construction must be legal on its own")
        _, bad6 = sg.canonicalize_or_reason(env, _work_reserve=6)
        assert bad6 is None, (
            "this envelope must be ADMITTED under the retired 6-unit "
            "reserve, or it does not discriminate 6 from 17")
        with pytest.raises(cn.CanonicalError, match="aggregate work bound"):
            sg.build_record(
                envelope_fields=env, segment_id="seg", environment=ENV,
                previous_record_digest=sg.genesis_digest(segment_id="seg",
                                                         environment=ENV),
                receive_ordinal=0)

        _, bad = sg.canonicalize_or_reason(
            env, _work_reserve=sg._RECORD_ENVELOPE_OVERHEAD_UNITS)
        assert bad is not None and "aggregate work bound" in bad, (
            "admission ACCEPTED a value whose wrapped record cannot encode; "
            "the reserve is too small")

        w, _ = make_writer(tmp_path, "seg-overhead")
        assert w.submit(env) is sg.RejectReason.NOT_CANONICAL
        assert w.state is sg.SegmentState.OPEN
        manifest = w.close()
        assert manifest["close_status"] == "clean"
        assert manifest["record_count"] == 0


# =====================================================================
# 5. ITEM 8 -- `submit()` returning None does NOT mean durable.
# =====================================================================

_SIGKILL_SCRIPT = r'''
import os, signal, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, {repo!r})
from app.realtime import archive_head as ah, canonical as cn, segment as sg
UTC = timezone.utc
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
root, n = sys.argv[1], int(sys.argv[2])
ah.initialize_archive(root, "demo", archive_identity="kalshi-realtime")
w = sg.SegmentWriter(root, environment="demo", segment_id="seg",
                     partition_identity="p", commit_to_head=False)
acked = 0
for i in range(n):
    r = w.submit({{
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": "KXA", "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1000000 + i,
        "raw_event": {{"price_dollars": "0.5100", "side": "no"}},
        "normalized_event": {{"raw_price_units": 5100}},
    }})
    assert r is None
    acked += 1
open(os.path.join(root, "acked.txt"), "w").write(str(acked))
os.kill(os.getpid(), signal.SIGKILL)   # no atexit, no __del__, no flush
'''


class TestAckedIsNotDurable:
    """`submit()` returning `None` used to be documented as meaning the
    record is DURABLY part of the segment. It is not: `flush_every` defaults
    to 256 and `EventArchive` never sets it, so between flush boundaries an
    acknowledged record lives in `GzipFile`'s buffer and the page cache."""

    @pytest.mark.parametrize("n,expected", [(100, 0), (600, 512)])
    def test_sigkill_loses_everything_since_the_last_flush(
            self, tmp_path, n, expected):
        root = tmp_path / f"kill-{n}"
        root.mkdir()
        script = tmp_path / f"kill-{n}.py"
        script.write_text(_SIGKILL_SCRIPT.format(repo=str(REPO_ROOT)))
        proc = subprocess.run([sys.executable, str(script), str(root), str(n)],
                              capture_output=True, text=True, timeout=180)
        assert proc.returncode == -9, (
            f"the child did not die by SIGKILL: rc={proc.returncode} "
            f"stderr={proc.stderr[-800:]}")
        acked = int((root / "acked.txt").read_text())
        assert acked == n
        events = root / "env=demo" / "segment=seg" / "events.jsonl.gz"
        recoverable = len(sg.read_segment_records(events))
        assert recoverable == expected, (
            f"{acked} records were acknowledged and {recoverable} survived "
            f"SIGKILL (expected {expected} = the last multiple of "
            f"flush_every=256). If this number changed, the durability "
            "boundary changed and `submit()`'s docstring has to change with "
            "it")
        assert recoverable < acked or n % 256 == 0

    def test_the_docstring_no_longer_claims_acked_means_durable(self):
        """The claim itself is the defect: a caller reading "DURABLY part of
        this segment's gzip stream" will not build its own replay
        protection."""
        doc = " ".join((sg.SegmentWriter.submit.__doc__ or "").split())
        assert "NOT that they have reached the platter" in doc, doc
        assert "THE NEXT FLUSH OR `close()`" in doc, doc
        assert "DURABLY part of this segment's gzip stream" not in doc, (
            "the docstring still promises durability on acceptance; measured "
            "against a real writer, 100 acked records survive SIGKILL 0 times")


# =====================================================================
# 6. ITEM 9 -- rotation must not stall the producer.
# =====================================================================

class TestRotationDoesNotStallTheProducer:

    def test_the_default_record_bound_was_re_derived_from_commit_to_head(self):
        """`DEFAULT_MAX_SEGMENT_RECORDS` was derived from a benchmark run
        with `commit_to_head=False`, but `SegmentWriter` defaults it True
        and `EventArchive._writer_for` never overrides it -- so the bound
        was set from a cheaper operation than the one that ships."""
        assert sg.DEFAULT_MAX_SEGMENT_RECORDS == 13_000
        assert (REPO_ROOT / "tests" / "benchmarks"
                / "segment_close_cost.py").is_file(), (
            "the benchmark the bound is derived from must be a committed, "
            "runnable script, not an ad hoc measurement in a comment")

    def test_append_does_not_wait_for_the_retiring_segment_to_close(
            self, tmp_path, monkeypatch):
        """THE PIN. `_writer_for` used to run `retiring.close()` inline, on
        whichever thread called `append()` -- for the Kalshi collector, the
        websocket reader. Measured at the old 20,000-record bound with
        `commit_to_head=True`: 2.9 s CPU / 3.4 s wall idle, 14.8 s wall
        under contention. A 3-15 s stall there is a ping timeout, a
        server-side disconnect, and a hole in the tape.

        Here the close is made to take 2 s. The `append()` that triggers
        rotation must still return promptly.
        """
        from app.realtime.book import EventEnvelope

        ah.initialize_archive(tmp_path, ENV, archive_identity="kalshi-realtime")
        store = ar.EventArchive(tmp_path, environment=ENV, venue="kalshi",
                                max_segment_records=5)
        real_close = sg.SegmentWriter.close

        def slow_close(self):
            time.sleep(2.0)
            return real_close(self)

        monkeypatch.setattr(sg.SegmentWriter, "close", slow_close)
        stamp = cn.canonical_datetime(NOW)

        def env(i):
            return EventEnvelope(
                schema_version=1, venue="kalshi", environment=ENV,
                channel="orderbook_delta", event_type="orderbook_delta",
                market_ticker="KXA", market_id="KXA", sid=4, seq=i,
                venue_time=stamp, collector_receive_time=stamp,
                normalization_time=stamp, receive_monotonic_ns=1_000 + i,
                normalize_monotonic_ns=1_100 + i, data_age_us=100,
                implementation_version="test",
                raw={"p": "0.51"}, normalized={"u": 5100})

        worst = 0.0
        for i in range(12):
            t0 = time.monotonic()
            store.append(env(i))
            worst = max(worst, time.monotonic() - t0)
        assert worst < 1.0, (
            f"the slowest append() took {worst:.2f}s against a 2.0s close -- "
            "rotation is still being committed on the producer's thread")
        # ...and the work is genuinely done, not dropped.
        assert store.wait_for_rotations(120.0), store.rotation_failures
        assert store.rotations >= 1
        assert not store.rotation_failures, store.rotation_failures
        monkeypatch.undo()
        store.close()
        assert sg.verify_archive(tmp_path, environment=ENV)["verdict"] == "VALID"

    def test_close_still_waits_for_every_in_flight_rotation(self, tmp_path):
        """Asynchronous rotation must not turn `close()` into a race:
        `close()` is the commit point, and a segment the closer had not
        reached yet must still be committed by the time it returns."""
        from app.realtime.book import EventEnvelope

        ah.initialize_archive(tmp_path, ENV, archive_identity="kalshi-realtime")
        store = ar.EventArchive(tmp_path, environment=ENV, venue="kalshi",
                                max_segment_records=5)
        stamp = cn.canonical_datetime(NOW)
        for i in range(23):
            store.append(EventEnvelope(
                schema_version=1, venue="kalshi", environment=ENV,
                channel="orderbook_delta", event_type="orderbook_delta",
                market_ticker="KXA", market_id="KXA", sid=4, seq=i,
                venue_time=stamp, collector_receive_time=stamp,
                normalization_time=stamp, receive_monotonic_ns=1_000 + i,
                normalize_monotonic_ns=1_100 + i, data_age_us=100,
                implementation_version="test",
                raw={"p": "0.51"}, normalized={"u": 5100}))
        store.close()
        assert store._closer.outstanding == 0
        assert not store.rotation_failures, store.rotation_failures
        out = sg.verify_archive(tmp_path, environment=ENV)
        assert out["verdict"] == "VALID", out["reasons"]
        assert out["records_read"] == 23


# =====================================================================
# 7. ITEM 12 -- the multi-member `consumed` arithmetic.
#
# `TestLegacyMultiMember` cannot catch either fix: its file is 2,761 bytes,
# BELOW `_DECODE_INPUT_CHUNK` (65,536), so `pos == len(data)` and the buggy
# and fixed formulas are numerically IDENTICAL (300/300 records recovered
# either way). These use a file large enough for the two to diverge.
# =====================================================================

def _member(payload: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(payload)
    return buf.getvalue()


class TestMultiMemberConsumedArithmetic:

    def test_decompress_prefix_reports_the_first_member_length_not_the_file(
            self):
        """`consumed = pos - len(dec.unused_data)`, not
        `len(data) - len(dec.unused_data)`. `pos` is what this call actually
        FED the decompressor; bytes beyond it were never offered and are not
        reflected in `unused_data`. The reviewer's discriminating shape: a
        tiny first member, a >64 KiB incompressible second member, a tiny
        third."""
        first = _member(b"FIRST\n")
        middle = _member(os.urandom(90000))
        last = _member(b"LAST\n")
        data = first + middle + last
        assert len(data) > sg._DECODE_INPUT_CHUNK, len(data)

        decoded, consumed, eof, capped, errored = sg._decompress_prefix(data)
        assert decoded == b"FIRST\n"
        assert eof is True and capped is False and errored is False
        assert consumed == len(first), (
            f"consumed={consumed} but the first member is {len(first)} "
            f"bytes (the whole file is {len(data)}). With the reverted "
            "`len(data) - len(unused_data)` formula this lands in the "
            "MIDDLE of member 2 and every later member is skipped or "
            "garbled")
        # And the caller's own advance lands exactly on member 2's header.
        assert data[consumed:consumed + 2] == b"\x1f\x8b"

    def test_all_three_members_are_recovered_end_to_end(self, tmp_path):
        """The same arithmetic through the real reader. Every member holds
        VALID canonical records so nothing else can end the readable
        prefix, and the middle member is padded past
        `_DECODE_INPUT_CHUNK` so the buggy and fixed formulas diverge."""
        prev = sg.genesis_digest(segment_id="seg", environment=ENV)
        ordinal = 0
        members = []
        # member 1: one small record.
        # member 2: records carrying large high-entropy strings, so the
        #           COMPRESSED member comfortably exceeds 64 KiB.
        # member 3: one small record.
        for count, pad in ((1, 0), (12, 8000), (1, 0)):
            lines = []
            for _ in range(count):
                extra = ({"blob": os.urandom(pad).hex()} if pad
                         else {"price_dollars": "0.5100", "side": "no"})
                rec = sg.build_record(
                    envelope_fields=fields(ordinal, raw_event=extra),
                    segment_id="seg", environment=ENV,
                    previous_record_digest=prev, receive_ordinal=ordinal)
                prev = rec["record_digest"]
                ordinal += 1
                lines.append(cn.canonical_bytes(rec) + b"\n")
            members.append(_member(b"".join(lines)))
        assert len(members[1]) > sg._DECODE_INPUT_CHUNK, len(members[1])

        seg_dir = tmp_path / f"env={ENV}" / "segment=seg"
        seg_dir.mkdir(parents=True)
        (seg_dir / "events.jsonl.gz").write_bytes(b"".join(members))

        records = sg.read_segment_records(seg_dir / "events.jsonl.gz")
        assert len(records) == 14, (
            f"recovered {len(records)} of 14 records across three gzip "
            "members; the multi-member `consumed` arithmetic is wrong")
        assert sg.verify_chain(records, segment_id="seg",
                               environment=ENV).ok

    def test_salvage_prefix_reports_the_fed_length_not_the_file(self):
        """The mirror fix in `_salvage_prefix` (`fed`, not `len(data)`).

        Pinned by calling `_salvage_prefix` DIRECTLY, and deliberately so:
        it is only reachable from `_decompress_prefix`'s fault handler, and
        the combination "a fault occurred AND a fresh 512-byte-chunked
        decompressobj then reaches `dec.eof`" has no known trigger through
        the public reader. A pin that cannot be reached through the front
        door is still worth more than none: it fixes the arithmetic in
        place so a future change that DOES reach it cannot inherit the
        bug."""
        first = _member(b"FIRST\n")
        data = first + _member(os.urandom(90000))
        decoded, consumed, eof, capped, errored = sg._salvage_prefix(data)
        assert decoded == b"FIRST\n"
        assert eof is True and errored is True
        assert consumed <= len(first) + sg._SALVAGE_CHUNK, (
            f"consumed={consumed} for a first member of {len(first)} bytes "
            f"in a {len(data)}-byte file -- with the reverted "
            "`len(data) - len(unused_data)` formula this overshoots the "
            "member boundary by most of the file")
        assert data[consumed:consumed + 2] == b"\x1f\x8b"
