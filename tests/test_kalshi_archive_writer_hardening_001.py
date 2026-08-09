"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 Gates D-G.

Controlled backpressure, the shutdown-race matrix, writer-failure injection at
every declared durability stage, and exclusive ownership across threads and
processes. No network, no SQLite, no credential.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.realtime import canonical as cn
from app.realtime import segment as sg


def _init_archive(root, environment="demo"):
    """Archives are brought into existence EXPLICITLY, exactly as an operator does.

    The collector cannot do this, and that is the point: "the head is missing,
    therefore this is a new archive" was the inference that let a rebuilt
    history certify its own deletions. Tests initialize on purpose.
    """
    from app.realtime import archive_head as _ah
    try:
        _ah.initialize_archive(Path(root), environment,
                               archive_identity="kalshi-realtime")
    except _ah.ArchiveHeadError:
        pass                       # already initialized in this test
    return root


def _arch(root, **kw):
    from app.realtime import archive as _ar
    _init_archive(root, kw.get("environment", "demo"))
    return _ar.EventArchive(root, **kw)


UTC = timezone.utc
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
ENV = "demo"


def fields(i):
    return {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": "KXA", "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {"price_dollars": "0.5100", "delta_fp": "1.00",
                      "side": "no", "ts_ms": 1786150148065 + i},
        "normalized_event": {"raw_price_units": 5100},
    }


def writer(tmp_path, **kw):
    _init_archive(tmp_path)
    kw.setdefault("segment_id", "seg-hard")
    kw.setdefault("partition_identity", "p")
    return sg.SegmentWriter(tmp_path, environment=ENV, **kw)


ALL_REJECTIONS = {r.value for r in sg.RejectReason}


def assert_reconciles(w, *, generated):
    """Every event reaches exactly one terminal STAGE.

    Two identities, not one. `attempted == written + rejected` could not tell a
    producer cancelled before its event was taken from an accepted event that
    was then lost, and only the second matters.
    """
    acc = w.accounting
    assert acc.attempted == generated
    assert acc.admission_holds(), acc.to_dict()
    assert acc.disposition_holds(), acc.to_dict()
    assert acc.attempted == (acc.rejected_before_accept + acc.written
                             + acc.failed_after_accept + acc.pending)
    assert set(acc.rejections) <= ALL_REJECTIONS, acc.rejections
    assert all(v > 0 for v in acc.rejections.values())


