"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 — genesis, head generations, recovery.

The acceptance tests for the commit protocol that replaced the append-only head
log. Every reproduction the three reviewers demonstrated against the previous
revision is here VERBATIM, not restated more cleanly: a tidier test proves the
fix agrees with the test's idea of the attack, which is what let two rounds of
"fixed" findings survive.

No network, no SQLite, no credential.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import json
from decimal import Decimal

import pytest

from app.realtime import archive as ar
from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

UTC = timezone.utc
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
ENV = "demo"
REPO = Path(__file__).resolve().parents[1]


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


def init(root, environment=ENV, **kw):
    return ah.initialize_archive(root, environment,
                                 archive_identity="kalshi-realtime", **kw)


def build(root, names, per=25, environment=ENV):
    made = []
    for n in names:
        w = sg.SegmentWriter(root, environment=environment,
                             segment_id=f"kalshi.seg-{n}",
                             partition_identity=f"venue=kalshi/date=2026-08-08/hour={n}",
                             subscription_metadata={"venue": "kalshi"})
        for i in range(per):
            assert w.submit(fields(i)) is None
        made.append(w.close())
    return made


def seg_dir(root, name, environment=ENV):
    return root / f"env={environment}" / f"segment=kalshi.seg-{name}"


def verdict(root, **kw):
    return sg.verify_archive(root, environment=ENV, **kw)


# --- Section 1: genesis is the root of trust --------------------------------------
class TestGenesis:
    def test_a_fresh_root_is_not_an_archive(self, tmp_path):
        """The collector refuses an uninitialized root instead of creating one."""
        with pytest.raises(ah.ArchiveNotInitializedError, match="never initialized"):
            ar.EventArchive(tmp_path, environment=ENV)
        assert verdict(tmp_path)["head_state"] == "NOT_INITIALIZED"

    def test_initialization_creates_genesis_and_generation_zero(self, tmp_path):
        g = init(tmp_path)
        assert ah.genesis_path(tmp_path, ENV).exists()
        assert ah.present_generations(tmp_path, ENV) == [0]
        assert g["genesis_digest"] == ah.genesis_digest_of(g)
        out = verdict(tmp_path)
        assert out["verdict"] == "VALID" and out["head_generation"] == 0
        assert out["archive_id"] == g["archive_id"]

    def test_initializing_twice_is_refused(self, tmp_path):
        init(tmp_path)
        with pytest.raises(ah.ArchiveHeadError, match="already been initialized"):
            init(tmp_path)

    def test_the_collector_has_no_path_to_initialization(self, tmp_path):
        """`EventArchive` must not be able to mint a genesis by any argument.

        API privacy is defence in depth, not the boundary — but a constructor
        that can bootstrap is a boundary with a door in it.
        """
        import inspect
        params = inspect.signature(ar.EventArchive.__init__).parameters
        assert not any("init" in p or "create" in p or "bootstrap" in p
                       for p in params), sorted(params)
        src = inspect.getsource(ar.EventArchive)
        assert "initialize_archive" not in src

    def test_a_deleted_genesis_is_never_recreated(self, tmp_path):
        init(tmp_path)
        build(tmp_path, ["A"])
        ah.genesis_path(tmp_path, ENV).unlink()
        with pytest.raises(ah.ArchiveNotInitializedError):
            ar.EventArchive(tmp_path, environment=ENV)
        with pytest.raises(ah.ArchiveNotInitializedError):
            ah.commit_segment(tmp_path, ENV, manifest=cn.parse_canonical(
                (seg_dir(tmp_path, "A") / "manifest.json").read_bytes()))
        assert verdict(tmp_path)["head_state"] == "NOT_INITIALIZED"

    def test_a_tampered_genesis_is_refused(self, tmp_path):
        g = init(tmp_path)
        g["archive_identity"] = "someone-elses-archive"
        ah.genesis_path(tmp_path, ENV).write_bytes(cn.canonical_bytes(g))
        assert verdict(tmp_path)["head_state"] == "GENESIS_INVALID"

    def test_a_configured_archive_id_rejects_a_substituted_archive(self, tmp_path):
        real = init(tmp_path / "real")
        build(tmp_path / "real", ["A", "B"])
        other = init(tmp_path / "other")
        build(tmp_path / "other", ["A"])
        # Build a whole archive elsewhere and move it over the real one.
        shutil.rmtree(tmp_path / "real" / f"env={ENV}")
        shutil.move(str(tmp_path / "other" / f"env={ENV}"),
                    str(tmp_path / "real" / f"env={ENV}"))
        assert other["archive_id"] != real["archive_id"]
        out = sg.verify_archive(tmp_path / "real", environment=ENV,
                                expected_archive_id=real["archive_id"])
        assert out["verdict"] == "INVALID"
        assert out["head_state"] == "GENESIS_INVALID"
        with pytest.raises(ah.ArchiveIdentityMismatch):
            ar.EventArchive(tmp_path / "real", environment=ENV,
                            expected_archive_id=real["archive_id"])


