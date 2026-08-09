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
        rec = ah.load_authoritative_head(tmp_path, ENV).generation_record
        assert [e["segment_id"] for e in rec["segments"]] == [
            f"kalshi.seg-{n}" for n in order], "order must be COMMIT order"
        assert rec["segments"][0]["previous_segment_digest"] is None
        assert rec["segments"][1]["previous_segment_digest"] == \
            rec["segments"][0]["manifest_digest"]

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
        assert store.rotations >= 3
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
        init(tmp_path)
        lock = ah.archive_lock_path(tmp_path, ENV)
        if lock.exists():
            lock.unlink()
        outside = tmp_path.parent / "outside.lock"
        outside.touch()
        lock.symlink_to(outside)
        with pytest.raises(OSError):
            with ah.archive_lock(tmp_path, ENV):
                pass