def owns(w) -> bool:
    """Is this writer's flock still held?

    The lock FILE persists by design — unlinking it on release deleted a live
    successor's lock and admitted two owners to one segment. Ownership is the
    flock, so that is what a test must ask about.
    """
    import fcntl
    if not w._lock_path.exists():
        return False
    fd = os.open(w._lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    fcntl.flock(fd, fcntl.LOCK_UN)
    return False


# --- Gate D: deterministic backpressure --------------------------------------------
class TestBackpressure:
    def test_saturation_produces_typed_rejections_and_no_silent_loss(self, tmp_path):
        """Tiny queue, deliberately slow writer, several producers.

        The point is not that nothing is rejected — under real backpressure
        things must be — but that every rejection is typed and counted, and
        nothing vanishes.
        """
        w = writer(tmp_path, queue_maxsize=4, enqueue_timeout_s=0.02)
        w.pre_write_hook = lambda _w: time.sleep(0.002)
        per, producers = 150, 4
        errors, results = [], []
        lock = threading.Lock()

        def produce(pid):
            try:
                for i in range(per):
                    r = w.submit(fields(pid * 10_000 + i))
                    with lock:
                        results.append(r)
            except BaseException as exc:            # nothing may be swallowed
                errors.append(exc)

        threads = [threading.Thread(target=produce, args=(p,))
                   for p in range(producers)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        elapsed = time.monotonic() - t0
        assert not any(t.is_alive() for t in threads), "a producer hung"
        assert not errors, errors
        w.close()

        generated = per * producers
        assert_reconciles(w, generated=generated)
        assert len(results) == generated, "a submission returned no verdict"
        rejected = [r for r in results if r is not None]
        assert len(rejected) == w.accounting.rejected_before_accept
        # The queue is bounded, and provably so.
        assert w.queue_high_water <= 4, w.queue_high_water
        assert elapsed < 60
        v = sg.verify_segment(w.dir, environment=ENV)
        assert v.valid and v.records_read == w.accounting.written

    def test_backpressure_relieves_and_later_submissions_succeed(self, tmp_path):
        """Saturate, then let the writer catch up: the archive must keep working
        rather than staying wedged."""
        # Block the writer on an Event rather than racing it with a sleep. The
        # sleep version let the writer drain both queue slots inside one
        # enqueue timeout, so whether backpressure occurred at all was a coin
        # flip — it failed 5 runs in 15 standalone. A test that only sometimes
        # exercises the condition it names is not evidence either way.
        w = writer(tmp_path, queue_maxsize=2, enqueue_timeout_s=0.01)
        gate = threading.Event()
        w.pre_write_hook = lambda _w: gate.wait(5)
        first = [w.submit(fields(i)) for i in range(120)]
        assert any(r is not None for r in first), "no backpressure was induced"
        gate.set()
        deadline = time.monotonic() + 5
        while w._queue.qsize() and time.monotonic() < deadline:
            time.sleep(0.005)                             # writer catches up
        later = [w.submit(fields(1000 + i)) for i in range(20)]
        assert any(r is None for r in later), "writer never recovered"
        w.close()
        assert_reconciles(w, generated=140)
        v = sg.verify_segment(w.dir, environment=ENV)
        assert v.valid
        assert w.read_manifest()["record_count"] == w.accounting.written

    def test_the_queue_is_bounded_not_merely_large(self, tmp_path):
        w = writer(tmp_path, queue_maxsize=3, enqueue_timeout_s=0.005)
        w.pre_write_hook = lambda _w: time.sleep(0.01)
        for i in range(200):
            w.submit(fields(i))
        w.close()
        assert w.queue_high_water <= 3
        assert_reconciles(w, generated=200)


# --- Gate E: shutdown race ---------------------------------------------------------
class TestShutdownRace:
    def test_close_drains_accepted_events_and_refuses_new_ones(self, tmp_path):
        w = writer(tmp_path, queue_maxsize=512)
        w.pre_write_hook = lambda _w: time.sleep(0.0005)
        stop = threading.Event()
        accepted = []
        lock = threading.Lock()

        def produce(pid):
            i = 0
            while not stop.is_set() and i < 400:
                r = w.submit(fields(pid * 10_000 + i))
                with lock:
                    accepted.append(r)
                i += 1

        threads = [threading.Thread(target=produce, args=(p,)) for p in (0, 1)]
        for t in threads:
            t.start()
        time.sleep(0.05)
        stop.set()
        for t in threads:
            t.join(timeout=30)
        manifest = w.close()

        # A late submitter, after close began.
        assert w.submit(fields(99_999)) is sg.RejectReason.SHUTDOWN_IN_PROGRESS
        assert w.state is sg.SegmentState.CLOSED
        assert manifest["record_count"] == w.accounting.written
        v = sg.verify_segment(w.dir, environment=ENV)
        assert v.valid and v.chain_valid and v.stream_digest_match and v.file_digest_match
        assert w.accounting.reconciles(), w.accounting.to_dict()
        assert len(accepted) == w.accounting.attempted - 1  # minus the late one

    def test_close_immediately_after_construction(self, tmp_path):
        w = writer(tmp_path)
        m = w.close()
        assert w.state is sg.SegmentState.CLOSED and m["record_count"] == 0
        # A legitimately empty segment is valid ONLY because its manifest says so.
        assert sg.verify_segment(w.dir, environment=ENV).valid

    def test_double_close_is_idempotent_and_publishes_once(self, tmp_path):
        w = writer(tmp_path)
        w.submit(fields(1))
        first = w.close()
        mtime = w.manifest_path.stat().st_mtime_ns
        second = w.close()
        assert second["manifest_digest"] == first["manifest_digest"]
        assert w.manifest_path.stat().st_mtime_ns == mtime, "manifest republished"

    def test_concurrent_close_callers_publish_once(self, tmp_path):
        w = writer(tmp_path)
        for i in range(20):
            w.submit(fields(i))
        results, errors = [], []

        def do_close():
            try:
                results.append(w.close())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_close) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, errors
        digests = {r["manifest_digest"] for r in results}
        assert len(digests) == 1, "concurrent closes disagreed"
        assert sg.verify_segment(w.dir, environment=ENV).valid

    def test_submit_after_closed_is_refused(self, tmp_path):
        w = writer(tmp_path)
        w.submit(fields(1))
        w.close()
        before = w.accounting.written
        assert w.submit(fields(2)) is not None
        assert w.accounting.written == before, "a record was written after CLOSED"
        assert w.accounting.reconciles()

    def test_no_accepted_but_unknown_state_exists(self, tmp_path):
        """The accounting model must have exactly three terminal states."""
        w = writer(tmp_path, queue_maxsize=8, enqueue_timeout_s=0.01)
        w.pre_write_hook = lambda _w: time.sleep(0.001)
        verdicts = [w.submit(fields(i)) for i in range(300)]
        w.close()
        never_accepted = sum(1 for v in verdicts if v is not None)
        assert never_accepted == w.accounting.rejected_before_accept
        assert w.accounting.written == len(sg.read_segment_records(w.events_path))
        assert w.accounting.written + never_accepted == 300


# --- Gate F: writer-failure injection ----------------------------------------------
class TestWriterFailure:
    @pytest.mark.parametrize("stage", [
        "flush", "fsync", "manifest_temp_create", "manifest_write",
        "manifest_fsync", "manifest_rename",
    ])
    def test_failure_before_commit_never_yields_a_closed_segment(self, stage, tmp_path):
        w = writer(tmp_path)
        for i in range(5):
            w.submit(fields(i))
        w.durability_hooks[stage] = lambda: (_ for _ in ()).throw(OSError("injected"))
        with pytest.raises(sg.SegmentError):
            w.close()
        assert w.state is sg.SegmentState.INVALID
        assert not w.healthy and not w.accepting
        assert w.submit(fields(99)) in (sg.RejectReason.WRITER_FAILED,
                                        sg.RejectReason.SHUTDOWN_IN_PROGRESS,
                                        sg.RejectReason.SEGMENT_INVALID)
        verdict = sg.verify_segment(w.dir, environment=ENV, allow_open=True)
        assert verdict.state is not sg.SegmentState.CLOSED
        assert not verdict.valid
        assert w.accounting.reconciles()

    def test_directory_fsync_failure_is_distinguished_from_pre_rename_failure(
            self, tmp_path):
        """After the rename the manifest is VISIBLE. A directory-fsync failure
        may not survive a power loss, but a reader today sees a committed
        segment — materially different from failing before the rename, and not
        collapsed into one generic error."""
        w = writer(tmp_path)
        for i in range(3):
            w.submit(fields(i))
        from app.realtime import archive_head as ah

        w.durability_hooks["directory_fsync"] = lambda: (
            _ for _ in ()).throw(OSError("injected"))
        # A DISTINCT TYPE, not a generic message. This test previously injected
        # at a hook that fired OUTSIDE the protected region, so it asserted the
        # generic "manifest publication failed" and proved nothing about the
        # distinction its own name promises — the new type had zero coverage.
        with pytest.raises(ah.DurabilityNotProven) as caught:
            w.close()
        assert "IS visible to readers" in str(caught.value)
        assert "durability is not proven" in str(caught.value)
        # The rename already happened, so the manifest exists and verifies...
        assert w.manifest_path.exists()
        assert sg.verify_segment(w.dir, environment=ENV).valid
        # ...but the writer does NOT claim CLOSED, because its own durability
        # contract was not satisfied.
        assert w.state is sg.SegmentState.INVALID
        # And a PRE-rename failure at the same errno is a different type.
        w2 = writer(tmp_path, segment_id="seg-pre-rename")
        w2.submit(fields(1))
        w2.durability_hooks["manifest_fsync"] = lambda: (
            _ for _ in ()).throw(OSError("injected"))
        with pytest.raises(sg.SegmentError) as pre:
            w2.close()
        assert not isinstance(pre.value, ah.DurabilityNotProven)
        assert not w2.manifest_path.exists()

    def test_a_float_is_refused_BEFORE_acceptance(self, tmp_path):
        """The float contract is uniform: refused at the door, not in the writer.

        It used to be discovered inside the writer, after the producer had been
        told the event was recorded, leaving only two outcomes — a silent drop
        or destroying the whole hour. Coercing floats at ingress instead made it
        worse: `canonical_decimal` quantizes any positive exponent, so an
        ordinary 1e30 raised `decimal.InvalidOperation`, an ArithmeticError that
        `_write_one` did not catch, and it killed the writer thread.
        """
        w = writer(tmp_path)
        for value in (1.5, 1.0, 1e30, 1e28, 5e-324, -0.0, float("inf"),
                      float("-inf"), float("nan")):
            bad = fields(1)
            bad["raw_event"] = {"x": value}
            assert w.submit(bad) is sg.RejectReason.NOT_CANONICAL, value
            assert "float" in (w.last_rejection_detail or "")
        for i in range(3):
            assert w.submit(fields(i + 10)) is None      # writer is untouched
        manifest = w.close()
        assert manifest["record_count"] == 3
        assert w.accounting.failed_after_accept == 0
        assert w.accounting.rejected_before_accept == 9
        assert w.accounting.clean()
        assert sg.verify_segment(w.dir, environment=ENV).valid

    def test_1e30_does_not_kill_the_writer_thread(self, tmp_path):
        """The exact reviewer finding: |f| >= ~1e28 raised decimal.InvalidOperation.

        That is an ArithmeticError, not a CanonicalError, so it escaped
        `_write_one`, set `_writer_error`, refused every later event for the
        hour, and left `read_verified()` refusing the entire archive.
        """
        w = writer(tmp_path)
        assert w.submit({**fields(1), "raw_event": {"notional": 1e30}}) \
            is sg.RejectReason.NOT_CANONICAL
        assert w._writer_error is None, "the writer thread must be untouched"
        assert w.healthy and w.accepting
        assert w.submit(fields(2)) is None
        w.close()
        assert sg.verify_archive(tmp_path, environment=ENV)["verdict"] == "VALID"

    def test_the_venue_boundary_parses_numerics_as_Decimal(self, tmp_path):
        """The transport is where Decimal is produced, not the writer.

        `json.loads(..., parse_float=Decimal)` costs nothing at the boundary
        that already parses the JSON, and it makes the archive's refusal a
        contract rather than a hazard.
        """
        import json
        from decimal import Decimal

        frame = json.loads('{"price": 0.51, "depth": [{"p": 1.25}], "n": 1e30}',
                           parse_float=Decimal)
        w = writer(tmp_path)
        assert sg.non_canonical_reason(frame) is None
        assert w.submit({**fields(1), "raw_event": frame}) is None
        manifest = w.close()
        assert manifest["record_count"] == 1
        assert w.accounting.clean()
        rec = sg.read_segment_records(w.events_path)[0]
        assert rec["raw_event"]["price"] == "0.51"      # canonical decimal TEXT
        assert sg.verify_archive(tmp_path, environment=ENV)["verdict"] == "VALID"


class TestOwnershipCleanup:
    def test_normal_close_releases_runtime_ownership(self, tmp_path):
        a = writer(tmp_path)
        assert owns(a)
        a.close()
        assert not owns(a)

    def test_writer_failure_releases_ownership_safely(self, tmp_path):
        a = writer(tmp_path)
        a.submit(fields(1))
        a.durability_hooks["flush"] = lambda: (_ for _ in ()).throw(OSError("x"))
        with pytest.raises(sg.SegmentError):
            a.close()
        assert not owns(a), "a failed writer must not hold ownership forever"

    def test_releasing_ownership_does_not_certify_evidence(self, tmp_path):
        """Ownership release and canonical validity are different properties."""
        a = writer(tmp_path)
        a.submit(fields(1))
        a.durability_hooks["manifest_rename"] = lambda: (
            _ for _ in ()).throw(OSError("x"))
        with pytest.raises(sg.SegmentError):
            a.close()
        assert not owns(a)                               # ownership released
        v = sg.verify_segment(a.dir, environment=ENV, allow_open=True)
        assert not v.valid                                # but NOT evidence
        assert v.state is not sg.SegmentState.CLOSED

    def test_process_death_releases_kernel_ownership_without_committing(self, tmp_path):
        """An abandoned segment is recoverable/uncommitted — never CLOSED."""
        code = (
            "import sys,os;sys.path.insert(0,'.');"
            "from app.realtime import segment as sg;"
            f"w=sg.SegmentWriter({str(tmp_path)!r}, environment='demo',"
            " segment_id='seg-hard', partition_identity='p');"
            "os._exit(1)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, cwd=str(Path(sg.__file__).parents[2]))
        assert r.returncode == 1
        seg_dir = tmp_path / f"env={ENV}" / "segment=seg-hard"
        v = sg.verify_segment(seg_dir, environment=ENV, allow_open=True)
        assert v.state is not sg.SegmentState.CLOSED
        assert not v.valid, "an abandoned segment must never verify"


class TestReview3Regressions:
    """Findings from the crash/filesystem adversarial review."""

    def _patched_write(self, fn):
        import app.realtime.segment as segmod

        orig = segmod.os.write

        class _Shim:
            def __getattr__(inner, name):
                return getattr(segmod.os, name)
            write = staticmethod(fn)
        return orig, _Shim

    def test_repeated_short_writes_converge_and_commit_completely(
            self, tmp_path, monkeypatch):
        """os.write may legally write fewer bytes than asked — partial-ENOSPC,
        NFS and EINTR all do. The old code discarded the return value, so a
        TRUNCATED manifest was fsynced, renamed and committed and close()
        returned CLOSED over unreadable evidence.

        The contract is convergence, not refusal: repeated partial writes must
        loop until every byte lands, and the segment must commit COMPLETELY.
        """
        import app.realtime.segment as segmod

        w = writer(tmp_path)
        for i in range(5):
            w.submit(fields(i))
        real = segmod.os.write
        calls = {"n": 0}

        def dribble(fd, data):
            calls["n"] += 1
            return real(fd, data[: max(1, len(data) // 3)])

        monkeypatch.setattr(segmod.os, "write", dribble)
        manifest = w.close()
        monkeypatch.undo()
        assert calls["n"] > 1, "the loop never had to retry"
        assert w.state is sg.SegmentState.CLOSED
        v = sg.verify_segment(w.dir, environment=ENV)
        assert v.valid, v.reasons
        assert manifest["record_count"] == 5
        # And the committed manifest is complete, not truncated.
        assert sg.verify_manifest_self_digest(
            cn.parse_canonical(w.manifest_path.read_bytes()))

    def test_zero_byte_progress_is_refused_rather_than_looping_forever(
            self, tmp_path, monkeypatch):
        """A write that reports no progress is a terminal condition, not a
        reason to spin."""
        import app.realtime.segment as segmod

        w = writer(tmp_path)
        w.submit(fields(1))
        real = segmod.os.write
        monkeypatch.setattr(segmod.os, "write", lambda fd, data: 0)
        with pytest.raises(sg.SegmentError):
            w.close()
        monkeypatch.undo()
        assert w.state is not sg.SegmentState.CLOSED
        assert not sg.verify_segment(w.dir, environment=ENV, allow_open=True).valid

    def test_a_partial_write_then_enospc_never_commits(self, tmp_path, monkeypatch):
        import app.realtime.segment as segmod

        w = writer(tmp_path)
        w.submit(fields(1))
        real = segmod.os.write
        state = {"first": True}

        def partial_then_enospc(fd, data):
            if state["first"]:
                state["first"] = False
                return real(fd, data[: len(data) // 2])
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(segmod.os, "write", partial_then_enospc)
        with pytest.raises(sg.SegmentError):
            w.close()
        monkeypatch.undo()
        assert w.state is not sg.SegmentState.CLOSED
        assert not sg.verify_segment(w.dir, environment=ENV, allow_open=True).valid

    def test_a_symlinked_manifest_temp_cannot_truncate_another_file(self, tmp_path):
        victim = tmp_path / "VICTIM.txt"
        victim.write_text("important operator log\n")
        w = writer(tmp_path)
        w.submit(fields(1))
        (w.dir / (sg.MANIFEST_FILENAME + sg.MANIFEST_TEMP_SUFFIX)).symlink_to(victim)
        w.close()
        assert victim.read_text() == "important operator log\n", "victim truncated"
        assert not w.manifest_path.is_symlink()

    def test_a_symlinked_event_file_is_refused(self, tmp_path):
        victim = tmp_path / "VICTIM2.txt"
        victim.write_text("do not append here\n")
        seg = tmp_path / f"env={ENV}" / "segment=seg-link"
        seg.mkdir(parents=True)
        (seg / sg.EVENTS_FILENAME).symlink_to(victim)
        with pytest.raises(sg.SegmentError, match="symlink"):
            writer(tmp_path, segment_id="seg-link")
        assert victim.read_text() == "do not append here\n"

    def test_ownership_is_released_on_every_failure_path(self, tmp_path):
        w = writer(tmp_path)
        w.submit(fields(1))
        w.durability_hooks["flush"] = lambda: (_ for _ in ()).throw(OSError("x"))
        with pytest.raises(sg.SegmentError):
            w.close()
        assert not owns(w)
        # And the partition is not locked out for the next process.
        w2 = writer(tmp_path, segment_id="seg-hard-next")
        w2.close()

    def test_the_writer_stops_after_its_first_error(self, tmp_path):
        """Continuing appends more records after a half-written one and
        amplifies the corruption."""
        w = writer(tmp_path)
        calls = {"n": 0}

        def boom(_w):
            calls["n"] += 1
            raise OSError("injected")

        w.pre_write_hook = boom
        for i in range(20):
            w.submit(fields(i))
        time.sleep(0.5)
        assert calls["n"] == 1, f"writer kept going after failing ({calls['n']})"
        assert w.state is sg.SegmentState.INVALID