# --- Section 2: Review-1 BLK-1, verbatim ------------------------------------------
class TestBlk1Verbatim:
    """The reproductions that defeated the previous revision, unchanged."""

    def _rebuild_attempt(self, root, survivors):
        errors = []
        for n in survivors:
            m = cn.parse_canonical(
                (seg_dir(root, n) / "manifest.json").read_bytes())
            try:
                ah.commit_segment(root, ENV, manifest=m)
            except ah.ArchiveHeadError as exc:
                errors.append(exc)
                break
        return errors

    def test_the_100_to_75_reproduction_now_fails_verification(self, tmp_path):
        """`rm` a segment, `rm` the head artifacts, replay the commit function.

        Returned VALID with `reasons == []` and `records_expected` silently
        100 -> 75, with `read_verified()` serving the result as canonical.
        """
        init(tmp_path)
        build(tmp_path, ["A", "B", "C", "D"])
        assert verdict(tmp_path)["records_expected"] == 100
        shutil.rmtree(seg_dir(tmp_path, "B"))
        ah.current_head_path(tmp_path, ENV).unlink()
        shutil.rmtree(ah.heads_dir(tmp_path, ENV))
        assert self._rebuild_attempt(tmp_path, ["A", "C", "D"]), "rebuild succeeded"
        out = verdict(tmp_path)
        assert out["verdict"] == "INVALID"
        assert out["head_state"] == "RECOVERY_REQUIRED"
        assert out["records_expected"] != 75

    def test_the_manifest_shuffle_ordering_bypass(self, tmp_path):
        """Rename each peer's manifest aside so the guard sees an empty set.

        This is what defeated the previous guard: it only ran on the FIRST
        commit, so suppressing the condition for one moment was enough.
        """
        init(tmp_path)
        build(tmp_path, ["A", "B", "C", "D"])
        shutil.rmtree(seg_dir(tmp_path, "B"))
        ah.current_head_path(tmp_path, ENV).unlink()
        shutil.rmtree(ah.heads_dir(tmp_path, ENV))
        survivors = ["A", "C", "D"]
        stash = {}
        for n in survivors:
            m = seg_dir(tmp_path, n) / "manifest.json"
            stash[n] = m.read_bytes()
            m.unlink()
        refused = None
        for n in survivors:
            (seg_dir(tmp_path, n) / "manifest.json").write_bytes(stash[n])
            try:
                ah.commit_segment(tmp_path, ENV,
                                  manifest=cn.parse_canonical(stash[n]))
            except ah.ArchiveHeadError as exc:
                refused = exc
                break
        assert refused is not None, "the ordering bypass rebuilt the history"
        assert verdict(tmp_path)["verdict"] == "INVALID"

    def test_a_planted_flock_cannot_influence_the_decision(self, tmp_path):
        """Liveness is not evidence, so it takes no part in an integrity call.

        The previous guard excused peers whose `writer.lock` was flocked, so
        holding those locks let the rebuild proceed.
        """
        init(tmp_path)
        build(tmp_path, ["A", "B", "C"])
        shutil.rmtree(seg_dir(tmp_path, "B"))
        ah.current_head_path(tmp_path, ENV).unlink()
        shutil.rmtree(ah.heads_dir(tmp_path, ENV))
        held = []
        for n in ("A", "C"):
            lock = seg_dir(tmp_path, n) / "writer.lock"
            lock.touch()
            fd = os.open(lock, os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(fd)
        try:
            assert self._rebuild_attempt(tmp_path, ["A", "C"]), \
                "a planted flock influenced an integrity decision"
        finally:
            for fd in held:
                os.close(fd)
        # And no liveness probe appears anywhere in the verification path.
        import inspect
        assert "flock" not in inspect.getsource(sg.verify_archive)

    def test_the_fabricated_segment_substitution(self, tmp_path):
        """`read_verified()` served 80 records of which 5 were fabricated."""
        init(tmp_path)
        build(tmp_path, ["A", "B", "C"])
        shutil.rmtree(seg_dir(tmp_path, "B"))
        forged = sg.SegmentWriter(
            tmp_path, environment=ENV, segment_id="kalshi.seg-B",
            partition_identity="venue=kalshi/date=2026-08-08/hour=B",
            subscription_metadata={"venue": "kalshi"}, commit_to_head=False)
        for i in range(99):
            forged.submit(fields(i))
        forged.close()
        out = verdict(tmp_path)
        assert out["verdict"] == "INVALID"
        store = ar.EventArchive(tmp_path, environment=ENV)
        with pytest.raises(ar.ArchiveError):
            store.read_verified()


# --- Section 3: crash recovery, cases A-D -----------------------------------------
class TestCrashRecovery:
    def test_case_A_segment_committed_head_generation_not(self, tmp_path):
        init(tmp_path)
        build(tmp_path, ["A"])
        w = sg.SegmentWriter(
            tmp_path, environment=ENV, segment_id="kalshi.seg-B",
            partition_identity="venue=kalshi/date=2026-08-08/hour=B",
            subscription_metadata={"venue": "kalshi"})
        w.submit(fields(1))
        w.durability_hooks["head_generation_publish"] = lambda: (
            _ for _ in ()).throw(OSError("crash"))
        with pytest.raises(sg.SegmentError, match="ORPHANED_COMMITTED_SEGMENT"):
            w.close()
        out = verdict(tmp_path)
        assert out["verdict"] == "INVALID"
        assert "kalshi.seg-B" in out["orphaned_committed_segments"]
        # Never silently adopted, and never silently discarded.
        assert ah.load_authoritative_head(tmp_path, ENV).generation == 1

    def test_case_B_generation_committed_pointer_behind(self, tmp_path):
        init(tmp_path)
        build(tmp_path, ["A", "B"])
        ah._publish_current_head(tmp_path, ENV, ah.read_generation(tmp_path, ENV, 1))
        out = verdict(tmp_path)
        assert out["head_state"] == "STALE_HEAD" and out["verdict"] == "INVALID"
        rec = ah.recover_current_head(tmp_path, ENV)
        assert rec["generation"] == 2
        assert verdict(tmp_path)["verdict"] == "VALID"

    def test_case_B_refuses_to_advance_past_absent_evidence(self, tmp_path):
        init(tmp_path)
        build(tmp_path, ["A", "B"])
        ah._publish_current_head(tmp_path, ENV, ah.read_generation(tmp_path, ENV, 1))
        shutil.rmtree(seg_dir(tmp_path, "B"))
        with pytest.raises(ah.ArchiveHeadError, match="evidence is absent"):
            ah.recover_current_head(tmp_path, ENV)

    def test_case_B_refuses_when_two_transitions_are_outstanding(self, tmp_path):
        init(tmp_path)
        build(tmp_path, ["A", "B", "C"])
        ah._publish_current_head(tmp_path, ENV, ah.read_generation(tmp_path, ENV, 1))
        with pytest.raises(ah.ArchiveHeadError, match="needs an operator"):
            ah.recover_current_head(tmp_path, ENV)

    def test_case_C_a_torn_staged_temp_is_not_a_commitment(self, tmp_path):
        init(tmp_path)
        build(tmp_path, ["A"])
        heads = ah.heads_dir(tmp_path, ENV)
        (heads / "0000000000000002.json.9999.deadbeef.tmp").write_bytes(b'{"tor')
        assert verdict(tmp_path)["verdict"] == "VALID"
        assert ah.present_generations(tmp_path, ENV) == [0, 1]

    def test_case_D_missing_pointer_halts_and_never_bootstraps(self, tmp_path):
        init(tmp_path)
        build(tmp_path, ["A", "B"])
        ah.current_head_path(tmp_path, ENV).unlink()
        out = verdict(tmp_path)
        assert out["head_state"] == "RECOVERY_REQUIRED"
        with pytest.raises(ah.HeadRecoveryRequired):
            ah.commit_segment(tmp_path, ENV, manifest=cn.parse_canonical(
                (seg_dir(tmp_path, "A") / "manifest.json").read_bytes()))
        # Recovery is explicit, and re-points at the newest IMMUTABLE record
        # rather than at anything derived from the segments on disk.
        rec = ah.recover_current_head(tmp_path, ENV)
        assert rec["generation"] == 2
        assert verdict(tmp_path)["verdict"] == "VALID"

    def test_a_real_sigkill_between_generation_and_pointer_is_recoverable(self, tmp_path):
        """Real subprocess, real SIGKILL, in the exact window."""
        script = f'''
import os, signal, sys
sys.path.insert(0, {str(REPO)!r})
from app.realtime import archive_head as ah, segment as sg
sys.path.insert(0, {str(REPO / "tests")!r})
from test_kalshi_archive_genesis_001 import fields
root = {str(tmp_path)!r}
w = sg.SegmentWriter(root, environment="demo", segment_id="kalshi.seg-B",
                     partition_identity="venue=kalshi/date=2026-08-08/hour=B",
                     subscription_metadata={{"venue": "kalshi"}})
w.submit(fields(1))
def die():
    os.kill(os.getpid(), signal.SIGKILL)
w.durability_hooks["current_head_publish"] = die
w.close()
'''
        init(tmp_path)
        build(tmp_path, ["A"])
        r = subprocess.run([sys.executable, "-c", script], capture_output=True,
                           text=True, cwd=str(REPO))
        assert r.returncode == -9, (r.returncode, r.stderr[-400:])
        out = verdict(tmp_path)
        assert out["head_state"] == "STALE_HEAD", out
        assert ah.recover_current_head(tmp_path, ENV)["generation"] == 2
        assert verdict(tmp_path)["verdict"] == "VALID"


# --- Section 4: the archive lock ---------------------------------------------------
class TestArchiveLock:
    def test_the_lock_file_is_never_unlinked(self, tmp_path):
        init(tmp_path)
        path = ah.archive_lock_path(tmp_path, ENV)
        with ah.archive_lock(tmp_path, ENV):
            inode = path.stat().st_ino
        assert path.exists() and path.stat().st_ino == inode

    def test_double_release_cannot_remove_a_successors_lock(self, tmp_path):
        """The Review-2 BLOCKING defect, at the primitive.

        A second `_release_lock()` on an already-released writer deleted
        whatever was at that path — including a live successor's lock file —
        after which the next process created a fresh inode, flocked it
        trivially, and two owners appended to one segment.
        """
        init(tmp_path)
        a = sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-x",
                             partition_identity="p", commit_to_head=False)
        lock = a._lock_path
        inode = lock.stat().st_ino
        a.close()
        a._release_lock()
        a._release_lock()                       # the extra releases
        b = sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-x2",
                             partition_identity="p", commit_to_head=False)
        try:
            a._release_lock()                   # ...and one after a successor
            assert b._lock_path.exists()
            fd = os.open(b._lock_path, os.O_RDWR)
            try:
                with pytest.raises(OSError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        finally:
            b.close()
        assert lock.exists() and lock.stat().st_ino == inode

    def test_a_successor_acquires_after_a_predecessor_releases(self, tmp_path):
        init(tmp_path)
        a = sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-y",
                             partition_identity="p", commit_to_head=False)
        a.close()
        # The same id is refused for IMMUTABILITY, not for the lock.
        with pytest.raises(sg.SegmentError, match="already committed"):
            sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-y",
                             partition_identity="p", commit_to_head=False)
        b = sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-y2",
                             partition_identity="p", commit_to_head=False)
        b.close()

    def test_concurrent_commits_across_processes_lose_nothing(self, tmp_path):
        init(tmp_path)
        script = f'''
import sys
sys.path.insert(0, {str(REPO)!r})
sys.path.insert(0, {str(REPO / "tests")!r})
from app.realtime import segment as sg
from test_kalshi_archive_genesis_001 import fields
w = sg.SegmentWriter({str(tmp_path)!r}, environment="demo",
                     segment_id="kalshi.seg-" + sys.argv[1],
                     partition_identity="venue=kalshi/date=2026-08-08/hour=" + sys.argv[1],
                     subscription_metadata={{"venue": "kalshi"}})
for i in range(5):
    w.submit(fields(i))
w.close()
'''
        procs = [subprocess.Popen([sys.executable, "-c", script, f"P{i}"],
                                  cwd=str(REPO), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) for i in range(6)]
        for p in procs:
            out, err = p.communicate(timeout=120)
            assert p.returncode == 0, err.decode()[-400:]
        out = verdict(tmp_path)
        assert out["verdict"] == "VALID", out["reasons"]
        assert out["segments"] == 6
        assert ah.present_generations(tmp_path, ENV) == list(range(7))


# --- Section 5: commit-time predecessor -------------------------------------------
class TestCommitTimePredecessor:
    @pytest.mark.parametrize("order", [("A", "B"), ("B", "A")])
    def test_overlapping_writers_close_in_any_order(self, tmp_path, order):
        """No ordinary rollover may become INVALID because writers overlapped."""
        init(tmp_path)
        writers = {}
        for n in ("A", "B"):
            writers[n] = sg.SegmentWriter(
                tmp_path, environment=ENV, segment_id=f"kalshi.seg-{n}",
                partition_identity=f"venue=kalshi/date=2026-08-08/hour={n}",
                subscription_metadata={"venue": "kalshi"})
            writers[n].submit(fields(1))
        for n in order:
            writers[n].close()
        out = verdict(tmp_path)
        assert out["verdict"] == "VALID", out["reasons"]
        # Order is read from the DELTA CHAIN, one transition per generation.
        chain = [ah.read_generation(tmp_path, ENV, g) for g in (1, 2)]
        assert [r["committed_segment_id"] for r in chain] == [
            f"kalshi.seg-{n}" for n in order], "order must be COMMIT order"
        assert chain[0]["previous_segment_digest"] is None
        assert chain[1]["previous_segment_digest"] == \
            chain[0]["committed_segment_digest"]
        assert [r["segment_count"] for r in chain] == [1, 2]

    def test_a_long_lived_writer_spanning_many_others(self, tmp_path):
        init(tmp_path)
        long = sg.SegmentWriter(
            tmp_path, environment=ENV, segment_id="kalshi.seg-LONG",
            partition_identity="venue=kalshi/date=2026-08-08/hour=LONG",
            subscription_metadata={"venue": "kalshi"})
        long.submit(fields(0))
        build(tmp_path, ["A", "B", "C"], per=2)
        long.close()
        assert verdict(tmp_path)["verdict"] == "VALID"

    def test_the_writer_cannot_be_told_its_predecessor(self, tmp_path):
        import inspect
        params = inspect.signature(sg.SegmentWriter.__init__).parameters
        assert "previous_segment_digest" not in params


# --- Section 6: rollover ------------------------------------------------------------
class TestSegmentRollover:
    def test_rotation_commits_without_waiting_for_shutdown(self, tmp_path):
        init(tmp_path)
        store = ar.EventArchive(tmp_path, environment=ENV, venue="kalshi",
                                max_segment_records=10)
        from app.realtime.book import EventEnvelope
        stamp = cn.canonical_datetime(NOW)
        for i in range(35):
            store.append(EventEnvelope(
                schema_version=1, venue="kalshi", environment=ENV,
                channel="orderbook_delta", event_type="orderbook_delta",
                market_ticker="KXA", market_id="KXA", sid=4, seq=i,
                venue_time=stamp, collector_receive_time=stamp,
                normalization_time=stamp, receive_monotonic_ns=1_000 + i,
                normalize_monotonic_ns=1_100 + i, data_age_us=100,
                implementation_version="test",
                raw={"p": "0.51"}, normalized={"u": 5100}))
        # Committed BEFORE any shutdown: this is the whole point.
        assert store.rotations >= 2, store.rotations
        assert not store.rotation_failures, store.rotation_failures
        out = verdict(tmp_path)
        assert out["head_generation"] >= 3
        assert out["records_read"] >= 30
        store.close()
        assert verdict(tmp_path)["verdict"] == "VALID"

    def test_a_crash_costs_only_the_open_segment(self, tmp_path):
        init(tmp_path)
        store = ar.EventArchive(tmp_path, environment=ENV, venue="kalshi",
                                max_segment_records=10)
        from app.realtime.book import EventEnvelope
        stamp = cn.canonical_datetime(NOW)
        for i in range(25):
            store.append(EventEnvelope(
                schema_version=1, venue="kalshi", environment=ENV,
                channel="orderbook_delta", event_type="orderbook_delta",
                market_ticker="KXA", market_id="KXA", sid=4, seq=i,
                venue_time=stamp, collector_receive_time=stamp,
                normalization_time=stamp, receive_monotonic_ns=1_000 + i,
                normalize_monotonic_ns=1_100 + i, data_age_us=100,
                implementation_version="test",
                raw={"p": "0.51"}, normalized={"u": 5100}))
        committed = verdict(tmp_path)["records_read"]
        assert committed >= 20, "rotation committed nothing"
        # Simulate the crash: abandon the process without closing.
        out = verdict(tmp_path)
        assert out["verdict"] == "VALID", out["reasons"]
        assert out["records_read"] == committed

    def test_policy_inputs_are_all_optional_and_off_by_default(self, tmp_path):
        init(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-r",
                             partition_identity="p", commit_to_head=False)
        assert w.rotation_due is False
        w.close()

    @pytest.mark.parametrize("policy", [
        {"max_records": 3}, {"max_age_s": 0.0}, {"max_bytes": 1},
    ])
    def test_each_policy_input_can_drive_rotation(self, tmp_path, policy):
        init(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-p",
                             partition_identity="p", commit_to_head=False,
                             flush_every=1, **policy)
        for i in range(4):
            w.submit(fields(i))
        deadline = time.monotonic() + 2
        while not w.rotation_due and time.monotonic() < deadline:
            time.sleep(0.01)
        assert w.rotation_due, policy
        w.close()


# --- Section 7: admission sealing --------------------------------------------------
class TestAdmissionSealing:
    @pytest.mark.parametrize("trial", range(100))
    def test_close_racing_a_live_producer_never_destroys_the_segment(
            self, tmp_path, trial):
        """The Review-2 reproduction: collector submits, shutdown handler closes.

        `attempted` used to be published to the reconciliation identity before
        the event had any outcome, so close() evaluated a TORN counter and
        destroyed the segment 18 times in 30.
        """
        init(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV,
                             segment_id=f"seg-race-{trial}",
                             partition_identity="p", commit_to_head=False,
                             queue_maxsize=64)
        stop = threading.Event()
        seen = []

        def produce():
            i = 0
            while not stop.is_set() and i < 5000:
                seen.append(w.submit(fields(i)))
                i += 1

        t = threading.Thread(target=produce)
        t.start()
        time.sleep(0.002)
        try:
            manifest = w.close()
        finally:
            stop.set()
            t.join(timeout=30)
        acc = w.accounting
        assert acc.admission_holds(), acc.to_dict()
        assert acc.disposition_holds(), acc.to_dict()
        assert acc.clean(), acc.to_dict()
        assert manifest["record_count"] == acc.written
        assert acc.attempted == len(seen)

    def test_the_seal_refuses_to_reconcile_against_a_moving_counter(self, tmp_path):
        init(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-seal",
                             partition_identity="p", commit_to_head=False)
        assert w._inflight == 0
        w.close()
        assert w._sealed is True
        assert w.submit(fields(1)) is sg.RejectReason.SHUTDOWN_IN_PROGRESS


# --- Section 8: durability semantics ------------------------------------------------
class TestDurabilitySemantics:
    @pytest.mark.parametrize("artifact,stage", [
        ("manifest", "directory_fsync"),
        ("head_generation", "head_generation_directory_fsync"),
        ("current_head", "current_head_directory_fsync"),
    ])
    def test_rename_ok_dir_fsync_failed_is_its_own_type(self, tmp_path, artifact,
                                                        stage):
        """The distinction must reach the operator, for ALL THREE artifacts.

        The previous test injected at a hook OUTSIDE the protected path, so it
        asserted the generic message and the new type had no coverage at all.
        """
        init(tmp_path)
        w = sg.SegmentWriter(
            tmp_path, environment=ENV, segment_id="kalshi.seg-D",
            partition_identity="venue=kalshi/date=2026-08-08/hour=D",
            subscription_metadata={"venue": "kalshi"})
        w.submit(fields(1))
        w.durability_hooks[stage] = lambda: (_ for _ in ()).throw(OSError(5, "io"))
        with pytest.raises(ah.DurabilityNotProven) as caught:
            w.close()
        assert "durability is not proven" in str(caught.value)
        assert "ORPHANED" not in str(caught.value)

    def test_a_pre_rename_failure_is_NOT_the_durability_type(self, tmp_path):
        init(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-pre",
                             partition_identity="p", commit_to_head=False)
        w.submit(fields(1))
        w.durability_hooks["manifest_fsync"] = lambda: (
            _ for _ in ()).throw(OSError(28, "ENOSPC"))
        with pytest.raises(sg.SegmentError) as caught:
            w.close()
        assert not isinstance(caught.value, ah.DurabilityNotProven)
        assert "manifest_fsync" in str(caught.value)
        assert not w.manifest_path.exists()


# --- Section 9: gzip tail recovery, with COUNTS --------------------------------------
class TestGzipTailRecovery:
    def _segment_with(self, tmp_path, n):
        init(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-gz",
                             partition_identity="p", commit_to_head=False)
        for i in range(n):
            w.submit(fields(i))
        w.close()
        return w

    def test_mid_stream_corruption_recovers_the_measured_prefix(self, tmp_path):
        """Counts asserted, because the previous 'fix' was dead code.

        It re-fed the failing chunk into the SAME decompressobj, which is
        permanently in error state once it has raised: 664 recovered where 998
        were available, and 0 on a small file.
        """
        w = self._segment_with(tmp_path, 2000)
        raw = bytearray(w.events_path.read_bytes())
        flip = len(raw) // 2
        raw[flip] ^= 0xFF
        w.events_path.write_bytes(bytes(raw))
        recovered = sg.read_segment_records(w.events_path)
        assert len(recovered) >= 900, len(recovered)
        assert len(recovered) < 2000

    def test_a_small_file_recovers_its_prefix_rather_than_nothing(self, tmp_path):
        w = self._segment_with(tmp_path, 6)
        raw = bytearray(w.events_path.read_bytes())
        raw[-3] ^= 0xFF
        w.events_path.write_bytes(bytes(raw))
        assert len(sg.read_segment_records(w.events_path)) >= 1

    def test_a_decompressobj_is_never_reused_after_it_raises(self):
        """Structural, because the measurement is what caught the dead code.

        The previous version re-fed the failing chunk into the SAME object; a
        `decompressobj` is permanently in error state once it has raised, so
        every retry contributed nothing.
        """
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(sg._salvage_prefix))
        loops = [n for n in ast.walk(tree) if isinstance(n, ast.For)]
        assert loops
        handlers = [h for loop in loops for n in ast.walk(loop)
                    if isinstance(n, ast.Try) for h in n.handlers]
        assert handlers, "the salvage loop has no terminal handler"
        assert all(any(isinstance(x, ast.Break) for x in ast.walk(h))
                   for h in handlers), \
            "the salvage loop must STOP on a terminal error, not retry"


# --- Section 10: filesystem containment ---------------------------------------------
class TestContainment:
    def test_a_symlinked_env_directory_never_verifies(self, tmp_path):
        """The former exploit. Permanently regressed to failure."""
        init(tmp_path / "real")
        build(tmp_path / "real", ["A"])
        root = tmp_path / "shell"
        root.mkdir()
        (root / f"env={ENV}").symlink_to(tmp_path / "real" / f"env={ENV}")
        out = verdict(root)
        assert out["verdict"] == "INVALID"
        assert out["head_state"] == "ROOT_NOT_CONTAINED"

    @pytest.mark.parametrize("victim", [
        "archive-genesis.json", "archive-head.json",
    ])
    def test_a_symlinked_head_artifact_is_refused(self, tmp_path, victim):
        init(tmp_path)
        build(tmp_path, ["A"])
        env = tmp_path / f"env={ENV}"
        outside = tmp_path.parent / f"outside-{victim}"
        shutil.move(str(env / victim), str(outside))
        (env / victim).symlink_to(outside)
        assert verdict(tmp_path)["verdict"] == "INVALID"

    def test_a_symlinked_generation_record_is_refused(self, tmp_path):
        init(tmp_path)
        build(tmp_path, ["A"])
        gen = ah.generation_path(tmp_path, ENV, 1)
        outside = tmp_path.parent / "outside-gen.json"
        shutil.move(str(gen), str(outside))
        gen.symlink_to(outside)
        out = verdict(tmp_path)
        assert out["verdict"] == "INVALID"

    def test_parent_traversal_is_refused(self, tmp_path):
        with pytest.raises(sg.SegmentError, match=r"\.\."):
            sg.assert_contained(tmp_path, tmp_path / "a" / ".." / ".." / "evil")

    def test_the_archive_lock_refuses_a_symlink(self, tmp_path):
        """KALSHI-ARCHIVE-CORE-REMEDIATION-002 A1 (defect #5b): the raw
        `OSError` (`O_NOFOLLOW` -> `ELOOP`) `os.open` raises for a symlinked
        lock path is now caught and re-raised as the module's own typed
        `ArchiveHeadError`, the same way every other filesystem refusal in
        this module is -- a raw, untyped `OSError` escaping a `with
        archive_lock(...)` block is exactly the uncaught-traceback defect
        class this milestone closes, not a property this test should keep
        pinning to the untyped shape.
        """
        init(tmp_path)
        lock = ah.archive_lock_path(tmp_path, ENV)
        if lock.exists():
            lock.unlink()
        outside = tmp_path.parent / "outside.lock"
        outside.touch()
        lock.symlink_to(outside)
        with pytest.raises(ah.ArchiveHeadError):
            with ah.archive_lock(tmp_path, ENV):
                pass


# --- Round 5: the properties three reviewers asked for --------------------------
class TestAdmissionTotality:
    """`non_canonical_reason(x) is None` MUST imply `canonical_bytes` succeeds.

    Three consecutive rounds patched this one value type at a time — float,
    then Decimal exponent, then str, then int — and each time the next round
    found another hole: a lone surrogate in a mapping KEY, then a naive
    datetime. Each hole cost an entire segment (501 accepted events) while
    `verify_archive` reported VALID with empty reasons. Only the property
    closes it, so the property is the test.
    """

    CORPUS = [
        None, True, False, 0, -1, 2**63, "", "ok", "é", "\U0001f600",
        Decimal("0"), Decimal("0.51"), Decimal("-1.5"), Decimal("1E+30"),
        Decimal("1E+4096"), Decimal("1E-4096"), Decimal("1E+999999"),
        Decimal("NaN"), Decimal("Infinity"),
        1.5, 1e30, float("nan"), float("inf"),
        "\udcff", "a\udcffb", "𐀀",
        {"k": 1}, {"\udcff": 1}, {"nested": {"\udcff": "v"}},
        [1, 2, {"\udcff": 3}], ["\udcff"],
        datetime(2026, 8, 9, 12, 0),                      # naive
        datetime(2026, 8, 9, 12, 0, tzinfo=UTC),          # aware
        # The calendar bound: astimezone() overflows, and OverflowError is not
        # a CanonicalError, so it escaped exactly as InvalidOperation once did.
        datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-5))),
        datetime(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=5))),
        {"k": datetime(9999, 12, 31, 23, 59, 59,
                       tzinfo=timezone(timedelta(hours=-5)))},
        2**8192, -(2**8192),
        {"deep": [[[{"x": Decimal("1E+999999")}]]]},
    ]

    def test_the_invariant_is_generative_not_enumerated(self):
        """Randomised sweep, because a hand-written corpus is what kept failing.

        Five rounds, five holes, each found by someone else's sweep and each
        "fixed" by adding one more value to a list. This builds thousands of
        values from every branch of the encoder — including KEY positions, which
        is where the fifth hole was — and asserts the implication directly.
        """
        import random

        rng = random.Random(20260809)
        tz_bad = [timezone(timedelta(hours=h)) for h in (-23, -5, 5, 23)]

        def leaf():
            return rng.choice([
                None, True, False,
                rng.randint(-(2**70), 2**70), rng.randint(2**8190, 2**8200),
                rng.choice(["", "ok", "é", "𐀀", "\udcff", "a\udcffb"]),
                Decimal(rng.choice(["0", "0.51", "-1.5", "1E+30", "1E+4097",
                                    "1E-4097", "NaN", "Infinity"])),
                rng.choice([1.5, 1e30, float("nan")]),
                datetime(2026, 8, 9, 12, tzinfo=UTC),
                datetime(9999, 12, 31, 23, 59, 59, tzinfo=rng.choice(tz_bad)),
                datetime(1, 1, 1, 0, 0, tzinfo=rng.choice(tz_bad)),
                datetime(2026, 8, 9, 12),
            ])

        def build(depth=0):
            if depth >= 3 or rng.random() < 0.4:
                return leaf()
            kind = rng.random()
            if kind < 0.45:
                # Keys drawn from the SAME generator as values.
                out = {}
                for _ in range(rng.randint(1, 4)):
                    k = leaf()
                    out[k if isinstance(k, str) else repr(k)] = build(depth + 1)
                if rng.random() < 0.25:
                    out["\udcff"] = build(depth + 1)     # hostile key
                return out
            if kind < 0.8:
                return [build(depth + 1) for _ in range(rng.randint(0, 4))]
            return tuple(build(depth + 1) for _ in range(rng.randint(0, 3)))

        checked = admitted = 0
        for _ in range(4000):
            value = build()
            checked += 1
            reason = sg.non_canonical_reason(value)      # must never raise
            if reason is not None:
                continue
            admitted += 1
            cn.canonical_bytes(value)                    # must never raise
            cn.assert_fixpoint(value)
        assert checked == 4000
        assert admitted > 200, f"corpus admitted only {admitted}; too weak"

    @pytest.mark.parametrize("value", CORPUS, ids=lambda v: repr(v)[:40])
    def test_admitted_values_always_serialise(self, value):
        reason = sg.non_canonical_reason(value)
        if reason is not None:
            return                                # refused: nothing to prove
        cn.canonical_bytes({"v": value})          # must not raise
        cn.assert_fixpoint({"v": value})

    def test_a_surrogate_KEY_is_refused_before_acceptance(self, tmp_path):
        """The exact reproduction: one venue-controlled byte in a KEY.

        `json.loads` decodes a lone surrogate in a key position without
        complaint, and the UTF-8 guard had been applied to values only.
        """
        init(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-key",
                             partition_identity="p", commit_to_head=False)
        bad = fields(1)
        bad["raw_event"] = {"market_ticker": "KXA", "\udcff": "x"}
        assert w.submit(bad) is sg.RejectReason.NOT_CANONICAL
        for i in range(3):
            assert w.submit(fields(i + 10)) is None
        m = w.close()
        assert m["record_count"] == 3
        assert w.accounting.clean()

    def test_a_naive_datetime_is_refused_before_acceptance(self, tmp_path):
        init(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-naive",
                             partition_identity="p", commit_to_head=False)
        bad = fields(1)
        bad["raw_event"] = {"t": datetime(2026, 8, 9, 12, 0)}
        assert w.submit(bad) is sg.RejectReason.NOT_CANONICAL
        assert "naive" in (w.last_rejection_detail or "")
        w.close()


class TestExternalAnchor:
    """`expected_head` is the ONLY defence against the accepted residual.

    It shipped with zero callers and zero tests, and its signature changed in
    the same commit. Without these, the next refactor removes it silently.
    """

    def _archive(self, tmp_path):
        init(tmp_path)
        build(tmp_path, ["A", "B", "C"], per=4)
        return ah.load_authoritative_head(tmp_path, ENV).generation_record

    def test_an_anchor_permits_honest_growth(self, tmp_path):
        rec = self._archive(tmp_path)
        anchor = (rec["generation"], rec["head_digest"])
        build(tmp_path, ["D", "E"], per=4)            # honest commits after it
        out = verdict(tmp_path, expected_head=anchor)
        assert out["verdict"] == "VALID", out["reasons"]

    def test_an_anchor_detects_a_full_chain_remint(self, tmp_path):
        rec = self._archive(tmp_path)
        anchor = (rec["generation"], rec["head_digest"])
        # Re-mint the whole chain in a different order: every manifest and the
        # genesis stay byte-identical and nothing is forged.
        for f in ah.heads_dir(tmp_path, ENV).glob("*.json"):
            if f.name != "0000000000000000.json":
                f.unlink()
        prev = ah.read_generation(tmp_path, ENV, 0)
        for name in ("kalshi.seg-C", "kalshi.seg-A", "kalshi.seg-B"):
            m = cn.parse_canonical(
                (tmp_path / f"env={ENV}" / f"segment={name}"
                 / "manifest.json").read_bytes())
            prev = ah._build_generation(previous=prev, manifest=m)
            ah._publish_generation(tmp_path, ENV, prev)
        ah._publish_current_head(tmp_path, ENV, prev)
        assert verdict(tmp_path)["verdict"] == "VALID"      # the accepted limit
        out = verdict(tmp_path, expected_head=anchor)
        assert out["verdict"] == "INVALID"
        assert any("HISTORY_REWRITTEN" in r for r in out["reasons"]), out["reasons"]

    def test_an_anchor_beyond_the_head_is_truncation(self, tmp_path):
        rec = self._archive(tmp_path)
        out = verdict(tmp_path, expected_head=(rec["generation"] + 5, "0" * 64))
        assert out["verdict"] == "INVALID"
        assert any("HISTORY_TRUNCATED" in r for r in out["reasons"]), out["reasons"]

    def test_a_wrong_digest_at_the_anchor_generation_is_rewriting(self, tmp_path):
        rec = self._archive(tmp_path)
        out = verdict(tmp_path, expected_head=(rec["generation"], "0" * 64))
        assert out["verdict"] == "INVALID"
        assert any("HISTORY_REWRITTEN" in r for r in out["reasons"]), out["reasons"]


class TestInitializationIsIdempotent:
    """The generation-0 brick recurred in three consecutive rounds.

    Every one was a crash between publishing generation 0 and linking the
    genesis, followed by re-running initialization — which is what the
    docstring tells the operator to do.
    """

    def test_reinit_after_a_crash_before_the_genesis_link(self, tmp_path):
        # Exactly the on-disk state that window leaves.
        zero = ah._build_generation_zero(archive_id="a" * 32, environment=ENV)
        ah.heads_dir(tmp_path, ENV).mkdir(parents=True)
        ah._publish_generation(tmp_path, ENV, zero)
        ah._publish_current_head(tmp_path, ENV, zero)
        assert not ah.genesis_path(tmp_path, ENV).exists()
        assert verdict(tmp_path)["head_state"] == "NOT_INITIALIZED"

        genesis = init(tmp_path)                       # the documented remedy
        assert genesis["archive_id"] == "a" * 32, "the durable identity must win"
        assert verdict(tmp_path)["verdict"] == "VALID"
        build(tmp_path, ["A"], per=2)                  # and it can move forward
        assert verdict(tmp_path)["verdict"] == "VALID"

    def test_a_foreign_generation_zero_is_refused(self, tmp_path):
        zero = ah._build_generation_zero(archive_id="b" * 32, environment=ENV)
        ah.heads_dir(tmp_path, ENV).mkdir(parents=True)
        ah._publish_generation(tmp_path, ENV, zero)
        with pytest.raises(ah.ArchiveIdentityMismatch):
            ah.initialize_archive(tmp_path, ENV,
                                  archive_identity="kalshi-realtime",
                                  archive_id="c" * 32)


class TestSalvageAndResidue:
    def test_diagnostic_read_still_works_on_head_level_damage(self, tmp_path):
        """Removing the glob fallback broke the reader that exists for this."""
        init(tmp_path)
        build(tmp_path, ["A", "B"], per=3)
        store = ar.EventArchive(tmp_path, environment=ENV, venue="kalshi")
        ah.current_head_path(tmp_path, ENV).unlink()
        with pytest.raises(ar.ArchiveError):
            store.read_verified()                      # canonical: refuses
        salvaged = store.read_unverified_diagnostic()  # salvage: still works
        assert len(salvaged) == 6
        assert store.diagnostic_order_unauthenticated is True

    def test_quarantined_residue_in_a_committed_segment_is_reported(self, tmp_path):
        init(tmp_path)
        build(tmp_path, ["A"], per=2)
        seg = tmp_path / f"env={ENV}" / "segment=kalshi.seg-A"
        (seg / f"{sg.EVENTS_FILENAME}.abandoned.2026-08-09T000000Z").write_bytes(
            b"\x1f\x8b" + b"x" * 500)
        out = verdict(tmp_path)
        assert out["abandoned_segments"], "crash residue must not be invisible"
        assert out["abandoned_segments"][0]["segment_id"] == "kalshi.seg-A"
        # REPORTED, not gating. Making it a `reason` turned an ordinary
        # crash-and-restart — during which the writer itself creates the
        # quarantine file — into a total replay outage, with `read_verified()`
        # refusing untouched committed segments and no command to clear it.
        assert any("ABANDONED_EVIDENCE" in w for w in out["warnings"])
        assert not any("ABANDONED_EVIDENCE" in r for r in out["reasons"])
        assert out["verdict"] == "VALID", out["reasons"]
        store = ar.EventArchive(tmp_path, environment=ENV, venue="kalshi")
        assert len(store.read_verified()) == 2, "committed evidence must remain readable"


class TestLegacyMultiMember:
    def test_every_gzip_member_is_imported(self, tmp_path):
        """One `_decompress_prefix` call kept only the FIRST member.

        A legacy collector that appends writes one member per flush, so this
        dropped two thirds of a realistic corpus while the provenance record
        certified that nothing was lost.
        """
        import gzip as gz

        from app.realtime import legacy_import as li
        src = tmp_path / "legacy" / f"env={ENV}" / "venue=kalshi" / "d" / "h"
        src.mkdir(parents=True)
        path = src / sg.EVENTS_FILENAME
        with open(path, "wb") as raw:
            for member in range(3):
                with gz.GzipFile(fileobj=raw, mode="wb") as fh:
                    for i in range(100):
                        fh.write(json.dumps({
                            "event_type": "orderbook_delta", "market_ticker": "KXA",
                            "sid": 4, "seq": member * 100 + i,
                            "collector_receive_time":
                                f"2026-07-01T0{member}:00:{i % 60:02d}.000000Z",
                            "receive_monotonic_ns": 1_000_000 + i,
                            "raw": {"p": "0.51"}}).encode() + b"\n")
        plan = li.migrate_legacy_archive(tmp_path / "legacy", tmp_path / "dest",
                                         environment=ENV)
        assert plan["records_readable"] == 300, plan["records_readable"]
        assert plan["torn_files"] == [] and plan["empty_files"] == []
        out = li.migrate_legacy_archive(tmp_path / "legacy", tmp_path / "dest",
                                        environment=ENV, confirm=True)
        assert out["records_imported"] == 300


class TestVerifierTotality:
    """The EACCES class, which shipped twice with a guard on the wrong call.

    Round 5 wrapped `os.path.lexists`, which swallows OSError internally and can
    never raise, nine lines above the two `Path.exists()` calls that do. Two
    reviewers reproduced it verbatim. There was no test either time.
    """

    def _archive(self, tmp_path):
        init(tmp_path)
        build(tmp_path, ["A", "B", "C"], per=4)
        return tmp_path

    @pytest.mark.parametrize("victim", ["events.jsonl.gz", "manifest.json", "."])
    def test_verify_segment_returns_a_verdict_on_eacces(self, tmp_path, victim):
        self._archive(tmp_path)
        seg = seg_dir(tmp_path, "A")
        target = seg if victim == "." else seg / victim
        os.chmod(target, 0o000)
        try:
            # The property is: RETURNS A VERDICT, never raises. The exact reason
            # differs by victim — an unreadable directory cannot be lstat'd, a
            # mode-0 file can, so it surfaces later as an unreadable manifest or
            # a record-count mismatch. All three are verdicts.
            v = sg.verify_segment(seg, environment=ENV, root=tmp_path)
            assert not v.valid
            assert v.reasons, "a refusal must say why"
            assert v.state is sg.SegmentState.INVALID
            out = sg.verify_archive(tmp_path, environment=ENV)
            assert out["verdict"] == "INVALID"
        finally:
            os.chmod(target, 0o755)

    def test_an_unreadable_directory_does_not_mask_a_real_deletion(self, tmp_path):
        """One chmod erased three concrete reasons and reported
        `records_expected: 0` — "nothing was lost", by omission."""
        self._archive(tmp_path)
        shutil.rmtree(seg_dir(tmp_path, "B"))
        before = verdict(tmp_path)
        assert any("kalshi.seg-B" in r for r in before["reasons"])
        os.chmod(seg_dir(tmp_path, "C"), 0o000)
        try:
            after = verdict(tmp_path)
            assert any("kalshi.seg-B" in r for r in after["reasons"]), after["reasons"]
            assert after["records_expected"] == before["records_expected"]
        finally:
            os.chmod(seg_dir(tmp_path, "C"), 0o755)

    def test_recover_current_head_never_leaks_a_raw_oserror(self, tmp_path):
        init(tmp_path)
        build(tmp_path, ["A", "B"], per=2)
        ah._publish_current_head(tmp_path, ENV, ah.read_generation(tmp_path, ENV, 1))
        os.chmod(seg_dir(tmp_path, "B"), 0o000)
        try:
            with pytest.raises(ah.ArchiveHeadError):
                ah.recover_current_head(tmp_path, ENV)
        except OSError:                       # pragma: no cover - the defect
            pytest.fail("recover_current_head leaked a raw OSError")
        finally:
            os.chmod(seg_dir(tmp_path, "B"), 0o755)


class TestAdmissionIsBounded:
    def test_exactly_one_encoder_call_per_admission(self, monkeypatch):
        """The by-construction fix recursed into the WRAPPER, so every subtree
        was re-encoded: 603 calls for one orderbook snapshot, 946 -> 362 rec/s
        end to end on venue-controlled input."""
        calls = {"n": 0}
        real = cn.canonical_bytes

        def counting(value):
            calls["n"] += 1
            return real(value)

        monkeypatch.setattr(sg, "canonical_bytes", counting)
        book = {"levels": [{"px": str(i), "sz": str(i)} for i in range(60)],
                "meta": {"a": {"b": {"c": "d"}}}}
        sg.non_canonical_reason({"raw_event": book})
        assert calls["n"] == 1, f"{calls['n']} encoder calls; must be exactly 1"

        deep = {"x": 1}
        for _ in range(120):
            deep = {"n": deep}
        calls["n"] = 0
        sg.non_canonical_reason(deep)
        assert calls["n"] == 1, calls["n"]

    def test_admission_never_raises_even_from_the_walk(self, tmp_path):
        """The walk was called OUTSIDE the try, so `items()` raising reached the
        producer raw: `attempted` incremented with no terminal booking, the
        identity violated, close() refusing, the whole segment lost with
        verify_archive reporting VALID."""
        class HostileMapping(dict):
            def items(self):
                raise RuntimeError("hostile items()")

        class HostileSeq(list):
            def __iter__(self):
                raise RuntimeError("hostile __iter__")

        for hostile in (HostileMapping(a=1), HostileSeq([1, 2])):
            reason = sg.non_canonical_reason(hostile)
            assert reason is not None and isinstance(reason, str)

        init(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-hostile",
                             partition_identity="p", commit_to_head=False)
        for i in range(5):
            assert w.submit(fields(i)) is None
        bad = fields(99)
        bad["raw_event"] = HostileMapping(a=1)
        assert w.submit(bad) is sg.RejectReason.NOT_CANONICAL
        acc = w.accounting
        assert acc.attempted == acc.rejected_before_accept + acc.accepted
        m = w.close()
        assert m["record_count"] == 5
        assert acc.clean()

    def test_the_walk_is_never_stricter_than_the_encoder(self):
        """`_encode` accepts any Sequence; the walk accepted only list/tuple and
        short-circuited first, so it refused values the encoder would take.
        "The encoder wins" has to hold in BOTH directions."""
        from collections import deque
        for value in (deque([1, 2, 3]), range(4), (1, 2)):
            reason = sg.non_canonical_reason(value)
            assert reason is None, f"{value!r} refused by the walk: {reason}"
            cn.canonical_bytes(value)


class TestAnchorInputValidation:
    @pytest.mark.parametrize("anchor", [(-1, "x"), (True, "x"), (0, 123),
                                        ("2", "x"), (1, None)])
    def test_a_malformed_anchor_fails_closed(self, tmp_path, anchor):
        """A negative generation passed the `< anchor_gen` test and never matched
        `gen == anchor_gen`, so the anchor was silently inert while a monitor
        believed it was pinning."""
        init(tmp_path)
        build(tmp_path, ["A"], per=2)
        out = sg.verify_archive(tmp_path, environment=ENV, expected_head=anchor)
        assert out["verdict"] == "INVALID"
        assert out["head_state"] == "INVALID_ANCHOR"


class TestResidueAccounting:
    def test_one_unreadable_entry_does_not_drop_later_residue(self, tmp_path):
        """The guard sat outside both loops, so one dangling symlink dropped
        7,000 bytes of residue in LATER directories and its sentinel was then
        counted as a file — a wrong count and a wrong byte total."""
        init(tmp_path)
        build(tmp_path, ["A", "B", "C"], per=2)
        (seg_dir(tmp_path, "A") / f"{sg.EVENTS_FILENAME}.abandoned.1").write_bytes(b"x" * 5000)
        (seg_dir(tmp_path, "C") / f"{sg.EVENTS_FILENAME}.abandoned.1").write_bytes(b"y" * 7000)
        (seg_dir(tmp_path, "B") / f"{sg.EVENTS_FILENAME}.abandoned.1").symlink_to(
            tmp_path / "does-not-exist")
        out = verdict(tmp_path)
        found = {r["segment_id"]: r["bytes"] for r in out["abandoned_segments"]
                 if r.get("segment_id")}
        assert found.get("kalshi.seg-A") == 5000
        assert found.get("kalshi.seg-C") == 7000, "later residue was dropped"
        assert any("RESIDUE_SCAN_INCOMPLETE" in w for w in out["warnings"])

    def test_the_facade_surfaces_residue_and_accepts_an_anchor(self, tmp_path):
        """Demoting residue to a warning was right; deleting the only signal the
        operator-facing wrapper carried was not."""
        init(tmp_path)
        build(tmp_path, ["A"], per=2)
        (seg_dir(tmp_path, "A") / f"{sg.EVENTS_FILENAME}.abandoned.1").write_bytes(b"z" * 99)
        store = ar.EventArchive(tmp_path, environment=ENV, venue="kalshi")
        report = store.verify()
        assert report["abandoned_segments"], "residue invisible on the facade"
        assert any("ABANDONED_EVIDENCE" in w for w in report["warnings"])
        assert report["intact"] is True
        rec = ah.load_authoritative_head(tmp_path, ENV).generation_record
        assert store.verify(expected_head=(rec["generation"], "0" * 64))["intact"] is False


class TestFilesystemShapeMatrix:
    """A MATRIX, not hand-picked cases.

    Three consecutive rounds fixed whichever call the last probe happened to
    hit, and each round's tests instantiated exactly the shapes the new guard
    handled. Reviewer 3's diagnosis: enumerate the shapes, not the call sites.
    """

    SHAPES = ["mode0_file", "mode0_dir", "fifo", "dir_in_place_of_file",
              "dangling_symlink", "symlink_to_unreadable", "symlink_loop",
              "enotdir"]

    def _plant(self, seg, shape, tmp_path):
        target = seg / sg.EVENTS_FILENAME
        if shape == "mode0_file":
            os.chmod(target, 0o000); return target
        if shape == "mode0_dir":
            os.chmod(seg, 0o000); return seg
        if shape == "fifo":
            target.unlink(); os.mkfifo(target); return target
        if shape == "dir_in_place_of_file":
            target.unlink(); target.mkdir(); return target
        if shape == "dangling_symlink":
            target.unlink(); target.symlink_to(tmp_path / "nope"); return target
        if shape == "symlink_to_unreadable":
            victim = tmp_path / "victim"; victim.write_bytes(b"x")
            os.chmod(victim, 0o000)
            target.unlink(); target.symlink_to(victim); return victim
        if shape == "symlink_loop":
            target.unlink(); target.symlink_to(target); return target
        if shape == "enotdir":
            target.unlink(); target.write_bytes(b"x")
            return seg / sg.EVENTS_FILENAME / "child"
        raise AssertionError(shape)

    @pytest.mark.parametrize("shape", SHAPES)
    def test_verify_segment_returns_a_bounded_verdict(self, tmp_path, shape):
        init(tmp_path)
        build(tmp_path, ["A"], per=2)
        seg = seg_dir(tmp_path, "A")
        planted = self._plant(seg, shape, tmp_path)
        try:
            start = time.monotonic()
            v = sg.verify_segment(seg, environment=ENV, root=tmp_path)
            assert time.monotonic() - start < 5, "verification did not terminate"
            assert not v.valid and v.reasons
            out = sg.verify_archive(tmp_path, environment=ENV)
            assert out["verdict"] == "INVALID"
        finally:
            for p in (planted, seg):
                try:
                    os.chmod(p, 0o755)
                except OSError:
                    pass

    def test_a_non_regular_file_never_blocks_a_reader(self, tmp_path):
        """A FIFO answered `lstat` as present and then blocked forever — no
        verdict, no timeout, a monitor that never returns. Strictly worse than
        the traceback the guard was written to remove."""
        init(tmp_path)
        build(tmp_path, ["A"], per=2)
        target = seg_dir(tmp_path, "A") / sg.EVENTS_FILENAME
        target.unlink()
        os.mkfifo(target)
        start = time.monotonic()
        v = sg.verify_segment(seg_dir(tmp_path, "A"), environment=ENV, root=tmp_path)
        assert time.monotonic() - start < 5
        assert any("not a regular file" in r for r in v.reasons), v.reasons


class TestAdmissionTerminates:
    """Mirroring an UNBOUNDED encoder into the pre-acceptance path lost 200
    accepted-and-written records from one submitted value, while
    verify_archive reported VALID with empty reasons."""

    def test_an_endless_sequence_is_refused_not_walked(self):
        from collections.abc import Sequence as _Seq

        class Endless(_Seq):
            def __len__(self): return 10 ** 9
            def __getitem__(self, i): return 1

        for value in (range(10 ** 9), Endless(), {"a": range(10 ** 9)}):
            start = time.monotonic()
            reason = sg.non_canonical_reason(value)
            assert time.monotonic() - start < 10, "admission did not terminate"
            assert reason is not None, f"{value!r} was admitted"


class TestAccountingSurvivesTheGate:
    """The identity is enforced in `submit()`, not inside the gate.

    Six rounds hardened `non_canonical_reason` so it could never raise, and each
    round found another way in — ending with a real SIGINT, which no
    `except Exception` catches.
    """

    @pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, RuntimeError])
    def test_any_escape_from_admission_keeps_the_identity(self, tmp_path, exc):
        init(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id=f"seg-{exc.__name__}",
                             partition_identity="p", commit_to_head=False)
        for i in range(5):
            assert w.submit(fields(i)) is None

        class Hostile(dict):
            def items(self):
                raise exc("from the gate")

        try:
            w.submit({**fields(9), "raw_event": Hostile(a=1)})
        except BaseException:
            pass
        acc = w.accounting
        assert acc.attempted == acc.rejected_before_accept + acc.accepted, acc.to_dict()
        manifest = w.close()
        assert manifest["record_count"] == 5
        assert acc.clean(), acc.to_dict()


class TestVerdictHonesty:
    def test_records_expected_is_a_record_count_on_every_path(self, tmp_path):
        """`segment_count` was written into a record-count field: a 12-segment /
        6000-record archive reported 12, which the facade turned into a
        shortfall of 12. `0` read as unknown; `12` reads as nearly satisfied."""
        init(tmp_path)
        build(tmp_path, ["A", "B", "C"], per=25)
        healthy = verdict(tmp_path)
        assert healthy["records_expected"] == 75
        victim = tmp_path / "victim"
        victim.write_bytes(b"x")
        os.chmod(victim, 0o000)
        target = seg_dir(tmp_path, "B") / sg.MANIFEST_FILENAME
        target.unlink()
        target.symlink_to(victim)
        try:
            out = verdict(tmp_path)
            assert out["verdict"] == "INVALID"
            assert out["records_expected"] in (None, 75), out["records_expected"]
            assert out["records_expected"] != 3
        finally:
            os.chmod(victim, 0o755)

    def test_an_unexaminable_manifest_cannot_downgrade_an_orphan(self, tmp_path):
        """`_presence(...)[0]` discarded the reason, so `(None, why)` read as
        absent and a grafted committed segment became benign `uncommitted` —
        VALID with zero reasons, from one chmod."""
        init(tmp_path)
        build(tmp_path, ["A"], per=2)
        other = tmp_path / "other"
        init(other)
        build(other, ["Z"], per=2)
        graft = tmp_path / f"env={ENV}" / "segment=kalshi.seg-Z"
        shutil.copytree(other / f"env={ENV}" / "segment=kalshi.seg-Z", graft)
        assert verdict(tmp_path)["verdict"] == "INVALID"
        os.chmod(graft, 0o000)
        try:
            out = verdict(tmp_path)
            assert out["verdict"] == "INVALID", "an unexaminable graft was certified"
            assert any("UNEXAMINABLE_SEGMENT" in r for r in out["reasons"]), out["reasons"]
        finally:
            os.chmod(graft, 0o755)

    def test_missing_committed_segments_comes_from_the_report(self, tmp_path):
        """It read stale instance state, so `[]` meant "no read method has been
        called", and a healthy archive could name a phantom."""
        init(tmp_path)
        build(tmp_path, ["A", "B"], per=2)
        shutil.rmtree(seg_dir(tmp_path, "B"))
        out = verdict(tmp_path)
        assert out["missing_committed_segments"] == ["kalshi.seg-B"]
        store = ar.EventArchive(tmp_path, environment=ENV, venue="kalshi")
        assert store.verify()["missing_committed_segments"] == ["kalshi.seg-B"]

    def test_malformed_content_returns_a_verdict(self, tmp_path):
        """A gzip of `null` or `[1,2]`, or a non-dict subscription_metadata,
        raised AttributeError. "Total" is stronger than "handles EACCES"."""
        import gzip as gz
        init(tmp_path)
        seg = tmp_path / f"env={ENV}" / "segment=seg-malformed"
        seg.mkdir(parents=True)
        with gz.open(seg / sg.EVENTS_FILENAME, "wb") as fh:
            fh.write(b"null\n[1,2]\n")
        (seg / sg.MANIFEST_FILENAME).write_bytes(
            cn.canonical_bytes({"subscription_metadata": "a-string"}))
        v = sg.verify_segment(seg, environment=ENV, root=tmp_path)
        assert not v.valid and v.reasons
